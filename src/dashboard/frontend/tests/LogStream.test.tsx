/**
 * LogStream tests — initial render, filter, auto-scroll.
 */

import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";
import { render, screen, cleanup, fireEvent, act, waitFor } from "@testing-library/react";
import { LogStreamView } from "../src/views/LogStream";
import { MockWebSocket, mockJsonResponse } from "./setup";
import type { LogEntry, ViewConfig } from "../src/types";

const baseConfig: ViewConfig = {
  view_id: "test-logs",
  name: "Logs",
  description: "",
  view_type: "log_stream",
  data_sources: ["/api/logs"],
  filters: {},
  refresh_interval_ms: 5000,
  created_by: "test",
  created_at: "2026-06-06T10:00:00Z",
  updated_at: "2026-06-06T10:00:00Z",
};

function makeLog(overrides: Partial<LogEntry>): LogEntry {
  return {
    envelope_id: crypto.randomUUID(),
    timestamp: "2026-06-06T10:00:00Z",
    envelope_type: "event",
    sender: "kernel",
    receiver: "coder-fast",
    payload_preview: "hello world",
    priority: 5,
    severity: "info",
    result: "ok",
    workflow_id: null,
    content_type: "text",
    tags: [],
    ...overrides,
  };
}

describe("<LogStreamView />", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });
  afterEach(() => cleanup());

  it("renders one line per log entry", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        mockJsonResponse([
          makeLog({ envelope_id: "a" }),
          makeLog({ envelope_id: "b" }),
          makeLog({ envelope_id: "c" }),
        ]),
      ),
    );

    render(<LogStreamView config={baseConfig} />);
    const lines = await screen.findAllByTestId("log-line");
    expect(lines).toHaveLength(3);
  });

  it("filters log lines by envelope type", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        mockJsonResponse([
          makeLog({ envelope_id: "a", envelope_type: "request" }),
          makeLog({ envelope_id: "b", envelope_type: "error" }),
          makeLog({ envelope_id: "c", envelope_type: "event" }),
        ]),
      ),
    );

    render(<LogStreamView config={baseConfig} />);
    await screen.findAllByTestId("log-line");
    const typeSelect = screen.getByTestId("filter-type") as HTMLSelectElement;
    fireEvent.change(typeSelect, { target: { value: "error" } });
    // AnimatePresence keeps exiting items briefly; wait for the filtered list.
    await waitFor(() => {
      const remaining = screen.queryAllByTestId("log-line");
      expect(remaining).toHaveLength(1);
    });
    const remaining = screen.getAllByTestId("log-line");
    expect(remaining[0]?.textContent).toContain("error");
  });

  it("appends a new line when a log_entry event arrives on the stream", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(mockJsonResponse([makeLog({ envelope_id: "a" })])),
    );

    render(<LogStreamView config={baseConfig} />);
    await screen.findAllByTestId("log-line");

    // open the socket to deliver a message
    const socket = MockWebSocket.instances[0]!;
    act(() => {
      socket.simulateOpen();
    });

    act(() => {
      socket.simulateMessage({
        type: "log_entry",
        timestamp: new Date().toISOString(),
        envelope: {
          envelope_id: "stream-1",
          timestamp: new Date().toISOString(),
          envelope_type: "event",
          sender: "kernel",
          receiver: "coder-fast",
          payload_preview: "from stream",
          priority: 5,
          severity: "info",
          result: "ok",
          workflow_id: null,
          content_type: "text",
          tags: [],
        },
      });
    });

    await waitFor(() => {
      const lines = screen.getAllByTestId("log-line");
      expect(lines.length).toBeGreaterThanOrEqual(2);
    });
    const lines = screen.getAllByTestId("log-line");
    expect(lines.some((l) => l.textContent?.includes("from stream"))).toBe(true);
  });
});
