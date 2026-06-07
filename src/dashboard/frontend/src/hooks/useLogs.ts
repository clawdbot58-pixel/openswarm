/**
 * `useLogs` — fetches an initial window of logs and appends new
 * entries as they stream in over WebSocket.
 *
 * Capped at `maxBuffer` to keep the DOM lean.  When the cap is hit
 * the oldest entries fall off.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { logsApi, ApiClientError } from "../api";
import type { LogEntry } from "../types";
import { useWebSocket } from "./useWebSocket";

export interface LogFilter {
  agent: string;
  workflowId: string;
  envelopeType: string;
  severity: string;
  search: string;
}

export const EMPTY_LOG_FILTER: LogFilter = {
  agent: "",
  workflowId: "",
  envelopeType: "",
  severity: "",
  search: "",
};

interface UseLogsState {
  logs: LogEntry[];
  loading: boolean;
  error: ApiClientError | Error | null;
  refresh: () => Promise<void>;
  append: (entry: LogEntry) => void;
  clear: () => void;
  setFilter: (filter: LogFilter) => void;
  filter: LogFilter;
}

const DEFAULT_BUFFER = 1000;
const DEFAULT_LIMIT = 200;

export function useLogs(opts?: {
  initialLimit?: number;
  maxBuffer?: number;
  filter?: Partial<LogFilter>;
}): UseLogsState {
  const limit = opts?.initialLimit ?? DEFAULT_LIMIT;
  const maxBuffer = opts?.maxBuffer ?? DEFAULT_BUFFER;
  const [logs, setLogs] = useState<LogEntry[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<ApiClientError | Error | null>(null);
  const [filter, setFilter] = useState<LogFilter>({ ...EMPTY_LOG_FILTER, ...(opts?.filter ?? {}) });
  const { subscribe } = useWebSocket();
  const seenIds = useRef(new Set<string>());

  const refresh = useCallback(async () => {
    try {
      const list = await logsApi.list({ limit });
      seenIds.current = new Set(list.map((entry) => entry.envelope_id));
      setLogs(list);
      setError(null);
    } catch (err) {
      setError(err instanceof ApiClientError ? err : new Error(String(err)));
    } finally {
      setLoading(false);
    }
  }, [limit]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  // Stream new log entries.
  useEffect(() => {
    return subscribe((event) => {
      if (event.type !== "log_entry" && event.event_type !== "log_entry") return;
      const payload = (event.envelope ?? event.data ?? event.payload ?? {}) as Partial<LogEntry>;
      if (!payload.envelope_id || !payload.timestamp) return;
      if (seenIds.current.has(payload.envelope_id)) return;
      seenIds.current.add(payload.envelope_id);
      const entry: LogEntry = {
        envelope_id: payload.envelope_id,
        timestamp: payload.timestamp ?? event.timestamp,
        envelope_type: (payload.envelope_type as LogEntry["envelope_type"]) ?? "event",
        sender: payload.sender ?? "unknown",
        receiver: payload.receiver ?? "unknown",
        payload_preview: payload.payload_preview ?? "",
        priority: payload.priority ?? 5,
        severity: (payload.severity as LogEntry["severity"]) ?? "info",
        result: payload.result ?? "ok",
        workflow_id: payload.workflow_id ?? null,
        content_type: payload.content_type ?? null,
        tags: payload.tags ?? [],
      };
      setLogs((prev) => {
        const next = [...prev, entry];
        if (next.length > maxBuffer) {
          const dropped = next.length - maxBuffer;
          const trimmed = next.slice(dropped);
          // GC the seen-set for the trimmed entries to avoid unbounded growth.
          for (let i = 0; i < dropped; i += 1) {
            seenIds.current.delete(next[i].envelope_id);
          }
          return trimmed;
        }
        return next;
      });
    });
  }, [subscribe, maxBuffer]);

  const append = useCallback((entry: LogEntry) => {
    if (seenIds.current.has(entry.envelope_id)) return;
    seenIds.current.add(entry.envelope_id);
    setLogs((prev) => [...prev, entry]);
  }, []);

  const clear = useCallback(() => {
    setLogs([]);
    seenIds.current.clear();
  }, []);

  return { logs, loading, error, refresh, append, clear, setFilter, filter };
}
