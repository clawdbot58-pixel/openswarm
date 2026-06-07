/**
 * `SwarmOverview` — grid of agent cards with filtering.
 *
 * Density-aware: filter row at the top, the rest is the grid.  A
 * stagger-cascade reveal on first paint gives the dashboard a
 * heartbeat-like entrance.
 */

import { useMemo, useState } from "react";
import { Funnel, Pulse, UsersThree } from "@phosphor-icons/react";
import { motion } from "framer-motion";
import { AgentCard } from "../components/AgentCard";
import { AgentCardSkeleton } from "../components/Skeleton";
import { useAgents } from "../hooks/useAgents";
import { cn } from "../utils/cn";
import { motion as motionTokens } from "../theme";
import type { AgentStatus, ViewConfig } from "../types";

interface SwarmOverviewProps {
  config: ViewConfig;
  onAgentClick?: (agentId: string) => void;
}

const STATUS_OPTIONS: AgentStatus[] = ["ready", "busy", "idle", "error", "zombie", "initializing"];

export function SwarmOverviewView({ config, onAgentClick }: SwarmOverviewProps): JSX.Element {
  const { agents, loading, error, refresh } = useAgents();
  const [statusFilter, setStatusFilter] = useState<AgentStatus | "all">("all");
  const [query, setQuery] = useState("");

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    return agents.filter((a) => {
      if (statusFilter !== "all" && a.status !== statusFilter) return false;
      if (!q) return true;
      return (
        a.agent_id.toLowerCase().includes(q) ||
        a.human_readable_name.toLowerCase().includes(q) ||
        a.role.toLowerCase().includes(q) ||
        a.category.toLowerCase().includes(q) ||
        a.tags.some((t) => t.toLowerCase().includes(q))
      );
    });
  }, [agents, statusFilter, query]);

  const counts = useMemo(() => {
    const c: Record<string, number> = {};
    for (const a of agents) c[a.status] = (c[a.status] ?? 0) + 1;
    return c;
  }, [agents]);

  return (
    <section className="h-full flex flex-col" data-testid="swarm-overview">
      <header className="px-4 pt-3 pb-2 flex flex-wrap items-center gap-2">
        <div className="flex items-center gap-2">
          <UsersThree size={14} weight="duotone" className="text-amber-glow" />
          <h2 className="panel-title">Swarm</h2>
          <span className="text-[10px] text-ink-300 font-mono">{agents.length} registered</span>
        </div>
        <div className="flex flex-wrap items-center gap-1.5 ml-2">
          {(["all", ...STATUS_OPTIONS] as const).map((status) => {
            const count = status === "all" ? agents.length : counts[status] ?? 0;
            const active = statusFilter === status;
            return (
              <button
                key={status}
                type="button"
                onClick={() => setStatusFilter(status)}
                data-testid={`filter-status-${status}`}
                className={cn(
                  "pill ring-1 transition-colors",
                  active
                    ? "bg-amber-glow/15 text-amber-glow ring-amber-glow/40"
                    : "bg-ink-900/40 text-ink-300 ring-ink-700/60 hover:text-ink-100",
                )}
              >
                <Pulse size={9} weight="fill" className={active ? "text-amber-glow" : "text-ink-500"} />
                <span className="capitalize">{status}</span>
                <span className="font-mono text-[10px] text-ink-300">{count}</span>
              </button>
            );
          })}
        </div>
        <div className="ml-auto flex items-center gap-2">
          <input
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Filter agents…"
            className="bg-ink-900/60 ring-1 ring-inset ring-ink-700/60 rounded h-7 px-2 text-[11px] text-ink-100 focus:outline-none focus:ring-amber-glow/60 w-44"
          />
          <button
            type="button"
            onClick={() => void refresh()}
            className="text-[10px] uppercase tracking-widest text-ink-300 hover:text-amber-glow focus-ring"
          >
            refresh
          </button>
        </div>
      </header>

      {error && (
        <div className="mx-4 mt-2 px-3 py-2 rounded-md bg-ember-500/10 ring-1 ring-ember-500/30 text-[11px] text-ember-400">
          {error.message}
        </div>
      )}

      <div className="flex-1 overflow-y-auto p-4">
        {loading ? (
          <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4 gap-3">
            {Array.from({ length: 6 }).map((_, i) => (
              <AgentCardSkeleton key={i} />
            ))}
          </div>
        ) : filtered.length === 0 ? (
          <EmptyState query={query} statusFilter={statusFilter} config={config} />
        ) : (
          <motion.div
            className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4 gap-3"
            initial="hidden"
            animate="visible"
            variants={{
              hidden: {},
              visible: { transition: { staggerChildren: 0.04, ...motionTokens.spring.gentle } },
            }}
          >
            {filtered.map((agent, idx) => (
              <AgentCard
                key={agent.agent_id}
                agent={agent}
                index={idx}
                onClick={(a) => onAgentClick?.(a.agent_id)}
              />
            ))}
          </motion.div>
        )}
      </div>
    </section>
  );
}

function EmptyState({
  query,
  statusFilter,
  config,
}: {
  query: string;
  statusFilter: string;
  config: ViewConfig;
}): JSX.Element {
  return (
    <div className="h-full grid place-items-center">
      <div className="text-center max-w-sm">
        <div className="mx-auto h-10 w-10 rounded-full bg-ink-800/60 grid place-items-center mb-3">
          <Funnel size={16} weight="duotone" className="text-ink-300" />
        </div>
        <h3 className="text-sm font-display text-ink-50">No agents match</h3>
        <p className="mt-1 text-xs text-ink-300 leading-relaxed">
          {query ? (
            <>
              Filter <span className="font-mono text-ink-100">"{query}"</span>{" "}
              returned nothing.
            </>
          ) : (
            <>
              No agents in status{" "}
              <span className="font-mono text-ink-100">{statusFilter}</span>.
            </>
          )}
        </p>
        <p className="mt-3 text-[10px] text-ink-400 font-mono">
          View: {config.view_id}
        </p>
      </div>
    </div>
  );
}
