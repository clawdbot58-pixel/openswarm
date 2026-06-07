/**
 * Runtime configuration.  Reads from Vite's `import.meta.env` (which is
 * statically replaced at build time) and falls back to sane defaults
 * for local development against the Phase 7 backend on port 8765.
 */

const DEFAULT_API_BASE = "http://localhost:8765";

function readString(value: unknown, fallback: string): string {
  if (typeof value === "string" && value.length > 0) return value;
  return fallback;
}

function trimTrailingSlash(value: string): string {
  return value.endsWith("/") ? value.slice(0, -1) : value;
}

const apiBaseRaw = readString(
  // @ts-expect-error -- import.meta.env is provided by Vite
  typeof import.meta !== "undefined" ? import.meta.env?.VITE_API_BASE : undefined,
  DEFAULT_API_BASE,
);

const wsBaseRaw = readString(
  // @ts-expect-error -- import.meta.env is provided by Vite
  typeof import.meta !== "undefined" ? import.meta.env?.VITE_WS_BASE : undefined,
  DEFAULT_API_BASE.replace(/^http/i, "ws"),
);

export const env = {
  apiBase: trimTrailingSlash(apiBaseRaw),
  wsBase: trimTrailingSlash(wsBaseRaw),
  isDev:
    // @ts-expect-error -- provided by Vite
    typeof import.meta !== "undefined" && import.meta.env?.DEV === true,
} as const;

export const API_BASE = env.apiBase;
export const WS_BASE = env.wsBase;
