/**
 * `useAgents` — list, detail, and live updates for agents.
 *
 * Subscribes to `agent_status_changed` events and re-fetches the
 * affected agent card.  Falls back to a polling cadence when the
 * socket is closed.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { agentsApi, ApiClientError } from "../api";
import type { AgentDetail, AgentStatus, AgentSummary } from "../types";
import { useWebSocket } from "./useWebSocket";

interface UseAgentsState {
  agents: AgentSummary[];
  loading: boolean;
  error: ApiClientError | Error | null;
  lastUpdated: string | null;
  refresh: () => Promise<void>;
  getDetail: (agentId: string) => Promise<AgentDetail | null>;
}

const POLL_INTERVAL_MS = 15_000;

export function useAgents(): UseAgentsState {
  const [agents, setAgents] = useState<AgentSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<ApiClientError | Error | null>(null);
  const [lastUpdated, setLastUpdated] = useState<string | null>(null);
  const { status, subscribeByType } = useWebSocket();
  const detailCache = useRef(new Map<string, AgentDetail>());

  const refresh = useCallback(async () => {
    try {
      const list = await agentsApi.list();
      setAgents(Array.isArray(list) ? list : []);
      setError(null);
      setLastUpdated(new Date().toISOString());
    } catch (err) {
      setError(err instanceof ApiClientError ? err : new Error(String(err)));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  // Re-fetch when the socket reports status changes.
  useEffect(() => {
    return subscribeByType("agent_status_changed", () => {
      void refresh();
    });
  }, [subscribeByType, refresh]);

  // Polling fallback.
  useEffect(() => {
    if (status === "open") return;
    const timer = setInterval(() => {
      void refresh();
    }, POLL_INTERVAL_MS);
    return () => clearInterval(timer);
  }, [status, refresh]);

  const getDetail = useCallback(async (agentId: string) => {
    if (detailCache.current.has(agentId)) {
      return detailCache.current.get(agentId) ?? null;
    }
    try {
      const detail = await agentsApi.detail(agentId);
      detailCache.current.set(agentId, detail);
      return detail;
    } catch (err) {
      setError(err instanceof ApiClientError ? err : new Error(String(err)));
      return null;
    }
  }, []);

  return { agents, loading, error, lastUpdated, refresh, getDetail };
}

/**
 * Helper to derive a stable "fingerprint" of an agent row so that
 * unchanged rows don't trigger downstream re-renders in the grid.
 */
export function agentFingerprint(agent: AgentSummary): string {
  return [
    agent.agent_id,
    agent.status,
    agent.model_tier,
    agent.heartbeat_age_seconds,
    agent.current_task ?? "",
    agent.connected_ws ? "1" : "0",
  ].join("|");
}

export type { AgentStatus };
