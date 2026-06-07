/**
 * `WorkflowDAG` — interactive workflow graph using @xyflow/react.
 *
 * Lays out the workflow's steps as a directed graph, with edges
 * styled by step status.  Click a node to see the step detail in a
 * side rail.
 */

import { useEffect, useMemo, useState } from "react";
import { Background, Controls, Handle, Position, ReactFlow, type NodeProps, type Node as FlowNode } from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import { motion, AnimatePresence } from "framer-motion";
import { ArrowRight, CircleNotch, Graph, X } from "@phosphor-icons/react";
import { workflowsApi, ApiClientError } from "../api";
import { StatusBadge } from "../components/StatusBadge";
import { useWebSocket } from "../hooks/useWebSocket";
import { useWorkflows } from "../hooks/useWorkflows";
import { buildDagNodesAndEdges, DAG_NODE_HEIGHT, DAG_NODE_WIDTH } from "../utils/dagre";
import { cn } from "../utils/cn";
import { formatTime, formatRelative, truncate } from "../utils/format";
import { motion as motionTokens } from "../theme";
import type { ViewConfig, WorkflowDetail, WorkflowStepStatus } from "../types";

interface WorkflowDAGProps {
  config: ViewConfig;
  /** Optional fixed workflow id.  When omitted, picks the most recent running one. */
  workflowId?: string;
}

