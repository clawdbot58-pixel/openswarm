/**
 * `GenericDataView` — the fallback for `view_type: custom`.
 *
 * Renders each entry of `config.data_sources` as a DataTable.  The
 * ConfigAPI is the source of truth on what data exists; the
 * dashboard is intentionally dumb about its shape.
 */

import { useEffect, useMemo, useState } from "react";
import { motion } from "framer-motion";
import { Database, Pulse } from "@phosphor-icons/react";
import { DataTable } from "../components/DataTable";
import { motion as motionTokens } from "../theme";
import type { ViewConfig } from "../types";

interface GenericDataViewProps {
  config: ViewConfig;
}

interface FetchState {
  source: string;
  data: unknown[] | null;
  error: string | null;
  loading: boolean;
}

async function fetchSource(source: string): Promise<unknown[]> {
  const url = source.startsWith("http") ? source : source.startsWith("/") ? source : `/${source}`;
  const res = await fetch(url, { headers: { Accept: "application/json" } });
  if (!res.ok) {
    throw new Error(`${res.status} ${res.statusText}`);
  }
  const json = await res.json();
  return Array.isArray(json) ? json : [json];
}

export function GenericDataView({ config }: GenericDataViewProps): JSX.Element {
  const sources = config.data_sources;
  const [states, setStates] = useState<FetchState[]>(() =>
    sources.map((s) => ({ source: s, data: null, error: null, loading: true })),
  );

  useEffect(() => {
    let cancelled = false;
    setStates(sources.map((s) => ({ source: s, data: null, error: null, loading: true })));
    (async () => {
      const next = await Promise.all(
        sources.map(async (source) => {
          try {
            const data = await fetchSource(source);
            return { source, data, error: null, loading: false };
          } catch (err) {
            return {
              source,
              data: null,
              error: err instanceof Error ? err.message : String(err),
              loading: false,
            };
          }
        }),
      );
      if (!cancelled) setStates(next);
    })();
    return () => {
      cancelled = true;
    };
  }, [sources.join("|")]);

  const allLoaded = useMemo(() => states.every((s) => !s.loading), [states]);

  return (
    <section className="h-full flex flex-col" data-testid="generic-data-view">
      <header className="px-4 py-3 border-b border-ink-700/60 flex items-center gap-2 flex-wrap">
        <Database size={14} weight="duotone" className="text-amber-glow" />
        <h2 className="panel-title">{config.name || "Custom View"}</h2>
        <span className="text-[10px] text-ink-300 font-mono">{sources.length} source{sources.length === 1 ? "" : "s"}</span>
        <span className="ml-auto inline-flex items-center gap-1 text-[10px] text-amber-glow">
          <Pulse size={11} weight="fill" />
          {allLoaded ? "synced" : "loading…"}
        </span>
      </header>

      {config.description && (
        <p className="px-4 pt-3 text-xs text-ink-300 leading-relaxed">{config.description}</p>
      )}

      <div className="flex-1 min-h-0 overflow-y-auto p-4 space-y-4">
        {states.map((state, idx) => (
          <motion.div
            key={state.source}
            initial={{ opacity: 0, y: 6 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ ...motionTokens.spring.gentle, delay: idx * 0.04 }}
          >
            {state.error ? (
              <div className="surface p-4">
                <div className="flex items-center gap-2 text-[11px] text-ember-400 font-mono">
                  <Database size={12} weight="duotone" />
                  <span className="truncate">{state.source}</span>
                </div>
                <p className="mt-2 text-xs text-ink-300">{state.error}</p>
              </div>
            ) : state.data === null ? (
              <div className="surface p-4 space-y-2">
                <div className="data-label">{state.source}</div>
                <div className="skeleton h-3 w-1/3" />
                <div className="skeleton h-3 w-2/3" />
                <div className="skeleton h-3 w-1/2" />
              </div>
            ) : (
              <DataTable title={state.source} data={state.data} />
            )}
          </motion.div>
        ))}
        {sources.length === 0 && (
          <div className="surface p-6 text-center text-sm text-ink-300">
            This view has no data sources.
          </div>
        )}
      </div>
    </section>
  );
}
