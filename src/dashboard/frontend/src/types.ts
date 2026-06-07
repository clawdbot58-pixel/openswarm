/**
 * OpenSwarm Dashboard — TypeScript types.
 *
 * These mirror the Pydantic models in `src/dashboard/backend/models.py`
 * (Phase 7) and the contracts in `contracts/`. The dashboard is
 * strictly read-only and treats the backend as the source of truth for
 * wire-format.
 *
 * Where the prompt spec diverges from the live backend (e.g. the
 * `panels` array on LayoutConfig vs. the backend's opaque `panes`
 * blob), we accept both shapes and normalise at the boundary.
 */

// ---------------------------------------------------------------------------
// Agents
// ---------------------------------------------------------------------------

export type AgentStatus =
  | "initializing"
  | "ready"
  | "busy"
  | "idle"
  | "draining"
  | "offline"
  | "error"
  | "zombie";

export type ModelTier = "fast" | "standard" | "powerful" | string;

export type AgentCategory =
  | "coding"
  | "planning"
  | "review"
  | "research"
  | "testing"
  | "deployment"
  | "analysis"
  | "custom"
  | string;

export interface AgentSummary {
  agent_id: string;
  human_readable_name: string;
  role: string;
  category: AgentCategory;
  status: AgentStatus;
  model_tier: ModelTier;
  current_task: string | null;
  heartbeat_age_seconds: number;
  connected_ws: boolean;
  registered_at: string;
  last_heartbeat: string | null;
  instance_id: string | null;
  tags: string[];
}

export interface MemoryItem {
  id: number;
  agent_id: string;
  timestamp: string;
  type: string;
  content: unknown;
  relevance_score: number;
  workflow_id: string | null;
  step_id: string | null;
  source: string;
  tags: string[];
}

export interface AgentEvent {
  event_id: string;
  timestamp: string;
  source: "envelope" | "audit";
  envelope_type: string | null;
  action: string | null;
  result: string | null;
  sender: string | null;
  receiver: string | null;
  summary: string;
}

export interface AgentDetail {
  agent_id: string;
  manifest: Record<string, unknown>;
  status: AgentStatus;
  last_heartbeat: string | null;
  registered_at: string;
  instance_id: string | null;
  connected_ws: boolean;
  heartbeat_age_seconds: number;
  current_task: string | null;
  recent_memory: MemoryItem[];
  recent_errors: LogEntry[];
  pending_queue_size: number;
}

export interface AgentMetrics {
  agent_id: string;
  tasks_completed: number;
  tasks_failed: number;
  avg_confidence: number;
  total_cost_usd: number;
  uptime_seconds: number;
  memory_count: number;
  queue_size: number;
}

// ---------------------------------------------------------------------------
// Workflows
// ---------------------------------------------------------------------------

export type WorkflowStatus =
  | "draft"
  | "submitted"
  | "running"
  | "paused"
  | "completed"
  | "failed"
  | "cancelled"
  | "recovering"
  | string;

export type StepStatus =
  | "pending"
  | "ready"
  | "running"
  | "completed"
  | "failed"
  | "recovering"
  | "skipped"
  | "cancelled"
  | string;

export interface WorkflowSummary {
  workflow_id: string;
  name: string;
  description: string | null;
  status: WorkflowStatus;
  owner_agent: string;
  step_count: number;
  completed_steps: number;
  created_at: string;
  updated_at: string;
}

export interface WorkflowStepStatus {
  step_id: string;
  name: string;
  agent_id: string;
  status: StepStatus;
  started_at: string | null;
  finished_at: string | null;
  error: string | null;
  attempts: number;
  output_preview: string | null;
}

export interface WorkflowDetail {
  workflow_id: string;
  name: string;
  description: string | null;
  status: WorkflowStatus;
  owner_agent: string;
  version: string;
  created_at: string;
  updated_at: string;
  steps: WorkflowStepStatus[];
  checkpoint: Record<string, unknown>;
  step_outputs: Record<string, unknown>;
  error_handling: Record<string, unknown>;
  timeline: AgentEvent[];
}

// ---------------------------------------------------------------------------
// Logs
// ---------------------------------------------------------------------------

export type EnvelopeType =
  | "request"
  | "response"
  | "event"
  | "error"
  | "heartbeat"
  | "chunk"
  | "intent"
  | string;

export type Severity = "debug" | "info" | "warning" | "error" | "critical" | string;

export interface LogEntry {
  envelope_id: string;
  timestamp: string;
  envelope_type: EnvelopeType;
  sender: string;
  receiver: string;
  payload_preview: string;
  priority: number;
  severity: Severity;
  result: string;
  workflow_id: string | null;
  content_type: string | null;
  tags: string[];
}

// ---------------------------------------------------------------------------
// Workspaces
// ---------------------------------------------------------------------------

export interface WorkspaceSummary {
  workflow_id: string;
  root_path: string;
  src_dir: string;
  output_dir: string;
  logs_dir: string;
  created_at: string;
  last_accessed: string;
  git_initialized: boolean;
  file_count: number;
  total_size_bytes: number;
}

export interface FileEntry {
  name: string;
  path: string;
  is_dir: boolean;
  size: number;
  modified_at: string;
}