export function WorkflowDAGView({ config: _config, workflowId }: WorkflowDAGProps): JSX.Element {
  const { workflows } = useWorkflows();
  const [activeId, setActiveId] = useState<string | undefined>(workflowId);
  const [detail, setDetail] = useState<WorkflowDetail | null>(null);
  const [error, setError] = useState<Error | null>(null);
  const [selectedStepId, setSelectedStepId] = useState<string | null>(null);
  const { subscribeByType } = useWebSocket();

  // If no explicit id, surface the most recent running/recovering workflow.
  useEffect(() => {
    if (activeId || workflows.length === 0) return;
    const live = workflows.find((w) => w.status === "running" || w.status === "recovering");
    setActiveId(live?.workflow_id ?? workflows[0]?.workflow_id);
  }, [activeId, workflows]);

  useEffect(() => {
    if (!activeId) return;
    let cancelled = false;
    (async () => {
      try {
        const data = await workflowsApi.detail(activeId);
        if (!cancelled) {
          setDetail(data);
          setError(null);
        }
      } catch (err) {
        if (!cancelled) {
          setError(err instanceof ApiClientError ? err : new Error(String(err)));
        }
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [activeId]);

  // Live updates — re-fetch on workflow status changes.
  useEffect(() => {
    return subscribeByType("workflow_status_changed", () => {
      if (!activeId) return;
      void workflowsApi.detail(activeId).then(setDetail).catch(() => undefined);
    });
  }, [activeId, subscribeByType]);

  useEffect(() => {
    return subscribeByType("step_recovered", () => {
      if (!activeId) return;
      void workflowsApi.detail(activeId).then(setDetail).catch(() => undefined);
    });
  }, [activeId, subscribeByType]);

  const { nodes, edges } = useMemo(() => {
    if (!detail) return { nodes: [], edges: [] };
    return buildDagNodesAndEdges(detail.steps, { rankdir: "LR" });
  }, [detail]);

  const selectedStep = useMemo(
    () => detail?.steps.find((s) => s.step_id === selectedStepId) ?? null,
    [detail, selectedStepId],
  );

  return (
    <section className="h-full flex flex-col" data-testid="workflow-dag">
      <header className="px-4 py-3 flex items-center gap-3 border-b border-ink-700/60">
        <div className="flex items-center gap-2">
          <Graph size={14} weight="duotone" className="text-amber-glow" />
          <h2 className="panel-title">Workflow DAG</h2>
        </div>
        <select
          value={activeId ?? ""}
          onChange={(e) => {
            setActiveId(e.target.value || undefined);
            setSelectedStepId(null);
          }}
          className="bg-ink-900/60 ring-1 ring-inset ring-ink-700/60 rounded h-7 px-2 text-[11px] text-ink-100 focus:outline-none focus:ring-amber-glow/60"
          data-testid="workflow-selector"
        >
          <option value="">— pick a workflow —</option>
          {workflows.map((w) => (
            <option key={w.workflow_id} value={w.workflow_id}>
              {w.name} ({w.status})
            </option>
          ))}
        </select>
        {detail && (
          <div className="ml-auto flex items-center gap-2 text-[10px] text-ink-300 font-mono">
            <span>{detail.steps.length} steps</span>
            <span>·</span>
            <span>{detail.steps.filter((s) => s.status === "completed").length} done</span>
            <StatusBadge status={detail.status} size="sm" />
          </div>
        )}
      </header>

      <div className="flex-1 relative">
        {error && (
          <div className="absolute inset-x-0 top-0 z-10 mx-4 mt-3 px-3 py-2 rounded-md bg-ember-500/10 ring-1 ring-ember-500/30 text-[11px] text-ember-400">
            {error.message}
          </div>
        )}
        {!activeId && !error && (
          <div className="absolute inset-0 grid place-items-center text-center text-sm text-ink-300">
            <div>
              <CircleNotch size={22} className="mx-auto animate-spin text-ink-400" weight="bold" />
              <p className="mt-2">Waiting for a workflow…</p>
            </div>
          </div>
        )}
        {detail && (
          <ReactFlow
            nodes={nodes as FlowNode[]}
            edges={edges}
            nodeTypes={NODE_TYPES}
            fitView
            fitViewOptions={{ padding: 0.2 }}
            proOptions={{ hideAttribution: false }}
            onNodeClick={(_, node) => setSelectedStepId(node.id)}
            defaultEdgeOptions={{ type: "smoothstep" }}
            nodesDraggable
            nodesConnectable={false}
            elementsSelectable
            panOnScroll
            zoomOnScroll
            minZoom={0.4}
            maxZoom={1.6}
          >
            <Background gap={24} size={1} color="rgba(255,255,255,0.04)" />
            <Controls showInteractive={false} />
          </ReactFlow>
        )}
      </div>

      <AnimatePresence>
        {selectedStep && (
          <motion.aside
            initial={{ x: "100%" }}
            animate={{ x: 0 }}
            exit={{ x: "100%" }}
            transition={motionTokens.spring.default}
            className="absolute top-0 right-0 h-full w-[320px] surface border-l border-ink-700/60 z-20 flex flex-col"
            data-testid="step-detail"
          >
            <div className="p-4 border-b border-ink-700/60 flex items-center gap-2">
              <h3 className="font-display text-sm text-ink-50 flex-1">{selectedStep.name}</h3>
              <button
                type="button"
                onClick={() => setSelectedStepId(null)}
                className="text-ink-300 hover:text-ink-50 focus-ring rounded p-1"
                aria-label="Close step detail"
              >
                <X size={14} weight="bold" />
              </button>
            </div>
            <div className="p-4 space-y-3 text-xs">
              <Row label="step id" value={selectedStep.step_id} mono />
              <Row label="agent" value={selectedStep.agent_id} mono />
              <Row label="status">
                <StatusBadge status={selectedStep.status} />
              </Row>
              <Row label="attempts" value={selectedStep.attempts.toString()} mono />
              <Row label="started" value={formatTime(selectedStep.started_at)} />
              <Row label="finished" value={formatTime(selectedStep.finished_at)} />
              {selectedStep.error && (
                <div className="rounded-md bg-ember-500/10 ring-1 ring-ember-500/30 p-2 text-ember-400 font-mono text-[11px] whitespace-pre-wrap break-words">
                  {selectedStep.error}
                </div>
              )}
              {selectedStep.output_preview && (
                <div className="space-y-1.5">
                  <div className="data-label">output preview</div>
                  <pre className="rounded-md bg-ink-800/60 p-2 font-mono text-[11px] text-ink-100 whitespace-pre-wrap break-words max-h-48 overflow-y-auto">
                    {truncate(selectedStep.output_preview, 600)}
                  </pre>
                </div>
              )}
            </div>
          </motion.aside>
        )}
      </AnimatePresence>

      {detail && (
        <footer className="px-4 py-2 border-t border-ink-700/60 text-[10px] text-ink-300 font-mono flex items-center gap-2">
          <ArrowRight size={11} weight="bold" />
          <span>updated {formatRelative(detail.updated_at)}</span>
          <span className="ml-auto">{detail.owner_agent}</span>
        </footer>
      )}
    </section>
  );
}

function Row({
  label,
  value,
  mono,
  children,
}: {
  label: string;
  value?: string;
  mono?: boolean;
  children?: React.ReactNode;
}): JSX.Element {
  return (
    <div className="flex items-start justify-between gap-3">
      <span className="data-label flex-shrink-0">{label}</span>
      <div className={cn("text-right text-xs", mono && "font-mono text-ink-100")}>
        {children ?? value}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Custom node renderer
// ---------------------------------------------------------------------------

interface StepNodeData extends Record<string, unknown> {
  step: WorkflowStepStatus;
  label: string;
  status: string;
  agent: string;
}

function StepNode({ data, selected }: NodeProps<FlowNode<StepNodeData, "step">>): JSX.Element {
  const step = data.step;
  return (
    <div
      className={cn(
        "rounded-xl px-3 py-2 ring-1 transition-shadow w-[220px] h-[96px] flex flex-col justify-between",
        "bg-ink-900/80 backdrop-blur-sm",
        selected ? "ring-amber-glow shadow-ring-amber" : "ring-ink-700/60",
      )}
      data-testid="step-node"
      data-status={step.status}
    >
      <Handle
        type="target"
        position={Position.Left}
        className="!bg-amber-glow !w-2 !h-2 !border-0"
      />
      <div className="flex items-center justify-between">
        <StatusBadge status={step.status} size="sm" />
        <span className="text-[9px] text-ink-300 font-mono uppercase tracking-widest">
          {step.attempts > 1 ? `×${step.attempts}` : "—"}
        </span>
      </div>
      <div>
        <div className="text-xs font-semibold text-ink-50 leading-tight tracking-tight truncate">
          {step.name}
        </div>
        <div className="text-[10px] text-ink-300 font-mono truncate">{step.agent_id}</div>
      </div>
      <Handle
        type="source"
        position={Position.Right}
        className="!bg-amber-glow !w-2 !h-2 !border-0"
      />
    </div>
  );
}

const NODE_TYPES = { step: StepNode } as const;

// silence unused-export warning for layout constants that may be reused
void DAG_NODE_WIDTH;
void DAG_NODE_HEIGHT;
