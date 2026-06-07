"""Read-only system introspection API.

The :class:`IntrospectionAPI` normalizes data from the kernel's
registry, the message bus, the persistent memory store, the loop
registry, and the on-disk harness workspaces into a single queryable
surface.  Every method is ``async`` and side-effect free.

Inspired by OpenClaw's channel adapters: the dashboard never talks
directly to a subsystem; it asks the introspection API and gets back
Pydantic models that match the dashboard's contract.

Data sources
------------
* **Kernel** — :class:`~kernel.registry.AgentRegistry` (agent rows),
  :class:`~kernel.bus.MessageBus` (live metrics), and
  :class:`~kernel.heartbeat.HeartbeatMonitor` (status).
* **Memory** — :class:`~memory.persistent.PersistentMemory` for
  cross-session memory rows.
* **Loops** — :class:`~loops.registry.LoopRegistry` for templates and
  performance stats.
* **Workspaces** — :class:`~harness.workspace.WorkspaceManager` and
  :class:`~harness.git_tracker.GitTracker` for file trees and git
  history.
* **Workflows** — derived from the persistent-memory ``workflow_id``
  column, the harness ``workspaces/`` directory, and the live bus.

The introspection layer is the **only** place in the dashboard that
imports kernel/memory/loops/harness types.  Everything else receives
Pydantic models.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

import aiosqlite

from .models import (
    AgentDetail,
    AgentEvent,
    AgentMetrics,
    AgentSummary,
    CommitInfo,
    CycleReport,
    FileContent,
    FileEntry,
    LeaderboardEntry,
    LogEntry,
    LoopPerformance,
    LoopTemplateSummary,
    MemoryItem,
    SystemMetrics,
    TrialRecord,
    WorkflowDetail,
    WorkflowStepStatus,
    WorkflowSummary,
    WorkspaceSummary,
)

if TYPE_CHECKING:  # pragma: no cover
    from kernel.bus import MessageBus
    from kernel.config import KernelSettings
    from kernel.heartbeat import HeartbeatMonitor
    from kernel.registry import AgentRegistry
    from loops.registry import LoopRegistry
    from loops.trial_store import TrialStore
    from loop_optimizer import LoopOptimizer
    from memory.persistent import PersistentMemory

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Severity classification
# ---------------------------------------------------------------------------


# Map (envelope_type, payload content_type) → severity bucket.  The
# values are stable strings; the dashboard uses them for color coding.
_SEVERITY_MAP: dict[tuple[str, str], str] = {
    ("error", ""): "error",
    ("error", "text"): "error",
    ("error", "data"): "error",
    ("error", "tool"): "error",
    ("error", "workflow"): "error",
    ("event", "data"): "info",
    ("event", "checkpoint"): "info",
    ("event", "workflow"): "info",
    ("event", ""): "info",
    ("intent", "workflow"): "warn",
    ("intent", "data"): "warn",
    ("intent", ""): "warn",
    ("heartbeat", ""): "debug",
    ("heartbeat", "data"): "debug",
    ("chunk", "text"): "info",
    ("request", "text"): "debug",
    ("request", "data"): "debug",
    ("request", "tool"): "info",
    ("response", "text"): "debug",
    ("response", "data"): "debug",
    ("response", "tool"): "info",
}


def _classify_severity(envelope_type: str, content_type: str) -> str:
    """Return a stable severity bucket for color coding."""
    return _SEVERITY_MAP.get(
        (envelope_type, content_type or ""), _SEVERITY_MAP.get((envelope_type, ""), "info")
    )


# ---------------------------------------------------------------------------
# Preview truncation
# ---------------------------------------------------------------------------


_PREVIEW_BYTES: int = 240
"""How many bytes of payload to surface in the log preview."""


def _truncate(text: str, limit: int = _PREVIEW_BYTES) -> str:
    """Truncate ``text`` with an ellipsis if it exceeds ``limit`` chars."""
    if text is None:
        return ""
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


# ---------------------------------------------------------------------------
# IntrospectionAPI
# ---------------------------------------------------------------------------


class IntrospectionAPI:
    """Read-only query layer over every Phase 1-6 subsystem.

    The class is dependency-injected: every collaborator is supplied
    via the constructor.  Tests can swap in stub registries/buses to
    exercise the surface without standing up the whole kernel.
    """

    def __init__(
        self,
        *,
        registry: AgentRegistry,
        bus: MessageBus,
        settings: KernelSettings,
        heartbeat: HeartbeatMonitor | None = None,
        persistent_memory: PersistentMemory | None = None,
        loop_registry: LoopRegistry | None = None,
        workspaces_dir: Path | None = None,
        trial_store: "TrialStore | None" = None,
        loop_optimizer: "LoopOptimizer | None" = None,
    ) -> None:
        """Store collaborators.

        Args:
            registry: The kernel's :class:`AgentRegistry`.
            bus: The kernel's :class:`MessageBus`.
            settings: The kernel's :class:`KernelSettings`.
            heartbeat: Optional heartbeat monitor (for fallback status
                when ``agents.status`` is stale).
            persistent_memory: Optional Phase 6 persistent memory
                store.  Required for ``get_agent_memory``.
            loop_registry: Optional Phase 4 loop registry.  Required
                for ``get_loop_templates``.
            workspaces_dir: Path to the harness ``workspaces`` root.
                Defaults to ``<settings.paths.data_dir>/../workspaces``
                (the canonical Phase 5 layout).
            trial_store: Optional Phase 10 trial store.  Required for
                ``get_trial_leaderboard`` / ``get_loop_trials``.
            loop_optimizer: Optional Phase 10 loop optimizer.  Required
                for ``run_optimization``.
        """
        self._registry = registry
        self._bus = bus
        self._settings = settings
        self._heartbeat = heartbeat
        self._memory = persistent_memory
        self._loops = loop_registry
        self._trial_store = trial_store
        self._loop_optimizer = loop_optimizer
        if workspaces_dir is None:
            # The harness keeps its workspaces under <project>/workspaces.
            # ``settings.paths.data_dir`` is data/, so the parent is the
            # project root.
            self._workspaces_dir: Path = (settings.paths.data_dir.parent / "workspaces").resolve()
        else:
            self._workspaces_dir = Path(workspaces_dir).resolve()

    # =====================================================================
    # AGENTS
    # =====================================================================

    async def get_agents(
        self,
        status: str | None = None,
        role: str | None = None,
        category: str | None = None,
        tags: list[str] | None = None,
    ) -> list[AgentSummary]:
        """Return agent summaries, optionally filtered.

        ``status`` filters by manifest status (``ready``/``busy``/...).
        ``role`` and ``category`` filter by manifest fields.
        ``tags`` requires ALL tags to be present in the manifest's
        ``tags`` list.
        """
        manifests = await self._registry.list()
        rows = await self._registry.list_status()
        by_id = {row["agent_id"]: row for row in rows}
        now = time.time()

        out: list[AgentSummary] = []
        for manifest in manifests:
            row = by_id.get(manifest.agent_id, {})
            if status is not None and row.get("status") != status:
                continue
            if role is not None and manifest.role != role:
                continue
            if category is not None and manifest.category != category:
                continue
            if tags is not None:
                if not all(t in (manifest.tags or []) for t in tags):
                    continue

            last_hb = row.get("last_heartbeat")
            hb_age = self._heartbeat_age_seconds(last_hb, now)
            out.append(
                AgentSummary(
                    agent_id=manifest.agent_id,
                    human_readable_name=(
                        manifest.human_readable_name or manifest.agent_id
                    ),
                    role=manifest.role,
                    category=str(manifest.category or "custom"),
                    status=str(row.get("status") or manifest.status or "initializing"),
                    model_tier=str(
                        manifest.model_tier.tier if manifest.model_tier else "standard"
                    ),
                    current_task=None,
                    heartbeat_age_seconds=hb_age,
                    connected_ws=bool(row.get("connected_ws", False)),
                    registered_at=self._parse_dt(row.get("registered_at"))
                    or manifest.registration_time,
                    last_heartbeat=self._parse_dt(last_hb) or manifest.last_heartbeat,
                    instance_id=row.get("instance_id"),
                    tags=list(manifest.tags or []),
                )
            )
        out.sort(key=lambda a: a.agent_id)
        return out

    async def get_agent_detail(self, agent_id: str) -> AgentDetail:
        """Return the full agent card: manifest + status + recent activity.

        :raises AgentNotFound: when the agent is not registered.
        """
        try:
            manifest = await self._registry.get(agent_id)
        except Exception:  # AgentNotFound
            raise

        status_row = await self._registry.get_status(agent_id)
        now = time.time()
        hb_age = self._heartbeat_age_seconds(status_row.get("last_heartbeat"), now)

        recent_memory: list[MemoryItem] = []
        if self._memory is not None:
            try:
                rows = await self._memory.retrieve_recent(agent_id, n=10)
                recent_memory = [self._memory_to_model(row, idx) for idx, row in enumerate(rows)]
            except Exception:
                logger.debug("memory lookup failed for %s", agent_id, exc_info=True)

        recent_errors: list[LogEntry] = []
        try:
            recent_errors = await self._get_recent_errors(agent_id, limit=10)
        except Exception:
            logger.debug("error log lookup failed for %s", agent_id, exc_info=True)

        return AgentDetail(
            agent_id=manifest.agent_id,
            manifest=manifest.model_dump(mode="json"),
            status=str(status_row.get("status") or manifest.status or "initializing"),
            last_heartbeat=self._parse_dt(status_row.get("last_heartbeat")),
            registered_at=self._parse_dt(status_row.get("registered_at"))
            or manifest.registration_time,
            instance_id=status_row.get("instance_id"),
            connected_ws=bool(status_row.get("connected_ws", False)),
            heartbeat_age_seconds=hb_age,
            current_task=None,
            recent_memory=recent_memory,
            recent_errors=recent_errors,
            pending_queue_size=self._bus.queue_size(agent_id),
        )

    async def get_agent_history(
        self, agent_id: str, limit: int = 50
    ) -> list[AgentEvent]:
        """Return the audit-log rows for ``agent_id``, newest first.

        The audit log is the kernel's append-only security record.  We
        surface it as :class:`AgentEvent` rows; envelope-driven events
        are not in the audit log (they flow through the bus, not the
        registry).
        """
        rows = await self._registry.audit_log(agent_id=agent_id, limit=limit)
        events: list[AgentEvent] = []
        for row in rows:
            events.append(
                AgentEvent(
                    event_id=str(row.get("id")),
                    timestamp=self._parse_dt(row.get("timestamp")) or datetime.now(timezone.utc),
                    source="audit",
                    envelope_type=None,
                    action=str(row.get("action") or ""),
                    result=str(row.get("result") or ""),
                    sender=agent_id,
                    receiver=None,
                    summary=self._audit_summary(row),
                )
            )
        return events

    # =====================================================================
    # WORKFLOWS
    # =====================================================================

    async def get_workflows(
        self,
        status: str | None = None,
        owner: str | None = None,
    ) -> list[WorkflowSummary]:
        """Return workflow summaries derived from persistent memory + workspaces.

        The kernel does not currently persist workflow state in its own
        table (Phase 2 emits inline workflow JSON inside envelopes);
        we therefore enumerate workflows by:

        1. walking ``workspaces/`` on disk (one workspace per
           workflow, by ``WorkspaceManager`` convention); and
        2. pulling every distinct ``workflow_id`` from the
           persistent-memory table.

        Both sources are unioned and deduplicated.  ``status`` and
        ``owner`` are best-effort filters (we use the workspace's
        mtime for a status proxy when the kernel hasn't emitted a
        lifecycle event for it).
        """
        by_id: dict[str, dict[str, Any]] = {}

        # 1) From workspaces.
        if self._workspaces_dir.exists():
            for entry in sorted(self._workspaces_dir.iterdir()):
                if not entry.is_dir():
                    continue
                wf_id = entry.name
                stat = entry.stat()
                created = datetime.fromtimestamp(stat.st_ctime, tz=timezone.utc)
                updated = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
                by_id[wf_id] = {
                    "workflow_id": wf_id,
                    "name": wf_id,
                    "description": "active harness workspace",
                    "status": "running",
                    "owner_agent": "main-agent",
                    "step_count": 0,
                    "completed_steps": 0,
                    "created_at": created,
                    "updated_at": updated,
                }

        # 2) From persistent memory.
        if self._memory is not None:
            try:
                async with self._memory._conn() as db:  # type: ignore[attr-defined]
                    cur = await db.execute(
                        """
                        SELECT workflow_id, MIN(timestamp) AS first_seen,
                               MAX(timestamp) AS last_seen,
                               COUNT(DISTINCT step_id) AS step_count
                        FROM memories
                        WHERE workflow_id IS NOT NULL
                        GROUP BY workflow_id
                        """
                    )
                    rows = await cur.fetchall()
                for row in rows:
                    wf_id = row["workflow_id"]
                    if wf_id is None:
                        continue
                    existing = by_id.get(wf_id)
                    first_seen = self._parse_dt(row["first_seen"]) or datetime.now(timezone.utc)
                    last_seen = self._parse_dt(row["last_seen"]) or datetime.now(timezone.utc)
                    info = {
                        "workflow_id": wf_id,
                        "name": wf_id,
                        "description": "tracked in persistent memory",
                        "status": "completed" if last_seen < datetime.now(timezone.utc) - timedelta(hours=1) else "running",
                        "owner_agent": "main-agent",
                        "step_count": int(row["step_count"] or 0),
                        "completed_steps": int(row["step_count"] or 0),
                        "created_at": first_seen,
                        "updated_at": last_seen,
                    }
                    if existing is None:
                        by_id[wf_id] = info
                    else:
                        # Merge: prefer the workspace as the canonical
                        # source for paths, but enrich with memory
                        # counts and updated_at.
                        existing["step_count"] = max(
                            int(existing.get("step_count", 0) or 0),
                            int(info["step_count"] or 0),
                        )
                        if last_seen > existing.get("updated_at", last_seen):
                            existing["updated_at"] = last_seen
            except Exception:
                logger.debug("workflow lookup from memory failed", exc_info=True)

        results: list[WorkflowSummary] = []
        for info in by_id.values():
            if status is not None and info.get("status") != status:
                continue
            if owner is not None and info.get("owner_agent") != owner:
                continue
            results.append(WorkflowSummary(**info))
        results.sort(key=lambda w: w.updated_at, reverse=True)
        return results

    async def get_workflow_detail(self, workflow_id: str) -> WorkflowDetail:
        """Return the full workflow detail.

        We synthesise the detail from:

        * the workspace on disk (filesystem state);
        * the persistent-memory rows attributed to the workflow
          (step status, timeline);
        * the git history of the workspace (commit timeline).

        :raises FileNotFoundError: when the workflow has no workspace
            and no memory rows.
        """
        steps: list[WorkflowStepStatus] = []
        timeline: list[AgentEvent] = []
        step_outputs: dict[str, Any] = {}
        checkpoint: dict[str, Any] = {}

        if self._memory is not None:
            try:
                items = await self._memory.retrieve_by_workflow(workflow_id)
                by_step: dict[str, list[Any]] = {}
                for item in items:
                    sid = item.step_id or "(no-step)"
                    by_step.setdefault(sid, []).append(item)
                for sid, group in sorted(by_step.items()):
                    latest = group[-1]
                    steps.append(
                        WorkflowStepStatus(
                            step_id=sid,
                            name=str(
                                (latest.content or {}).get("name", sid)
                                if isinstance(latest.content, dict)
                                else sid
                            ),
                            agent_id=str(latest.agent_id),
                            status="completed" if latest.type != "error" else "failed",
                            started_at=group[0].timestamp,
                            finished_at=group[-1].timestamp,
                            error=str(latest.content) if latest.type == "error" else None,
                            attempts=len(group),
                            output_preview=self._content_preview(latest.content),
                        )
                    )
                    step_outputs[sid] = latest.content
                    timeline.append(
                        AgentEvent(
                            event_id=f"mem-{sid}-{int(latest.timestamp.timestamp())}",
                            timestamp=latest.timestamp,
                            source="envelope",
                            envelope_type=None,
                            action=str(latest.type),
                            result="ok" if latest.type != "error" else "error",
                            sender=latest.agent_id,
                            receiver=None,
                            summary=self._content_preview(latest.content, 120),
                        )
                    )
                # Checkpoint is whatever the last step reported.
                if items:
                    last = items[-1]
                    checkpoint = {
                        "last_step_id": last.step_id,
                        "timestamp": last.timestamp.isoformat().replace("+00:00", "Z"),
                    }
            except Exception:
                logger.debug("workflow detail from memory failed", exc_info=True)

        # Workspace-derived metadata.
        workspace = self._workspaces_dir / workflow_id
        created_at = datetime.now(timezone.utc)
        updated_at = created_at
        if workspace.exists():
            stat = workspace.stat()
            created_at = datetime.fromtimestamp(stat.st_ctime, tz=timezone.utc)
            updated_at = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)

        if not steps and not workspace.exists():
            raise FileNotFoundError(f"workflow {workflow_id!r} not found")

        return WorkflowDetail(
            workflow_id=workflow_id,
            name=workflow_id,
            description=("Active harness workspace" if workspace.exists() else None),
            status="completed" if updated_at < datetime.now(timezone.utc) - timedelta(hours=1) else "running",
            owner_agent="main-agent",
            version="1.0.0",
            created_at=created_at,
            updated_at=updated_at,
            steps=steps,
            checkpoint=checkpoint,
            step_outputs=step_outputs,
            error_handling={},
            timeline=timeline,
        )

    async def get_workflow_logs(
        self, workflow_id: str, limit: int = 100
    ) -> list[LogEntry]:
        """Return the audit-log rows attributed to ``workflow_id``.

        The audit log is keyed on ``agent_id`` so we synthesise a
        workflow-level view by joining on any memory row attributed to
        the workflow.  This is best-effort: when the workflow has
        never written to memory, the result is empty.
        """
        if self._memory is None:
            return []
        try:
            items = await self._memory.retrieve_by_workflow(workflow_id)
        except Exception:
            return []

        agent_ids = sorted({it.agent_id for it in items})
        out: list[LogEntry] = []
        now = datetime.now(timezone.utc)
        for agent_id in agent_ids:
            try:
                rows = await self._registry.audit_log(agent_id=agent_id, limit=limit)
            except Exception:
                continue
            for row in rows:
                out.append(
                    LogEntry(
                        envelope_id=f"audit-{row.get('id')}",
                        timestamp=self._parse_dt(row.get("timestamp")) or now,
                        envelope_type="event",
                        sender=agent_id,
                        receiver=workflow_id,
                        payload_preview=self._audit_summary(row, 240),
                        priority=5,
                        severity=_classify_severity("event", "data"),
                        result=str(row.get("result", "ok") or "ok"),
                        workflow_id=workflow_id,
                        content_type="data",
                    )
                )
        out.sort(key=lambda e: e.timestamp, reverse=True)
        return out[:limit]

    # =====================================================================
    # LOGS
    # =====================================================================

    async def get_logs(
        self,
        agent_id: str | None = None,
        workflow_id: str | None = None,
        envelope_type: str | None = None,
        severity: str | None = None,
        start_time: datetime | None = None,
        end_time: datetime | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[LogEntry]:
        """Return envelope + audit rows for the log stream view.

        The kernel registry's ``audit_log`` table is the only durable
        store we have.  We query it directly and apply all the
        optional filters in SQL.  This is intentionally a thin wrapper
        over :meth:`AgentRegistry.audit_log` so the kernel stays the
        single source of truth.
        """
        db_path = self._settings.db_path
        if not db_path.exists():
            return []

        sql = [
            "SELECT id, envelope_id, agent_id, action, result, details, timestamp",
            "FROM audit_log",
            "WHERE 1=1",
        ]
        params: list[Any] = []

        if agent_id is not None:
            sql.append("AND agent_id = ?")
            params.append(agent_id)
        if start_time is not None:
            sql.append("AND timestamp >= ?")
            params.append(self._format_dt(start_time))
        if end_time is not None:
            sql.append("AND timestamp <= ?")
            params.append(self._format_dt(end_time))
        if envelope_type is not None or workflow_id is not None or severity is not None:
            # The audit log doesn't carry envelope_type / workflow_id
            # columns; fall through to in-process filtering below.
            pass

        sql.append("ORDER BY id DESC LIMIT ? OFFSET ?")
        params.extend([max(1, limit), max(0, offset)])
        query = "\n".join(sql)

        out: list[LogEntry] = []
        try:
            async with aiosqlite.connect(str(db_path)) as db:
                db.row_factory = aiosqlite.Row
                async with db.execute(query, tuple(params)) as cur:
                    rows = await cur.fetchall()
        except Exception:
            logger.exception("log query failed")
            return []

        for row in rows:
            details_raw = row["details"] or "{}"
            try:
                details = json.loads(details_raw) if isinstance(details_raw, str) else {}
            except json.JSONDecodeError:
                details = {}
            et = self._extract_envelope_type(details)
            wf = self._extract_workflow_id(details)
            content_type = str(details.get("content_type", "") or "")
            sev = _classify_severity(et, content_type)
            if envelope_type is not None and et != envelope_type:
                continue
            if workflow_id is not None and wf != workflow_id:
                continue
            if severity is not None and sev != severity:
                continue
            sender = str(row["agent_id"] or "kernel")
            preview = self._payload_preview(details)
            ts = self._parse_dt(row["timestamp"]) or datetime.now(timezone.utc)
            out.append(
                LogEntry(
                    envelope_id=str(row["envelope_id"] or f"audit-{row['id']}"),
                    timestamp=ts,
                    envelope_type=et,
                    sender=sender,
                    receiver=str(details.get("receiver", "") or "*"),
                    payload_preview=preview,
                    priority=int(details.get("priority", 5) or 5),
                    severity=sev,
                    result=str(row["result"] or "ok"),
                    workflow_id=wf,
                    content_type=content_type or None,
                    tags=list(details.get("tags", []) or []),
                )
            )
        return out

    # =====================================================================
    # WORKSPACES
    # =====================================================================

    async def get_workspaces(self) -> list[WorkspaceSummary]:
        """Enumerate the harness workspaces on disk."""
        if not self._workspaces_dir.exists():
            return []
        out: list[WorkspaceSummary] = []
        for entry in sorted(self._workspaces_dir.iterdir()):
            if not entry.is_dir():
                continue
            try:
                file_count, total_size = self._walk_stats(entry)
                stat = entry.stat()
                out.append(
                    WorkspaceSummary(
                        workflow_id=entry.name,
                        root_path=str(entry),
                        src_dir=str(entry / "src"),
                        output_dir=str(entry / "output"),
                        logs_dir=str(entry / "logs"),
                        created_at=datetime.fromtimestamp(stat.st_ctime, tz=timezone.utc),
                        last_accessed=datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc),
                        git_initialized=(entry / ".git").exists(),
                        file_count=file_count,
                        total_size_bytes=total_size,
                    )
                )
            except OSError:
                continue
        out.sort(key=lambda w: w.last_accessed, reverse=True)
        return out

    async def get_workspace_files(
        self, workflow_id: str, path: str = "/"
    ) -> list[FileEntry]:
        """Return the file tree of ``workflow_id``'s workspace rooted at ``path``."""
        workspace = self._workspaces_dir / workflow_id
        if not workspace.exists():
            return []
        try:
            target = (workspace / path.lstrip("/")).resolve()
            target.relative_to(workspace.resolve())  # path-traversal guard
        except (ValueError, OSError):
            return []
        if not target.exists() or not target.is_dir():
            return []
        out: list[FileEntry] = []
        for child in sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name)):
            try:
                stat = child.stat()
            except OSError:
                continue
            rel = child.relative_to(workspace)
            out.append(
                FileEntry(
                    name=child.name,
                    path="/" + str(rel).replace("\\", "/"),
                    is_dir=child.is_dir(),
                    size=stat.st_size,
                    modified_at=datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc),
                )
            )
        return out

    async def get_workspace_file(self, workflow_id: str, path: str) -> FileContent:
        """Return the contents of a single workspace file.

        :raises FileNotFoundError: when the file does not exist.
        """
        workspace = self._workspaces_dir / workflow_id
        if not workspace.exists():
            raise FileNotFoundError(f"workspace {workflow_id!r} not found")
        candidate = (workspace / path.lstrip("/")).resolve()
        try:
            candidate.relative_to(workspace.resolve())
        except ValueError as exc:
            raise FileNotFoundError(f"path {path!r} escapes workspace") from exc
        if not candidate.exists() or not candidate.is_file():
            raise FileNotFoundError(f"file {path!r} not found")
        stat = candidate.stat()
        if stat.st_size > 5_000_000:
            # Hard cap to avoid blowing up the WebSocket frame.
            return FileContent(
                workflow_id=workflow_id,
                path=path,
                content="[file too large to inline — use git history]",
                size=stat.st_size,
                modified_at=datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc),
            )
        try:
            content = candidate.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            content = candidate.read_text(encoding="utf-8", errors="replace")
            return FileContent(
                workflow_id=workflow_id,
                path=path,
                content=content,
                size=stat.st_size,
                encoding="utf-8-replace",
                modified_at=datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc),
            )
        return FileContent(
            workflow_id=workflow_id,
            path=path,
            content=content,
            size=stat.st_size,
            modified_at=datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc),
        )

    async def get_workspace_diff(self, workflow_id: str, commit_hash: str) -> str:
        """Return the unified diff for ``commit_hash`` in the workspace's git history.

        :raises ValueError: when the commit is unknown.
        """
        workspace = self._workspaces_dir / workflow_id
        if not workspace.exists():
            raise FileNotFoundError(f"workspace {workflow_id!r} not found")
        if not (workspace / ".git").exists():
            return ""
        # Defer to GitTracker; import lazily to avoid pulling the
        # harness module when the dashboard runs without one.
        from harness.git_tracker import GitTracker
        from harness.workspace import Workspace

        ws = Workspace(
            workflow_id=workflow_id,
            root=workspace,
            src_dir=workspace / "src",
            output_dir=workspace / "output",
            logs_dir=workspace / "logs",
            temp_dir=workspace / "temp",
            created_at=datetime.now(timezone.utc),
            last_accessed=datetime.now(timezone.utc),
            git_initialized=True,
        )
        return GitTracker().get_diff(ws, commit_hash)

    async def get_workspace_history(self, workflow_id: str) -> list[CommitInfo]:
        """Return the commit graph for ``workflow_id``'s workspace."""
        workspace = self._workspaces_dir / workflow_id
        if not workspace.exists() or not (workspace / ".git").exists():
            return []
        from harness.git_tracker import GitTracker
        from harness.workspace import Workspace

        ws = Workspace(
            workflow_id=workflow_id,
            root=workspace,
            src_dir=workspace / "src",
            output_dir=workspace / "output",
            logs_dir=workspace / "logs",
            temp_dir=workspace / "temp",
            created_at=datetime.now(timezone.utc),
            last_accessed=datetime.now(timezone.utc),
            git_initialized=True,
        )
        commits = GitTracker().get_history(ws)
        return [
            CommitInfo(
                hash=c.hash,
                agent_id=c.agent_id,
                message=c.message,
                timestamp=c.timestamp,
                files_changed=list(c.files_changed),
                insertions=int(c.insertions),
                deletions=int(c.deletions),
            )
            for c in commits
        ]

    # =====================================================================
    # LOOPS
    # =====================================================================

    async def get_loop_templates(
        self,
        task_type: str | None = None,
        min_success_rate: float = 0.0,
    ) -> list[LoopTemplateSummary]:
        """Return loop templates, sorted by success rate desc."""
        if self._loops is None:
            return []
        rows = await self._loops.alist_templates(
            task_type=task_type, min_success_rate=min_success_rate
        )
        out: list[LoopTemplateSummary] = []
        for row in rows:
            out.append(
                LoopTemplateSummary(
                    id=str(row.get("id")),
                    name=str(row.get("name") or row.get("id")),
                    description=row.get("description"),
                    task_type=row.get("task_type"),
                    success_rate=float(row.get("success_rate") or 0.0),
                    avg_score=float(row.get("avg_score") or 0.0),
                    avg_cost_usd=float(row.get("avg_cost_usd") or 0.0),
                    avg_latency_ms=float(row.get("avg_latency_ms") or 0.0),
                    usage_count=int(row.get("usage_count") or 0),
                    is_premade=bool(row.get("is_premade") or False),
                )
            )
        out.sort(key=lambda t: (t.success_rate, t.avg_score), reverse=True)
        return out

    async def get_loop_performance(self, template_id: str) -> LoopPerformance:
        """Return a per-template performance breakdown."""
        if self._loops is None:
            raise FileNotFoundError("loop registry not configured")
        stats = await asyncio.to_thread(self._loops.get_stats, template_id)
        if stats is None:
            raise FileNotFoundError(f"loop template {template_id!r} not found")
        recs = await self._loops.aget_recommendation(task_type=stats.success_rate.__class__.__name__, limit=3)
        # We don't have historical usage timeseries in the schema; the
        # aggregator layer will fill that in once the loop optimizer
        # lands.  Return an empty list for now.
        return LoopPerformance(
            template_id=template_id,
            name=template_id,
            success_rate=stats.success_rate,
            avg_score=stats.avg_score,
            avg_cost_usd=stats.avg_cost_usd,
            avg_latency_ms=stats.avg_latency_ms,
            usage_count=stats.usage_count,
            usage_over_time=[],
            recommendations=[{"id": r.get("id"), "score": r.get("recommendation_score")} for r in recs],
        )

    # =====================================================================
    # LOOPS — Phase 10 trial/error cycle
    # =====================================================================

    async def get_trial_leaderboard(
        self,
        task_type: str | None = None,
        sort_by: str = "score",
        min_trials: int = 3,
    ) -> list[LeaderboardEntry]:
        """Return the Phase 10 trial/error leaderboard.

        Args:
            task_type: Filter to a single task type.  ``None`` returns
                the global leaderboard.
            sort_by: One of ``"score"``, ``"cost"``, ``"speed"``,
                ``"trials"``.
            min_trials: Drop entries with fewer trials.  Defaults to
                3 (single-trial results are noise).

        Returns:
            A list of :class:`LeaderboardEntry`, sorted as requested.
        """
        if self._trial_store is None:
            return []
        rows = await self._trial_store.aget_leaderboard(
            task_type=task_type,
            sort_by=sort_by,  # type: ignore[arg-type]
            min_trials=min_trials,
        )
        return [
            LeaderboardEntry(
                loop_id=e.loop_id,
                task_type=e.task_type,
                avg_score=float(e.avg_score),
                avg_quality=float(e.avg_quality),
                avg_cost_usd=float(e.avg_cost_usd),
                avg_latency_ms=float(e.avg_latency_ms),
                trial_count=int(e.trial_count),
                last_trial=e.last_trial,
                best_variant=dict(e.best_variant or {}),
            )
            for e in rows
        ]

    async def get_loop_trials(
        self,
        loop_id: str | None = None,
        task_type: str | None = None,
        limit: int = 50,
    ) -> list[TrialRecord]:
        """Return the immutable trial records matching the filters.

        Args:
            loop_id: Optional loop-graph id filter.
            task_type: Optional task-type filter.
            limit: Maximum number of trials to return (newest first).

        Returns:
            A list of :class:`TrialRecord` rows.
        """
        if self._trial_store is None:
            return []
        trials = await self._trial_store.aget_trials(
            loop_id=loop_id, task_type=task_type, limit=limit
        )
        return [
            TrialRecord(
                trial_id=t.trial_id,
                loop_id=t.loop_id,
                task_type=t.task_type,
                loop_graph=dict(t.loop_graph or {}),
                score=dict(t.score.to_dict() if t.score else {}),
                result={
                    "output": t.result.output,
                    "confidence": t.result.confidence,
                    "tokens_used": t.result.tokens_used,
                    "cost_usd": t.result.cost_usd,
                    "latency_ms": t.result.latency_ms,
                    "iterations": t.result.iterations,
                    "intermediate_outputs": list(t.result.intermediate_outputs or []),
                },
                timestamp=t.timestamp,
                task_preview=t.task_preview,
                output_preview=t.output_preview,
            )
            for t in trials
        ]

    async def run_optimization(
        self,
        task_type: str,
        task_sample: str = "",
        n_trials: int = 3,
        base_loop: str = "reflection",
        include_builtins: bool = True,
    ) -> CycleReport:
        """Run one trial/error cycle and return the report.

        :raises RuntimeError: when no :class:`LoopOptimizer` is wired
            into the introspection layer.
        """
        if self._loop_optimizer is None:
            raise RuntimeError("loop optimizer is not configured")
        report = await self._loop_optimizer.run_optimization_cycle(
            task_type=task_type,
            task_sample=task_sample,
            n_trials=n_trials,
            base_loop=base_loop,
            include_builtins=include_builtins,
        )
        return CycleReport(
            cycle_id=report.cycle_id,
            task_type=report.task_type,
            base_loop=report.base_loop,
            trial_count=len(report.trials),
            best_loop_id=report.best_loop_id,
            best_score=float(report.best_score),
            trials=[
                TrialRecord(
                    trial_id=t.trial_id,
                    loop_id=t.loop_id,
                    task_type=t.task_type,
                    loop_graph=dict(t.loop_graph or {}),
                    score=dict(t.score.to_dict() if t.score else {}),
                    result={
                        "output": t.result.output,
                        "confidence": t.result.confidence,
                        "tokens_used": t.result.tokens_used,
                        "cost_usd": t.result.cost_usd,
                        "latency_ms": t.result.latency_ms,
                        "iterations": t.result.iterations,
                        "intermediate_outputs": list(
                            t.result.intermediate_outputs or []
                        ),
                    },
                    timestamp=t.timestamp,
                    task_preview=t.task_preview,
                    output_preview=t.output_preview,
                )
                for t in report.trials
            ],
        )

    # =====================================================================
    # MEMORY
    # =====================================================================

    async def get_agent_memory(
        self,
        agent_id: str,
        type: str | None = None,
        workflow_id: str | None = None,
        query: str | None = None,
        limit: int = 20,
    ) -> list[MemoryItem]:
        """Return memory rows for ``agent_id``.

        When ``query`` is set, runs an FTS5 search via the persistent
        store.  Otherwise returns the most recent rows.  The two
        filters (``type`` and ``workflow_id``) compose with both modes.
        """
        if self._memory is None:
            return []
        items: list[Any] = []
        if query is not None and query.strip():
            try:
                items = await self._memory.retrieve_relevant(
                    agent_id=agent_id, query=query, threshold=0.0, n=limit
                )
            except Exception:
                logger.debug("FTS5 search failed for %s", agent_id, exc_info=True)
                items = []
        else:
            try:
                items = await self._memory.retrieve_recent(agent_id, n=limit, type=type)  # type: ignore[arg-type]
            except Exception:
                logger.debug("recent memory lookup failed for %s", agent_id, exc_info=True)
                items = []

        out: list[MemoryItem] = []
        for idx, item in enumerate(items):
            if type is not None and item.type != type:
                continue
            if workflow_id is not None and item.workflow_id != workflow_id:
                continue
            out.append(self._memory_to_model(item, idx))
        return out[:limit]

    # =====================================================================
    # METRICS
    # =====================================================================

    async def get_system_metrics(self) -> SystemMetrics:
        """Return a swarm-level metrics snapshot.

        Source: registry status rows + bus counters.  ``total_cost_today_usd``
        is approximated by counting ``cost_usd`` mentions in the audit log
        for the last 24 hours; the loop optimizer will track real costs
        in Phase 10.
        """
        status_rows = await self._registry.list_status()
        status_counts: dict[str, int] = {}
        for row in status_rows:
            status_counts[row["status"]] = status_counts.get(row["status"], 0) + 1

        manifests = await self._registry.list()
        active = sum(
            1 for m in manifests if (m.status or "initializing") in {"ready", "busy"}
        )
        zombies = status_counts.get("zombie", 0)
        busy = status_counts.get("busy", 0)
        idle = status_counts.get("idle", 0)

        workflows = await self.get_workflows()
        running_wf = sum(1 for w in workflows if w.status == "running")
        completed_wf = sum(1 for w in workflows if w.status == "completed")
        failed_wf = sum(1 for w in workflows if w.status == "failed")

        bus_metrics = self._bus.metrics
        rate = self._compute_message_rate(bus_metrics)
        avg_latency = await self._avg_loop_latency()
        cost_today = await self._cost_today()

        started = bus_metrics.started_at
        if started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)
        uptime = (datetime.now(timezone.utc) - started).total_seconds()

        return SystemMetrics(
            total_agents=len(manifests),
            active_agents=active,
            zombie_agents=zombies,
            busy_agents=busy,
            idle_agents=idle,
            total_workflows=len(workflows),
            running_workflows=running_wf,
            completed_workflows=completed_wf,
            failed_workflows=failed_wf,
            messages_per_minute=rate,
            avg_loop_latency_ms=avg_latency,
            total_cost_today_usd=cost_today,
            uptime_seconds=uptime,
            queue_total=self._bus.total_queued(),
            started_at=started,
        )

    async def get_agent_metrics(self, agent_id: str) -> AgentMetrics:
        """Return a per-agent performance breakdown."""
        # Audit log is the only source we have for per-agent counters.
        rows = await self._registry.audit_log(agent_id=agent_id, limit=1000)
        tasks_total = len(rows)
        tasks_failed = sum(1 for r in rows if r.get("result") == "error")
        # avg_confidence: not in the audit schema; approximate with
        # 1 - error_rate so a perfectly-healthy agent scores 1.0.
        avg_conf = 1.0 - (tasks_failed / max(1, tasks_total))

        memory_count = 0
        if self._memory is not None:
            try:
                memory_count = await self._memory.count(agent_id=agent_id)
            except Exception:
                memory_count = 0

        status_row = await self._registry.get_status(agent_id)
        started_at = self._parse_dt(status_row.get("registered_at")) or datetime.now(timezone.utc)
        uptime = (datetime.now(timezone.utc) - started_at).total_seconds()

        return AgentMetrics(
            agent_id=agent_id,
            tasks_completed=tasks_total - tasks_failed,
            tasks_failed=tasks_failed,
            avg_confidence=round(avg_conf, 3),
            total_cost_usd=0.0,  # Phase 10 will wire this in
            uptime_seconds=max(0.0, uptime),
            memory_count=memory_count,
            queue_size=self._bus.queue_size(agent_id),
        )

    # =====================================================================
    # helpers
    # =====================================================================

    @staticmethod
    def _parse_dt(value: Any) -> datetime | None:
        """Parse an ISO 8601 string or pass through a :class:`datetime`."""
        if value is None:
            return None
        if isinstance(value, datetime):
            return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        if isinstance(value, str):
            try:
                dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
                return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
            except ValueError:
                return None
        return None

    @staticmethod
    def _format_dt(value: datetime) -> str:
        """Format a :class:`datetime` for SQLite text comparison."""
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

    @staticmethod
    def _heartbeat_age_seconds(last_hb: Any, now_monotonic: float) -> int:
        """Best-effort heartbeat age in seconds.

        ``last_hb`` may be an ISO string, a :class:`datetime`, or
        ``None``.  ``now_monotonic`` is the caller's monotonic
        reference; we use the wall clock for the subtraction.

        When the agent has no recorded heartbeat yet (``last_hb`` is
        falsy or unparseable) we return ``0`` — a freshly registered
        agent is treated as "just heartbeated".
        """
        if not last_hb:
            return 0
        if isinstance(last_hb, str):
            try:
                dt = datetime.fromisoformat(last_hb.replace("Z", "+00:00"))
            except ValueError:
                return 0
        elif isinstance(last_hb, datetime):
            dt = last_hb
        else:
            return 0
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        delta = datetime.now(timezone.utc) - dt
        return max(0, int(delta.total_seconds()))

    @staticmethod
    def _walk_stats(root: Path) -> tuple[int, int]:
        """Return ``(file_count, total_size_bytes)`` for ``root``."""
        count = 0
        total = 0
        for child in root.rglob("*"):
            try:
                if child.is_file():
                    count += 1
                    total += child.stat().st_size
            except OSError:
                continue
        return count, total

    @staticmethod
    def _memory_to_model(item: Any, idx: int) -> MemoryItem:
        """Map a memory row (from persistent store) to :class:`MemoryItem`."""
        return MemoryItem(
            id=idx + 1,
            agent_id=str(getattr(item, "agent_id", "unknown")),
            timestamp=getattr(item, "timestamp", datetime.now(timezone.utc)),
            type=str(getattr(item, "type", "context")),
            content=getattr(item, "content", None),
            relevance_score=float(getattr(item, "relevance_score", 0.0) or 0.0),
            workflow_id=getattr(item, "workflow_id", None),
            step_id=getattr(item, "step_id", None),
            source=str(getattr(item, "source", "self")),
            tags=[],
        )

    @staticmethod
    def _content_preview(content: Any, limit: int = 200) -> str:
        """Return a short, JSON-safe preview of a memory payload."""
        if isinstance(content, str):
            return _truncate(content, limit)
        try:
            return _truncate(json.dumps(content, default=str), limit)
        except (TypeError, ValueError):
            return _truncate(repr(content), limit)

    @staticmethod
    def _audit_summary(row: dict[str, Any], limit: int = 200) -> str:
        """Compose a one-line summary of an audit row."""
        details = row.get("details") or {}
        action = row.get("action") or ""
        result = row.get("result") or ""
        agent = row.get("agent_id") or "kernel"
        bits = [f"{action} ({result})", f"agent={agent}"]
        if details:
            try:
                detail_str = json.dumps(details, default=str)
            except (TypeError, ValueError):
                detail_str = str(details)
            bits.append(_truncate(detail_str, limit))
        return " · ".join(bits)

    @staticmethod
    def _extract_envelope_type(details: dict[str, Any]) -> str:
        """Pull the envelope_type from a stored audit-row details blob."""
        et = details.get("envelope_type")
        if et is None:
            et = details.get("event_type")
        if et is None:
            return "event"
        return str(et)

    @staticmethod
    def _extract_workflow_id(details: dict[str, Any]) -> str | None:
        wf = details.get("workflow_id")
        if wf is None:
            return None
        return str(wf)

    @staticmethod
    def _payload_preview(details: dict[str, Any]) -> str:
        """Generate a payload preview from a stored details blob."""
        if "data" in details:
            return IntrospectionAPI._content_preview(details.get("data"), 200)
        return IntrospectionAPI._content_preview(details, 200)

    async def _get_recent_errors(
        self, agent_id: str, limit: int = 10
    ) -> list[LogEntry]:
        """Return the most recent error audit rows for ``agent_id``."""
        rows = await self._registry.audit_log(agent_id=agent_id, limit=200)
        out: list[LogEntry] = []
        for row in rows:
            if row.get("result") != "error":
                continue
            details = row.get("details") or {}
            out.append(
                LogEntry(
                    envelope_id=str(row.get("envelope_id") or f"audit-{row.get('id')}"),
                    timestamp=self._parse_dt(row.get("timestamp")) or datetime.now(timezone.utc),
                    envelope_type="error",
                    sender=agent_id,
                    receiver=str(details.get("receiver", "") or "*"),
                    payload_preview=self._audit_summary(row, 200),
                    priority=8,
                    severity="error",
                    result=str(row.get("result") or "error"),
                    workflow_id=self._extract_workflow_id(details),
                    content_type=str(details.get("content_type") or "data"),
                )
            )
            if len(out) >= limit:
                break
        return out

    async def _avg_loop_latency(self) -> float:
        """Return the average loop latency in ms, or 0 if no data."""
        if self._loops is None:
            return 0.0
        try:
            templates = await self._loops.alist_templates()
        except Exception:
            return 0.0
        weighted_total = 0.0
        weight = 0
        for tpl in templates:
            n = int(tpl.get("usage_count") or 0)
            if n <= 0:
                continue
            weighted_total += float(tpl.get("avg_latency_ms") or 0.0) * n
            weight += n
        return round(weighted_total / weight, 2) if weight else 0.0

    async def _cost_today(self) -> float:
        """Return the total cost in the last 24 hours, parsed from audit rows.

        Real cost tracking lands with the loop optimizer in Phase 10.
        For now we sum ``cost_usd`` keys in audit details.
        """
        db_path = self._settings.db_path
        if not db_path.exists():
            return 0.0
        cutoff = self._format_dt(datetime.now(timezone.utc) - timedelta(hours=24))
        total = 0.0
        try:
            async with aiosqlite.connect(str(db_path)) as db:
                async with db.execute(
                    "SELECT details FROM audit_log WHERE timestamp >= ?",
                    (cutoff,),
                ) as cur:
                    rows = await cur.fetchall()
        except Exception:
            return 0.0
        for (raw,) in rows:
            try:
                details = json.loads(raw or "{}")
            except json.JSONDecodeError:
                continue
            cost = details.get("cost_usd")
            if isinstance(cost, (int, float)):
                total += float(cost)
        return round(total, 4)

    @staticmethod
    def _compute_message_rate(metrics: Any) -> float:
        """Approximate messages-per-minute from the bus's running counters.

        The bus does not keep a true sliding window, so we approximate
        by dividing total received by minutes-of-uptime.  This is
        intentionally naive — the aggregator maintains a more accurate
        window.
        """
        try:
            started = metrics.started_at
            if started.tzinfo is None:
                started = started.replace(tzinfo=timezone.utc)
            uptime_minutes = max(0.01, (datetime.now(timezone.utc) - started).total_seconds() / 60.0)
            return round(metrics.envelopes_received / uptime_minutes, 2)
        except Exception:
            return 0.0


__all__ = ["IntrospectionAPI"]
