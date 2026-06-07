/**
 * `Metrics` — system metrics dashboard.
 *
 * A high-level "swarm vital signs" panel: agent counts, workflow
 * counts, throughput, cost, uptime.  Live-updates via WebSocket.
 */

import { useMemo } from "react";
import { motion } from "framer-motion";
import {
  Coins,
  CurrencyDollar,
  Cpu,
  Pulse,
  Stack,
  Timer,
  UsersThree,
  Waveform as ActivityIcon,
} from "@phosphor-icons/react";
import { useMetrics } from "../hooks/useViews";
import { cn } from "../utils/cn";
import { formatCount, formatCurrency, formatDuration, formatPercent } from "../utils/format";
import { motion as motionTokens } from "../theme";
import type { ViewConfig } from "../types";

interface MetricsProps {
  config: ViewConfig;
}

export function MetricsView({ config }: MetricsProps): JSX.Element {
  const { metrics, loading, error, refresh } = useMetrics();

  const ratio = useMemo(() => {
    if (!metrics || metrics.total_agents === 0) return 0;
    return metrics.active_agents / metrics.total_agents;
  }, [metrics]);

  return (
    <section className="h-full flex flex-col" data-testid="metrics-view">
      <header className="px-4 py-3 border-b border-ink-700/60 flex items-center gap-2">
        <ActivityIcon size={14} weight="duotone" className="text-amber-glow" />
        <h2 className="panel-title">System Metrics</h2>
        <span className="ml-auto text-[10px] text-ink-300 font-mono">view: {config.view_id}</span>
        <button
          type="button"
          onClick={() => void refresh()}
          className="text-[10px] uppercase tracking-widest text-ink-300 hover:text-amber-glow focus-ring"
        >
          refresh
        </button>
      </header>

      {error && (
        <div className="mx-4 mt-3 px-3 py-2 rounded-md bg-ember-500/10 ring-1 ring-ember-500/30 text-[11px] text-ember-400">
          {error.message}
        </div>
      )}

      <div className="flex-1 min-h-0 overflow-y-auto p-4">
        {loading && !metrics ? (
          <SkeletonGrid />
        ) : !metrics ? (
          <div className="h-full grid place-items-center text-sm text-ink-300">No metrics yet.</div>
        ) : (
          <div className="space-y-4">
            <div className="grid grid-cols-2 lg:grid-cols-4 gap-3">
              <MetricCard
                label="Active agents"
                value={formatCount(metrics.active_agents)}
                sub={`${metrics.total_agents} registered`}
                icon={<UsersThree size={14} weight="duotone" />}
                tone="moss"
              />
              <MetricCard
                label="Running workflows"
                value={formatCount(metrics.running_workflows)}
                sub={`${metrics.completed_workflows} done / ${metrics.failed_workflows} failed`}
                icon={<Stack size={14} weight="duotone" />}
                tone="amber"
              />
              <MetricCard
                label="Throughput"
                value={`${metrics.messages_per_minute.toFixed(1)}`}
                sub="envelopes / min"
                icon={<Pulse size={14} weight="duotone" />}
                tone="sea"
              />
              <MetricCard
                label="Loop latency"
                value={`${Math.round(metrics.avg_loop_latency_ms)}ms`}
                sub="p50 over last minute"
                icon={<Timer size={14} weight="duotone" />}
                tone="plum"
              />
            </div>

            <div className="grid grid-cols-1 lg:grid-cols-3 gap-3">
              <RatioCard
                title="Agent utilization"
                ratio={ratio}
                detail={`${metrics.busy_agents} busy · ${metrics.idle_agents} idle · ${metrics.zombie_agents} zombie`}
                icon={<Cpu size={14} weight="duotone" className="text-amber-glow" />}
              />
              <CostCard
                cost={metrics.total_cost_today_usd}
                budget="daily envelope"
                icon={<CurrencyDollar size={14} weight="duotone" className="text-amber-glow" />}
              />
              <UptimeCard
                uptimeSeconds={metrics.uptime_seconds}
                queueTotal={metrics.queue_total}
                startedAt={metrics.started_at}
                icon={<ActivityIcon size={14} weight="duotone" className="text-amber-glow" />}
              />
            </div>

            <div className="grid grid-cols-2 lg:grid-cols-4 gap-3">
              <MiniStat label="Total workflows" value={formatCount(metrics.total_workflows)} />
              <MiniStat label="Completed" value={formatCount(metrics.completed_workflows)} />
              <MiniStat label="Failed" value={formatCount(metrics.failed_workflows)} />
              <MiniStat label="Queue depth" value={formatCount(metrics.queue_total)} />
            </div>
          </div>
        )}
      </div>
    </section>
  );
}

interface MetricCardProps {
  label: string;
  value: string;
  sub: string;
  icon: React.ReactNode;
  tone: "moss" | "amber" | "sea" | "plum";
}

const TONE_BG: Record<MetricCardProps["tone"], string> = {
  moss: "ring-moss-500/25 from-moss-500/8",
  amber: "ring-amber-glow/25 from-amber-glow/8",
  sea: "ring-sea-500/25 from-sea-500/8",
  plum: "ring-plum-500/25 from-plum-500/8",
};

