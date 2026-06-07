/**
 * WebSocket client for the live event stream.
 *
 * Reconnects with exponential backoff (capped at 8s) and exposes a
 * small subscriber interface that the React layer can plug into.  The
 * connection is *additive*: it never throws, never blocks renders, and
 * degrades gracefully when the backend is down.
 */

import type { StreamEvent, WebSocketStatus } from "./types";
import { WS_BASE } from "./env";

export type StreamHandler = (event: StreamEvent) => void;
export type StatusHandler = (status: WebSocketStatus) => void;

export interface StreamClientOptions {
  url?: string;
  reconnectMinMs?: number;
  reconnectMaxMs?: number;
  heartbeatMs?: number;
}

interface Subscription {
  id: number;
  handler: StreamHandler;
}

interface StatusSubscription {
  id: number;
  handler: StatusHandler;
}

const DEFAULT_URL = `${WS_BASE}/stream`;
const DEFAULT_RECONNECT_MIN = 500;
const DEFAULT_RECONNECT_MAX = 8000;
const DEFAULT_HEARTBEAT = 20000;

export class StreamClient {
  private url: string;
  private socket: WebSocket | null = null;
  private status: WebSocketStatus = "closed";
  private reconnectAttempt = 0;
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  private heartbeatTimer: ReturnType<typeof setInterval> | null = null;
  private subs = new Map<number, Subscription>();
  private statusSubs = new Map<number, StatusSubscription>();
  private nextSubId = 1;
  private closedByUser = false;
  private reconnectMin: number;
  private reconnectMax: number;
  private heartbeatInterval: number;

  constructor(options: StreamClientOptions = {}) {
    this.url = options.url ?? DEFAULT_URL;
    this.reconnectMin = options.reconnectMinMs ?? DEFAULT_RECONNECT_MIN;
    this.reconnectMax = options.reconnectMaxMs ?? DEFAULT_RECONNECT_MAX;
    this.heartbeatInterval = options.heartbeatMs ?? DEFAULT_HEARTBEAT;
  }

  connect(): void {
    if (typeof window === "undefined" || typeof WebSocket === "undefined") return;
    if (this.socket && (this.socket.readyState === WebSocket.OPEN || this.socket.readyState === WebSocket.CONNECTING)) {
      return;
    }
    this.closedByUser = false;
    this.setStatus("connecting");
    try {
      this.socket = new WebSocket(this.url);
    } catch (err) {
      this.scheduleReconnect();
      this.setStatus("error");
      // swallow — surface via status only
      void err;
      return;
    }
    this.socket.addEventListener("open", this.handleOpen);
    this.socket.addEventListener("close", this.handleClose);
    this.socket.addEventListener("error", this.handleError);
    this.socket.addEventListener("message", this.handleMessage);
  }

  close(): void {
    this.closedByUser = true;
    if (this.reconnectTimer) {
      clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }
    if (this.heartbeatTimer) {
      clearInterval(this.heartbeatTimer);
      this.heartbeatTimer = null;
    }
    if (this.socket) {
      this.socket.removeEventListener("open", this.handleOpen);
      this.socket.removeEventListener("close", this.handleClose);
      this.socket.removeEventListener("error", this.handleError);
      this.socket.removeEventListener("message", this.handleMessage);
      try {
        this.socket.close(1000, "client_close");
      } catch {
        /* noop */
      }
      this.socket = null;
    }
    this.setStatus("closed");
  }

  subscribe(handler: StreamHandler): () => void {
    const id = this.nextSubId++;
    this.subs.set(id, { id, handler });
    return () => {
      this.subs.delete(id);
    };
  }

  onStatusChange(handler: StatusHandler): () => void {
    const id = this.nextSubId++;
    this.statusSubs.set(id, { id, handler });
    // immediately fire current status so the consumer doesn't render
    // an incorrect intermediate state
    try {
      handler(this.status);
    } catch {
      /* noop */
    }
    return () => {
      this.statusSubs.delete(id);
    };
  }

  getStatus(): WebSocketStatus {
    return this.status;
  }

  // -----------------------------------------------------------------------
  // Private
  // -----------------------------------------------------------------------

  private setStatus(next: WebSocketStatus): void {
    if (this.status === next) return;
    this.status = next;
    for (const sub of this.statusSubs.values()) {
      try {
        sub.handler(next);
      } catch {
        /* swallow subscriber errors */
      }
    }
  }

  private handleOpen = (): void => {
    this.reconnectAttempt = 0;
    this.setStatus("open");
    if (this.heartbeatTimer) clearInterval(this.heartbeatTimer);
    this.heartbeatTimer = setInterval(() => {
      if (this.socket?.readyState === WebSocket.OPEN) {
        try {
          // server may not require this; harmless if it ignores.
          this.socket.send('{"type":"ping"}');
        } catch {
          /* noop */
        }
      }
    }, this.heartbeatInterval);
  };

  private handleClose = (): void => {
    this.cleanupSocket();
    this.setStatus("closed");
    if (!this.closedByUser) this.scheduleReconnect();
  };

  private handleError = (): void => {
    this.setStatus("error");
    // The close handler will fire immediately after; let it drive the
    // reconnect to avoid duplicate timers.
  };

  private handleMessage = (ev: MessageEvent<string>): void => {
    let parsed: unknown;
    try {
      parsed = JSON.parse(ev.data);
    } catch {
      return;
    }
    if (!parsed || typeof parsed !== "object") return;
    const obj = parsed as Partial<StreamEvent> & {
      type?: string;
      event_type?: string;
      timestamp?: string;
    };
    const normalised: StreamEvent = {
      type: (obj.type ?? obj.event_type) as StreamEvent["type"],
      event_type: obj.event_type as StreamEvent["event_type"],
      timestamp: obj.timestamp ?? new Date().toISOString(),
      envelope: (obj.envelope as Record<string, unknown> | null | undefined) ?? null,
      metrics: obj.metrics as StreamEvent["metrics"],
      data: obj.data ?? {},
      payload: obj.payload,
    };
    for (const sub of this.subs.values()) {
      try {
        sub.handler(normalised);
      } catch {
        /* swallow */
      }
    }
  };

  private scheduleReconnect(): void {
    if (this.closedByUser) return;
    if (this.reconnectTimer) return;
    const attempt = this.reconnectAttempt++;
    const base = Math.min(this.reconnectMax, this.reconnectMin * 2 ** attempt);
    const jitter = Math.random() * this.reconnectMin;
    const delay = base + jitter;
    this.reconnectTimer = setTimeout(() => {
      this.reconnectTimer = null;
      this.connect();
    }, delay);
  }

  private cleanupSocket(): void {
    if (this.heartbeatTimer) {
      clearInterval(this.heartbeatTimer);
      this.heartbeatTimer = null;
    }
    if (this.socket) {
      this.socket.removeEventListener("open", this.handleOpen);
      this.socket.removeEventListener("close", this.handleClose);
      this.socket.removeEventListener("error", this.handleError);
      this.socket.removeEventListener("message", this.handleMessage);
      this.socket = null;
    }
  }
}

let singleton: StreamClient | null = null;

export function getStreamClient(): StreamClient {
  if (!singleton) singleton = new StreamClient();
  return singleton;
}
