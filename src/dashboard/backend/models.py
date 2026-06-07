"""Pydantic v2 response models for the dashboard backend.

Every public endpoint returns one of the models in this module.  The
models are deliberately decoupled from the kernel's wire-format
Pydantic models so the dashboard can:

* flatten nested manifest blobs into summary cards;
* add fields the kernel does not care about (``heartbeat_age_seconds``,
  ``current_task``, etc.);
* keep the OpenAPI schema stable even when the underlying kernel
  models evolve.

The contract for the *envelope payload* is :class:`Envelope` from
:mod:`kernel.models`; we re-export it here so consumers can build
typed clients without importing the kernel.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

# Re-export the kernel's envelope model so downstream code can build
# fully-typed clients without depending on the kernel's import path.
from kernel.models import Envelope

# ---------------------------------------------------------------------------
# Agents
# ---------------------------------------------------------------------------


class AgentSummary(BaseModel):
    """One-line projection of an agent for grid/list views."""

    model_config = ConfigDict(extra="forbid")

    agent_id: str
    human_readable_name: str
    role: str
    category: str
    status: str
    model_tier: str
    current_task: str | None = None
    heartbeat_age_seconds: int
    connected_ws: bool
    registered_at: datetime
    last_heartbeat: datetime | None = None
    instance_id: str | None = None
    tags: list[str] = Field(default_factory=list)


class AgentDetail(BaseModel):
    """Full agent card payload — manifest + live status + recent activity."""

    model_config = ConfigDict(extra="forbid")

    agent_id: str
    manifest: dict[str, Any]
    status: str
    last_heartbeat: datetime | None = None
    registered_at: datetime
    instance_id: str | None = None
    connected_ws: bool
    heartbeat_age_seconds: int
    current_task: str | None = None
    recent_memory: list[MemoryItem] = Field(default_factory=list)
    recent_errors: list[LogEntry] = Field(default_factory=list)
    pending_queue_size: int = 0


class AgentEvent(BaseModel):
    """A single envelope or audit-log row attributed to an agent."""

    model_config = ConfigDict(extra="forbid")

    event_id: str
    timestamp: datetime
    source: Literal["envelope", "audit"]
    envelope_type: str | None = None
    action: str | None = None
    result: str | None = None
    sender: str | None = None
    receiver: str | None = None
    summary: str


# ---------------------------------------------------------------------------
# Workflows
# ---------------------------------------------------------------------------


class WorkflowSummary(BaseModel):
    """One-line projection of a workflow for list views."""

    model_config = ConfigDict(extra="forbid")

    workflow_id: str
    name: str
    description: str | None = None
    status: str
    owner_agent: str
    step_count: int
    completed_steps: int
    created_at: datetime
    updated_at: datetime


class WorkflowStepStatus(BaseModel):
    """A single step's runtime state for the workflow detail view."""

    model_config = ConfigDict(extra="forbid")

    step_id: str
    name: str
    agent_id: str
    status: str
    started_at: datetime | None = None
    finished_at: datetime | None = None
    error: str | None = None
    attempts: int = 0
    output_preview: str | None = None


class WorkflowDetail(BaseModel):
    """Full workflow payload with steps, checkpoint, and timeline."""

    model_config = ConfigDict(extra="forbid")

    workflow_id: str
    name: str
    description: str | None = None
    status: str
    owner_agent: str
    version: str
    created_at: datetime
    updated_at: datetime
    steps: list[WorkflowStepStatus]
    checkpoint: dict[str, Any] = Field(default_factory=dict)
    step_outputs: dict[str, Any] = Field(default_factory=dict)
    error_handling: dict[str, Any] = Field(default_factory=dict)
    timeline: list[AgentEvent] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Logs
# ---------------------------------------------------------------------------


class LogEntry(BaseModel):
    """A single envelope or audit row surfaced through ``GET /api/logs``."""

    model_config = ConfigDict(extra="forbid")

    envelope_id: str
    timestamp: datetime
    envelope_type: str
    sender: str
    receiver: str
    payload_preview: str
    priority: int
    severity: str
    result: str = "ok"
    workflow_id: str | None = None
    content_type: str | None = None
    tags: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Workspaces (harness)
# ---------------------------------------------------------------------------


class WorkspaceSummary(BaseModel):
    """High-level summary of a single harness workspace on disk."""

    model_config = ConfigDict(extra="forbid")

    workflow_id: str
    root_path: str
    src_dir: str
    output_dir: str
    logs_dir: str
    created_at: datetime
    last_accessed: datetime
    git_initialized: bool
    file_count: int
    total_size_bytes: int


