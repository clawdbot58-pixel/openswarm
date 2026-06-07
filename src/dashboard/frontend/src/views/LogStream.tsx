/**
 * `LogStream` — real-time, filtered, auto-scrolling log feed.
 *
 * Subscribes to the WebSocket to append new entries and keeps a
 * rolling buffer.  Auto-scroll only if the user is "at the bottom"
 * — otherwise respect their scroll position so they can read history
 * while new lines arrive.
 */

import { useEffect, useMemo, useRef, useState } from "react";
import { motion, AnimatePresence } from "framer-motion";
import { ArrowDown, Circle, RadioButton } from "@phosphor-icons/react";
import { LogFilterBar } from "../components/LogFilterBar";
import { useLogs, EMPTY_LOG_FILTER, type LogFilter } from "../hooks/useLogs";
import { cn } from "../utils/cn";
import { formatTime } from "../utils/format";
import { motion as motionTokens } from "../theme";
import type { LogEntry, ViewConfig } from "../types";

interface LogStreamProps {
  config: ViewConfig;
}

const ENVELOPE_COLORS: Record<string, string> = {
  request: "text-sea-400",
  response: "text-moss-400",
  event: "text-amber-glow",
  error: "text-ember-400",
  heartbeat: "text-ink-300",
  chunk: "text-plum-400",
  intent: "text-amber-pulse",
};

const SEVERITY_RANK: Record<string, number> = {
  critical: 5,
  error: 4,
  warning: 3,
  info: 2,
  debug: 1,
};

export function LogStreamView({ config }: LogStreamProps): JSX.Element {
  const { logs, loading, filter, setFilter, clear } = useLogs({
    initialLimit: 200,
    maxBuffer: 2000,
  });
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const [autoScroll, setAutoScroll] = useState(true);
  const [unread, setUnread] = useState(0);

  const filtered = useMemo(() => {
    return logs.filter((log) => matches(log, filter));
  }, [logs, filter]);

  const agents = useMemo(() => uniq(logs.map((l) => l.sender)), [logs]);
  const types = useMemo(() => uniq(logs.map((l) => l.envelope_type)), [logs]);
  const severities = useMemo(
    () => uniq(logs.map((l) => l.severity)).sort((a, b) => (SEVERITY_RANK[b] ?? 0) - (SEVERITY_RANK[a] ?? 0)),
    [logs],
  );

  // Auto-scroll when at bottom; otherwise count unread lines.
  useEffect(() => {
    const el = scrollRef.current;
    if (!el) return;
    if (autoScroll) {
      el.scrollTop = el.scrollHeight;
    } else {
      setUnread((u) => u + 1);
    }
  }, [filtered.length, autoScroll]);

  const handleScroll = () => {
    const el = scrollRef.current;
    if (!el) return;
    const distance = el.scrollHeight - (el.scrollTop + el.clientHeight);
    const atBottom = distance < 24;
    if (atBottom !== autoScroll) setAutoScroll(atBottom);
    if (atBottom) setUnread(0);
  };

  return (
    <section className="h-full flex flex-col" data-testid="log-stream">
      <LogFilterBar
        filter={filter}
        onChange={setFilter}
        agents={agents}
        envelopeTypes={types}
        severities={severities}
        totalCount={logs.length}
        visibleCount={filtered.length}
        onClear={() => {
          setFilter(EMPTY_LOG_FILTER);
          clear();
        }}
      />
      <div className="relative flex-1 min-h-0">
        <div
          ref={scrollRef}
          onScroll={handleScroll}
          data-testid="log-list"
          className="absolute inset-0 overflow-y-auto font-mono text-[11px] leading-[1.55]"
        >
          {loading ? (
            <div className="p-6 text-center text-ink-300">Loading recent envelopes…</div>
          ) : filtered.length === 0 ? (
            <div className="p-6 text-center text-ink-300 text-sm">
              No envelopes match the current filter.
            </div>
          ) : (
            <ul className="divide-y divide-ink-700/30">
              <AnimatePresence initial={false}>
                {filtered.map((entry) => (
                  <LogLine key={entry.envelope_id} entry={entry} />
                ))}
              </AnimatePresence>
            </ul>
          )}
        </div>

        {!autoScroll && unread > 0 && (
          <motion.button
            type="button"
            onClick={() => {
              setAutoScroll(true);
              setUnread(0);
            }}
            initial={{ opacity: 0, y: 6 }}
            animate={{ opacity: 1, y: 0 }}
            transition={motionTokens.spring.gentle}
            className="absolute right-4 bottom-4 inline-flex items-center gap-1.5 rounded-full px-3 h-8 bg-amber-glow/90 text-ink-950 text-xs font-semibold shadow-diffuse focus-ring"
            data-testid="jump-to-latest"
          >
            <ArrowDown size={12} weight="bold" />
            {unread} new
          </motion.button>
        )}
      </div>
      <footer className="px-4 py-2 border-t border-ink-700/60 flex items-center gap-2 text-[10px] text-ink-300 font-mono">
        <RadioButton size={11} weight="fill" className="text-amber-glow" />
        <span>stream</span>
        <span>·</span>
        <span>{filtered.length} of {logs.length} envelopes</span>
        <span className="ml-auto">view: {config.view_id}</span>
      </footer>
    </section>
  );
}

function matches(log: LogEntry, filter: LogFilter): boolean {
  if (filter.agent && log.sender !== filter.agent) return false;
  if (filter.workflowId && log.workflow_id !== filter.workflowId) return false;
  if (filter.envelopeType && log.envelope_type !== filter.envelopeType) return false;
  if (filter.severity && log.severity !== filter.severity) return false;
  if (filter.search && !log.payload_preview.toLowerCase().includes(filter.search.toLowerCase())) return false;
  return true;
}

function uniq(values: string[]): string[] {
  return Array.from(new Set(values.filter(Boolean))).sort();
}

function LogLine({ entry }: { entry: LogEntry }): JSX.Element {
  const color = ENVELOPE_COLORS[entry.envelope_type] ?? "text-ink-200";
  const isHighSeverity = entry.severity === "error" || entry.severity === "critical";
  return (
    <motion.li
      layout="position"
      initial={{ opacity: 0, x: -4 }}
      animate={{ opacity: 1, x: 0 }}
      transition={motionTokens.spring.gentle}
      className={cn(
        "px-3 py-1 flex items-start gap-3 hover:bg-ink-800/30 transition-colors",
        isHighSeverity && "bg-ember-500/5 hover:bg-ember-500/10",
      )}
      data-testid="log-line"
      data-severity={entry.severity}
    >
      <span className="text-ink-400 tabular-nums whitespace-nowrap">{formatTime(entry.timestamp)}</span>
      <span className={cn("uppercase tracking-widest text-[9px] font-semibold w-14", color)}>
        {entry.envelope_type}
      </span>
      <span className="text-ink-200 font-mono whitespace-nowrap w-40 truncate" title={entry.sender}>
        {entry.sender}
      </span>
      <Circle size={4} weight="fill" className="mt-1.5 text-ink-500 flex-shrink-0" />
      <span className="text-ink-100 flex-1 break-words">{entry.payload_preview || <em className="text-ink-400">no preview</em>}</span>
    </motion.li>
  );
}
