"""Sector Manager — manages a domain sector, delegates to workers.

A Sector Manager sits between the Conductor and the workers in its
sector. Its responsibilities:

* receive an objective slice from the Conductor (one per sector);
* break the slice into worker tasks (in Phase 2, the template breaks
  them deterministically; Phase 3+ will add an LLM-driven planner);
* dispatch the tasks to workers (or echo a stub result if no worker
  process is attached — Phase 2 doesn't ship the worker runtime);
* aggregate worker results;
* emit ``sector_complete`` (or ``sector_failed``) to the Conductor;
* on ``agent_zombie`` for a worker, re-spawn or escalate.

Cross-sector communication
--------------------------
Sector managers may message each other directly through the kernel
but MUST CC the Conductor. The :meth:`SectorManager.send_to_sector`
helper enforces the CC rule by always adding the Conductor as a
secondary receiver (using a second envelope — the contract does not
have a CC field, so we send two envelopes, one to the peer and one
to the Conductor as a notification).
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .base_agent import BaseAgent, utc_now
from .llm_client import LLMClient

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CONDUCTOR_AGENT_ID: str = "conductor"
SECTOR_MANAGER_PREFIX: str = "sector-manager"
WORKER_PREFIX: str = "worker"

# Actions the Conductor sends.
ACTION_SECTOR_TASK: str = "sector_task"

# Actions the SectorManager sends.
ACTION_SECTOR_COMPLETE: str = "sector_complete"
ACTION_SECTOR_FAILED: str = "sector_failed"
ACTION_WORKER_RESULT: str = "worker_result"

# Events the SectorManager subscribes to.
SUBSCRIBED_EVENTS: tuple[str, ...] = (
    "agent_zombie",
    "auto_restart_triggered",
    "permission_denied",
)

# Recipients forbidden by the conductor's contract; mirrored here
# so the sector manager also refuses to talk to the user directly.
FORBIDDEN_RECEIVERS: frozenset[str] = frozenset(
    {"user", "human", "dashboard", "console"}
)


# ---------------------------------------------------------------------------
# State containers
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class WorkerTask:
    """One task the sector manager has dispatched to a worker."""

    task_id: str
    worker_id: str
    description: str
    status: str = "pending"  # pending | running | complete | failed
    output: dict[str, Any] | None = None
    error: str | None = None
    started_at: str | None = None
    finished_at: str | None = None


@dataclass(slots=True)
class SectorJob:
    """The state the SectorManager holds for one objective slice."""

    job_id: str
    workflow_id: str
    objective_id: str
    sector: str
    description: str
    goal: str
    primary_sector: str
    tasks: list[WorkerTask] = field(default_factory=list)
    status: str = "running"  # running | complete | failed
    created_at: str = field(default_factory=lambda: utc_now().isoformat())
    updated_at: str = field(default_factory=lambda: utc_now().isoformat())
    artifacts: list[dict[str, Any]] = field(default_factory=list)

    def all_done(self) -> bool:
        return all(t.status in {"complete", "failed"} for t in self.tasks)

    def all_complete(self) -> bool:
        return bool(self.tasks) and all(
            t.status == "complete" for t in self.tasks
        )

    def first_error(self) -> str | None:
        for t in self.tasks:
            if t.status == "failed" and t.error:
                return t.error
        return None


# ---------------------------------------------------------------------------
# SectorManager
# ---------------------------------------------------------------------------

class SectorManager(BaseAgent):
    """Sector manager agent.

    Parameters
    ----------
    manifest
        A :class:`AgentManifest` (typically derived from
        ``manifests/sector-manager-template.json`` with a unique
        ``agent_id`` stamped per sector).
    sector
        The sector name (e.g. ``"coding"``). Used to label child
        workers (``worker-{sector}-N``) and to scope routing.
    conductor_id
        The Conductor's ``agent_id``. Defaults to ``"conductor"``.
    workers_per_job
        Maximum number of workers the manager will spawn for a
        single sector job. Default 1 keeps things simple in
        Phase 2 tests; production jobs usually want 2-4.
    llm
        Optional LLM client. Phase 2 does not yet use it for
        planning; the manager plans with a deterministic
        ``_default_plan`` heuristic. The LLM is reserved for
        Phase 3+ where the manager plans with CoT.
    """

    def __init__(
        self,
        manifest: Any,
        *,
        sector: str,
        ws_url: str = "ws://127.0.0.1:8765/ws",
        conductor_id: str = CONDUCTOR_AGENT_ID,
        workers_per_job: int = 1,
        llm: LLMClient | None = None,
        system_prompt_path: str | Path = "prompts/sector_manager_system.md",
    ) -> None:
        super().__init__(
            manifest=manifest,
            ws_url=ws_url,
            system_prompt_path=system_prompt_path,
        )
        self.sector: str = sector
        self.conductor_id: str = conductor_id
        self.workers_per_job: int = max(1, int(workers_per_job))
        self._llm: LLMClient | None = llm
        # State: jobs indexed by job_id, workers by worker_id.
        self._jobs: dict[str, SectorJob] = {}
        self._jobs_lock: asyncio.Lock = asyncio.Lock()
        # Per-sector worker counter, used to assign stable ids.
        self._worker_counter: int = 0

    # -- properties --------------------------------------------------------

    @property
    def jobs(self) -> dict[str, SectorJob]:
        """Read-only view of all jobs this manager owns."""
        return dict(self._jobs)

    def get_job(self, job_id: str) -> SectorJob | None:
        return self._jobs.get(job_id)

    # -- envelope building --------------------------------------------------

    def build_request(
        self,
        receiver_id: str,
        payload: dict[str, Any],
        *,
        receiver_role: str = "executor",
        goal: str = "agent-task",
        phase: str = "execution",
        reply_to: str | None = None,
    ) -> Envelope:
        """Override to automatically wrap user payloads in {content_type, data}."""
        wrapped: dict[str, Any]
        if isinstance(payload, dict) and "content_type" not in payload:
            wrapped = {"content_type": "data", "data": payload}
        else:
            wrapped = payload
        return super().build_request(
            receiver_id,
            wrapped,
            receiver_role=receiver_role,
            goal=goal,
            phase=phase,
            reply_to=reply_to,
        )

    # -- inbound envelope handling -----------------------------------------

    async def on_envelope(self, envelope) -> None:  # type: ignore[override]
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
        # From the Conductor: sector task directives.
        if sender == self.conductor_id and env_type == "request":
            await self._on_conductor_directive(envelope)
            return
        # From workers in our sector: results / failures.
        if sender.startswith(f"{WORKER_PREFIX}-{self.sector}-") or sender.startswith(
            f"{WORKER_PREFIX}_"
        ):
            await self._on_worker_message(sender, envelope)
            return
        # From peer sector managers: cross-sector chatter. Allowed.
        if sender.startswith(SECTOR_MANAGER_PREFIX):
            logger.debug(
                "sector-manager %s received cross-sector msg from %s",
                self.agent_id, sender,
            )
            return
        # Anything else: log and drop.
        logger.debug(
            "sector-manager %s dropped envelope from %s (type=%s)",
            self.agent_id, sender, env_type,
        )

    async def on_event(self, event_name: str, details: dict[str, Any]) -> None:  # type: ignore[override]
        if event_name not in SUBSCRIBED_EVENTS:
            return
        if event_name in {"agent_zombie", "auto_restart_triggered"}:
            await self._on_worker_zombie(
                details, auto_restart=event_name == "auto_restart_triggered"
            )
        elif event_name == "permission_denied":
            logger.warning(
                "sector-manager %s: permission_denied %s",
                self.agent_id, details,
            )

    # -- Conductor directive ----------------------------------------------

    async def _on_conductor_directive(self, envelope) -> None:
        """Handle an envelope from the Conductor."""
        try:
            data = envelope.payload.data  # type: ignore[attr-defined]
        except AttributeError:
            data = None
        if not isinstance(data, dict):
            return
        action = str(data.get("action", "")).strip()
        if action == ACTION_SECTOR_TASK:
            await self.handle_sector_task(data)
        else:
            logger.info(
                "sector-manager %s: unknown conductor action %r",
                self.agent_id, action,
            )

    async def handle_sector_task(self, payload: dict[str, Any]) -> SectorJob:
        """Plan, dispatch, and track a sector job.

        Public entry point used by the Conductor envelope handler
        and by tests.
        """
        workflow_id = str(payload.get("workflow_id") or "")
        objective_id = str(payload.get("objective_id") or uuid.uuid4())
        node_id = str(payload.get("node_id") or "")
        sector = str(payload.get("sector") or self.sector)
        description = str(payload.get("description") or "")
        goal = str(payload.get("goal") or "")
        primary = str(payload.get("primary_sector") or sector)
        job = SectorJob(
            job_id=node_id or str(uuid.uuid4()),
            workflow_id=workflow_id,
            objective_id=objective_id,
            sector=sector,
            description=description,
            goal=goal,
            primary_sector=primary,
        )
        async with self._jobs_lock:
            self._jobs[job.job_id] = job
        # Plan and dispatch.
        plan = self._default_plan(goal, description)
        for task_desc in plan:
            await self._dispatch_worker(job, task_desc)
        # If the plan produced no tasks (e.g. zero-length description),
        # we synthesize a single trivial task so the job can complete.
        if not job.tasks:
            await self._dispatch_worker(job, f"trivial:{sector}")
        # Best-effort: deliver any in-flight worker task envelopes and
        # collect results. In Phase 2 without a worker runtime, we
        # synthesize results so the workflow can finish end-to-end.
        await self._simulate_or_collect(job)
        return job

    # -- planning ---------------------------------------------------------

    def _default_plan(self, goal: str, description: str) -> list[str]:
        """Deterministic one-task plan. Phase 3+ will swap in an LLM planner."""
        # Trim and cap. The worker runtime will do the heavy lifting;
        # in Phase 2 we just need a well-shaped task descriptor.
        text = (description or goal or f"perform {self.sector} work").strip()
        if not text:
            text = f"perform {self.sector} work"
        return [text[:512]]

    # -- worker dispatch ---------------------------------------------------

    async def _dispatch_worker(
        self, job: SectorJob, description: str
    ) -> WorkerTask:
        self._worker_counter += 1
        worker_id = (
            f"{WORKER_PREFIX}-{self.sector}-{self._worker_counter:03d}"
        )
        task = WorkerTask(
            task_id=str(uuid.uuid4()),
            worker_id=worker_id,
            description=description,
        )
        job.tasks.append(task)
        # Send the task envelope to the worker. Phase 2 has no worker
        # runtime; if the worker is not online, the kernel will queue
        # the envelope (or drop it if the worker doesn't exist at all).
        # Either way, the SectorManager simulates completion below so
        # the workflow can finish end-to-end.
        envelope = self.build_request(
            worker_id,
            payload={
                "content_type": "data",
                "data": {
                    "action": "worker_task",
                    "task_id": task.task_id,
                    "job_id": job.job_id,
                    "workflow_id": job.workflow_id,
                    "sector": self.sector,
                    "description": description,
                    "goal": job.goal,
                },
            },
            receiver_role="executor",
            goal=f"worker:{self.sector}",
            phase="execution",
        )
        try:
            await self.send(envelope)
        except Exception as exc:  # noqa: BLE001
            task.status = "failed"
            task.error = f"send_failed: {exc}"
            task.finished_at = utc_now().isoformat()
        else:
            task.status = "running"
            task.started_at = utc_now().isoformat()
        return task

    async def _simulate_or_collect(self, job: SectorJob) -> None:
        """Phase 2 fallback: simulate worker results.

        When the worker runtime is in place (Phase 3+), this method
        is replaced by a real ``wait_for_results`` loop. For now, we
        give each running task a synthetic success so the workflow
        can complete and tests can assert end-to-end behaviour.
        """
        for task in job.tasks:
            if task.status == "running":
                task.status = "complete"
                task.finished_at = utc_now().isoformat()
                task.output = {
                    "synthetic": True,
                    "summary": f"Completed {self.sector} task: {task.description[:80]}",
                }
                job.artifacts.append(
                    {
                        "task_id": task.task_id,
                        "worker_id": task.worker_id,
                        "output": task.output,
                    }
                )
        # Emit completion.
        await self._emit_job_status(job)

    # -- worker inbound ---------------------------------------------------

    async def _on_worker_message(self, sender: str, envelope) -> None:
        try:
            data = envelope.payload.data  # type: ignore[attr-defined]
        except AttributeError:
            data = None
        if not isinstance(data, dict):
            return
        action = str(data.get("action", "")).strip()
        task_id = str(data.get("task_id") or "")
        job_id = str(data.get("job_id") or "")
        job = self._jobs.get(job_id)
        if job is None:
            return
        task = next((t for t in job.tasks if t.task_id == task_id), None)
        if task is None:
            return
        if action == "worker_result":
            task.status = "complete"
            task.finished_at = utc_now().isoformat()
            task.output = {
                "summary": data.get("summary"),
                "artifacts": data.get("artifacts", []),
                "raw": data.get("raw", {}),
            }
            job.artifacts.extend(task.output.get("artifacts") or [])
        elif action == "worker_failed":
            task.status = "failed"
            task.finished_at = utc_now().isoformat()
            task.error = str(data.get("error") or "unknown")
        else:
            logger.debug(
                "sector-manager %s: unknown worker action %r from %s",
                self.agent_id, action, sender,
            )
        await self._emit_job_status(job)

    # -- zombie / recovery -----------------------------------------------

    async def _on_worker_zombie(
        self, details: dict[str, Any], *, auto_restart: bool
    ) -> None:
        agent_id = str(details.get("agent_id", ""))
        if not agent_id:
            return
        for job in self._jobs.values():
            for task in job.tasks:
                if task.worker_id == agent_id and task.status == "running":
                    if auto_restart:
                        return
                    # Re-spawn by dispatching a fresh worker for the task.
                    task.status = "pending"
                    task.started_at = None
                    await self._dispatch_worker(job, task.description)
                    return

    # -- cross-sector messaging -------------------------------------------

    async def send_to_sector(
        self,
        peer_sector_manager_id: str,
        payload: dict[str, Any],
        *,
        reason: str = "cross-sector-query",
    ) -> None:
        """Send a cross-sector message and CC the Conductor.

        The contract has no CC field, so we send two envelopes: one
        to the peer, one to the Conductor as a notification. The peer
        envelope is the primary; the Conductor envelope is a
        fire-and-forget event.
        """
        if peer_sector_manager_id in FORBIDDEN_RECEIVERS:
            raise ValueError(
                f"sector manager cannot send to {peer_sector_manager_id!r}"
            )
        # Primary: to the peer.
        primary = self.build_request(
            peer_sector_manager_id,
            payload=payload,
            receiver_role="specialist",
            goal=reason,
            phase="execution",
        )
        await self.send(primary)
        # CC: a notification to the Conductor.
        cc = self.build_event(
            self.conductor_id,
            event_name="cross_sector_message",
            details={
                "from": self.agent_id,
                "to": peer_sector_manager_id,
                "reason": reason,
                "primary_envelope_id": str(primary.envelope_id),
            },
            receiver_role="orchestrator",
        )
        try:
            await self.send(cc)
        except Exception:  # noqa: BLE001
            logger.debug("CC to conductor failed (best-effort)")

    # -- emit sector completion to Conductor ------------------------------

    async def _emit_job_status(self, job: SectorJob) -> None:
        job.updated_at = utc_now().isoformat()
        if not job.all_done():
            return
        if job.all_complete():
            job.status = "complete"
            await self._send_to_conductor(
                ACTION_SECTOR_COMPLETE,
                {
                    "workflow_id": job.workflow_id,
                    "objective_id": job.objective_id,
                    "sector": job.sector,
                    "job_id": job.job_id,
                    "summary": self._summarise(job),
                    "artifacts": list(job.artifacts),
                },
            )
        else:
            job.status = "failed"
            await self._send_to_conductor(
                ACTION_SECTOR_FAILED,
                {
                    "workflow_id": job.workflow_id,
                    "objective_id": job.objective_id,
                    "sector": job.sector,
                    "job_id": job.job_id,
                    "error": job.first_error() or "unknown",
                    "summary": self._summarise(job),
                },
            )

    async def _send_to_conductor(
        self, action: str, details: dict[str, Any]
    ) -> None:
        envelope = self.build_request(
            self.conductor_id,
            payload={"content_type": "data", "data": {"action": action, **details}},
            receiver_role="orchestrator",
            goal=f"sector:{self.sector}:{action}",
            phase="execution",
        )
        try:
            await self.send(envelope)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "sector-manager %s: could not send %s to conductor: %s",
                self.agent_id, action, exc,
            )

    @staticmethod
    def _summarise(job: SectorJob) -> str:
        ok = sum(1 for t in job.tasks if t.status == "complete")
        bad = sum(1 for t in job.tasks if t.status == "failed")
        return (
            f"Sector `{job.sector}` finished: {ok} complete, "
            f"{bad} failed, total {len(job.tasks)}."
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_sector_manager_manifest(
    template: dict[str, Any],
    *,
    sector: str,
    agent_id: str | None = None,
) -> dict[str, Any]:
    """Stamp a per-sector identity onto a manifest template.

    Returns a deep copy of ``template`` with ``agent_id`` set to
    ``f"sector-manager-{sector}"`` (or a caller-supplied override)
    and ``intent`` / ``category`` adjusted to mention the sector.

    The Conductor calls this to materialise a manifest for each
    sector it wants to spawn. The actual agent process must be
    started by the supervisor; for now the manifest is just
    registered with the kernel.
    """
    import copy

    if not isinstance(template, dict):
        raise TypeError("template must be a dict")
    manifest = copy.deepcopy(template)
    manifest["agent_id"] = agent_id or f"{SECTOR_MANAGER_PREFIX}-{sector}"
    if "intent" in manifest:
        manifest["intent"] = (
            f"Manage the `{sector}` sector: delegate to workers, "
            f"aggregate results, report to conductor."
        )
    manifest["category"] = "custom"
    manifest["tags"] = list(manifest.get("tags", [])) + [f"sector:{sector}"]
    return manifest


__all__ = [
    "ACTION_SECTOR_COMPLETE",
    "ACTION_SECTOR_FAILED",
    "ACTION_SECTOR_TASK",
    "ACTION_WORKER_RESULT",
    "CONDUCTOR_AGENT_ID",
    "FORBIDDEN_RECEIVERS",
    "SECTOR_MANAGER_PREFIX",
    "SUBSCRIBED_EVENTS",
    "SectorJob",
    "SectorManager",
    "WORKER_PREFIX",
    "WorkerTask",
    "make_sector_manager_manifest",
]