class FileEntry(BaseModel):
    """One row of the workspace file tree."""

    model_config = ConfigDict(extra="forbid")

    name: str
    path: str
    is_dir: bool
    size: int
    modified_at: datetime


class FileContent(BaseModel):
    """Body of a single file in a workspace."""

    model_config = ConfigDict(extra="forbid")

    workflow_id: str
    path: str
    content: str
    size: int
    encoding: str = "utf-8"
    modified_at: datetime


class CommitInfo(BaseModel):
    """A single commit in a workspace's git history."""

    model_config = ConfigDict(extra="forbid")

    hash: str
    agent_id: str
    message: str
    timestamp: datetime
    files_changed: list[str] = Field(default_factory=list)
    insertions: int = 0
    deletions: int = 0


# ---------------------------------------------------------------------------
# Loops
# ---------------------------------------------------------------------------


class LoopTemplateSummary(BaseModel):
    """A row of the loop registry leaderboard."""

    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    description: str | None = None
    task_type: str | None = None
    success_rate: float
    avg_score: float
    avg_cost_usd: float
    avg_latency_ms: float
    usage_count: int
    is_premade: bool
    created_at: datetime | None = None
    updated_at: datetime | None = None


class LoopPerformance(BaseModel):
    """Per-template performance breakdown for the laboratory view."""

    model_config = ConfigDict(extra="forbid")

    template_id: str
    name: str
    success_rate: float
    avg_score: float
    avg_cost_usd: float
    avg_latency_ms: float
    usage_count: int
    usage_over_time: list[dict[str, Any]] = Field(default_factory=list)
    recommendations: list[dict[str, Any]] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Loops — Phase 10: trial/error cycle surfaces
# ---------------------------------------------------------------------------


class LeaderboardEntry(BaseModel):
    """One row of the Phase 10 trial/error leaderboard.

    Aggregates immutable trial records by (loop_id, task_type) into a
    single ranked view.  See :mod:`loops.trial_store` for the
    underlying storage.
    """

    model_config = ConfigDict(extra="forbid")

    loop_id: str
    task_type: str | None = None
    avg_score: float
    avg_quality: float
    avg_cost_usd: float
    avg_latency_ms: float
    trial_count: int
    last_trial: datetime
    best_variant: dict[str, Any] = Field(default_factory=dict)


class TrialRecord(BaseModel):
    """One immutable trial record (one row of ``trials``)."""

    model_config = ConfigDict(extra="forbid")

    trial_id: str
    loop_id: str
    task_type: str | None = None
    loop_graph: dict[str, Any] = Field(default_factory=dict)
    score: dict[str, Any] = Field(default_factory=dict)
    result: dict[str, Any] = Field(default_factory=dict)
    timestamp: datetime
    task_preview: str = ""
    output_preview: str = ""


class CycleReport(BaseModel):
    """A summary of one trial/error cycle (response of the optimize endpoint)."""

    model_config = ConfigDict(extra="forbid")

    cycle_id: str
    task_type: str
    base_loop: str
    trial_count: int
    best_loop_id: str | None = None
    best_score: float = 0.0
    trials: list[TrialRecord] = Field(default_factory=list)


class OptimizeRequest(BaseModel):
    """Body of ``POST /api/loops/optimize``."""

    model_config = ConfigDict(extra="forbid")

    task_type: str = Field(default="general", min_length=1, max_length=100)
    task_sample: str = Field(default="", max_length=8000)
    n_trials: int = Field(default=3, ge=1, le=20)
    base_loop: str = Field(default="", max_length=64)
    include_builtins: bool = True


# ---------------------------------------------------------------------------
# Memory
# ---------------------------------------------------------------------------


class MemoryItem(BaseModel):
    """A single memory record (FTS5 query result or recent list)."""

    model_config = ConfigDict(extra="forbid")

    id: int
    agent_id: str
    timestamp: datetime
    type: str
    content: Any
    relevance_score: float
    workflow_id: str | None = None
    step_id: str | None = None
    source: str = "self"
    tags: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


