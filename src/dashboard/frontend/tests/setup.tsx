/**
 * Vitest setup.
 *
 *  * Mocks @xyflow/react and @monaco-editor/react, which pull in DOM
 *    APIs and worker shims that jsdom does not provide.
 *  * Stubs out ResizeObserver / IntersectionObserver, used by RGL
 *    and react-flow.
 *  * Provides a controllable WebSocket mock so tests can drive the
 *    stream lifecycle.
 */

import "@testing-library/jest-dom/vitest";
import { vi, beforeEach, afterEach } from "vitest";

// ---------------------------------------------------------------------------
// Browser polyfills jsdom does not provide.
// ---------------------------------------------------------------------------

class ResizeObserverMock {
  observe(): void {}
  unobserve(): void {}
  disconnect(): void {}
}
(globalThis as unknown as { ResizeObserver: typeof ResizeObserverMock }).ResizeObserver =
  ResizeObserverMock;

class IntersectionObserverMock {
  root = null;
  rootMargin = "";
  thresholds: ReadonlyArray<number> = [];
  observe(): void {}
  unobserve(): void {}
  disconnect(): void {}
  takeRecords(): IntersectionObserverEntry[] {
    return [];
  }
}
(globalThis as unknown as { IntersectionObserver: typeof IntersectionObserverMock }).IntersectionObserver =
  IntersectionObserverMock;

function installMatchMediaMock(): void {
  const mock = (query: string) => ({
    matches: false,
    media: query,
    onchange: null,
    addEventListener: () => undefined,
    removeEventListener: () => undefined,
    addListener: () => undefined,
    removeListener: () => undefined,
    dispatchEvent: () => false,
  });
  Object.defineProperty(globalThis, "matchMedia", {
    writable: true,
    configurable: true,
    value: mock,
  });
  if (typeof window !== "undefined") {
    Object.defineProperty(window, "matchMedia", {
      writable: true,
      configurable: true,
      value: mock,
    });
  }
}

installMatchMediaMock();

// ---------------------------------------------------------------------------
// WebSocket mock
// ---------------------------------------------------------------------------

type Listener = (event: { data: string }) => void;
type OpenListener = () => void;
type CloseListener = () => void;

export class MockWebSocket {
  static instances: MockWebSocket[] = [];

  static OPEN = 1;
  static CLOSED = 3;

  url: string;
  readyState = 0;
  listeners = { open: [] as OpenListener[], close: [] as CloseListener[], message: [] as Listener[] };
  sent: string[] = [];

  constructor(url: string) {
    this.url = url;
    MockWebSocket.instances.push(this);
  }

  send(data: string): void {
    this.sent.push(data);
  }

  close(): void {
    this.readyState = MockWebSocket.CLOSED;
    for (const fn of this.listeners.close) fn();
  }

  addEventListener(type: "open" | "close" | "message", cb: unknown): void {
    if (type === "message") this.listeners.message.push(cb as Listener);
    else if (type === "open") this.listeners.open.push(cb as OpenListener);
    else this.listeners.close.push(cb as CloseListener);
  }

  removeEventListener(): void {
    // no-op for tests
  }

  // helpers for tests
  simulateOpen(): void {
    this.readyState = MockWebSocket.OPEN;
    for (const fn of this.listeners.open) fn();
  }
  simulateMessage(payload: unknown): void {
    for (const fn of this.listeners.message) fn({ data: JSON.stringify(payload) });
  }
  simulateClose(): void {
    this.readyState = MockWebSocket.CLOSED;
    for (const fn of this.listeners.close) fn();
  }
}

beforeEach(() => {
  MockWebSocket.instances = [];
  (globalThis as unknown as { WebSocket: typeof MockWebSocket }).WebSocket = MockWebSocket;
});

afterEach(() => {
  vi.restoreAllMocks();
});

// ---------------------------------------------------------------------------
// react-flow mock — render a flat list of nodes/edges so we can assert
// against the data the view produces.
// ---------------------------------------------------------------------------

vi.mock("@xyflow/react", () => {
  return {
    ReactFlow: ({ nodes, edges }: { nodes: { id: string; data?: unknown }[]; edges: { id: string; source: string; target: string; animated?: boolean }[] }) => {
      return (
        <div data-testid="react-flow">
          <ul data-testid="rf-nodes">
            {nodes.map((n) => (
              <li key={n.id} data-testid="rf-node" data-node-id={n.id} data-node-status={(n.data as { status?: string } | undefined)?.status ?? ""}>
                {(n.data as { label?: string } | undefined)?.label ?? n.id}
              </li>
            ))}
          </ul>
          <ul data-testid="rf-edges">
            {edges.map((e) => (
              <li
                key={e.id}
                data-testid="rf-edge"
                data-source={e.source}
                data-target={e.target}
                data-animated={e.animated ? "true" : "false"}
              />
            ))}
          </ul>
        </div>
      );
    },
    Background: () => null,
    Controls: () => null,
    Handle: () => null,
    Position: { Left: "left", Right: "right", Top: "top", Bottom: "bottom" },
  };
});

// ---------------------------------------------------------------------------
// Monaco mock — render a <pre> with the value.
// ---------------------------------------------------------------------------

vi.mock("@monaco-editor/react", () => ({
  default: ({ value }: { value: string }) => <pre data-testid="monaco">{value}</pre>,
}));

// ---------------------------------------------------------------------------
// fetch helper used in tests
// ---------------------------------------------------------------------------

export function mockJsonResponse(body: unknown, init?: ResponseInit): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { "Content-Type": "application/json" },
    ...init,
  });
}
