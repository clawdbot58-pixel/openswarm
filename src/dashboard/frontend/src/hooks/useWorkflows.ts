/**
 * `useWorkflows` — list and detail with live status updates.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { workflowsApi, ApiClientError } from "../api";
import type { WorkflowDetail, WorkflowSummary } from "../types";
import { useWebSocket } from "./useWebSocket";

interface UseWorkflowsState {
  workflows: WorkflowSummary[];
  loading: boolean;
  error: ApiClientError | Error | null;
  refresh: () => Promise<void>;
  getDetail: (workflowId: string) => Promise<WorkflowDetail | null>;
  getDetailCached: (workflowId: string) => WorkflowDetail | null;
}

const POLL_INTERVAL_MS = 20_000;

export function useWorkflows(): UseWorkflowsState {
  const [workflows, setWorkflows] = useState<WorkflowSummary[]>([]);
  const [, forceRender] = useState(0);
  void forceRender; // keeps the line used; ensures stable hook surface
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<ApiClientError | Error | null>(null);
  const detailCache = useRef(new Map<string, WorkflowDetail>());
  const { status, subscribeByType } = useWebSocket();

  const refresh = useCallback(async () => {
    try {
      const list = await workflowsApi.list();
      setWorkflows(Array.isArray(list) ? list : []);
      setError(null);
    } catch (err) {
      setError(err instanceof ApiClientError ? err : new Error(String(err)));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  useEffect(() => {
    return subscribeByType("workflow_status_changed", () => {
      void refresh();
    });
  }, [subscribeByType, refresh]);

  useEffect(() => {
    return subscribeByType("execution_complete", () => {
      void refresh();
    });
  }, [subscribeByType, refresh]);

  useEffect(() => {
    if (status === "open") return;
    const timer = setInterval(() => {
      void refresh();
    }, POLL_INTERVAL_MS);
    return () => clearInterval(timer);
  }, [status, refresh]);

  const getDetail = useCallback(async (workflowId: string) => {
    try {
      const detail = await workflowsApi.detail(workflowId);
      detailCache.current.set(workflowId, detail);
      return detail;
    } catch (err) {
      setError(err instanceof ApiClientError ? err : new Error(String(err)));
      return null;
    }
  }, []);

  const getDetailCached = useCallback((workflowId: string) => {
    return detailCache.current.get(workflowId) ?? null;
  }, []);

  return { workflows, loading, error, refresh, getDetail, getDetailCached };
}