class SystemMetrics(BaseModel):
    """Snapshot of swarm-level metrics for the dashboard header."""

    model_config = ConfigDict(extra="forbid")

    total_agents: int
    active_agents: int
    zombie_agents: int
    busy_agents: int
    idle_agents: int
    total_workflows: int
    running_workflows: int
    completed_workflows: int
    failed_workflows: int
    messages_per_minute: float
    avg_loop_latency_ms: float
    total_cost_today_usd: float
    uptime_seconds: float
    queue_total: int
    started_at: datetime


class AgentMetrics(BaseModel):
    """Per-agent performance breakdown."""

    model_config = ConfigDict(extra="forbid")

    agent_id: str
    tasks_completed: int
    tasks_failed: int
    avg_confidence: float
    total_cost_usd: float
    uptime_seconds: float
    memory_count: int
    queue_size: int


# ---------------------------------------------------------------------------
# Configuration (views & layouts)
# ---------------------------------------------------------------------------


class ViewConfig(BaseModel):
    """A user-defined dashboard view.

    The backend does not interpret ``view_type`` beyond storing it; the
    Phase 8 frontend (and the Main Agent) are the source of truth on
    what view types exist.
    """

    model_config = ConfigDict(extra="forbid")

    view_id: str
    name: str
    description: str = ""
    view_type: str = "custom"
    data_sources: list[str] = Field(default_factory=list)
    filters: dict[str, Any] = Field(default_factory=dict)
    refresh_interval_ms: int = 5000
    created_by: str = "system"
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class ViewConfigInput(BaseModel):
    """The body of ``POST /api/views``.  ``view_id`` is server-assigned."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=1, max_length=200)
    description: str = Field(default="", max_length=2000)
    view_type: str = Field(default="custom", max_length=100)
    data_sources: list[str] = Field(default_factory=list)
    filters: dict[str, Any] = Field(default_factory=dict)
    refresh_interval_ms: int = Field(default=5000, ge=100, le=86_400_000)
    created_by: str = Field(default="system", max_length=200)


class LayoutConfig(BaseModel):
    """A dashboard layout — which views, where, how sized.

    ``panes`` is an opaque JSON blob keyed by ``view_id``.  The backend
    does not validate its shape; the frontend owns that contract.
    """

    model_config = ConfigDict(extra="forbid")

    layout_id: str
    name: str
    description: str = ""
    panes: dict[str, Any] = Field(default_factory=dict)
    created_by: str = "system"
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class LayoutConfigInput(BaseModel):
    """Body of ``POST /api/layouts``.  ``layout_id`` is server-assigned."""

    model_config = ConfigDict(extra="forbid")

    name: str
    description: str = ""
    panes: dict[str, Any] = Field(default_factory=dict)
    created_by: str = "system"


# ---------------------------------------------------------------------------
# WebSocket stream payloads
# ---------------------------------------------------------------------------


class StreamEvent(BaseModel):
    """A single message pushed down the ``/stream`` WebSocket.

    The ``type`` field is a free-form discriminator; the rest of the
    payload is forwarded from the underlying kernel event envelope.
    """

    model_config = ConfigDict(extra="forbid")

    type: str
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    envelope: dict[str, Any] | None = None
    metrics: SystemMetrics | None = None
    data: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Misc
# ---------------------------------------------------------------------------


class ErrorResponse(BaseModel):
    """Standard error body."""

    model_config = ConfigDict(extra="forbid")

    code: str
    message: str
    details: dict[str, Any] = Field(default_factory=dict)


class HealthResponse(BaseModel):
    """Body of ``GET /health`` on the dashboard backend."""

    model_config = ConfigDict(extra="forbid")

    status: str = "ok"
    db_ok: bool
    kernel_reachable: bool
    started_at: datetime
    uptime_seconds: float


# Forward refs: AgentDetail mentions MemoryItem + LogEntry.
AgentDetail.model_rebuild()
__all__ = [
    "AgentDetail",
    "AgentEvent",
    "AgentMetrics",
    "AgentSummary",
    "CommitInfo",
    "Envelope",
    "ErrorResponse",
    "FileContent",
    "FileEntry",
    "HealthResponse",
    "LayoutConfig",
    "LayoutConfigInput",
    "LeaderboardEntry",
    "LogEntry",
    "LoopPerformance",
    "LoopTemplateSummary",
    "MemoryItem",
    "OptimizeRequest",
    "StreamEvent",
    "TrialRecord",
    "SystemMetrics",
    "ViewConfig",
    "ViewConfigInput",
    "WorkflowDetail",
    "WorkflowStepStatus",
    "WorkflowSummary",
    "WorkspaceSummary",
]