export interface FileContent {
  workflow_id: string;
  path: string;
  content: string;
  size: number;
  encoding: string;
  modified_at: string;
}

export interface CommitInfo {
  hash: string;
  agent_id: string;
  message: string;
  timestamp: string;
  files_changed: string[];
  insertions: number;
  deletions: number;
}

// ---------------------------------------------------------------------------
// Loops
// ---------------------------------------------------------------------------

export interface LoopTemplateSummary {
  id: string;
  name: string;
  description: string | null;
  task_type: string | null;
  success_rate: number;
  avg_score: number;
  avg_cost_usd: number;
  avg_latency_ms: number;
  usage_count: number;
  is_premade: boolean;
  created_at: string | null;
  updated_at: string | null;
}

// ---------------------------------------------------------------------------
// Metrics
// ---------------------------------------------------------------------------

export interface SystemMetrics {
  total_agents: number;
  active_agents: number;
  zombie_agents: number;
  busy_agents: number;
  idle_agents: number;
  total_workflows: number;
  running_workflows: number;
  completed_workflows: number;
  failed_workflows: number;
  messages_per_minute: number;
  avg_loop_latency_ms: number;
  total_cost_today_usd: number;
  uptime_seconds: number;
  queue_total: number;
  started_at: string;
}

// ---------------------------------------------------------------------------
// Views & Layouts
// ---------------------------------------------------------------------------

/**
 * A user-defined dashboard view. The frontend owns the `view_type`
 * vocabulary; the backend just stores it opaquely. Adding a new view
 * type means registering a renderer — see `View.tsx`.
 */
export type ViewType =
  | "swarm_overview"
  | "workflow_dag"
  | "log_stream"
  | "workspace_explorer"
  | "agent_detail"
  | "metrics"
  | "custom";

export interface ViewConfig {
  view_id: string;
  name: string;
  description: string;
  view_type: ViewType | string;
  data_sources: string[];
  filters: Record<string, unknown>;
  refresh_interval_ms: number;
  created_by: string;
  created_at: string;
  updated_at: string;
}

export interface ViewConfigInput {
  name: string;
  description?: string;
  view_type?: string;
  data_sources?: string[];
  filters?: Record<string, unknown>;
  refresh_interval_ms?: number;
  created_by?: string;
}

/**
 * Panel coordinates inside a layout.  Coordinates are in grid units
 * (12-column responsive grid, 60px row height).  The frontend renders
 * layouts through `react-grid-layout` which uses the same units.
 */
export interface PanelPosition {
  x: number;
  y: number;
  w: number;
  h: number;
}

export interface PanelConfig {
  panel_id: string;
  view_id: string;
  position: PanelPosition;
  pinned: boolean;
  title?: string;
}

export interface LayoutConfig {
  layout_id: string;
  name: string;
  description?: string;
  /**
   * The Phase 8 prompt spec uses `panels: PanelConfig[]`.  The Phase 7
   * backend stores an opaque `panes` blob keyed by view_id.  The
   * frontend accepts both shapes and normalises to `panels`.
   */
  panels?: PanelConfig[];
  panes?: Record<string, PanelConfig | PanelPosition>;
  created_by: string;
  created_at: string;
  updated_at: string;
}

export interface LayoutConfigInput {
  name: string;
  description?: string;
  panels?: PanelConfig[];
  panes?: Record<string, PanelConfig | PanelPosition>;
  created_by?: string;
}

// ---------------------------------------------------------------------------
// WebSocket stream
// ---------------------------------------------------------------------------

/**
 * The kernel can push a variety of `type` discriminators.  The
 * dashboard only cares about a small subset for reactive updates.
 */
export type StreamEventType =
  | "agent_status_changed"
  | "workflow_status_changed"
  | "log_entry"
  | "file_changed"
  | "execution_complete"
  | "system_metrics"
  | "plan_pending_approval"
  | "plan_approved"
  | "plan_rejected"
  | "loop_detected"
  | "budget_exhausted"
  | "integration_unavailable"
  | "step_timeout"
  | "step_recovered"
  | string;

/**
 * Normalised shape for any event arriving on `/stream`.  The backend's
 * `StreamEvent` model exposes `type`, `timestamp`, `envelope`,
 * `metrics`, and `data`.  The prompt's example uses `event_type` /
 * `payload`; we accept both.
 */
export interface StreamEvent {
  type?: StreamEventType;
  event_type?: StreamEventType;
  timestamp: string;
  envelope?: Record<string, unknown> | null;
  metrics?: SystemMetrics | null;
  data?: Record<string, unknown>;
  payload?: Record<string, unknown>;
}

export type WebSocketStatus = "connecting" | "open" | "closed" | "error";

export interface WebSocketMessage<T = unknown> {
  type: StreamEventType;
  data: T;
  timestamp: string;
}

// ---------------------------------------------------------------------------
// Errors
// ---------------------------------------------------------------------------

export interface ApiError {
  code: string;
  message: string;
  details?: Record<string, unknown>;
  status: number;
}

// ---------------------------------------------------------------------------
// Utility
// ---------------------------------------------------------------------------

export type AnyRecord = Record<string, unknown>;
