/**
 * Smoke tests for the API client.
 */

import { beforeEach, describe, expect, it, vi } from "vitest";
import { agentsApi, ApiClientError, layoutsApi, viewsApi } from "../src/api";
import { mockJsonResponse } from "./setup";

describe("api client", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it("lists agents and returns typed summaries", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      mockJsonResponse([
        {
          agent_id: "coder-fast",
          human_readable_name: "Coder",
          role: "executor",
          category: "coding",
          status: "busy",
          model_tier: "fast",
          current_task: "writing tests",
          heartbeat_age_seconds: 3,
          connected_ws: true,
          registered_at: "2026-06-06T10:00:00Z",
          last_heartbeat: "2026-06-06T10:00:03Z",
          instance_id: null,
          tags: ["python"],
        },
      ]),
    );
    vi.stubGlobal("fetch", fetchMock);

    const list = await agentsApi.list();
    expect(list).toHaveLength(1);
    expect(list[0]?.agent_id).toBe("coder-fast");
    expect(list[0]?.status).toBe("busy");
  });

  it("wraps non-2xx responses in ApiClientError", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(JSON.stringify({ detail: { code: "agent_not_found", message: "nope" } }), {
          status: 404,
          headers: { "Content-Type": "application/json" },
        }),
      ),
    );
    await expect(agentsApi.detail("missing")).rejects.toBeInstanceOf(ApiClientError);
  });

  it("creates a view via POST", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        mockJsonResponse({
          view_id: "v-1",
          name: "My view",
          description: "",
          view_type: "custom",
          data_sources: ["/api/agents"],
          filters: {},
          refresh_interval_ms: 5000,
          created_by: "dashboard-user",
          created_at: "2026-06-06T10:00:00Z",
          updated_at: "2026-06-06T10:00:00Z",
        }),
      ),
    );

    const view = await viewsApi.create({ name: "My view", view_type: "custom", data_sources: ["/api/agents"] });
    expect(view.view_id).toBe("v-1");
    expect(view.view_type).toBe("custom");
  });

  it("creates a layout via POST and preserves the panel shape", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      mockJsonResponse({
        layout_id: "l-1",
        name: "Coding",
        description: "",
        panels: [
          {
            panel_id: "p-1",
            view_id: "v-1",
            position: { x: 0, y: 0, w: 6, h: 4 },
            pinned: false,
          },
        ],
        created_by: "dashboard-user",
        created_at: "2026-06-06T10:00:00Z",
        updated_at: "2026-06-06T10:00:00Z",
      }),
    );
    vi.stubGlobal("fetch", fetchMock);

    const layout = await layoutsApi.create({
      name: "Coding",
      panels: [
        { panel_id: "p-1", view_id: "v-1", position: { x: 0, y: 0, w: 6, h: 4 }, pinned: false },
      ],
    });
    expect(layout.layout_id).toBe("l-1");
    expect(layout.panels?.[0]?.view_id).toBe("v-1");
  });
});
