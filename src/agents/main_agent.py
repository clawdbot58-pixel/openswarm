"""The Main Agent — the user-facing face of the swarm.

The Main Agent is the only agent the user talks to. It does not
execute tools, write files, or spawn workers directly. It:

* accepts a user message in one of two modes (CLI stdin/stdout or
  WebSocket via the kernel);
* translates the message into a structured objective via
  :mod:`src.agents.objective_parser`;
* if the message is a status query, summarises the kernel registry;
* if the message is a goal, sends an ``intent: spawn_initial_swarm``
  envelope to the Conductor;
* on kernel events (``agent_zombie``, ``permission_denied``,
  ``queue_overflow``, …), logs them, reports critical ones to the
  Conductor, and reports progress to the user in natural language.

This module is deliberately small. The heavy reasoning lives in the
LLM via the system prompt; the Python code is plumbing.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable

import httpx

from .base_agent import (
    BaseAgent,
    utc_now,
)
from .llm_client import LLMClient, LLMError
from .objective_parser import (
    ObjectiveParseResult,
    StructuredObjective,
    objective_to_spawn_payload,
    parse_objective,
    parse_objective_heuristic,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Conventional agent_id of the Conductor. The Main Agent only ever
# talks to this single agent — workers are the Conductor's concern.
CONDUCTOR_AGENT_ID: str = "conductor"

# The action the Main Agent uses to ask the Conductor to start a
# workflow. The Conductor's :meth:`on_envelope` looks for this name in
# ``payload.data.action`` and dispatches accordingly.
SPAWN_INITIAL_SWARM_ACTION: str = "spawn_initial_swarm"

# Event names the Main Agent subscribes to. The base class dispatches
# any kernel event to :meth:`on_event`; we keep a list here so tests
# can assert the set.
SUBSCRIBED_KERNEL_EVENTS: tuple[str, ...] = (
    "agent_zombie",
    "auto_restart_triggered",
    "permission_denied",
    "queue_overflow",
    "envelope_rejected",
    "registration_rejected",
)

# When the Main Agent asks the kernel's REST API for a status
# summary, it pages results in chunks of this size.
REGISTRY_PAGE_SIZE: int = 200

# Default Conductor prompt location; subclasses can override.
DEFAULT_CONDUCTOR_PROMPT_PATH: str = "prompts/conductor_system.md"


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class SwarmStatusSummary:
    """Snapshot of the registry used to answer status queries."""

    main_agent_status: str
    main_agent_last_heartbeat: str | None
    total_agents: int
    status_counts: dict[str, int]
    connected: int
    sample_agents: list[dict[str, Any]]

    def to_user_text(self) -> str:
        """Render the summary as concise natural language for the user."""
        lines: list[str] = []
        lines.append(
            f"Swarm has **{self.total_agents}** registered agent(s)."
        )
        if self.status_counts:
            ordered = ", ".join(
                f"{k}={v}" for k, v in sorted(self.status_counts.items())
            )
            lines.append(f"By status: {ordered}.")
        lines.append(
            f"**{self.connected}** connected over WebSocket; "
            f"Main Agent status = `{self.main_agent_status}`."
        )
        if self.main_agent_status == "zombie":
            lines.append(
                "**Warning:** Main Agent is marked zombie — "
                "the kernel has lost its heartbeat."
            )
        if self.sample_agents:
            sample = ", ".join(
                f"`{a['agent_id']}`" for a in self.sample_agents[:5]
            )
            more = (
                f" and {len(self.sample_agents) - 5} more"
                if len(self.sample_agents) > 5
                else ""
            )
            lines.append(f"Agents: {sample}{more}.")
        return "\n".join(lines)


@dataclass(slots=True)
class UserReply:
    """A reply the Main Agent wants to surface to the user."""

    text: str
    """The natural-language text to show."""

    objective: StructuredObjective | None = None
    """The structured objective that produced this reply, if any."""

    sent_envelope_id: str | None = None
    """If the Main Agent sent a workflow-related envelope, its id."""

    is_status: bool = False
    """True if the reply is a status snapshot, not a workflow ack."""

    is_error: bool = False
    """True if the reply is an error message."""


# ---------------------------------------------------------------------------
# Main Agent
# ---------------------------------------------------------------------------

class MainAgent(BaseAgent):
    """The user-facing agent. Singleton per swarm.

    Parameters
    ----------
    manifest
        The :class:`AgentManifest` (typically loaded from
        ``manifests/main-agent.json``).
    llm
        A configured :class:`LLMClient`. If ``None``, the Main Agent
        uses the heuristic parser and emits no LLM-driven text —
        enough to drive smoke tests without a key.
    kernel_rest_url
        Base URL of the kernel's REST API. Defaults to
        ``http://127.0.0.1:8765``.
    """

    def __init__(
        self,
        manifest: Any,
        *,
        llm: LLMClient | None = None,
        ws_url: str = BaseAgent.__init__.__defaults__[0] if False else "ws://127.0.0.1:8765/ws",  # noqa
        kernel_rest_url: str = "http://127.0.0.1:8765",
        http_timeout: float = 10.0,
        objective_min_confidence: float = 0.35,
        system_prompt_path: str | Path = "prompts/main_agent_system.md",
    ) -> None:
        super().__init__(
            manifest=manifest,
            ws_url=ws_url,
            system_prompt_path=system_prompt_path,
        )
        self._llm: LLMClient | None = llm
        self._kernel_rest_url: str = kernel_rest_url.rstrip("/")
        self._http_timeout: float = float(http_timeout)
        self._objective_min_confidence: float = float(objective_min_confidence)
        # Track the most recent objective per workflow for status queries.
        self._recent_objectives: dict[str, StructuredObjective] = {}
        # Outbound queue for user replies (consumed by the CLI / WS UI).
        self._user_replies: asyncio.Queue[UserReply] = asyncio.Queue()
        # Lock for updating recent objectives.
        self._obj_lock: asyncio.Lock = asyncio.Lock()
        # Optional callback for user replies (used by tests/dashboards).
        self._on_user_reply: (
            Callable[[UserReply], Awaitable[None]] | None
        ) = None

    # -- properties --------------------------------------------------------

    @property
    def llm(self) -> LLMClient | None:
        """The LLM client (or ``None`` if running in heuristic-only mode)."""
        return self._llm

    @property
    def kernel_rest_url(self) -> str:
        """The kernel REST API base URL."""
        return self._kernel_rest_url

    def on_user_reply(
        self, callback: Callable[[UserReply], Awaitable[None]]
    ) -> None:
        """Register an async callback invoked for every user reply.

        Used by the CLI loop and the dashboard WS bridge to consume
        replies without polling a queue.
        """
        self._on_user_reply = callback

    # -- inbound envelope handling -----------------------------------------

    async def on_envelope(self, envelope) -> None:  # type: ignore[override]
        """Handle a non-event envelope from the kernel.

        The Main Agent's normal inbound traffic is:

        * ``swarm_deployed`` events from the Conductor (ack that the
          initial swarm was set up);
        * ``objective_complete`` events from the Conductor (the user
          goal is done);
        * ``sector_complete`` events if the Conductor forwards them.

        Everything else is logged and dropped.
        """
        sender = envelope.sender.agent_id
        env_type = envelope.envelope_type
        # Pull a name from the payload if present.
        event_name: str | None = None
        try:
            data = envelope.payload.data  # type: ignore[attr-defined]
            if isinstance(data, dict):
                event_name = str(data.get("event", ""))
        except AttributeError:
            event_name = None
        if env_type == "event" and event_name:
            await self.on_event(event_name, dict(envelope.payload.data) if isinstance(envelope.payload.data, dict) else {})  # type: ignore[attr-defined]
            return
        if env_type == "response":
            await self._on_conductor_response(envelope)
            return
        logger.debug(
            "main-agent received envelope type=%s from %s",
            env_type, sender,
        )

    async def on_event(self, event_name: str, details: dict[str, Any]) -> None:  # type: ignore[override]
        """Handle a kernel-emitted event.

        We never act on these directly — the Conductor does. We log
        them, optionally forward critical ones, and surface a brief
        message to the user when the event affects the user-visible
        state.
        """
        if event_name not in SUBSCRIBED_KERNEL_EVENTS:
            return
        if event_name == "agent_zombie":
            await self._on_agent_zombie(details)
        elif event_name == "permission_denied":
            await self._on_permission_denied(details)
        elif event_name == "queue_overflow":
            await self._on_queue_overflow(details)
        elif event_name == "auto_restart_triggered":
            await self._surface(
                UserReply(
                    text=(
                        f"Auto-restart triggered for `{details.get('agent_id')}` "
                        f"(policy={details.get('restart_policy')})."
                    ),
                )
            )
        else:
            # envelope_rejected, registration_rejected — log only.
            logger.info("kernel event %s: %s", event_name, details)

    # -- core: handle a user message ---------------------------------------

    async def handle_user_message(
        self, user_text: str
    ) -> UserReply:
        """Process a single user message and return a :class:`UserReply`.

        This is the single public entry point for both input modes
        (CLI and WebSocket). It is safe to call from any context
        (including before :meth:`start`) but the agent must be
        started if the reply is to be sent to the Conductor.
        """
        text = (user_text or "").strip()
        if not text:
            return UserReply(
                text="(empty message — nothing to do)",
                is_error=False,
            )
        # Parse → objective.
        try:
            parsed = await self._parse(text)
        except LLMError as exc:
            logger.warning("LLM parse failed entirely: %s", exc)
            parsed = ObjectiveParseResult(
                objective=parse_objective_heuristic(text),
                source="heuristic",
            )
        objective = parsed.objective
        # Cache for status queries.
        async with self._obj_lock:
            self._recent_objectives[objective.objective_id] = objective
        # Cancel / status branches short-circuit.
        if objective.is_cancellation:
            return await self._handle_cancellation(objective)
        if objective.is_status_query:
            return await self._handle_status_query(objective)
        if objective.confidence < self._objective_min_confidence:
            return await self._handle_uncertain(objective, parsed)
        # Otherwise: dispatch to the Conductor.
        return await self._dispatch_objective(objective, parsed)

    # -- helpers: parse / dispatch / status --------------------------------

    async def _parse(self, text: str) -> ObjectiveParseResult:
        if self._llm is not None:
            return await parse_objective(text, self._llm)
        return ObjectiveParseResult(
            objective=parse_objective_heuristic(text),
            source="heuristic",
        )

    async def _dispatch_objective(
        self,
        objective: StructuredObjective,
        parsed: ObjectiveParseResult,
    ) -> UserReply:
        """Send the objective to the Conductor and tell the user."""
        payload_data = objective_to_spawn_payload(objective)
        envelope = self.build_request(
            CONDUCTOR_AGENT_ID,
            payload={
                "content_type": "data",
                "data": {
                    "action": SPAWN_INITIAL_SWARM_ACTION,
                    "objective": payload_data,
                    "source": parsed.source,
                },
            },
            receiver_role="orchestrator",
            goal=f"orchestrate:{objective.verb}",
            phase="execution",
        )
        try:
            await self.send(envelope)
        except Exception as exc:  # noqa: BLE001
            return await self._surface(
                UserReply(
                    text=(
                        f"Failed to dispatch objective to the Conductor: {exc}. "
                        "The kernel may be down."
                    ),
                    objective=objective,
                    is_error=True,
                )
            )
        sectors = ", ".join(f"`{s}`" for s in objective.suggested_sectors) or "(none)"
        text = (
            f"Understood. Goal: **{objective.goal}**\n"
            f"- Primary sector: `{objective.primary_sector}`\n"
            f"- Suggested sectors: {sectors}\n"
            f"- Confidence: {objective.confidence:.2f}\n"
            f"- Approval required: `{objective.needs_approval}`\n\n"
            f"Dispatching to the Conductor for workflow setup."
        )
        return await self._surface(
            UserReply(
                text=text,
                objective=objective,
                sent_envelope_id=str(envelope.envelope_id),
            )
        )

    async def _handle_status_query(
        self, objective: StructuredObjective
    ) -> UserReply:
        try:
            summary = await self.fetch_swarm_status()
        except Exception as exc:  # noqa: BLE001
            return await self._surface(
                UserReply(
                    text=f"Couldn't fetch swarm status: {exc}",
                    objective=objective,
                    is_error=True,
                )
            )
        return await self._surface(
            UserReply(
                text=summary.to_user_text(),
                objective=objective,
                is_status=True,
            )
        )

    async def _handle_cancellation(
        self, objective: StructuredObjective
    ) -> UserReply:
        envelope = self.build_event(
            CONDUCTOR_AGENT_ID,
            event_name="user_cancel",
            details={"objective_id": objective.objective_id},
            receiver_role="orchestrator",
        )
        try:
            await self.send(envelope)
            sent_id = str(envelope.envelope_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("could not send user_cancel: %s", exc)
            sent_id = None
        return await self._surface(
            UserReply(
                text=(
                    "Cancelling the current workflow. "
                    "The Conductor will drain in-flight steps."
                ),
                objective=objective,
                sent_envelope_id=sent_id,
            )
        )

    async def _handle_uncertain(
        self,
        objective: StructuredObjective,
        parsed: ObjectiveParseResult,
    ) -> UserReply:
        notes = "\n".join(f"- {n}" for n in objective.notes) or "- (no notes)"
        return await self._surface(
            UserReply(
                text=(
                    f"I parsed your message with low confidence "
                    f"({objective.confidence:.2f}). Could you rephrase?\n\n"
                    f"Notes:\n{notes}"
                ),
                objective=objective,
                is_error=True,
            )
        )

    async def _on_conductor_response(self, envelope) -> None:
        """Handle a Conductor response envelope.

        The Conductor replies with ``swarm_deployed`` once the sector
        managers are up, and with ``objective_complete`` once the
        workflow finishes. We surface both to the user.
        """
        try:
            data = envelope.payload.data  # type: ignore[attr-defined]
        except AttributeError:
            data = None
        if not isinstance(data, dict):
            return
        event_name = str(data.get("event", ""))
        if event_name == "swarm_deployed":
            sectors = data.get("sectors", [])
            sectors_text = ", ".join(f"`{s}`" for s in sectors) or "(none)"
            await self._surface(
                UserReply(
                    text=(
                        f"Swarm deployed. Active sectors: {sectors_text}. "
                        f"Tracking `{data.get('workflow_id', '?')}`."
                    )
                )
            )
        elif event_name == "objective_complete":
            summary = data.get("summary") or "(no summary returned)"
            await self._surface(
                UserReply(
                    text=f"Workflow complete.\n\n{summary}",
                )
            )
        elif event_name == "sector_complete":
            sector = data.get("sector", "?")
            await self._surface(
                UserReply(
                    text=f"Sector `{sector}` finished its part of the workflow.",
                )
            )
        else:
            logger.debug("unknown conductor event: %s", event_name)

    async def _on_agent_zombie(self, details: dict[str, Any]) -> None:
        agent_id = details.get("agent_id", "?")
        await self._surface(
            UserReply(
                text=(
                    f"Heads-up: agent `{agent_id}` missed its heartbeat and "
                    "is now `zombie`. The Conductor will decide whether to "
                    "retry, mutate, or replace it."
                )
            )
        )

    async def _on_permission_denied(self, details: dict[str, Any]) -> None:
        sender = details.get("sender", "?")
        reason = details.get("reason", "?")
        await self._surface(
            UserReply(
                text=(
                    f"Permission denied for `{sender}`: {reason}. "
                    "The envelope was dropped."
                )
            )
        )

    async def _on_queue_overflow(self, details: dict[str, Any]) -> None:
        agent_id = details.get("agent_id", "?")
        size = details.get("queue_size", "?")
        await self._surface(
            UserReply(
                text=(
                    f"Queue overflow for `{agent_id}` (size={size}); "
                    "oldest envelope was dropped."
                )
            )
        )

    async def _surface(self, reply: UserReply) -> UserReply:
        """Record the reply, fan it out, and return it."""
        await self._user_replies.put(reply)
        if self._on_user_reply is not None:
            try:
                await self._on_user_reply(reply)
            except Exception:  # noqa: BLE001
                logger.exception("user reply callback failed")
        return reply

    # -- kernel REST queries -----------------------------------------------

    async def fetch_swarm_status(self) -> SwarmStatusSummary:
        """Pull a small summary from the kernel registry."""
        timeout = httpx.Timeout(self._http_timeout)
        async with httpx.AsyncClient(timeout=timeout) as client:
            agents_resp = await client.get(
                f"{self._kernel_rest_url}/registry/agents",
                params={"limit": REGISTRY_PAGE_SIZE},
            )
            agents_resp.raise_for_status()
            agents_raw = agents_resp.json() or []
            main_resp = await client.get(
                f"{self._kernel_rest_url}/registry/agents/{self.agent_id}/status"
            )
            main_status = "unknown"
            main_hb: str | None = None
            if main_resp.status_code == 200:
                body = main_resp.json()
                main_status = str(body.get("status", "unknown"))
                main_hb = body.get("last_heartbeat")
            elif main_resp.status_code == 404:
                main_status = "not_registered"
        # Build counts.
        status_counts: dict[str, int] = {}
        connected = 0
        for a in agents_raw:
            s = str(a.get("status", "unknown"))
            status_counts[s] = status_counts.get(s, 0) + 1
            if a.get("connected_ws"):
                connected += 1
        return SwarmStatusSummary(
            main_agent_status=main_status,
            main_agent_last_heartbeat=main_hb,
            total_agents=len(agents_raw),
            status_counts=status_counts,
            connected=connected,
            sample_agents=agents_raw,
        )

    # -- CLI loop ----------------------------------------------------------

    async def run_cli(self) -> None:
        """Run a tiny interactive CLI on stdin/stdout.

        This is the simplest possible "user" — a developer typing into
        a terminal. The dashboard WebSocket bridge (Phase 7+) is the
        real production surface; the CLI exists so we can demo Phase 2
        without a frontend.
        """
        if not self.is_connected and self._closed:
            await self.start()
        # Drain the user_replies queue and print in a background task.
        async def _printer() -> None:
            while not self._closed:
                try:
                    reply = await asyncio.wait_for(
                        self._user_replies.get(), timeout=0.5
                    )
                except asyncio.TimeoutError:
                    continue
                print(f"\n[main-agent] {reply.text}\n> ", end="", flush=True)
        printer_task = asyncio.create_task(_printer())
        print(
            "OpenSwarm Main Agent ready. Type a goal, 'status', or 'quit'.",
            flush=True,
        )
        print("> ", end="", flush=True)
        try:
            loop = asyncio.get_running_loop()
            while not self._closed:
                line = await loop.run_in_executor(None, sys.stdin.readline)
                if not line:
                    # EOF.
                    break
                text = line.strip()
                if not text:
                    print("> ", end="", flush=True)
                    continue
                if text.lower() in {"quit", "exit"}:
                    break
                await self.handle_user_message(text)
                print("> ", end="", flush=True)
        finally:
            printer_task.cancel()
            try:
                await printer_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass


# ---------------------------------------------------------------------------
# Manifest loader helper
# ---------------------------------------------------------------------------

def load_main_agent_manifest(
    path: str | Path = "manifests/main-agent.json",
) -> Any:
    """Load the Main Agent's manifest from disk.

    Kept as a tiny helper so entry-point scripts (and tests) can
    import one function instead of duplicating the read-and-validate
    logic.
    """
    from kernel.models import AgentManifest  # local import to keep the
                                             # public surface clean
    raw = Path(path).read_text(encoding="utf-8")
    data = json.loads(raw)
    return AgentManifest.model_validate(data)


__all__ = [
    "CONDUCTOR_AGENT_ID",
    "MainAgent",
    "SPAWN_INITIAL_SWARM_ACTION",
    "SUBSCRIBED_KERNEL_EVENTS",
    "SwarmStatusSummary",
    "UserReply",
    "load_main_agent_manifest",
]
