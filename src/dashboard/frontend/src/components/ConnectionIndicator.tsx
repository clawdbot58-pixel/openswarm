/**
 * `ConnectionIndicator` — small chip in the top bar that reflects
 * WebSocket state.  Visible signal that the live stream is healthy.
 */

import { CircleNotch, Lightning, LinkBreak, Plugs, PlugsConnected } from "@phosphor-icons/react";
import { motion } from "framer-motion";
import { cn } from "../utils/cn";
import type { WebSocketStatus } from "../types";
import { motion as motionTokens } from "../theme";

interface ConnectionIndicatorProps {
  status: WebSocketStatus;
  className?: string;
  compact?: boolean;
}

const STATUS_PRESET: Record<
  WebSocketStatus,
  { label: string; dot: string; text: string; ring: string; icon: React.ReactNode }
> = {
  open: {
    label: "Live",
    dot: "bg-moss-500",
    text: "text-moss-400",
    ring: "ring-moss-500/40",
    icon: <PlugsConnected size={12} weight="bold" />,
  },
  connecting: {
    label: "Connecting",
    dot: "bg-amber-glow",
    text: "text-amber-glow",
    ring: "ring-amber-glow/40",
    icon: <CircleNotch size={12} weight="bold" className="animate-spin" />,
  },
  closed: {
    label: "Idle",
    dot: "bg-ink-400",
    text: "text-ink-300",
    ring: "ring-ink-500/30",
    icon: <Plugs size={12} weight="bold" />,
  },
  error: {
    label: "Offline",
    dot: "bg-ember-500",
    text: "text-ember-400",
    ring: "ring-ember-500/40",
    icon: <LinkBreak size={12} weight="bold" />,
  },
};

export function ConnectionIndicator({ status, className, compact }: ConnectionIndicatorProps): JSX.Element {
  const preset = STATUS_PRESET[status];
  return (
    <div
      role="status"
      aria-live="polite"
      aria-label={`Stream status: ${preset.label}`}
      data-status={status}
      className={cn(
        "inline-flex items-center gap-2 rounded-full px-2.5 h-7 bg-ink-900/60 ring-1 ring-inset",
        preset.ring,
        className,
      )}
    >
      <span className="relative inline-flex h-1.5 w-1.5">
        {status === "open" && (
          <motion.span
            className={cn("absolute inset-0 rounded-full", preset.dot)}
            animate={{ scale: [1, 2.6, 1], opacity: [0.5, 0, 0.5] }}
            transition={{ duration: 1.8, ease: "easeOut", repeat: Infinity }}
          />
        )}
        <span className={cn("relative h-1.5 w-1.5 rounded-full", preset.dot)} />
      </span>
      <span className={cn("inline-flex items-center gap-1 text-[11px] font-medium", preset.text)}>
        {preset.icon}
        {!compact && <span>{preset.label}</span>}
      </span>
    </div>
  );
}

/**
 * Inline use of a <Lightning /> glyph for the "Live" pill in headers.
 */
export function LivePulse({ className }: { className?: string }): JSX.Element {
  return (
    <motion.span
      className={cn("inline-flex items-center gap-1.5 text-[11px] text-amber-glow font-medium", className)}
      initial={{ opacity: 0.7 }}
      animate={{ opacity: [0.7, 1, 0.7] }}
      transition={{ duration: 2.4, ease: "easeInOut", repeat: Infinity, ...motionTokens.spring }}
    >
      <Lightning size={11} weight="fill" />
      <span>LIVE</span>
    </motion.span>
  );
}
