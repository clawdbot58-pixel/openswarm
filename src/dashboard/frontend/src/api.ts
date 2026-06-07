/**
 * Thin typed client over the Phase 7 FastAPI backend.
 *
 * Conventions:
 *   * Every function returns a fully-typed payload.  The backend
 *     already validates at the edge, so we trust the wire format.
 *   * Network failures surface as thrown `ApiError` so callers can
 *     render a meaningful error state.
 *   * The client does not cache.  Views cache through their own hooks.
 */

import type {
  AgentDetail,
  AgentMetrics,
  AgentSummary,
  ApiError,
  CommitInfo,
  FileContent,
  FileEntry,
  LayoutConfig,
  LayoutConfigInput,
  LogEntry,
  LoopTemplateSummary,
  MemoryItem,
  SystemMetrics,
  ViewConfig,
  ViewConfigInput,
  WorkflowDetail,
  WorkflowSummary,
  WorkspaceSummary,
} from "./types";
import { API_BASE } from "./env";

// ---------------------------------------------------------------------------
// Core fetch wrapper
// ---------------------------------------------------------------------------

export class ApiClientError extends Error implements ApiError {
  readonly code: string;
  readonly status: number;
  readonly details: Record<string, unknown>;

  constructor(opts: { code: string; message: string; status: number; details?: Record<string, unknown> }) {
    super(opts.message);
    this.name = "ApiClientError";
    this.code = opts.code;
    this.status = opts.status;
    this.details = opts.details ?? {};
  }
}

function buildUrl(path: string, query?: Record<string, string | number | undefined>): string {
  const base = path.startsWith("http") ? path : `${API_BASE}${path.startsWith("/") ? path : `/${path}`}`;
  if (!query) return base;
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(query)) {
    if (value === undefined || value === null) continue;
    params.append(key, String(value));
  }
  const qs = params.toString();
  return qs.length > 0 ? `${base}?${qs}` : base;
}

async function request<T>(
  path: string,
  init?: RequestInit & { query?: Record<string, string | number | undefined> },
): Promise<T> {
  const { query, ...rest } = init ?? {};
  const url = buildUrl(path, query);
  let response: Response;
  try {
    response = await fetch(url, {
      ...rest,
      headers: {
        Accept: "application/json",
        ...(rest?.body && !(rest.body instanceof FormData)
          ? { "Content-Type": "application/json" }
          : {}),
        ...(rest?.headers ?? {}),
      },
    });
  } catch (cause) {
    throw new ApiClientError({
      code: "network_error",
      message: cause instanceof Error ? cause.message : "network request failed",
      status: 0,
      details: { url },
    });
  }

  if (!response.ok) {
    let payload: unknown = null;
    try {
      payload = await response.json();
    } catch {
      // body may be empty for 204
    }
    const detail =
      payload && typeof payload === "object" && "detail" in payload
        ? (payload as { detail?: { code?: string; message?: string; details?: Record<string, unknown> } }).detail
        : null;
    throw new ApiClientError({
      code: detail?.code ?? `http_${response.status}`,
      message:
        detail?.message ??
        (typeof payload === "string" ? payload : `request failed with status ${response.status}`),
      status: response.status,
      details: detail?.details ?? {},
    });
  }

  if (response.status === 204) return undefined as T;

  const text = await response.text();
  if (!text) return undefined as T;
  return JSON.parse(text) as T;
}

// ---------------------------------------------------------------------------
// Agents
// ---------------------------------------------------------------------------

export const agentsApi = {
  list(query?: { status?: string; role?: string; category?: string }): Promise<AgentSummary[]> {
    return request<AgentSummary[]>("/api/agents", { query });
  },
  detail(agentId: string): Promise<AgentDetail> {
    return request<AgentDetail>(`/api/agents/${encodeURIComponent(agentId)}`);
  },
  history(agentId: string, limit = 50): Promise<import("./types").AgentEvent[]> {
    return request<import("./types").AgentEvent[]>(
      `/api/agents/${encodeURIComponent(agentId)}/history`,
      { query: { limit } },
    );
  },
  metrics(agentId: string): Promise<AgentMetrics> {
    return request<AgentMetrics>(`/api/agents/${encodeURIComponent(agentId)}/metrics`);
  },
};

// ---------------------------------------------------------------------------
// Workflows
// ---------------------------------------------------------------------------

