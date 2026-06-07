/**
 * WorkflowDAG tests — node/edge construction, status colouring,
 * animated edges for running steps.
 */

import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";
import { render, screen, cleanup, waitFor } from "@testing-library/react";
import { WorkflowDAGView } from "../src/views/WorkflowDAG";
import { mockJsonResponse } from "./setup";
import type { ViewConfig, WorkflowDetail } from "../src/types";

const baseConfig: ViewConfig = {
  view_id: "test-dag",
  name: "DAG",
  description: "",
  view_type: "workflow_dag",
  data_sources: [],
  filters: {},
  refresh_interval_ms: 5000,
  created_by: "test",
  created_at: "2026-06-06T10:00:00Z",
  updated_at: "2026-06-06T10:00:00Z",
};

function makeWorkflow(): WorkflowDetail {
  return {
    workflow_id: "wf-1",
    name: "Build a thing",
    description: null,
    status: "running",
    owner_agent: "main-agent",
    version: "1.0.0",
    created_at: "2026-06-06T10:00:00Z",
    updated_at: "2026-06-06T10:00:00Z",
    steps: [
      {
        step_id: "step_plan",
        name: "Plan",
        agent_id: "planner",
        status: "completed",
        started_at: "2026-06-06T10:00:00Z",
        finished_at: "2026-06-06T10:00:30Z",
        error: null,
        attempts: 1,
        output_preview: "plan ready",
      },
      {
        step_id: "step_codegen",
        name: "Generate code",
        agent_id: "coder",
        status: "running",
        started_at: "2026-06-06T10:00:30Z",
        finished_at: null,
        error: null,
        attempts: 1,
        output_preview: "writing…",
      },
      {
        step_id: "step_test",
        name: "Test",
        agent_id: "tester",
        status: "pending",
        started_at: null,
        finished_at: null,
        error: null,
        attempts: 0,
        output_preview: null,
      },
    ],
    checkpoint: {},
    step_outputs: {},
    error_handling: {},
    timeline: [],
  };
}

describe("<WorkflowDAGView />", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });
  afterEach(() => cleanup());

  it("renders one node per step with the right status data attribute", async () => {
    const workflow = makeWorkflow();
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation((url: string) => {
        if (/\/api\/workflows\/[^/]+$/.test(url)) {
          return Promise.resolve(mockJsonResponse(workflow));
        }
        if (url.endsWith("/api/workflows") || url.includes("/api/workflows?")) {
          return Promise.resolve(mockJsonResponse([]));
        }
        if (url.includes("/api/logs")) return Promise.resolve(mockJsonResponse([]));
        return Promise.resolve(mockJsonResponse({}));
      }),
    );

    render(<WorkflowDAGView config={baseConfig} workflowId="wf-1" />);

    await waitFor(() => {
      expect(screen.getByTestId("react-flow")).toBeInTheDocument();
    });
    const nodes = screen.getAllByTestId("rf-node");
    expect(nodes).toHaveLength(3);
    expect(nodes[0]?.getAttribute("data-node-status")).toBe("completed");
    expect(nodes[1]?.getAttribute("data-node-status")).toBe("running");
    expect(nodes[2]?.getAttribute("data-node-status")).toBe("pending");
  });

  it("marks the edge into a running step as animated", async () => {
    const workflow = makeWorkflow();
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation((url: string) => {
        if (/\/api\/workflows\/[^/]+$/.test(url)) {
          return Promise.resolve(mockJsonResponse(workflow));
        }
        if (url.endsWith("/api/workflows") || url.includes("/api/workflows?")) {
          return Promise.resolve(mockJsonResponse([]));
        }
        if (url.includes("/api/logs")) return Promise.resolve(mockJsonResponse([]));
        return Promise.resolve(mockJsonResponse({}));
      }),
    );

    render(<WorkflowDAGView config={baseConfig} workflowId="wf-1" />);
    await waitFor(() => screen.getByTestId("react-flow"));
    const edges = screen.getAllByTestId("rf-edge");
    expect(edges.length).toBeGreaterThan(0);
    // Edge into the running step (`step_codegen`) is animated.
    const runningEdge = edges.find((e) => e.getAttribute("data-target") === "step_codegen");
    expect(runningEdge?.getAttribute("data-animated")).toBe("true");
  });

  it("falls back to the most recent workflow when no id is given", async () => {
    const workflow = makeWorkflow();
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation((url: string) => {
        if (url.endsWith("/api/workflows")) {
          return Promise.resolve(
            mockJsonResponse([
              {
                workflow_id: "wf-1",
                name: "Build a thing",
                description: null,
                status: "running",
                owner_agent: "main-agent",
                step_count: 3,
                completed_steps: 1,
                created_at: "2026-06-06T10:00:00Z",
                updated_at: "2026-06-06T10:00:00Z",
              },
            ]),
          );
        }
        if (url.includes("/api/workflows/wf-1")) return Promise.resolve(mockJsonResponse(workflow));
        return Promise.resolve(mockJsonResponse({}));
      }),
    );
    render(<WorkflowDAGView config={baseConfig} />);
    await waitFor(() => screen.getByTestId("react-flow"));
    expect(screen.getAllByTestId("rf-node")).toHaveLength(3);
  });
});
