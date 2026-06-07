/**
 * Theme tokens.  Single source of truth for status colours, motion
 * durations, and easing curves.  Components read from here rather
 * than duplicating magic numbers.
 *
 * Status colour strategy: committed amber/gold accent on warm zinc,
 * with desaturated per-status hues.  No "AI purple".  No pure black.
 */

export type StatusKind =
  | "ready"
  | "busy"
  | "idle"
  | "error"
  | "zombie"
  | "draining"
  | "offline"
  | "initializing"
  | "pending"
  | "running"
  | "completed"
  | "failed"
  | "recovering"
  | "skipped"
  | "cancelled"
  | "draft"
  | "submitted"
  | "paused";

export interface StatusTokens {
  label: string;
  fg: string; // text on light surface
  bg: string; // tinted background
  ring: string; // border / outline
  dot: string; // primary signal dot
  /** phosphor icon name for the status */
  icon: "Pulse" | "Circle" | "CheckCircle" | "Warning" | "XCircle" | "Hourglass" | "Minus" | "Power";
}

const STATUS_MAP: Record<StatusKind, StatusTokens> = {
  // agents
  ready: {
    label: "Ready",
    fg: "text-moss-400",
    bg: "bg-moss-500/12",
    ring: "ring-moss-500/40",
    dot: "bg-moss-500",
    icon: "CheckCircle",
  },
  busy: {
    label: "Busy",
    fg: "text-amber-glow",
    bg: "bg-amber-glow/14",
    ring: "ring-amber-glow/40",
    dot: "bg-amber-glow",
    icon: "Pulse",
  },
  idle: {
    label: "Idle",
    fg: "text-ink-300",
    bg: "bg-ink-600/40",
    ring: "ring-ink-500/40",
    dot: "bg-ink-400",
    icon: "Minus",
  },
  error: {
    label: "Error",
    fg: "text-ember-400",
    bg: "bg-ember-500/14",
    ring: "ring-ember-500/40",
    dot: "bg-ember-500",
    icon: "XCircle",
  },
  zombie: {
    label: "Zombie",
    fg: "text-plum-400",
    bg: "bg-plum-500/14",
    ring: "ring-plum-500/40",
    dot: "bg-plum-500",
    icon: "Warning",
  },
  draining: {
    label: "Draining",
    fg: "text-ink-200",
    bg: "bg-ink-500/40",
    ring: "ring-ink-400/40",
    dot: "bg-ink-300",
    icon: "Power",
  },
  offline: {
    label: "Offline",
    fg: "text-ink-300",
    bg: "bg-ink-700/40",
    ring: "ring-ink-500/30",
    dot: "bg-ink-500",
    icon: "Power",
  },
  initializing: {
    label: "Booting",
    fg: "text-sea-400",
    bg: "bg-sea-500/12",
    ring: "ring-sea-500/40",
    dot: "bg-sea-500",
    icon: "Hourglass",
  },
  // workflows / steps
  pending: {
    label: "Pending",
    fg: "text-ink-300",
    bg: "bg-ink-600/30",
    ring: "ring-ink-500/30",
    dot: "bg-ink-400",
    icon: "Hourglass",
  },
  running: {
    label: "Running",
    fg: "text-amber-glow",
    bg: "bg-amber-glow/14",
    ring: "ring-amber-glow/40",
    dot: "bg-amber-glow",
    icon: "Pulse",
  },
  completed: {
    label: "Completed",
    fg: "text-moss-400",
    bg: "bg-moss-500/12",
    ring: "ring-moss-500/40",
    dot: "bg-moss-500",
    icon: "CheckCircle",
  },
  failed: {
    label: "Failed",
    fg: "text-ember-400",
    bg: "bg-ember-500/14",
    ring: "ring-ember-500/40",
    dot: "bg-ember-500",
    icon: "XCircle",
  },
  recovering: {
    label: "Recovering",
    fg: "text-amber-pulse",
    bg: "bg-amber-pulse/12",
    ring: "ring-amber-pulse/40",
    dot: "bg-amber-pulse",
    icon: "Pulse",
  },
  skipped: {
    label: "Skipped",
    fg: "text-ink-300",
    bg: "bg-ink-600/30",
    ring: "ring-ink-500/30",
    dot: "bg-ink-500",
    icon: "Minus",
  },
  cancelled: {
    label: "Cancelled",
    fg: "text-ink-300",
    bg: "bg-ink-700/40",
    ring: "ring-ink-500/30",
    dot: "bg-ink-500",
    icon: "XCircle",
  },
  draft: {
    label: "Draft",
    fg: "text-ink-200",
    bg: "bg-ink-600/30",
    ring: "ring-ink-500/30",
    dot: "bg-ink-400",
    icon: "Circle",
  },
  submitted: {
    label: "Submitted",
    fg: "text-sea-400",
    bg: "bg-sea-500/12",
    ring: "ring-sea-500/40",
    dot: "bg-sea-500",
    icon: "Hourglass",
  },
  paused: {
    label: "Paused",
    fg: "text-amber-pulse",
    bg: "bg-amber-pulse/10",
    ring: "ring-amber-pulse/30",
    dot: "bg-amber-pulse",
    icon: "Power",
  },
};

export function getStatusTokens(status: string | null | undefined): StatusTokens {
  if (!status) return STATUS_MAP.idle;
  return STATUS_MAP[status as StatusKind] ?? STATUS_MAP.idle;
}

// ---------------------------------------------------------------------------
// Motion
// ---------------------------------------------------------------------------

/**
 * Easings follow Emil Kowalski's guidance: ease-out as the default,
 * custom cubic-beziers over built-ins, asymmetric press/release.
 */
export const motion = {
  ease: {
    out: "cubic-bezier(0.16, 1, 0.3, 1)", // quart-out feel
    outExpo: "cubic-bezier(0.19, 1, 0.22, 1)",
    inOut: "cubic-bezier(0.4, 0, 0.2, 1)",
    spring: "cubic-bezier(0.34, 1.56, 0.64, 1)",
    drawer: "cubic-bezier(0.32, 0.72, 0, 1)",
  },
  duration: {
    instant: 80,
    fast: 150,
    base: 220,
    slow: 300,
    drawer: 500,
  },
  spring: {
    /** Stiffness / damping tuned for premium physicality. */
    default: { type: "spring" as const, stiffness: 320, damping: 28, mass: 0.9 },
    gentle: { type: "spring" as const, stiffness: 220, damping: 24, mass: 0.9 },
    snap: { type: "spring" as const, stiffness: 480, damping: 32, mass: 0.6 },
  },
} as const;
