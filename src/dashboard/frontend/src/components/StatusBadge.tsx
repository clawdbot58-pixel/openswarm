/**
 * `StatusBadge` — a single pill showing a status with icon + dot.
 *
 * Used in cards, log lines, and headers.  The dot is a continuous
 * pulse for "live" states (busy, running, recovering) so the dashboard
 * always feels alive.
 */

import {
  CheckCircle,
  Circle,
  Hourglass,
  Minus,
  Power,
  Pulse,
  Warning,
  XCircle,
  type IconProps,
} from "@phosphor-icons/react";
import { motion } from "framer-motion";
import { cn } from "../utils/cn";
import { getStatusTokens } from "../theme";
import type { StatusTokens } from "../theme";

interface StatusBadgeProps {
  status: string;
  size?: "sm" | "md";
  showLabel?: boolean;
  className?: string;
  /** Animate the dot pulse — set false for high-density tables. */
  pulse?: boolean;
}

const ICONS: Record<StatusTokens["icon"], React.ComponentType<IconProps>> = {
  Pulse,
  Circle,
  CheckCircle,
  Warning,
  XCircle,
  Hourglass,
  Minus,
  Power,
};

export function StatusBadge({
  status,
  size = "sm",
  showLabel = true,
  className,
  pulse = true,
}: StatusBadgeProps): JSX.Element {
  const tokens = getStatusTokens(status);
  void ICONS[tokens.icon]; // future-proof; icon glyph available for richer variants
  const isLive = ["busy", "running", "recovering"].includes(status);

  return (
    <span
      role="status"
      aria-label={tokens.label}
      data-status={status}
      className={cn(
        "pill ring-1",
        tokens.bg,
        tokens.ring,
        tokens.fg,
        size === "sm" ? "h-5 px-1.5 text-[10px]" : "h-6 px-2 text-[11px]",
        className,
      )}
    >
      <span className="relative inline-flex h-1.5 w-1.5">
        {isLive && pulse ? (
          <>
            <motion.span
              className={cn("absolute inset-0 rounded-full", tokens.dot)}
              animate={{ scale: [1, 2.4, 1], opacity: [0.6, 0, 0.6] }}
              transition={{ duration: 1.6, ease: "easeOut", repeat: Infinity }}
            />
            <span className={cn("relative h-1.5 w-1.5 rounded-full", tokens.dot)} />
          </>
        ) : (
          <span className={cn("h-1.5 w-1.5 rounded-full", tokens.dot)} />
        )}
      </span>
      {showLabel ? <span className="font-medium leading-none">{tokens.label}</span> : null}
    </span>
  );
}
