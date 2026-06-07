"""HTTP-backed introspection for a dashboard running beside the kernel."""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib import error as urlerror
from urllib import request as urlrequest

from .models import (
    AgentDetail,
    AgentEvent,
    AgentMetrics,
    AgentSummary,
    CommitInfo,
    FileContent,
    FileEntry,
    LogEntry,
    LoopPerformance,
    LoopTemplateSummary,
    MemoryItem,
    SystemMetrics,
    WorkflowDetail,
    WorkflowStepStatus,
    WorkflowSummary,
    WorkspaceSummary,
)

logger = logging.getLogger(__name__)


class HttpIntrospectionAPI:
    """Proxy :class:`IntrospectionAPI` calls to the kernel REST API."""

    def __init__(
        self,
        kernel_url: str,
        *,
        workspaces_dir: Path | None = None,
        harness_dir: Path | None = None,
        timeout: float = 5.0,
    ) -> None:
        self._base = kernel_url.rstrip("/")
        self._timeout = timeout
        self._workspaces_dir = workspaces_dir
        self._harness_dir = harness_dir or (Path("data") / "workspaces")

    def _get_json(self, path: str) -> Any:
        try:
            with urlrequest.urlopen(  # noqa: S310
                f"{self._base}{path}", timeout=self._timeout
            ) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except (urlerror.URLError, OSError, json.JSONDecodeError) as exc:
            logger.debug("http introspection GET %s failed: %s", path, exc)
            raise

    async def get_agents(
        self,
        status: str | None = None,
        role: str | None = None,
        category: str | None = None,
        tags: list[str] | None = None,
    ) -> list[AgentSummary]:
        path = "/registry/agents"
        if status:
            path += f"?status_filter={status}"
        rows = self._get_json(path)
        out: list[AgentSummary] = []
        for row in rows:
            agent_id = row.get("agent_id", "")
            try:
                manifest = self._get_json(f"/registry/agents/{agent_id}")
            except Exception:  # noqa: BLE001
                manifest = {}
            if role and manifest.get("role") != role:
                continue
            if category and manifest.get("category") != category:
                continue
            if tags:
                mtags = manifest.get("tags") or []
                if not all(t in mtags for t in tags):
                    continue
            last_hb = self._parse_dt(row.get("last_heartbeat"))
            hb_age = self._heartbeat_age(last_hb)
            out.append(
                AgentSummary(
                    agent_id=agent_id,
                    human_readable_name=manifest.get("human_readable_name") or agent_id,
                    role=str(manifest.get("role") or row.get("role") or "worker"),
                    category=str(manifest.get("category") or "custom"),
                    status=str(row.get("status") or "unknown"),
                    model_tier=str(
                        (manifest.get("model_tier") or {}).get("tier", "standard")
                    ),
                    heartbeat_age_seconds=hb_age,
                    connected_ws=bool(row.get("connected_ws", False)),
                    registered_at=self._parse_dt(row.get("registered_at")),
                    last_heartbeat=last_hb,
                    instance_id=row.get("instance_id"),
                    tags=list(manifest.get("tags") or []),
                )
            )
        out.sort(key=lambda a: a.agent_id)
        return out

    async def get_agent_detail(self, agent_id: str) -> AgentDetail:
        manifest = self._get_json(f"/registry/agents/{agent_id}")
        status_row = self._get_json(f"/registry/agents/{agent_id}/status")
        last_hb = self._parse_dt(status_row.get("last_heartbeat"))
        return AgentDetail(
            agent_id=agent_id,
            manifest=manifest,
            status=str(status_row.get("status") or "unknown"),
            last_heartbeat=last_hb,
            registered_at=self._parse_dt(status_row.get("registered_at")),
            instance_id=status_row.get("instance_id"),
            connected_ws=bool(status_row.get("connected_ws", False)),
            heartbeat_age_seconds=self._heartbeat_age(last_hb),
            recent_memory=[],
            recent_errors=[],
            pending_queue_size=0,
        )

    async def get_agent_history(self, agent_id: str, limit: int = 50) -> list[AgentEvent]:
        rows = self._get_json(f"/audit?agent_id={agent_id}&limit={limit}")
        events: list[AgentEvent] = []
        for row in rows:
            events.append(
                AgentEvent(
                    event_id=str(row.get("id", "")),
                    timestamp=self._parse_dt(row.get("timestamp"))
                    or datetime.now(timezone.utc),
                    source="audit",
                    envelope_type=None,
                    action=str(row.get("action") or ""),
                    result=str(row.get("result") or ""),
                    sender=agent_id,
                    receiver=None,
                    summary=str(row.get("reason") or row.get("action") or ""),
                )
            )
        return events

    async def get_agent_metrics(self, agent_id: str) -> AgentMetrics:
        return AgentMetrics(agent_id=agent_id, messages_sent=0, messages_received=0)

    async def get_workflows(
        self,
        status: str | None = None,
        owner: str | None = None,
    ) -> list[WorkflowSummary]:
        rows = self._get_json("/workflows")
        out: list[WorkflowSummary] = []
        for row in rows:
            if status and row.get("status") != status:
                continue
            goal = str(row.get("goal") or "")
            out.append(
                WorkflowSummary(
                    workflow_id=row["workflow_id"],
                    name=goal[:80] or row["workflow_id"],
                    description=goal,
                    status=str(row.get("status") or "unknown"),
                    owner_agent=owner or "main-agent",
                    created_at=self._parse_dt(row.get("submitted_at"))
                    or datetime.now(timezone.utc),
                    updated_at=self._parse_dt(row.get("updated_at"))
                    or datetime.now(timezone.utc),
                    step_count=1,
                    completed_steps=1 if row.get("status") == "completed" else 0,
                )
            )
        return out

    async def get_workflow_detail(self, workflow_id: str) -> WorkflowDetail:
        row = self._get_json(f"/workflows/{workflow_id}")
        goal = str(row.get("goal") or "")
        return WorkflowDetail(
            workflow_id=workflow_id,
            name=goal[:80] or workflow_id,
            description=goal,
            status=str(row.get("status") or "unknown"),
            owner_agent="main-agent",
            version="0.1.0",
            created_at=self._parse_dt(row.get("submitted_at"))
            or datetime.now(timezone.utc),
            updated_at=self._parse_dt(row.get("updated_at"))
            or datetime.now(timezone.utc),
            steps=[
                WorkflowStepStatus(
                    step_id="main",
                    name="orchestration",
                    status=str(row.get("status") or "unknown"),
                    agent_id="main-agent",
                )
            ],
        )

    async def get_workflow_logs(
        self, workflow_id: str, limit: int = 100
    ) -> list[LogEntry]:
        return await self.get_logs(workflow_id=workflow_id, limit=limit)

    async def get_logs(
        self,
        *,
        agent_id: str | None = None,
        workflow_id: str | None = None,
        severity: str | None = None,
        limit: int = 100,
    ) -> list[LogEntry]:
        path = f"/audit?limit={limit}"
        if agent_id:
            path += f"&agent_id={agent_id}"
        rows = self._get_json(path)
        out: list[LogEntry] = []
        for row in rows:
            out.append(
                LogEntry(
                    envelope_id=str(row.get("envelope_id") or row.get("id") or ""),
                    timestamp=self._parse_dt(row.get("timestamp"))
                    or datetime.now(timezone.utc),
                    envelope_type="audit",
                    sender=str(row.get("agent_id") or "kernel"),
                    receiver="kernel",
                    payload_preview=str(row.get("reason") or row.get("action") or ""),
                    priority=0,
                    severity="info",
                    workflow_id=workflow_id,
                )
            )
        return out[:limit]

    async def get_workspaces(self) -> list[WorkspaceSummary]:
        summaries: list[WorkspaceSummary] = []
        agent_ws = self._workspaces_dir
        harness = self._harness_dir
        roots: list[Path] = []
        if agent_ws and agent_ws.is_dir():
            roots.append(agent_ws)
        if harness.is_dir():
            roots.extend(p for p in sorted(harness.iterdir()) if p.is_dir())
        for entry in roots:
            wf_id = "agent" if entry.name == "agent" else entry.name
            try:
                file_count = sum(1 for _ in entry.rglob("*") if _.is_file())
                total_size = sum(f.stat().st_size for f in entry.rglob("*") if f.is_file())
                stat = entry.stat()
                summaries.append(
                    WorkspaceSummary(
                        workflow_id=wf_id,
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
        return summaries

    async def get_workspace_files(
        self, workflow_id: str, path: str = "/"
    ) -> list[FileEntry]:
        root = self._resolve_workspace_root(workflow_id)
        target = (root / path.lstrip("/")).resolve()
        if not str(target).startswith(str(root.resolve())):
            return []
        if not target.is_dir():
            return []
        entries: list[FileEntry] = []
        for item in sorted(target.iterdir()):
            rel = "/" + str(item.relative_to(root)).replace("\\", "/")
            entries.append(
                FileEntry(
                    path=rel,
                    name=item.name,
                    is_dir=item.is_dir(),
                    size_bytes=item.stat().st_size if item.is_file() else 0,
                )
            )
        return entries

    async def get_workspace_file(self, workflow_id: str, path: str) -> FileContent:
        root = self._resolve_workspace_root(workflow_id)
        target = (root / path.lstrip("/")).resolve()
        if not str(target).startswith(str(root.resolve())) or not target.is_file():
            raise FileNotFoundError(path)
        return FileContent(
            path=path,
            content=target.read_text(encoding="utf-8", errors="replace"),
            size_bytes=target.stat().st_size,
        )

    async def get_workspace_diff(self, workflow_id: str, commit_hash: str) -> str:
        return f"(diff unavailable in HTTP mode for {workflow_id}@{commit_hash})"

    async def get_workspace_history(self, workflow_id: str) -> list[CommitInfo]:
        return []

    async def get_loop_templates(
        self, category: str | None = None
    ) -> list[LoopTemplateSummary]:
        return []

    async def get_loop_performance(self, template_id: str) -> LoopPerformance:
        return LoopPerformance(template_id=template_id, trials=0, avg_score=0.0)

    async def get_trial_leaderboard(
        self, task_type: str | None = None, limit: int = 10
    ) -> list:
        return []

    async def get_loop_trials(
        self, template_id: str, limit: int = 50
    ) -> list:
        return []

    async def run_optimization(self, request: Any) -> Any:
        raise NotImplementedError("optimization requires in-process kernel")

    async def get_agent_memory(
        self,
        agent_id: str,
        *,
        memory_type: str | None = None,
        limit: int = 50,
    ) -> list[MemoryItem]:
        return []

    async def get_system_metrics(self) -> SystemMetrics:
        metrics = self._get_json("/metrics")
        counts = dict(metrics.get("registry_status_counts") or {})
        total = int(metrics.get("registry_agent_count", 0))
        workflows = await self.get_workflows()
        started = datetime.now(timezone.utc)
        return SystemMetrics(
            total_agents=total,
            active_agents=counts.get("ready", 0) + counts.get("busy", 0),
            zombie_agents=counts.get("zombie", 0),
            busy_agents=counts.get("busy", 0),
            idle_agents=counts.get("idle", 0),
            total_workflows=len(workflows),
            running_workflows=sum(1 for w in workflows if w.status == "running"),
            completed_workflows=sum(1 for w in workflows if w.status == "completed"),
            failed_workflows=sum(1 for w in workflows if w.status == "failed"),
            messages_per_minute=0.0,
            avg_loop_latency_ms=0.0,
            total_cost_today_usd=0.0,
            uptime_seconds=float(metrics.get("uptime_seconds", 0)),
            queue_total=int(metrics.get("queue_total", 0)),
            started_at=started,
        )

    def _resolve_workspace_root(self, workflow_id: str) -> Path:
        if workflow_id == "agent" and self._workspaces_dir:
            return self._workspaces_dir
        return self._harness_dir / workflow_id

    @staticmethod
    def _heartbeat_age(last_hb: datetime | None) -> int | None:
        if last_hb is None:
            return None
        if last_hb.tzinfo is None:
            last_hb = last_hb.replace(tzinfo=timezone.utc)
        return max(0, int((datetime.now(timezone.utc) - last_hb).total_seconds()))

    @staticmethod
    def _parse_dt(value: Any) -> datetime | None:
        if not value:
            return None
        if isinstance(value, datetime):
            return value
        try:
            text = str(value).replace("Z", "+00:00")
            return datetime.fromisoformat(text)
        except (TypeError, ValueError):
            return None


__all__ = ["HttpIntrospectionAPI"]
