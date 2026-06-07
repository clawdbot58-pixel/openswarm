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
  Conductor, and reports progress to the user in natural language;
* on Phase 9 self-healing events (``loop_detected``,
  ``budget_exhausted``, ``step_timeout``, ``workflow_resume``,
  ``step_recovered``) decides the recovery strategy and emits a
  :class:`~kernel.recovery.RecoveryDecision` back to the kernel.

This module is deliberately small. The heavy reasoning lives in the
LLM via the system prompt; the Python code is plumbing.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
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


# Re-export the kernel's recovery types so callers can build decisions
# without importing the kernel module directly. Keeps the public
# surface narrow.
try:  # pragma: no cover — defensive: kernel may not be importable in tests
    from kernel.recovery import (  # type: ignore[import-not-found]
        RecoveryActionLiteral,
        RecoveryDecision,
    )
except Exception:  # noqa: BLE001
    RecoveryActionLiteral = str  # type: ignore[assignment,misc]
    RecoveryDecision = Any  # type: ignore[assignment,misc]


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
    # Phase 9 self-healing events.
    "loop_detected",
    "step_timeout",
    "budget_exhausted",
    "workflow_resume",
    "step_recovered",
    "fallback_invoked",
    "compensation_invoked",
    "respawn_requested",
    "escalation_requested",
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
        loop_optimizer: Any | None = None,
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
        # Phase 10: optional loop optimizer.  When set, the Main Agent
        # can ask it to pick a thinking-loop variant for a given task
        # type via :meth:`select_thinking_loop`.
        self._loop_optimizer: Any | None = loop_optimizer

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

    # -- Phase 10: thinking-loop selection --------------------------------

    @property
    def loop_optimizer(self) -> Any:
        """The configured :class:`loop_optimizer.LoopOptimizer` (or ``None``)."""
        return self._loop_optimizer

    def set_loop_optimizer(self, optimizer: Any) -> None:
        """Attach a :class:`loop_optimizer.LoopOptimizer` to this agent.

        When an optimizer is attached, the Main Agent can be asked to
        pick a thinking-loop variant for a given task type.  This is
        the Phase 10 hook that lets the agent pick *which* loop to run
        for a given user goal — the previous behaviour (always use
        ``reflection``) is preserved when no optimizer is attached.
        """
        self._loop_optimizer = optimizer

    async def select_thinking_loop(
        self,
        task_type: str,
        *,
        min_trials: int = 3,
    ) -> Any:
        """Pick the best-known thinking loop for ``task_type``.

        Args:
            task_type: Tag (e.g. ``"code_review"``,
                ``"summarisation"``).
            min_trials: Minimum evidence threshold for leaderboard
                selection; falls back to a premade loop when no
                entry clears the bar.

        Returns:
            A :class:`loops.assembler.LoopGraph`.  When no optimizer
            is attached, returns the result of
            :meth:`assemble_builtin` for the natural base loop
            (``reflection`` / ``cot`` / ``tree`` / ``debate``).

        Raises:
            RuntimeError: when the optimizer raises (and the user did
                not pass a custom base loop).
        """
        if self._loop_optimizer is None:
            # Defensive default: assemble a premade loop without ever
            # touching the leaderboard.  Keeps callers that never
            # wired an optimizer working.
            try:
                from loops.assembler import LoopAssembler  # local import

                base_id = self._default_base_for_task(task_type)
                return LoopAssembler().assemble_builtin(base_id)
            except Exception:  # noqa: BLE001
                # Last-resort: an empty direct graph.
                from loops.assembler import LoopAssembler

                return LoopAssembler().assemble_builtin("reflection")
        try:
            return await self._loop_optimizer.select_for_task(
                task_type=task_type, min_trials=min_trials
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("select_thinking_loop failed: %s", exc)
            from loops.assembler import LoopAssembler

            return LoopAssembler().assemble_builtin(
                self._default_base_for_task(task_type)
            )

    @staticmethod
    def _default_base_for_task(task_type: str) -> str:
        """Map a task type to a sensible premade loop (Phase 4 default)."""
        mapping = {
            "code": "reflection",
            "code_review": "reflection",
            "review": "reflection",
            "writing": "reflection",
            "edit": "reflection",
            "summary": "reflection",
            "summarisation": "reflection",
            "math": "cot",
            "logic": "cot",
            "research": "cot",
            "analysis": "cot",
            "design": "tree",
            "planning": "tree",
            "brainstorm": "tree",
            "decision": "debate",
            "tradeoff": "debate",
        }
        return mapping.get(task_type, "reflection")

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
        if env_type == "intent":
            await self._on_intent(envelope)
            return
        logger.debug(
            "main-agent received envelope type=%s from %s",
            env_type, sender,
        )

    async def _on_intent(self, envelope: Any) -> None:
        """Handle ``intent`` envelopes — chat turns and legacy goal API."""
        try:
            data = envelope.payload.data  # type: ignore[attr-defined]
        except AttributeError:
            return
        if not isinstance(data, dict):
            return

        chat_id = str(data.get("chat_id") or "")
        goal = str(data.get("goal") or "").strip()
        workflow_id = str(data.get("workflow_id") or "")

        if chat_id:
            reply = await self._handle_chat_turn(goal, chat_id)
            late = await self._consume_steering(chat_id)
            text = reply.text
            if late:
                text = text.rstrip() + "\n\n(Noted: " + "; ".join(late) + ")"
            await self._complete_chat(chat_id, text, error=None)
            return

        if not goal:
            return
        if workflow_id:
            await self._patch_workflow(workflow_id, status="running")
        reply = await self.handle_user_message(goal)
        if workflow_id:
            if reply.is_error:
                await self._patch_workflow(
                    workflow_id, status="failed", error=reply.text
                )
            else:
                await self._patch_workflow(
                    workflow_id,
                    status="completed",
                    result={"reply": reply.text, "envelope_id": reply.sent_envelope_id},
                )

    async def _consume_steering(self, chat_id: str) -> list[str]:
        import json
        import urllib.request

        body = b"{}"
        req = urllib.request.Request(  # noqa: S310
            f"{self._kernel_rest_url}/chat/{chat_id}/steering/consume",
            data=body,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self._http_timeout) as resp:  # noqa: S310
                data = json.loads(resp.read().decode("utf-8"))
                return list(data.get("steering") or [])
        except Exception:  # noqa: BLE001
            return []

    async def _handle_chat_turn(self, text: str, chat_id: str) -> UserReply:
        """Conversational Telegram/CLI turn — reply directly, steer on follow-ups."""
        steering = await self._consume_steering(chat_id)
        if steering:
            text = (
                text
                + "\n\n(User steering while you work:\n"
                + "\n".join(f"- {s}" for s in steering)
                + ")"
            )
        try:
            parsed = await self._parse(text)
        except LLMError as exc:
            logger.warning("LLM parse failed entirely: %s", exc)
            parsed = ObjectiveParseResult(
                objective=parse_objective_heuristic(text),
                source="heuristic",
            )
        objective = parsed.objective
        async with self._obj_lock:
            self._recent_objectives[objective.objective_id] = objective
        if objective.is_cancellation:
            return await self._handle_cancellation(objective)
        if objective.is_status_query:
            return await self._handle_status_query(objective)
        if self._is_conversational_chat(text, objective):
            return await self._conversational_reply(text)
        if objective.confidence < self._objective_min_confidence:
            return await self._conversational_reply(text)
        reply = await self._dispatch_objective(objective, parsed)
        # Strip markdown bold — Telegram plain-text channel.
        plain = reply.text.replace("**", "").replace("`", "")
        return UserReply(
            text=plain,
            objective=reply.objective,
            sent_envelope_id=reply.sent_envelope_id,
            is_error=False,
        )

    @staticmethod
    def _is_conversational_chat(text: str, objective: StructuredObjective) -> bool:
        lower = text.lower().strip().rstrip("!?.")
        greetings = {
            "hi", "hello", "hey", "yo", "howdy", "hiya",
            "thanks", "thank you", "good morning", "good evening",
        }
        if lower in greetings:
            return True
        if len(text.split()) <= 3 and not objective.suggested_sectors:
            return True
        return False

    async def _conversational_reply(self, text: str) -> UserReply:
        if self._llm is None:
            return UserReply(
                text=(
                    "Hey! I'm OpenSwarm — your multi-agent orchestrator. "
                    "Tell me what you'd like built, researched, or reviewed."
                ),
                is_error=False,
            )
        try:
            result = await self._llm.complete_text(
                system=(
                    "You are OpenSwarm, a friendly multi-agent orchestrator. "
                    "Reply in 1-3 short sentences. Be warm and direct. "
                    "If the user greets you, greet back and invite a task. "
                    "You always get the last word — end with something helpful."
                ),
                user=text,
                temperature=0.4,
                max_tokens=256,
            )
            reply = (result.text or "").strip()
            if reply:
                return UserReply(text=reply, is_error=False)
        except LLMError as exc:
            logger.warning("conversational LLM failed: %s", exc)
        return UserReply(
            text="I'm here — what would you like me to work on?",
            is_error=False,
        )

    async def _complete_chat(
        self, chat_id: str, reply: str, *, error: str | None = None
    ) -> None:
        import json
        import urllib.error
        import urllib.request

        body: dict[str, Any] = {"reply": reply}
        if error:
            body["error"] = error
        req = urllib.request.Request(  # noqa: S310
            f"{self._kernel_rest_url}/chat/{chat_id}/complete",
            data=json.dumps(body).encode("utf-8"),
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self._http_timeout):  # noqa: S310
                pass
        except (urllib.error.URLError, OSError, urllib.error.HTTPError) as exc:
            logger.warning("chat complete failed for %s: %s", chat_id, exc)

    async def _patch_workflow(
        self,
        workflow_id: str,
        *,
        status: str,
        result: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        """Best-effort workflow status update via kernel REST."""
        import json
        import urllib.error
        import urllib.request

        body: dict[str, Any] = {"status": status}
        if result is not None:
            body["result"] = result
        if error is not None:
            body["error"] = error
        req = urllib.request.Request(  # noqa: S310
            f"{self._kernel_rest_url}/workflows/{workflow_id}",
            data=json.dumps(body).encode("utf-8"),
            method="PATCH",
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self._http_timeout):  # noqa: S310
                pass
        except (urllib.error.URLError, OSError, urllib.error.HTTPError) as exc:
            logger.debug("workflow patch failed for %s: %s", workflow_id, exc)

    async def on_event(self, event_name: str, details: dict[str, Any]) -> None:  # type: ignore[override]
        """Handle a kernel-emitted event.

        We never act on these directly — the Conductor does. We log
        them, optionally forward critical ones, and surface a brief
        message to the user when the event affects the user-visible
        state. Phase 9 self-healing events go through the recovery
        handler chain which produces a :class:`RecoveryDecision`.
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
        elif event_name == "loop_detected":
            await self._on_loop_detected(details)
        elif event_name == "budget_exhausted":
            await self._on_budget_exhausted(details)
        elif event_name == "step_timeout":
            await self._on_step_timeout(details)
        elif event_name == "workflow_resume":
            await self._on_workflow_resume(details)
        elif event_name == "step_recovered":
            await self._on_step_recovered(details)
        elif event_name in (
            "fallback_invoked",
            "compensation_invoked",
            "respawn_requested",
            "escalation_requested",
        ):
            await self._on_recovery_event(event_name, details)
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
            f"On it — **{objective.goal}**\n\n"
            f"I'll spin up the swarm"
            + (f" ({sectors})" if sectors != "(none)" else "")
            + ". You'll see progress on the dashboard."
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

    # -- Phase 9 self-healing handlers -------------------------------------

    async def _on_loop_detected(self, details: dict[str, Any]) -> None:
        """Handle a kernel-emitted ``loop_detected`` event.

        Builds a :class:`RecoveryDecision` via the pure-Python strategy
        in :meth:`handle_loop_detected`, stores it for the conductor
        to act on, and surfaces a short user-facing message.
        """
        decision = self.handle_loop_detected(details)
        await self._record_recovery_decision(decision, source="loop_detected")
        # Surface a brief message to the user.
        agent_id = details.get("agent_id") or "?"
        pattern = details.get("pattern") or "unknown"
        count = details.get("consecutive_count") or 0
        await self._surface(
            UserReply(
                text=(
                    f"⚠️ Loop Detected: `{pattern}`\n"
                    f"Agent: `{agent_id}`\n"
                    f"Count: {count}\n"
                    f"Decision: `{decision.action}`\n"
                    f"Reason: {decision.reason}"
                ),
                is_error=(decision.action == "escalate_to_user"),
            )
        )

    async def _on_budget_exhausted(self, details: dict[str, Any]) -> None:
        """Handle ``budget_exhausted``: build a decision (default: escalate)."""
        decision = self.handle_budget_exhausted(details)
        await self._record_recovery_decision(decision, source="budget_exhausted")
        wf = details.get("workflow_id") or "?"
        step = details.get("step_id") or "?"
        cost = details.get("cost_so_far")
        budget = details.get("budget")
        cost_str = f"${cost:.2f}" if isinstance(cost, (int, float)) else "?"
        budget_str = f"${budget:.2f}" if isinstance(budget, (int, float)) else "?"
        await self._surface(
            UserReply(
                text=(
                    f"⚠️ Budget Exhausted\n"
                    f"Step: `{step}` (workflow `{wf}`)\n"
                    f"Cost: {cost_str} / {budget_str} budget\n"
                    f"Decision: `{decision.action}`\n"
                    f"Reason: {decision.reason}"
                ),
                is_error=(decision.action != "budget_override"),
            )
        )

    async def _on_step_timeout(self, details: dict[str, Any]) -> None:
        """Handle ``step_timeout``: escalate to user by default."""
        wf = details.get("workflow_id") or "?"
        step = details.get("step_id") or "?"
        elapsed = details.get("elapsed_minutes")
        max_min = details.get("max_minutes")
        elapsed_str = (
            f"{elapsed:.1f} min" if isinstance(elapsed, (int, float)) else "?"
        )
        max_str = (
            f"{max_min:.1f} min" if isinstance(max_min, (int, float)) else "?"
        )
        await self._surface(
            UserReply(
                text=(
                    f"⏱️ Step timeout\n"
                    f"Step `{step}` of workflow `{wf}` exceeded its "
                    f"{max_str} budget (elapsed {elapsed_str}).\n"
                    f"Decision: escalate_to_user"
                ),
                is_error=True,
            )
        )

    async def _on_workflow_resume(self, details: dict[str, Any]) -> None:
        """Handle ``workflow_resume``: pick a strategy and surface."""
        decision = self.handle_workflow_resume(details)
        await self._record_recovery_decision(
            decision, source="workflow_resume"
        )
        wf = details.get("workflow_id") or "?"
        last = details.get("last_step") or "(none)"
        await self._surface(
            UserReply(
                text=(
                    f"🔄 Workflow resume\n"
                    f"Workflow `{wf}` is recovering from last step `{last}`.\n"
                    f"Decision: `{decision.action}`\n"
                    f"Reason: {decision.reason}"
                )
            )
        )

    async def _on_step_recovered(self, details: dict[str, Any]) -> None:
        """Handle ``step_recovered`` (positive feedback from the kernel)."""
        strategy = details.get("strategy") or "?"
        wf = details.get("workflow_id") or "?"
        step = details.get("step_id") or "?"
        mutate = details.get("mutate_count", 0)
        await self._surface(
            UserReply(
                text=(
                    f"✅ Step recovered\n"
                    f"Step `{step}` of workflow `{wf}` (strategy "
                    f"`{strategy}`, mutate {mutate}/3) is back on track."
                )
            )
        )

    async def _on_recovery_event(
        self, event_name: str, details: dict[str, Any]
    ) -> None:
        """Generic log + user-surface for fallback / compensation / respawn / escalation events."""
        logger.info("recovery event %s: %s", event_name, details)
        wf = details.get("workflow_id") or "?"
        if event_name == "fallback_invoked":
            steps = details.get("fallback_steps") or []
            steps_text = ", ".join(f"`{s}`" for s in steps) or "(none)"
            await self._surface(
                UserReply(
                    text=(
                        f"Fallback chain invoked for workflow `{wf}`: "
                        f"{steps_text}."
                    )
                )
            )
        elif event_name == "compensation_invoked":
            steps = details.get("compensation_steps") or []
            steps_text = ", ".join(f"`{s}`" for s in steps) or "(none)"
            await self._surface(
                UserReply(
                    text=(
                        f"Compensation chain running for workflow `{wf}`: "
                        f"{steps_text}."
                    )
                )
            )
        elif event_name == "respawn_requested":
            await self._surface(
                UserReply(
                    text=(
                        f"All agents in workflow `{wf}` will be respawned. "
                        f"Reason: {details.get('reason') or '(not given)'}"
                    )
                )
            )
        elif event_name == "escalation_requested":
            await self._surface(
                UserReply(
                    text=(
                        f"Escalation requested for workflow `{wf}` / step "
                        f"`{details.get('step_id', '?')}` — "
                        f"action `{details.get('action', '?')}`."
                    ),
                    is_error=True,
                )
            )

    # -- public Phase 9 decision helpers (testable, no LLM required) -------

    def handle_loop_detected(
        self, event: dict[str, Any]
    ) -> "RecoveryDecision":
        """Decide a recovery strategy for a ``loop_detected`` event.

        Pure-Python and deterministic. The Main Agent may later call
        the LLM to refine this, but the default policy is:

        * ``mutate_exhausted`` → ``escalate_to_user``
        * ``action_repeat``     → ``retry_with_different_approach``
                                   (alias for ``retry_with_same_agent``)
        * ``clarification_spin``→ ``escalate_to_user``
        * ``tool_failure_repeat``→ ``mutate_config`` (upgrade model)
        * anything else         → ``retry_with_same_agent``
        """
        pattern = str(event.get("pattern") or "")
        if pattern == "mutate_exhausted":
            return self._make_decision(
                action="escalate_to_user",
                reason="Mutate chain exhausted; need human guidance.",
            )
        if pattern == "clarification_spin":
            return self._make_decision(
                action="escalate_to_user",
                reason="Agent keeps asking for clarification with no new input.",
            )
        if pattern == "tool_failure_repeat":
            return self._make_decision(
                action="mutate_config",
                manifest_delta={"model_tier": "powerful"},
                reason="Tool failing repeatedly; upgrading model tier.",
            )
        if pattern == "action_repeat":
            return self._make_decision(
                action="retry_with_same_agent",
                reason="Agent repeating the same action; retrying once.",
            )
        # Unknown pattern: be conservative.
        return self._make_decision(
            action="retry_with_same_agent",
            reason=f"Unrecognised loop pattern {pattern!r}; retrying.",
        )

    def handle_budget_exhausted(
        self, event: dict[str, Any]
    ) -> "RecoveryDecision":
        """Decide a recovery strategy for a ``budget_exhausted`` event.

        Default: ``escalate_to_user``. The Main Agent may issue a
        one-time ``budget_override`` by calling :meth:`grant_budget_override`
        and re-emitting the decision.
        """
        wf = event.get("workflow_id") or "?"
        step = event.get("step_id") or "?"
        return self._make_decision(
            action="escalate_to_user",
            reason=(
                f"Step {step!r} of workflow {wf!r} exceeded its USD budget."
            ),
        )

    def handle_workflow_resume(
        self, event: dict[str, Any]
    ) -> "RecoveryDecision":
        """Decide a recovery strategy on ``workflow_resume``.

        Default: ``continue_from_step`` from the most recent
        checkpoint. The Main Agent may instead pick
        ``rollback_n_steps`` or ``respawn_all_agents`` based on
        user input.
        """
        wf = event.get("workflow_id") or "?"
        last = event.get("last_step") or "(none)"
        return self._make_decision(
            action="continue_from_step",
            reason=(
                f"Resuming workflow {wf!r} from last step {last!r}."
            ),
        )

    # -- internal helpers -------------------------------------------------

    def _make_decision(
        self,
        *,
        action: str,
        reason: str,
        manifest_delta: dict[str, Any] | None = None,
        budget_override_usd: float | None = None,
        rollback_n_steps: int = 0,
        fallback_steps: list[str] | None = None,
        compensation_steps: list[str] | None = None,
    ) -> "RecoveryDecision":
        """Build a :class:`RecoveryDecision` without depending on the LLM."""
        try:
            return RecoveryDecision(
                action=action,  # type: ignore[arg-type]
                manifest_delta=manifest_delta or {},
                reason=reason,
                budget_override_usd=budget_override_usd,
                rollback_n_steps=rollback_n_steps,
                fallback_steps=fallback_steps or [],
                compensation_steps=compensation_steps or [],
            )
        except Exception:  # noqa: BLE001 — tests may pass a stub
            return RecoveryDecision(  # type: ignore[call-arg]
                action=action,
                manifest_delta=manifest_delta or {},
                reason=reason,
            )

    async def _record_recovery_decision(
        self,
        decision: "RecoveryDecision",
        *,
        source: str,
    ) -> None:
        """Audit the decision in the kernel registry (best-effort)."""
        try:
            payload = {
                "source": source,
                "action": getattr(decision, "action", "unknown"),
                "reason": getattr(decision, "reason", ""),
            }
            md = getattr(decision, "manifest_delta", None)
            if isinstance(md, dict) and md:
                payload["manifest_delta"] = md
            override = getattr(decision, "budget_override_usd", None)
            if override is not None:
                payload["budget_override_usd"] = override
            async with httpx.AsyncClient(timeout=2.0) as client:
                # We don't fail loudly if the kernel is offline; the
                # decision is still in self._recent_decisions.
                try:
                    await client.post(
                        f"{self._kernel_rest_url}/audit",
                        json={
                            "action": "recovery_decision",
                            "result": "ok",
                            "details": payload,
                        },
                    )
                except Exception:  # noqa: BLE001
                    logger.debug(
                        "audit POST failed for recovery_decision"
                    )
        except Exception:  # noqa: BLE001
            logger.exception("record_recovery_decision failed")

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


async def run_daemon() -> None:
    """Run the Main Agent as a long-lived background process."""
    logging.basicConfig(
        level=os.environ.get("OPENSWARM_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    from agents.llm_setup import build_llm_client_from_section
    from config import get_config

    cfg = get_config()
    manifest_path = os.environ.get("MAIN_AGENT_MANIFEST_PATH", "manifests/main-agent.json")
    ws_url = os.environ.get("KERNEL_WS", "ws://127.0.0.1:8765/ws")
    kernel_rest_url = os.environ.get("KERNEL_REST_URL", "http://127.0.0.1:8765")

    llm = build_llm_client_from_section(cfg.llm)
    agent = MainAgent(
        load_main_agent_manifest(manifest_path),
        llm=llm,
        ws_url=ws_url,
        kernel_rest_url=kernel_rest_url,
    )
    await agent.start()
    logger.info("main agent connected to %s (llm profile=%s)", ws_url, cfg.llm.profile)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError):
            pass
    try:
        await stop.wait()
    finally:
        await agent.close()


def main() -> None:
    """Entry point for ``python -m agents.main_agent``."""
    try:
        asyncio.run(run_daemon())
    except KeyboardInterrupt:
        return


__all__ = [
    "CONDUCTOR_AGENT_ID",
    "MainAgent",
    "RecoveryActionLiteral",
    "RecoveryDecision",
    "SPAWN_INITIAL_SWARM_ACTION",
    "SUBSCRIBED_KERNEL_EVENTS",
    "SwarmStatusSummary",
    "UserReply",
    "load_main_agent_manifest",
    "main",
    "run_daemon",
]


if __name__ == "__main__":  # pragma: no cover
    main()
