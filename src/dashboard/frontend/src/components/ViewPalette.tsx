/**
 * `ViewPalette` — left rail listing every available view in the
 * ConfigAPI.  Used by the LayoutBuilder to drag views onto the grid.
 *
 * Pure presentational.  Drag handling is opt-in via the optional
 * `onDragStart` prop.
 */

import { useState, useMemo } from "react";
import { Cube, MagnifyingGlass, Plus } from "@phosphor-icons/react";
import { motion, AnimatePresence } from "framer-motion";
import { cn } from "../utils/cn";
import { motion as motionTokens } from "../theme";
import type { ViewConfig } from "../types";

interface ViewPaletteProps {
  views: ViewConfig[];
  onDragStart?: (view: ViewConfig) => void;
  onCreate?: () => void;
  className?: string;
}

const TYPE_LABEL: Record<string, string> = {
  swarm_overview: "Swarm",
  workflow_dag: "DAG",
  log_stream: "Logs",
  workspace_explorer: "Workspace",
  agent_detail: "Agent",
  metrics: "Metrics",
  custom: "Custom",
};

export function ViewPalette({ views, onDragStart, onCreate, className }: ViewPaletteProps): JSX.Element {
  const [query, setQuery] = useState("");
  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return views;
    return views.filter(
      (v) => v.name.toLowerCase().includes(q) || v.view_type.toLowerCase().includes(q),
    );
  }, [query, views]);

  return (
    <aside className={cn("surface-quiet flex flex-col", className)}>
      <header className="p-3 border-b border-ink-700/60 flex items-center gap-2">
        <Cube size={14} weight="duotone" className="text-amber-glow" />
        <h2 className="panel-title flex-1">View Palette</h2>
        {onCreate && (
          <button
            type="button"
            onClick={onCreate}
            className="inline-flex items-center gap-1 text-[10px] text-amber-glow hover:text-amber-pulse uppercase tracking-widest focus-ring"
            data-testid="palette-create"
          >
            <Plus size={11} weight="bold" /> New
          </button>
        )}
      </header>

      <div className="p-2 border-b border-ink-700/40">
        <div className="relative">
          <MagnifyingGlass
            size={11}
            weight="bold"
            className="absolute left-2 top-1/2 -translate-y-1/2 text-ink-400 pointer-events-none"
          />
          <input
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Filter views"
            className="w-full bg-ink-800/60 ring-1 ring-inset ring-ink-700/60 rounded h-7 pl-7 pr-2 text-[11px] text-ink-100 focus:outline-none focus:ring-amber-glow/60"
          />
        </div>
      </div>

      <ul className="flex-1 overflow-y-auto p-2 space-y-1.5" data-testid="view-palette-list">
        <AnimatePresence initial={false}>
          {filtered.map((view, idx) => (
            <motion.li
              key={view.view_id}
              layout
              initial={{ opacity: 0, y: 6 }}
              animate={{ opacity: 1, y: 0 }}
              exit={{ opacity: 0, y: -4 }}
              transition={{ ...motionTokens.spring.gentle, delay: Math.min(idx, 12) * 0.02 }}
              drag={false}
              draggable={Boolean(onDragStart)}
              onDragStartCapture={
                onDragStart
                  ? (e: React.DragEvent<HTMLLIElement>) => {
                      onDragStart(view);
                      e.dataTransfer.setData("application/x-os-view", view.view_id);
                      e.dataTransfer.effectAllowed = "copy";
                    }
                  : undefined
              }
              className={cn(
                "rounded-md p-2.5 ring-1 ring-inset ring-ink-700/60 bg-ink-900/40 cursor-grab",
                "hover:ring-amber-glow/40 transition-shadow select-none",
              )}
              data-testid="palette-item"
              data-view-id={view.view_id}
              data-view-type={view.view_type}
            >
              <div className="flex items-center justify-between gap-2">
                <span className="text-xs font-medium text-ink-50 truncate">{view.name}</span>
                <span className="text-[9px] uppercase tracking-widest text-amber-glow font-mono">
                  {TYPE_LABEL[view.view_type] ?? view.view_type}
                </span>
              </div>
              {view.description && (
                <p className="mt-0.5 text-[10px] text-ink-300 line-clamp-2">{view.description}</p>
              )}
              <div className="mt-1 text-[9px] text-ink-400 font-mono">
                {view.data_sources.length} source{view.data_sources.length === 1 ? "" : "s"} · {view.refresh_interval_ms}ms
              </div>
            </motion.li>
          ))}
        </AnimatePresence>
        {filtered.length === 0 && (
          <li className="text-[11px] text-ink-300 p-3 text-center">
            No views match{" "}
            <span className="text-ink-100 font-mono">{query || "your filter"}</span>.
          </li>
        )}
      </ul>
    </aside>
  );
}
