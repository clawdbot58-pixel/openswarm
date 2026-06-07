/**
 * SwarmOverview tests — card rendering, status colour, click handler.
 */

import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";
import { render, screen, within, cleanup, fireEvent } from "@testing-library/react";
import { SwarmOverviewView } from "../src/views/SwarmOverview";
import { mockJsonResponse } from "./setup";
import type { AgentSummary, ViewConfig } from "../src/types";

const baseConfig: ViewConfig = {
  view_id: "test-swarm",
  name: "Swarm",
  description: "",
  view_type: "swarm_overview",
  data_sources: ["/api/agents"],
  filters: {},
  refresh_interval_ms: 5000,
  created_by: "test",
  created_at: "2026-06-06T10:00:00Z",
  updated_at: "2026-06-06T10:00:00Z",
};

function makeAgent(overrides: Partial<AgentSummary>): AgentSummary {
  return {
    agent_id: "coder-fast",
    human_readable_name: "Coder",
    role: "executor",
    category: "coding",
    status: "ready",
    model_tier: "fast",
    current_task: "writing tests",
    heartbeat_age_seconds: 2,
    connected_ws: true,
    registered_at: "2026-06-06T10:00:00Z",
    last_heartbeat: "2026-06-06T10:00:02Z",
    instance_id: null,
    tags: ["python"],
    ...overrides,
  };
}

function mockFetchOnce(agents: AgentSummary[]): void {
  vi.stubGlobal(
    "fetch",
    vi.fn().mockImplementation((url: string) => {
      if (url.includes("/api/agents")) return Promise.resolve(mockJsonResponse(agents));
      return Promise.resolve(mockJsonResponse({}));
    }),
  );
}

describe("<SwarmOverviewView />", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });
  afterEach(() => cleanup());

  it("renders one card per agent with the correct status data attribute", async () => {
    mockFetchOnce([
      makeAgent({ agent_id: "a-1", status: "busy" }),
      makeAgent({ agent_id: "a-2", status: "ready" }),
      makeAgent({ agent_id: "a-3", status: "error" }),
    ]);

    render(<SwarmOverviewView config={baseConfig} />);
    const cards = await screen.findAllByTestId("agent-card");
    expect(cards).toHaveLength(3);
    expect(cards[0]?.getAttribute("data-status")).toBe("busy");
    expect(cards[1]?.getAttribute("data-status")).toBe("ready");
    expect(cards[2]?.getAttribute("data-status")).toBe("error");
  });

  it("colours the status badge according to the agent status", async () => {
    mockFetchOnce([makeAgent({ agent_id: "a-1", status: "zombie" })]);

    render(<SwarmOverviewView config={baseConfig} />);
    const card = await screen.findByTestId("agent-card");
    const badge = within(card).getByRole("status");
    expect(badge.getAttribute("data-status")).toBe("zombie");
  });

  it("invokes onAgentClick when a card is clicked", async () => {
    mockFetchOnce([makeAgent({ agent_id: "a-1", status: "ready" })]);
    const onClick = vi.fn();
    render(<SwarmOverviewView config={baseConfig} onAgentClick={onClick} />);
    const card = await screen.findByTestId("agent-card");
    fireEvent.click(card);
    expect(onClick).toHaveBeenCalledWith("a-1");
  });

  it("filters agents by status when a status pill is clicked", async () => {
    mockFetchOnce([
      makeAgent({ agent_id: "a-1", status: "ready" }),
      makeAgent({ agent_id: "a-2", status: "busy" }),
    ]);
    render(<SwarmOverviewView config={baseConfig} />);
    await screen.findAllByTestId("agent-card");
    fireEvent.click(screen.getByTestId("filter-status-busy"));
    const remaining = screen.queryAllByTestId("agent-card");
    expect(remaining).toHaveLength(1);
    expect(remaining[0]?.getAttribute("data-status")).toBe("busy");
  });
});