const TONE_TEXT: Record<MetricCardProps["tone"], string> = {
  moss: "text-moss-400",
  amber: "text-amber-glow",
  sea: "text-sea-400",
  plum: "text-plum-400",
};

function MetricCard({ label, value, sub, icon, tone }: MetricCardProps): JSX.Element {
  return (
    <motion.div
      layout
      initial={{ opacity: 0, y: 6 }}
      animate={{ opacity: 1, y: 0 }}
      transition={motionTokens.spring.gentle}
      className={cn(
        "rounded-xl ring-1 bg-gradient-to-br to-transparent p-3",
        TONE_BG[tone],
      )}
    >
      <div className="flex items-center justify-between text-[10px] uppercase tracking-widest text-ink-300">
        <span>{label}</span>
        <span className={cn("opacity-80", TONE_TEXT[tone])}>{icon}</span>
      </div>
      <div className="mt-2 data-value tabular-nums">{value}</div>
      <div className="mt-1 text-[10px] text-ink-300 font-mono">{sub}</div>
    </motion.div>
  );
}

function RatioCard({
  title,
  ratio,
  detail,
  icon,
}: {
  title: string;
  ratio: number;
  detail: string;
  icon: React.ReactNode;
}): JSX.Element {
  const pct = Math.max(0, Math.min(1, ratio));
  return (
    <div className="rounded-xl ring-1 ring-ink-700/60 bg-ink-900/40 p-3">
      <div className="flex items-center gap-2 data-label">
        {icon}
        {title}
      </div>
      <div className="mt-2 flex items-end justify-between">
        <span className="data-value">{formatPercent(pct, 0)}</span>
        <span className="text-[10px] text-ink-300 font-mono">live</span>
      </div>
      <div className="mt-2 h-1.5 rounded-full bg-ink-800 overflow-hidden">
        <motion.div
          className="h-full bg-gradient-to-r from-amber-deep via-amber-glow to-amber-pulse"
          initial={{ width: 0 }}
          animate={{ width: `${pct * 100}%` }}
          transition={motionTokens.spring.default}
        />
      </div>
      <p className="mt-2 text-[10px] text-ink-300 font-mono">{detail}</p>
    </div>
  );
}

function CostCard({
  cost,
  budget,
  icon,
}: {
  cost: number;
  budget: string;
  icon: React.ReactNode;
}): JSX.Element {
  return (
    <div className="rounded-xl ring-1 ring-ink-700/60 bg-ink-900/40 p-3">
      <div className="flex items-center gap-2 data-label">
        {icon}
        Cost today
      </div>
      <div className="mt-2 flex items-end gap-2">
        <Coins size={18} weight="duotone" className="text-amber-pulse" />
        <span className="data-value">{formatCurrency(cost)}</span>
      </div>
      <p className="mt-1 text-[10px] text-ink-300 font-mono">{budget}</p>
    </div>
  );
}

function UptimeCard({
  uptimeSeconds,
  queueTotal,
  startedAt,
  icon,
}: {
  uptimeSeconds: number;
  queueTotal: number;
  startedAt: string;
  icon: React.ReactNode;
}): JSX.Element {
  return (
    <div className="rounded-xl ring-1 ring-ink-700/60 bg-ink-900/40 p-3">
      <div className="flex items-center gap-2 data-label">
        {icon}
        Uptime
      </div>
      <div className="mt-2 data-value">{formatDuration(uptimeSeconds)}</div>
      <p className="mt-1 text-[10px] text-ink-300 font-mono">
        since {new Date(startedAt).toISOString().slice(0, 16).replace("T", " ")} UTC
      </p>
      <p className="mt-1 text-[10px] text-ink-300 font-mono">queue · {formatCount(queueTotal)} pending</p>
    </div>
  );
}

function MiniStat({ label, value }: { label: string; value: string }): JSX.Element {
  return (
    <div className="rounded-md bg-ink-800/40 ring-1 ring-ink-700/40 p-2.5">
      <div className="data-label text-[9px]">{label}</div>
      <div className="mt-0.5 text-base font-display text-ink-50 tabular-nums">{value}</div>
    </div>
  );
}

function SkeletonGrid(): JSX.Element {
  return (
    <div className="space-y-4">
      <div className="grid grid-cols-2 lg:grid-cols-4 gap-3">
        {Array.from({ length: 4 }).map((_, i) => (
          <div key={i} className="rounded-xl bg-ink-900/40 ring-1 ring-ink-700/60 p-3 space-y-2">
            <div className="skeleton h-2 w-16" />
            <div className="skeleton h-6 w-24" />
            <div className="skeleton h-2 w-32" />
          </div>
        ))}
      </div>
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-3">
        {Array.from({ length: 3 }).map((_, i) => (
          <div key={i} className="rounded-xl bg-ink-900/40 ring-1 ring-ink-700/60 p-3 space-y-2">
            <div className="skeleton h-2 w-20" />
            <div className="skeleton h-6 w-16" />
            <div className="skeleton h-1.5 w-full" />
          </div>
        ))}
      </div>
    </div>
  );
}