export const workflowsApi = {
  list(query?: { status?: string; owner?: string }): Promise<WorkflowSummary[]> {
    return request<WorkflowSummary[]>("/api/workflows", { query });
  },
  detail(workflowId: string): Promise<WorkflowDetail> {
    return request<WorkflowDetail>(`/api/workflows/${encodeURIComponent(workflowId)}`);
  },
  logs(workflowId: string, limit = 100): Promise<LogEntry[]> {
    return request<LogEntry[]>(`/api/workflows/${encodeURIComponent(workflowId)}/logs`, {
      query: { limit },
    });
  },
};

// ---------------------------------------------------------------------------
// Logs
// ---------------------------------------------------------------------------

export const logsApi = {
  list(query?: {
    agent_id?: string;
    workflow_id?: string;
    envelope_type?: string;
    severity?: string;
    limit?: number;
    offset?: number;
  }): Promise<LogEntry[]> {
    return request<LogEntry[]>("/api/logs", { query });
  },
};

// ---------------------------------------------------------------------------
// Workspaces
// ---------------------------------------------------------------------------

export const workspacesApi = {
  list(): Promise<WorkspaceSummary[]> {
    return request<WorkspaceSummary[]>("/api/workspaces");
  },
  files(workflowId: string, path = "/"): Promise<FileEntry[]> {
    return request<FileEntry[]>(`/api/workspaces/${encodeURIComponent(workflowId)}/files`, {
      query: { path },
    });
  },
  file(workflowId: string, path: string): Promise<FileContent> {
    return request<FileContent>(`/api/workspaces/${encodeURIComponent(workflowId)}/file`, {
      query: { path },
    });
  },
  diff(workflowId: string, commit: string): Promise<string> {
    return request<string>(`/api/workspaces/${encodeURIComponent(workflowId)}/diff`, {
      query: { commit },
    });
  },
  history(workflowId: string): Promise<CommitInfo[]> {
    return request<CommitInfo[]>(`/api/workspaces/${encodeURIComponent(workflowId)}/history`);
  },
};

// ---------------------------------------------------------------------------
// Loops
// ---------------------------------------------------------------------------

export const loopsApi = {
  list(query?: { task_type?: string; min_success_rate?: number }): Promise<LoopTemplateSummary[]> {
    return request<LoopTemplateSummary[]>("/api/loops", { query });
  },
};

// ---------------------------------------------------------------------------
// Memory
// ---------------------------------------------------------------------------

export const memoryApi = {
  list(
    agentId: string,
    query?: { type?: string; workflow_id?: string; query?: string; limit?: number },
  ): Promise<MemoryItem[]> {
    return request<MemoryItem[]>(`/api/memory/${encodeURIComponent(agentId)}`, { query });
  },
};

// ---------------------------------------------------------------------------
// Metrics
// ---------------------------------------------------------------------------

export const metricsApi = {
  live(): Promise<SystemMetrics> {
    return request<SystemMetrics>("/api/metrics");
  },
  cached(): Promise<SystemMetrics> {
    return request<SystemMetrics>("/api/metrics/cached");
  },
};

// ---------------------------------------------------------------------------
// Views & layouts
// ---------------------------------------------------------------------------

export const viewsApi = {
  list(): Promise<ViewConfig[]> {
    return request<ViewConfig[]>("/api/views");
  },
  create(input: ViewConfigInput): Promise<ViewConfig> {
    return request<ViewConfig>("/api/views", { method: "POST", body: JSON.stringify(input) });
  },
  get(viewId: string): Promise<ViewConfig> {
    return request<ViewConfig>(`/api/views/${encodeURIComponent(viewId)}`);
  },
};

export const layoutsApi = {
  list(): Promise<LayoutConfig[]> {
    return request<LayoutConfig[]>("/api/layouts");
  },
  get(layoutId: string): Promise<LayoutConfig> {
    return request<LayoutConfig>(`/api/layouts/${encodeURIComponent(layoutId)}`);
  },
  create(input: LayoutConfigInput): Promise<LayoutConfig> {
    return request<LayoutConfig>("/api/layouts", { method: "POST", body: JSON.stringify(input) });
  },
};

// ---------------------------------------------------------------------------
// Convenience: a single object re-exporting every endpoint.
// ---------------------------------------------------------------------------

export const api = {
  agents: agentsApi,
  workflows: workflowsApi,
  logs: logsApi,
  workspaces: workspacesApi,
  loops: loopsApi,
  memory: memoryApi,
  metrics: metricsApi,
  views: viewsApi,
  layouts: layoutsApi,
};

export default api;
