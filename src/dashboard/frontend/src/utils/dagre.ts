/**
 * `dagre` layout helper for the workflow DAG view.
 *
 * The Phase 7 backend does not expose step coordinates or `depends_on`
 * (the latter lives in the workflow contract but isn't part of the
 * runtime `WorkflowStepStatus` model).  We infer a layered layout from
 * the step order and use dagre to keep the graph legible for any size.
 */

import dagre from "dagre";
import type { Edge, Node } from "@xyflow/react";
import type { WorkflowStepStatus } from "../types";

export const DAG_NODE_WIDTH = 220;
export const DAG_NODE_HEIGHT = 96;

export interface LaidOutStep {
  step: WorkflowStepStatus;
  node: Node;
  index: number;
}

export function buildDagNodesAndEdges(
  steps: WorkflowStepStatus[],
  options?: { rankdir?: "LR" | "TB" },
): { nodes: Node[]; edges: Edge[] } {
  const g = new dagre.graphlib.Graph();
  g.setGraph({ rankdir: options?.rankdir ?? "LR", nodesep: 28, ranksep: 64, marginx: 24, marginy: 24 });
  g.setDefaultEdgeLabel(() => ({}));

  if (steps.length === 0) {
    return { nodes: [], edges: [] };
  }

  // Build a positional "depends_on" guess: each step after the first
  // depends on the previous one.  This keeps the graph a clean chain
  // for unannotated workflows, which is what the backend hands us.
  const inferredDeps: Record<string, string[]> = {};
  steps.forEach((step, i) => {
    if (i === 0) {
      inferredDeps[step.step_id] = [];
    } else {
      inferredDeps[step.step_id] = [steps[i - 1].step_id];
    }
  });

  for (const step of steps) {
    g.setNode(step.step_id, { width: DAG_NODE_WIDTH, height: DAG_NODE_HEIGHT });
  }
  for (const [child, deps] of Object.entries(inferredDeps)) {
    for (const parent of deps) {
      g.setEdge(parent, child);
    }
  }

  dagre.layout(g);

  const nodes: Node[] = steps.map((step, i) => {
    const layoutNode = g.node(step.step_id);
    const x = layoutNode ? layoutNode.x - DAG_NODE_WIDTH / 2 : i * (DAG_NODE_WIDTH + 64);
    const y = layoutNode ? layoutNode.y - DAG_NODE_HEIGHT / 2 : 0;
    return {
      id: step.step_id,
      type: "step",
      position: { x, y },
      data: { step, label: step.name, status: step.status, agent: step.agent_id },
      draggable: true,
    };
  });

  const edges: Edge[] = [];
  for (const [child, deps] of Object.entries(inferredDeps)) {
    for (const parent of deps) {
      edges.push({
        id: `${parent}->${child}`,
        source: parent,
        target: child,
        type: "smoothstep",
        animated: steps.find((s) => s.step_id === child)?.status === "running",
      });
    }
  }

  return { nodes, edges };
}
