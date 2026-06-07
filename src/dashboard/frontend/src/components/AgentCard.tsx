/**
 * `AgentCard` — one card in the swarm overview grid.
 *
 * Renders an agent as a tappable card with status, model tier, and
 * current task.  The card breathes when the agent is busy.
 */

import { Cpu, Pulse, Tag, UserCircle } from "@phosphor-icons/react";
import { motion } from "framer-motion";
import { StatusBadge } from "./StatusBadge";
import { cn } from "../utils/cn";
import { formatRelative, truncate } from "../utils/format";
import { motion as motionTokens } from "../theme";
import type { AgentSummary } from "../types";

interface AgentCardProps {
  agent: AgentSummary;
  onClick?: (agent: AgentSummary) => void;
  index?: number;
}

export function AgentCard({ agent, onClick, index = 0 }: AgentCardProps): JSX.Element {
  const isLive = ["busy", "running"].includes(agent.status);
  const isDown = ["error", "zombie", "offline"].includes(agent.status);

  return (
    <motion.button
      type="button"
      onClick={() => onClick?.(agent)}
      data-testid="agent-card"
      data-agent-id={agent.agent_id}
      data-status={agent.status}
      layout
      initial={{ opacity: 0, y: 8 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ ...motionTokens.spring.gentle, delay: Math.min(index, 12) * 0.035 }}
      whileHover={{ y: -2 }}
      whileTap={{ scale: 0.985 }}
      className={cn(
        "surface w-full text-left p-4 focus-ring relative overflow-hidden group",
        "hover:border-amber-glow/30 hover:shadow-ring-amber",
        isDown && "opacity-80",
      )}
    >
      {/* breathing live-edge */}
      {isLive && (
        <span className="pointer-events-none absolute inset-x-0 top-0 h-px overflow-hidden">
          <motion.span
            className="block h-full w-1/3 bg-gradient-to-r from-transparent via-amber-glow to-transparent"
            initial={{ x: "-100%" }}
            animate={{ x: "400%" }}
            transition={{ duration: 2.6, ease: "easeInOut", repeat: Infinity }}
          />
        </span>
      )}

      <div className="flex items-center justify-between gap-2">
        <StatusBadge status={agent.status} size="sm" />
        <span className="inline-flex items-center gap-1 text-[10px] uppercase tracking-[0.18em] text-ink-300">
          <Cpu size={11} weight="bold" />
          {agent.model_tier || "—"}
        </span>
      </div>

      <div className="mt-3 space-y-1.5">
        <div className="flex items-center gap-2">
          <span className="text-ink-400">
            <UserCircle size={14} weight="duotone" />
          </span>
          <h3 className="text-sm font-semibold text-ink-50 leading-tight tracking-tight">
            {agent.human_readable_name || agent.agent_id}
          </h3>
        </div>
        <div className="text-[11px] text-ink-300 font-mono">
          {agent.role} · {agent.category}
        </div>
        <div
          className={cn(
            "mt-2 text-xs leading-snug",
            agent.current_task ? "text-ink-100" : "text-ink-300 italic",
          )}
        >
          {truncate(agent.current_task ?? "Awaiting assignment", 96)}
        </div>
      </div>

      <div className="mt-3 pt-3 hairline flex items-center justify-between text-[10px] text-ink-300">
        <span className="inline-flex items-center gap-1">
          <Pulse size={10} weight="fill" className={cn(isLive ? "text-amber-glow" : "text-ink-500")} />
          {agent.heartbeat_age_seconds}s ago
        </span>
        {agent.tags.length > 0 && (
          <span className="inline-flex items-center gap-1">
            <Tag size={10} weight="bold" />
            <span className="font-mono">{agent.tags.slice(0, 2).join("·")}</span>
          </span>
        )}
      </div>

      {/* offline watermark */}
      {!agent.connected_ws && (
        <div className="absolute bottom-2 right-2 text-[9px] uppercase tracking-widest text-ember-400 font-semibold">
          socket down
        </div>
      )}

      <div className="sr-only">Last registered {formatRelative(agent.registered_at)}</div>
    </motion.button>
  );
}
