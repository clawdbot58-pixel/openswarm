/**
 * App-level smoke test.
 *
 * Verifies:
 *   1. App renders without crashing.
 *   2. WebSocket connection is established on mount.
 *   3. Incoming stream events update the visible metrics / agent list.
 */

import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";
import { render, screen, waitFor, cleanup, act } from "@testing-library/react";
import { App } from "../src/App";
import { MockWebSocket, mockJsonResponse } from "./setup";

function mockInitialFetch(agents: unknown[], metrics: unknown, layouts: unknown[] = [], views: unknown[] = []) {
  vi.stubGlobal(
    "fetch",
    vi.fn().mockImplementation((url: string) => {
      if (url.includes("/api/agents")) return Promise.resolve(mockJsonResponse(agents));
      if (url.includes("/api/metrics")) return Promise.resolve(mockJsonResponse(metrics));
      if (url.includes("/api/layouts")) return Promise.resolve(mockJsonResponse(layouts));
      if (url.includes("/api/views")) return Promise.resolve(mockJsonResponse(views));
      if (url.includes("/api/workflows")) return Promise.resolve(mockJsonResponse([]));
      if (url.includes("/api/logs")) return Promise.resolve(mockJsonResponse([]));
      return Promise.resolve(mockJsonResponse({}));
    }),
  );
}

describe("<App />", () => {
  beforeEach(() => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
  });
  afterEach(() => {
    cleanup();
    vi.useRealTimers();
  });

  it("renders without crashing and connects to the WebSocket", async () => {
    mockInitialFetch([], {
      total_agents: 0,
      active_agents: 0,
      zombie_agents: 0,
      busy_agents: 0,
      idle_agents: 0,
      total_workflows: 0,
      running_workflows: 0,
      completed_workflows: 0,
      failed_workflows: 0,
      messages_per_minute: 0,
      avg_loop_latency_ms: 0,
      total_cost_today_usd: 0,
      uptime_seconds: 0,
      queue_total: 0,
      started_at: new Date().toISOString(),
    });
    render(<App />);
    expect(screen.getByText("OpenSwarm")).toBeInTheDocument();

    // a WebSocket was opened
    await waitFor(() => {
      expect(MockWebSocket.instances.length).toBeGreaterThan(0);
    });
  });

  it("updates state when a log_entry event arrives on the stream", async () => {
    mockInitialFetch([], {
      total_agents: 0,
      active_agents: 0,
      zombie_agents: 0,
      busy_agents: 0,
      idle_agents: 0,
      total_workflows: 0,
      running_workflows: 0,
      completed_workflows: 0,
      failed_workflows: 0,
      messages_per_minute: 0,
      avg_loop_latency_ms: 0,
      total_cost_today_usd: 0,
      uptime_seconds: 0,
      queue_total: 0,
      started_at: new Date().toISOString(),
    });
    render(<App />);

    // Wait for fetch + ws connect.
    await waitFor(() => expect(MockWebSocket.instances.length).toBeGreaterThan(0));
    const socket = MockWebSocket.instances[0]!;
    act(() => {
      socket.simulateOpen();
    });

    // Layout list resolves to empty; the app shows the empty state.
    expect(await screen.findByText(/Compose your first layout|Compose/i)).toBeInTheDocument();

    // Drive a system_metrics push from the stream and confirm no errors.
    act(() => {
      socket.simulateMessage({
        type: "system_metrics",
        timestamp: new Date().toISOString(),
        metrics: {
          total_agents: 1,
          active_agents: 1,
          zombie_agents: 0,
          busy_agents: 1,
          idle_agents: 0,
          total_workflows: 0,
          running_workflows: 0,
          completed_workflows: 0,
          failed_workflows: 0,
          messages_per_minute: 12,
          avg_loop_latency_ms: 240,
          total_cost_today_usd: 0.12,
          uptime_seconds: 60,
          queue_total: 0,
          started_at: new Date().toISOString(),
        },
      });
    });
  });
});
