/**
 * `useViews` + `useLayouts` — fetch and cache dashboard config.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { layoutsApi, viewsApi, ApiClientError } from "../api";
import type { LayoutConfig, LayoutConfigInput, ViewConfig, ViewConfigInput } from "../types";

interface AsyncState<T> {
  data: T;
  loading: boolean;
  error: ApiClientError | Error | null;
  refresh: () => Promise<void>;
}

export function useViews(): AsyncState<ViewConfig[]> & { save: (input: ViewConfigInput) => Promise<ViewConfig | null> } {
  const [views, setViews] = useState<ViewConfig[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<ApiClientError | Error | null>(null);

  const refresh = useCallback(async () => {
    try {
      const list = await viewsApi.list();
      setViews(Array.isArray(list) ? list : []);
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

  const save = useCallback(async (input: ViewConfigInput) => {
    try {
      const view = await viewsApi.create(input);
      await refresh();
      return view;
    } catch (err) {
      setError(err instanceof ApiClientError ? err : new Error(String(err)));
      return null;
    }
  }, [refresh]);

  return { data: views, loading, error, refresh, save };
}

export function useLayouts(): AsyncState<LayoutConfig[]> & {
  save: (input: LayoutConfigInput) => Promise<LayoutConfig | null>;
  getCached: (id: string) => LayoutConfig | null;
} {
  const [layouts, setLayouts] = useState<LayoutConfig[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<ApiClientError | Error | null>(null);
  const cache = useRef(new Map<string, LayoutConfig>());

  const refresh = useCallback(async () => {
    try {
      const list = await layoutsApi.list();
      const safe = Array.isArray(list) ? list : [];
      setLayouts(safe);
      cache.current = new Map(safe.map((l) => [l.layout_id, l]));
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

  const save = useCallback(async (input: LayoutConfigInput) => {
    try {
      const layout = await layoutsApi.create(input);
      await refresh();
      return layout;
    } catch (err) {
      setError(err instanceof ApiClientError ? err : new Error(String(err)));
      return null;
    }
  }, [refresh]);

  const getCached = useCallback((id: string) => cache.current.get(id) ?? null, []);

  return { data: layouts, loading, error, refresh, save, getCached };
}

/**
 * `useMetrics` — system-wide metrics snapshot.  Polls at a low
 * cadence; live updates flow through the WebSocket.
 */

import { metricsApi } from "../api";
import type { SystemMetrics } from "../types";
import { useWebSocket } from "./useWebSocket";

interface UseMetricsState {
  metrics: SystemMetrics | null;
  loading: boolean;
  error: ApiClientError | Error | null;
  refresh: () => Promise<void>;
}

const METRICS_POLL_MS = 5_000;

export function useMetrics(): UseMetricsState {
  const [metrics, setMetrics] = useState<SystemMetrics | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<ApiClientError | Error | null>(null);
  const { status, subscribeByType } = useWebSocket();

  const refresh = useCallback(async () => {
    try {
      const data = await metricsApi.live();
      setMetrics(data);
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
    return subscribeByType("system_metrics", (event) => {
      if (event.metrics) {
        setMetrics(event.metrics);
      }
    });
  }, [subscribeByType]);

  useEffect(() => {
    if (status === "open") return;
    const timer = setInterval(() => {
      void refresh();
    }, METRICS_POLL_MS);
    return () => clearInterval(timer);
  }, [status, refresh]);

  return { metrics, loading, error, refresh };
}
