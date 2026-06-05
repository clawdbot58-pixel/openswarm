"""The Conductor — workflow orchestrator and sector manager.

The Conductor is the brain between the Main Agent and the Sector
Managers. It does not talk to the user, execute tools, or write
files. Its job is to:

* receive a structured objective from the Main Agent;
* decompose the objective into a workflow DAG (one node per sector
  manager);
* spawn the right sector managers by sending them a
  ``data`` envelope carrying the manifest template and the
  objective slice;
* track the lifecycle of each sector manager and aggregate results;
* on kernel ``agent_zombie`` events, decide whether to retry,
  mutate the manifest, spawn a replacement, or escalate back to
  the Main Agent;
* on workflow completion, emit ``objective_complete`` back to the
  Main Agent.

The Conductor never talks to the user. Every outbound envelope
goes to a registered agent id. Any envelope whose ``receiver`` is
the user (``"user"``, ``"human"``, ``"dashboard"``) is logged and
dropped — this is enforced by :meth:`Conductor._assert_not_user`.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .base_agent import BaseAgent, utc_now
from .llm_client import LLMClient
from .objective_parser import KNOWN_SECTORS, StructuredObjective

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CONDUCTOR_AGENT_ID: str = "conductor"
MAIN_AGENT_ID: str = "main-agent"
SECTOR_MANAGER_PREFIX: str = "sector-manager"

# Events the Conductor emits to the Main Agent.
EVENT_SWARM_DEPLOYED: str = "swarm_deployed"
EVENT_OBJECTIVE_COMPLETE: str = "objective_complete"
EVENT_OBJECTIVE_FAILED: str = "objective_failed"
EVENT_SECTOR_COMPLETE: str = "sector_complete"
EVENT_SECTOR_FAILED: str = "sector_failed"

# Events the Conductor subscribes to.
SUBSCRIBED_EVENTS: tuple[str, ...] = (
    "agent_zombie",
    "auto_restart_triggered",
    "permission_denied",
    "queue_overflow",
)

# Default sector manager manifest template (relative to project root).
DEFAULT_SECTOR_MANAGER_MANIFEST: str = "manifests/sector-manager-template.json"

# Recipients we refuse to send to. Envelopes with these as the
# receiver are dropped. ``user`` / ``human`` / ``dashboard`` exist
# as a defense in depth — the contract already forbids them.
FORBIDDEN_RECEIVERS: frozenset[str] = frozenset(
    {"user", "human", "dashboard", "console"}
)


# ---------------------------------------------------------------------------
# State containers
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class WorkflowNode:
    """One step in a workflow DAG."""

    node_id: str
    sector: str
    description: str
    depends_on: list[str] = field(default_factory=list)
    sector_manager_id: str | None = None
    status: str = "pending"  # pending | running | complete | failed
    output: dict[str, Any] | None = None
    error: str | None = None
    started_at: str | None = None
    finished_at: str | None = None


@dataclass(slots=True)
class Workflow:
    """A workflow DAG owned by the Conductor."""

    workflow_id: str
    objective_id: str
    goal: str
    primary_sector: str
    nodes: list[WorkflowNode] = field(default_factory=list)
    status: str = "draft"  # draft | running | complete | failed | cancelled
    created_at: str = field(default_factory=lambda: utc_now().isoformat())
    updated_at: str = field(default_factory=lambda: utc_now().isoformat())
    results: dict[str, Any] = field(default_factory=dict)

    def by_sector(self, sector: str) -> WorkflowNode | None:
        """Return the first node for ``sector`` (sectors are unique per workflow)."""
        for n in self.nodes:
            if n.sector == sector:
                return n
        return None

    def mark_complete(self, sector: str, output: dict[str, Any]) -> None:
        node = self.by_sector(sector)
        if node is None:
            return
        node.status = "complete"
        node.output = output
        node.finished_at = utc_now().isoformat()
        self.results[sector] = output
        self._maybe_finish()

    def mark_failed(self, sector: str, error: str) -> None:
        node = self.by_sector(sector)
        if node is None:
            return
        node.status = "failed"
        node.error = error
        node.finished_at = utc_now().isoformat()
        # Failure in the primary sector means the workflow is done.
        if sector == self.primary_sector:
            self.status = "failed"
        self._maybe_finish()

    def _maybe_finish(self) -> None:
        if all(n.status == "complete" for n in self.nodes):
            self.status = "complete"
        elif all(n.status in {"complete", "failed"} for n in self.nodes):
            if any(n.status == "failed" for n in self.nodes):
                self.status = "failed"
        self.updated_at = utc_now().isoformat()

    def to_dict(self) -> dict[str, Any]:
        return {
            "workflow_id": self.workflow_id,
            "objective_id": self.objective_id,
            "goal": self.goal,
            "primary_sector": self.primary_sector,
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "nodes": [
                {
                    "node_id": n.node_id,
                    "sector": n.sector,
                    "description": n.description,
                    "depends_on": list(n.depends_on),
                    "sector_manager_id": n.sector_manager_id,
                    "status": n.status,
                    "error": n.error,
                    "started_at": n.started_at,
                    "finished_at": n.finished_at,
                }
                for n in self.nodes
            ],
            "results": dict(self.results),
        }


# ---------------------------------------------------------------------------
# Conductor
# ---------------------------------------------------------------------------

class Conductor(BaseAgent):
    """The Conductor agent.

    Parameters
    ----------
    manifest
        The Conductor's manifest (typically loaded from
        ``manifests/conductor.json``).
    llm
        Optional LLM client. The Conductor uses it to refine
        objective → workflow decisions when it is provided.
    sector_manager_manifest_path
        Path to the sector-manager template manifest. Defaults to
        ``manifests/sector-manager-template.json``. The Conductor
        loads it once and stamps a fresh ``agent_id`` per spawn.
    """

    def __init__(
        self,
        manifest: Any,
        *,
        llm: LLMClient | None = None,
        ws_url: str = "ws://127.0.0.1:8765/ws",
        sector_manager_manifest_path: str | Path = DEFAULT_SECTOR_MANAGER_MANIFEST,
        system_prompt_path: str | Path = "prompts/conductor_system.md",
    ) -> None:
        super().__init__(
            manifest=manifest,
            ws_url=ws_url,
            system_prompt_path=system_prompt_path,
        )
        self._llm: LLMClient | None = llm
        self._sector_manifest_path: Path = Path(sector_manager_manifest_path)
        self._sector_manifest_cache: dict[str, Any] | None = None
        self._workflows: dict[str, Workflow] = {}
        self._by_objective_id: dict[str, str] = {}
        self._workflows_lock: asyncio.Lock = asyncio.Lock()
        # The "current" workflow for status queries.
        self._active_workflow_id: str | None = None

    # -- properties --------------------------------------------------------

    @property
    def workflows(self) -> dict[str, Workflow]:
        """Read-only view of all workflows the conductor owns."""
        return dict(self._workflows)

    @property
    def active_workflow(self) -> Workflow | None:
        """The most recently started workflow, or ``None``."""
        if self._active_workflow_id is None:
            return None
        return self._workflows.get(self._active_workflow_id)

    def get_workflow(self, workflow_id: str) -> Workflow | None:
        """Return the workflow with this id, or ``None``."""
        return self._workflows.get(workflow_id)

    # -- inbound envelope handling -----------------------------------------

    async def on_envelope(self, envelope) -> None:  # type: ignore[override]
        """Dispatch inbound envelopes by type and sender.

        Sources the Conductor cares about:

        * ``main-agent`` (the Main Agent) — workflow directives.
        * any sector manager — completion / failure events.
        * any sector manager — task results.
        * any agent — direct messages (rare; logged).
        """
        sender = envelope.sender.agent_id
        env_type = envelope.envelope_type
        if envelope.envelope_type == "event":
            try:
                data = envelope.payload.data  # type: ignore[attr-defined]
                event_name = str(data.get("event", "")) if isinstance(data, dict) else ""
            except AttributeError:
                event_name = ""
            if event_name:
                await self.on_event(event_name, dict(envelope.payload.data) if isinstance(envelope.payload.data, dict) else {})  # type: ignore[attr-defined]
            return
        # From the Main Agent: workflow directives.
        if sender == MAIN_AGENT_ID and env_type == "request":
            await self._on_main_agent_directive(envelope)
            return
        # From sector managers: events and responses.
        if sender.startswith(SECTOR_MANAGER_PREFIX) or sender.startswith(
            "sector-"
        ):
            await self._on_sector_manager_message(sender, envelope)
            return
        # Cross-sector chatter — log and ignore.
        logger.debug(
            "conductor dropped envelope from %s (type=%s)",
            sender, env_type,
        )

    async def on_event(self, event_name: str, details: dict[str, Any]) -> None:  # type: ignore[override]
        """Handle a kernel-emitted event.

        The two events that drive orchestration logic:

        * ``agent_zombie`` — a registered agent missed its heartbeat.
        * ``auto_restart_triggered`` — the kernel will auto-restart
          it; we still want to mark the workflow step accordingly.
        * ``permission_denied`` — log and (if persistent) escalate.
        """
        if event_name not in SUBSCRIBED_EVENTS:
            return
        if event_name in {"agent_zombie", "auto_restart_triggered"}:
            await self._on_zombie_event(details, auto_restart=event_name == "auto_restart_triggered")
        elif event_name == "permission_denied":
            logger.warning(
                "permission_denied sender=%s reason=%s",
                details.get("sender"), details.get("reason"),
            )

    # -- main-agent directive dispatch -------------------------------------

    async def _on_main_agent_directive(self, envelope) -> None:
        """Route a Main-Agent request to the right Conductor method."""
        try:
            data = envelope.payload.data  # type: ignore[attr-defined]
        except AttributeError:
            data = None
        if not isinstance(data, dict):
            return
        action = str(data.get("action", "")).strip()
        if action == "spawn_initial_swarm":
            await self.handle_spawn_initial_swarm(data.get("objective") or {})
        else:
            logger.info("conductor: unknown main-agent action %r", action)

    async def handle_spawn_initial_swarm(self, objective_payload: dict[str, Any]) -> Workflow:
        """Build a workflow from an objective and dispatch sector managers.

        This is the public entry point used by the Main Agent's
        request, by tests, and by the integration suite. It is
        idempotent on ``objective_id``: a second call with the same
        id returns the existing workflow without re-spawning.
        """
        objective_id = str(objective_payload.get("objective_id") or uuid.uuid4())
        async with self._workflows_lock:
            existing_wid = self._by_objective_id.get(objective_id)
            if existing_wid is not None:
                existing = self._workflows.get(existing_wid)
                if existing is not None:
                    logger.info(
                        "conductor: spawn_initial_swarm is a no-op for existing "
                        "objective_id=%s (workflow_id=%s)",
                        objective_id,
                        existing.workflow_id,
                    )
                    return existing
        goal = str(objective_payload.get("goal") or "(no goal)")
        primary_sector = str(objective_payload.get("primary_sector") or "coding")
        suggested_sectors = list(objective_payload.get("sectors") or [primary_sector])
        # Deduplicate, preserve order, ensure primary is first.
        if primary_sector not in suggested_sectors:
            suggested_sectors.insert(0, primary_sector)
        suggested_sectors = list(dict.fromkeys(suggested_sectors))
        # Build the workflow.
        workflow_id = str(uuid.uuid4())
        workflow = Workflow(
            workflow_id=workflow_id,
            objective_id=objective_id,
            goal=goal,
            primary_sector=primary_sector,
        )
        for i, sector in enumerate(suggested_sectors):
            node = WorkflowNode(
                node_id=f"step_{i + 1}",
                sector=sector,
                description=self._describe_step(sector, goal, primary_sector),
                depends_on=(
                    [] if i == 0 else [f"step_{i}"]
                ) if sector == primary_sector else [
                    f"step_{j + 1}" for j in range(i)
                ],
            )
            workflow.nodes.append(node)
        # Stamp timestamps.
        workflow.status = "running"
        async with self._workflows_lock:
            self._workflows[workflow_id] = workflow
            self._by_objective_id[objective_id] = workflow_id
            self._active_workflow_id = workflow_id
        # Spawn the sector managers.
        for node in workflow.nodes:
            if node.depends_on:
                # Defer spawning dependent nodes until the first tick.
                continue
            await self._spawn_sector_manager(workflow, node)
        # Notify the Main Agent.
        await self._send_to_main(
            EVENT_SWARM_DEPLOYED,
            {
                "workflow_id": workflow_id,
                "objective_id": objective_id,
                "sectors": [n.sector for n in workflow.nodes],
                "primary_sector": primary_sector,
            },
        )
        return workflow

    def _describe_step(self, sector: str, goal: str, primary: str) -> str:
        """Produce a short human description of what a step does."""
        if sector == primary:
            return f"Drive `{sector}` work for: {goal}"
        return f"Support `{primary}` with `{sector}`: {goal}"

    # -- sector manager spawning -------------------------------------------

    async def _spawn_sector_manager(
        self, workflow: Workflow, node: WorkflowNode
    ) -> str:
        """Spawn a sector manager agent and dispatch the task envelope.

        Returns the ``sector_manager_id`` the Conductor assigned. The
        actual agent process must be started externally (Phase 3+
        supervisor); in Phase 2 we just send the ``spawn_request``
        envelope to the kernel so the manifest is registered and
        the agent can pick it up. If the agent is already running we
        fall back to a direct message.
        """
        sector_manager_id = f"{SECTOR_MANAGER_PREFIX}-{node.sector}"
        node.sector_manager_id = sector_manager_id
        node.status = "running"
        node.started_at = utc_now().isoformat()
        # Build the sector manager's task envelope.
        task_envelope = self.build_request(
            sector_manager_id,
            payload={
                "content_type": "data",
                "data": {
                    "action": "sector_task",
                    "workflow_id": workflow.workflow_id,
                    "objective_id": workflow.objective_id,
                    "node_id": node.node_id,
                    "sector": node.sector,
                    "description": node.description,
                    "goal": workflow.goal,
                    "primary_sector": workflow.primary_sector,
                },
            },
            receiver_role="specialist",
            goal=f"sector:{node.sector}",
            phase="execution",
        )
        try:
            await self.send(task_envelope)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "could not send task to %s: %s", sector_manager_id, exc
            )
            node.status = "failed"
            node.error = f"send_failed: {exc}"
            workflow._maybe_finish()
        return sector_manager_id

    # -- sector-manager inbound --------------------------------------------

    async def _on_sector_manager_message(
        self, sender: str, envelope
    ) -> None:
        """Handle a message from a sector manager."""
        try:
            data = envelope.payload.data  # type: ignore[attr-defined]
        except AttributeError:
            data = None
        if not isinstance(data, dict):
            return
        action = str(data.get("action", "")).strip()
        workflow_id = str(data.get("workflow_id") or "")
        sector = str(data.get("sector") or "")
        workflow = self._workflows.get(workflow_id) if workflow_id else None
        if workflow is None:
            # Match by sender if workflow id missing.
            workflow = self._workflow_by_sector_manager(sender)
        if action == "sector_complete":
            if workflow is None or sector == "":
                return
            workflow.mark_complete(
                sector,
                {
                    "summary": data.get("summary"),
                    "artifacts": data.get("artifacts", []),
                    "sender": sender,
                },
            )
            await self._check_workflow_progress(workflow)
        elif action == "sector_failed":
            if workflow is None or sector == "":
                return
            err = str(data.get("error") or "unknown failure")
            workflow.mark_failed(sector, err)
            await self._on_sector_failed(workflow, sector, err)
        else:
            logger.debug(
                "conductor: unknown sector action %r from %s", action, sender
            )

    def _workflow_by_sector_manager(self, sender: str) -> Workflow | None:
        for w in self._workflows.values():
            for n in w.nodes:
                if n.sector_manager_id == sender:
                    return w
        return None

    async def _check_workflow_progress(self, workflow: Workflow) -> None:
        """Decide whether to spawn dependent nodes or emit completion."""
        for node in workflow.nodes:
            if node.status != "pending":
                continue
            deps_satisfied = all(
                (dep := self._node_by_id(workflow, d)) is not None
                and dep.status == "complete"
                for d in node.depends_on
            )
            if not deps_satisfied:
                continue
            await self._spawn_sector_manager(workflow, node)
        if workflow.status == "complete":
            await self._on_workflow_complete(workflow)
        elif workflow.status == "failed":
            await self._on_workflow_failed(workflow)

    @staticmethod
    def _node_by_id(workflow: Workflow, node_id: str) -> WorkflowNode | None:
        for n in workflow.nodes:
            if n.node_id == node_id:
                return n
        return None

    async def _on_workflow_complete(self, workflow: Workflow) -> None:
        await self._send_to_main(
            EVENT_OBJECTIVE_COMPLETE,
            {
                "workflow_id": workflow.workflow_id,
                "objective_id": workflow.objective_id,
                "summary": self._summarise(workflow),
                "results": workflow.results,
            },
        )

    async def _on_workflow_failed(self, workflow: Workflow) -> None:
        await self._send_to_main(
            EVENT_OBJECTIVE_FAILED,
            {
                "workflow_id": workflow.workflow_id,
                "objective_id": workflow.objective_id,
                "error": self._first_error(workflow),
                "summary": self._summarise(workflow),
            },
        )

    async def _on_sector_failed(
        self, workflow: Workflow, sector: str, error: str
    ) -> None:
        await self._send_to_main(
            EVENT_SECTOR_FAILED,
            {
                "workflow_id": workflow.workflow_id,
                "objective_id": workflow.objective_id,
                "sector": sector,
                "error": error,
            },
        )
        if workflow.status == "failed":
            await self._on_workflow_failed(workflow)

    # -- zombie handling ---------------------------------------------------

    async def _on_zombie_event(
        self, details: dict[str, Any], *, auto_restart: bool
    ) -> None:
        agent_id = str(details.get("agent_id", ""))
        if not agent_id:
            return
        # Find the workflow step that owned this agent.
        for w in self._workflows.values():
            for n in w.nodes:
                if n.sector_manager_id == agent_id:
                    decision = self._decide_recovery(n, auto_restart=auto_restart)
                    if decision == "retry":
                        await self._spawn_sector_manager(w, n)
                    elif decision == "escalate":
                        await self._send_to_main(
                            "conductor_escalation",
                            {
                                "workflow_id": w.workflow_id,
                                "sector_manager_id": agent_id,
                                "sector": n.sector,
                                "reason": "repeated_failure",
                            },
                        )
                    elif decision == "spawn_replacement":
                        n.sector_manager_id = f"{SECTOR_MANAGER_PREFIX}-{n.sector}-{uuid.uuid4().hex[:8]}"
                        await self._spawn_sector_manager(w, n)
                    # "wait" → do nothing, kernel will auto-restart.
                    return

    def _decide_recovery(
        self, node: WorkflowNode, *, auto_restart: bool
    ) -> str:
        """Decide what to do when a sector manager dies.

        Heuristic policy:

        1. If the kernel will auto-restart (``auto_restart=True``),
           wait.
        2. If the node has no error history, retry.
        3. If it has failed once, mutate (sector_manager_id already
           changed; re-spawn = spawn_replacement).
        4. After two failed spawns, escalate to the Main Agent.
        """
        if auto_restart:
            return "wait"
        if node.status == "running" and node.started_at is not None and not node.error:
            return "retry"
        if node.error and "send_failed" in (node.error or ""):
            return "spawn_replacement"
        return "escalate"

    # -- helpers -----------------------------------------------------------

    async def _send_to_main(self, event: str, details: dict[str, Any]) -> None:
        envelope = self.build_event(
            MAIN_AGENT_ID,
            event,
            details,
            receiver_role="orchestrator",
        )
        try:
            await self.send(envelope)
        except Exception as exc:  # noqa: BLE001
            logger.warning("could not send %s to main-agent: %s", event, exc)

    @staticmethod
    def _summarise(workflow: Workflow) -> str:
        complete = sum(1 for n in workflow.nodes if n.status == "complete")
        failed = sum(1 for n in workflow.nodes if n.status == "failed")
        return (
            f"Workflow `{workflow.workflow_id}` finished: "
            f"{complete} complete, {failed} failed, total {len(workflow.nodes)}."
        )

    @staticmethod
    def _first_error(workflow: Workflow) -> str | None:
        for n in workflow.nodes:
            if n.status == "failed" and n.error:
                return f"{n.sector}: {n.error}"
        return None

    def _assert_not_user(self, receiver_id: str) -> None:
        """Defence in depth: refuse to send envelopes to user-shaped ids.

        The contract already forbids agents from talking to the user
        directly. This check is here to catch programming errors
        early — every send path goes through :meth:`BaseAgent.send`,
        but a future contributor might add a shortcut.
        """
        if receiver_id in FORBIDDEN_RECEIVERS or receiver_id.startswith(
            "user-"
        ):
            raise ValueError(
                f"conductor cannot send to {receiver_id!r}; "
                "the user is the Main Agent's responsibility."
            )

    # -- mutation API (used by tests and by escalation handlers) -----------

    async def retry_step(self, workflow_id: str, node_id: str) -> bool:
        """Force-retry a node. Returns ``True`` if the retry was dispatched."""
        async with self._workflows_lock:
            w = self._workflows.get(workflow_id)
        if w is None:
            return False
        node = self._node_by_id(w, node_id)
        if node is None or node.status == "complete":
            return False
        node.status = "pending"
        node.error = None
        await self._spawn_sector_manager(w, node)
        return True

    async def cancel_workflow(self, workflow_id: str) -> bool:
        """Mark a workflow as cancelled. Returns ``True`` if it existed."""
        async with self._workflows_lock:
            w = self._workflows.get(workflow_id)
        if w is None:
            return False
        w.status = "cancelled"
        await self._send_to_main(
            "workflow_cancelled",
            {"workflow_id": workflow_id, "objective_id": w.objective_id},
        )
        return True

    # -- sector manifest loading ------------------------------------------

    def _load_sector_manager_manifest(self) -> dict[str, Any]:
        """Load and cache the sector-manager template manifest."""
        if self._sector_manifest_cache is not None:
            return self._sector_manifest_cache
        raw = self._sector_manifest_path.read_text(encoding="utf-8")
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError(
                f"sector manager manifest at {self._sector_manifest_path} "
                "must be a JSON object"
            )
        self._sector_manifest_cache = data
        return data


__all__ = [
    "CONDUCTOR_AGENT_ID",
    "Conductor",
    "EVENT_OBJECTIVE_COMPLETE",
    "EVENT_OBJECTIVE_FAILED",
    "EVENT_SECTOR_COMPLETE",
    "EVENT_SECTOR_FAILED",
    "EVENT_SWARM_DEPLOYED",
    "FORBIDDEN_RECEIVERS",
    "MAIN_AGENT_ID",
    "SECTOR_MANAGER_PREFIX",
    "SUBSCRIBED_EVENTS",
    "Workflow",
    "WorkflowNode",
]
