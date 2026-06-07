/**
 * `AgentDetail` — single agent deep-dive.
 *
 * Shows the manifest, current status, recent memory, and recent
 * errors.  If `config.data_sources` includes an agent id segment
 * (e.g. `/api/agents/coder-python-fast`), we use that; otherwise
 * show the first available agent.
 */

import { useEffect, useState } from "react";
import { motion } from "framer-motion";
import { Brain, Clock, Pulse, Stack, UserCircle, Warning } from "@phosphor-icons/react";
import { agentsApi, ApiClientError } from "../api";
import { StatusBadge } from "../components/StatusBadge";
import { useAgents } from "../hooks/useAgents";
import { cn } from "../utils/cn";
import { formatCount, formatDateTime, formatRelative, truncate } from "../utils/format";
import { motion as motionTokens } from "../theme";
import type { AgentDetail as AgentDetailModel, ViewConfig } from "../types";

interface AgentDetailProps {
  config: ViewConfig;
}

function extractAgentId(config: ViewConfig, fallback?: string): string | undefined {
  for (const source of config.data_sources) {
    const match = source.match(/\/api\/agents\/([^/?#]+)/);
    if (match) return match[1];
  }
  return fallback;
}

export function AgentDetailView({ config }: AgentDetailProps): JSX.Element {
  const { agents } = useAgents();
  const targetId = extractAgentId(config, agents[0]?.agent_id);
  const [detail, setDetail] = useState<AgentDetailModel | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!targetId) return;
    let cancelled = false;
    setLoading(true);
    agentsApi
      .detail(targetId)
      .then((d) => {
        if (!cancelled) setDetail(d);
      })
      .catch((err) => {
        if (!cancelled) setError(err instanceof ApiClientError ? err.message : String(err));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [targetId]);

  if (!targetId) {
    return (
      <div className="h-full grid place-items-center text-sm text-ink-300">
        <div className="text-center">
          <UserCircle size={22} className="mx-auto text-ink-400 mb-2" weight="duotone" />
          No agent selected.
        </div>
      </div>
    );
  }

  if (loading && !detail) {
    return <div className="h-full grid place-items-center text-sm text-ink-300">Loading agent…</div>;
  }

  if (error && !detail) {
    return <div className="h-full grid place-items-center text-sm text-ember-400">{error}</div>;
  }

  if (!detail) return <></>;

  const manifest = (detail.manifest ?? {}) as Record<string, unknown>;

  return (
    <section className="h-full flex flex-col" data-testid="agent-detail">
      <header className="px-4 py-4 border-b border-ink-700/60 flex flex-wrap items-center gap-3">
        <div className="h-10 w-10 rounded-full bg-ink-800/60 grid place-items-center ring-1 ring-amber-glow/30">
          <UserCircle size={22} weight="duotone" className="text-amber-glow" />
        </div>
        <div className="min-w-0">
          <h2 className="text-sm font-display font-semibold text-ink-50 truncate">
            {(manifest.human_readable_name as string | undefined) ?? detail.agent_id}
          </h2>
          <div className="text-[11px] text-ink-300 font-mono">
            {detail.agent_id} · {(manifest.role as string | undefined) ?? "—"}
          </div>
        </div>
        <div className="ml-auto flex items-center gap-2">
          <StatusBadge status={detail.status} />
          <span className="text-[10px] font-mono text-ink-300">
            {detail.connected_ws ? "ws ok" : "ws down"}
          </span>
        </div>
      </header>

      <div className="flex-1 min-h-0 overflow-y-auto p-4 space-y-4">
        <Section title="Vital signs" icon={<Pulse size={12} weight="duotone" className="text-amber-glow" />}>
          <div className="grid grid-cols-2 sm:grid-cols-4 gap-2">
            <Stat label="Heartbeat" value={`${detail.heartbeat_age_seconds}s ago`} />
            <Stat label="Queue" value={formatCount(detail.pending_queue_size)} />
            <Stat label="Current task" value={truncate(detail.current_task ?? "—", 32)} />
            <Stat label="Registered" value={formatRelative(detail.registered_at)} />
          </div>
        </Section>

        <Section title="Manifest" icon={<Stack size={12} weight="duotone" className="text-amber-glow" />}>
          <pre className="rounded-md bg-ink-800/60 p-3 font-mono text-[11px] text-ink-100 whitespace-pre-wrap break-words max-h-72 overflow-y-auto">
            {JSON.stringify(manifest, null, 2)}
          </pre>
        </Section>

        <Section title="Recent memory" icon={<Brain size={12} weight="duotone" className="text-amber-glow" />}>
          {detail.recent_memory.length === 0 ? (
            <p className="text-xs text-ink-300">No memory items in the working window.</p>
          ) : (
            <ul className="space-y-1.5">
              {detail.recent_memory.slice(0, 12).map((item) => (
                <li
                  key={item.id}
                  className="rounded-md bg-ink-800/40 ring-1 ring-ink-700/40 p-2.5 text-[11px]"
                >
                  <div className="flex items-center justify-between text-ink-300 font-mono">
                    <span className="text-amber-glow uppercase tracking-widest text-[9px]">
                      {item.type}
                    </span>
                    <span>{formatDateTime(item.timestamp)}</span>
                  </div>
                  <div className="mt-1 text-ink-100 font-mono break-words">
                    {typeof item.content === "string"
                      ? truncate(item.content, 240)
                      : truncate(JSON.stringify(item.content), 240)}
                  </div>
                </li>
              ))}
            </ul>
          )}
        </Section>

        {detail.recent_errors.length > 0 && (
          <Section
            title="Recent errors"
            icon={<Warning size={12} weight="duotone" className="text-ember-400" />}
            tone="warning"
          >
            <ul className="space-y-1.5">
              {detail.recent_errors.slice(0, 8).map((log) => (
                <li
                  key={log.envelope_id}
                  className="rounded-md bg-ember-500/8 ring-1 ring-ember-500/20 p-2.5 text-[11px] font-mono"
                >
                  <div className="flex items-center justify-between text-ink-300">
                    <span className="uppercase tracking-widest text-[9px] text-ember-400">
                      {log.envelope_type}
                    </span>
                    <span>{formatRelative(log.timestamp)}</span>
                  </div>
                  <div className="mt-1 text-ink-100 break-words">
                    {truncate(log.payload_preview, 240)}
                  </div>
                </li>
              ))}
            </ul>
          </Section>
        )}
      </div>

      <footer className="px-4 py-2 border-t border-ink-700/60 text-[10px] text-ink-300 font-mono flex items-center gap-2">
        <Clock size={11} weight="bold" />
        last heartbeat {formatRelative(detail.last_heartbeat)}
        <span className="ml-auto">view: {config.view_id}</span>
      </footer>
    </section>
  );
}

function Section({
  title,
  icon,
  tone,
  children,
}: {
  title: string;
  icon: React.ReactNode;
  tone?: "warning";
  children: React.ReactNode;
}): JSX.Element {
  return (
    <motion.section
      initial={{ opacity: 0, y: 6 }}
      animate={{ opacity: 1, y: 0 }}
      transition={motionTokens.spring.gentle}
      className={cn(
        "rounded-xl ring-1 ring-ink-700/60 bg-ink-900/40 p-3",
        tone === "warning" && "ring-ember-500/20",
      )}
    >
      <h3 className="data-label flex items-center gap-1.5 mb-2">
        {icon}
        {title}
      </h3>
      {children}
    </motion.section>
  );
}

function Stat({ label, value }: { label: string; value: string }): JSX.Element {
  return (
    <div className="rounded-md bg-ink-800/40 ring-1 ring-ink-700/40 p-2.5">
      <div className="data-label text-[9px]">{label}</div>
      <div className="mt-0.5 text-sm font-display text-ink-50 tabular-nums truncate">{value}</div>
    </div>
  );
}
