/**
 * React hook for a managed WebSocket connection.
 *
 * Encapsulates the StreamClient singleton and exposes:
 *   * `status` — current connection state
 *   * `lastEvent` — most recent event (any kind)
 *   * `subscribe(handler)` — typed subscription by event type
 *
 * Reconnect / lifecycle is handled inside StreamClient; this hook
 * just wires it to React's render cycle.
 */

import { useEffect, useState, useCallback, useRef } from "react";
import { getStreamClient, StreamClient } from "../websocket";
import type { StreamEvent, StreamEventType, WebSocketStatus } from "../types";

export interface UseWebSocketResult {
  status: WebSocketStatus;
  lastEvent: StreamEvent | null;
  subscribe: (handler: (event: StreamEvent) => void) => () => void;
  subscribeByType: (type: StreamEventType, handler: (event: StreamEvent) => void) => () => void;
  client: StreamClient;
}

export function useWebSocket(): UseWebSocketResult {
  const [status, setStatus] = useState<WebSocketStatus>("closed");
  const [lastEvent, setLastEvent] = useState<StreamEvent | null>(null);
  const clientRef = useRef<StreamClient | null>(null);

  if (clientRef.current === null && typeof window !== "undefined") {
    clientRef.current = getStreamClient();
  }

  useEffect(() => {
    const client = clientRef.current;
    if (!client) return;
    client.connect();
    const offStatus = client.onStatusChange(setStatus);
    const offEvent = client.subscribe((event) => {
      setLastEvent(event);
    });
    return () => {
      offStatus();
      offEvent();
    };
  }, []);

  const subscribe = useCallback((handler: (event: StreamEvent) => void) => {
    const client = clientRef.current;
    if (!client) return () => undefined;
    return client.subscribe(handler);
  }, []);

  const subscribeByType = useCallback(
    (type: StreamEventType, handler: (event: StreamEvent) => void) => {
      const client = clientRef.current;
      if (!client) return () => undefined;
      return client.subscribe((event) => {
        if (event.type === type || event.event_type === type) {
          handler(event);
        }
      });
    },
    [],
  );

  return {
    status,
    lastEvent,
    subscribe,
    subscribeByType,
    client: clientRef.current as StreamClient,
  };
}
