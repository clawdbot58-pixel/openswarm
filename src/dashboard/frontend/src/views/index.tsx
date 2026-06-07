/**
 * `View` — generic dispatcher.  Maps a `ViewConfig.view_type` to a
 * concrete renderer.  Adding a new view type is a one-line change in
 * the switch below (and the new component).
 */

import { SquaresFour } from "@phosphor-icons/react";
import { SwarmOverviewView } from "./SwarmOverview";
import { WorkflowDAGView } from "./WorkflowDAG";
import { LogStreamView } from "./LogStream";
import { WorkspaceExplorerView } from "./WorkspaceExplorer";
import { AgentDetailView } from "./AgentDetail";
import { MetricsView } from "./Metrics";
import { GenericDataView } from "./GenericDataView";
import type { ViewConfig } from "../types";

interface ViewProps {
  config: ViewConfig;
  onAgentClick?: (agentId: string) => void;
}

export function View({ config, onAgentClick }: ViewProps): JSX.Element {
  switch (config.view_type) {
    case "swarm_overview":
      return <SwarmOverviewView config={config} onAgentClick={onAgentClick} />;
    case "workflow_dag":
      return <WorkflowDAGView config={config} />;
    case "log_stream":
      return <LogStreamView config={config} />;
    case "workspace_explorer":
      return <WorkspaceExplorerView config={config} />;
    case "agent_detail":
      return <AgentDetailView config={config} />;
    case "metrics":
      return <MetricsView config={config} />;
    case "custom":
    default:
      return <GenericDataView config={config} />;
  }
}

/**
 * A small fallback used by the layout shell when a `panel.view_id`
 * has no matching `ViewConfig` in the registry.
 */
export function UnknownView({ viewId }: { viewId: string }): JSX.Element {
  return (
    <div className="h-full grid place-items-center text-center text-sm text-ink-300">
      <div>
        <SquaresFour size={22} className="mx-auto text-ink-400 mb-2" weight="duotone" />
        <p>No view registered for</p>
        <p className="mt-1 font-mono text-[11px] text-ink-100">{viewId}</p>
      </div>
    </div>
  );
}
