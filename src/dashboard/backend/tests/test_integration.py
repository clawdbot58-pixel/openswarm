"""End-to-end integration test for the dashboard backend.

Boots the full FastAPI app (with kernel collaborators wired in)
behind an ``httpx.ASGITransport`` and exercises every public
endpoint, including:

* the WebSocket ``/stream`` endpoint;
* agent creation, listing, and detail;
* workflow + workspace queries;
* log filtering;
* view/layout storage;
* metrics snapshots.

Each test stands up its own ASGI app via the
:func:`create_dashboard_app` factory and tears it down at the end.
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

# Make ``src`` importable.
_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_SRC = _PROJECT_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from dashboard.backend.tests.conftest import make_manifest  # noqa: E402
from kernel.models import (  # noqa: E402
    AgentManifest,
)

# ---------------------------------------------------------------------------
# Helper: open the lifespan explicitly
# ---------------------------------------------------------------------------


class _LifespanProxy:
    """Context manager that opens/closes the FastAPI lifespan on enter/exit."""

    def __init__(self, app):
        self._app = app
        self._cm = None

    async def __aenter__(self):
        self._cm = self._app.router.lifespan_context(self._app)
        await self._cm.__aenter__()
        return self._app

    async def __aexit__(self, exc_type, exc, tb):
        if self._cm is not None:
            try:
                await self._cm.__aexit__(exc_type, exc, tb)
            finally:
                self._cm = None


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


async def test_health_endpoint(dashboard_client):
    resp = await dashboard_client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["db_ok"] is True


async def test_root_endpoint(dashboard_client):
    resp = await dashboard_client.get("/")
    assert resp.status_code == 200
    body = resp.json()
    assert body["service"] == "openswarm-dashboard-backend"
    assert body["phase"] == "7"


async def test_agents_endpoint(dashboard_client, dashboard_harness):
    registry = dashboard_harness["registry"]
    await registry.register(
        AgentManifest.model_validate(make_manifest("api-agent-1"))
    )
    await registry.register(
        AgentManifest.model_validate(
            make_manifest("api-agent-2", role="specialist", category="review")
        )
    )
    resp = await dashboard_client.get("/api/agents")
    assert resp.status_code == 200
    agents = resp.json()
    assert {a["agent_id"] for a in agents} >= {"api-agent-1", "api-agent-2"}

    resp = await dashboard_client.get("/api/agents?role=specialist")
    assert resp.status_code == 200
    assert {a["agent_id"] for a in resp.json()} == {"api-agent-2"}


async def test_agent_detail_endpoint(dashboard_client, dashboard_harness):
    registry = dashboard_harness["registry"]
    await registry.register(AgentManifest.model_validate(make_manifest("detail-1")))
    resp = await dashboard_client.get("/api/agents/detail-1")
    assert resp.status_code == 200
    body = resp.json()
    assert body["agent_id"] == "detail-1"
    assert body["manifest"]["agent_id"] == "detail-1"


async def test_agent_detail_404(dashboard_client):
    resp = await dashboard_client.get("/api/agents/missing")
    assert resp.status_code == 404
    assert resp.json()["detail"]["code"] == "agent_not_found"


async def test_workflows_endpoint(dashboard_client, dashboard_harness):
    workspaces_dir: Path = dashboard_harness["workspaces_dir"]
    (workspaces_dir / "wf-x").mkdir(parents=True)
    (workspaces_dir / "wf-x" / "src").mkdir()
    (workspaces_dir / "wf-x" / "src" / "main.py").write_text("x = 1")
    resp = await dashboard_client.get("/api/workflows")
    assert resp.status_code == 200
    workflows = resp.json()
    assert {w["workflow_id"] for w in workflows} >= {"wf-x"}


async def test_logs_endpoint(dashboard_client, dashboard_harness):
    registry = dashboard_harness["registry"]
    await registry.register(AgentManifest.model_validate(make_manifest("log-1")))
    await registry.audit(
        action="envelope_sent",
        result="ok",
        agent_id="log-1",
        details={"envelope_type": "request", "content_type": "text"},
    )
    resp = await dashboard_client.get("/api/logs?agent_id=log-1")
    assert resp.status_code == 200
    logs = resp.json()
    assert len(logs) >= 1
    assert all(item["sender"] == "log-1" for item in logs)


async def test_workspaces_endpoint(dashboard_client, dashboard_harness):
    workspaces_dir: Path = dashboard_harness["workspaces_dir"]
    workspaces_dir.mkdir(parents=True, exist_ok=True)
    for wf in ("a", "b", "c"):
        (workspaces_dir / wf).mkdir()
    resp = await dashboard_client.get("/api/workspaces")
    assert resp.status_code == 200
    wfs = resp.json()
    assert {w["workflow_id"] for w in wfs} >= {"a", "b", "c"}


async def test_workspace_files_endpoint(dashboard_client, dashboard_harness):
    workspaces_dir: Path = dashboard_harness["workspaces_dir"]
    ws = workspaces_dir / "files-api"
    (ws / "src").mkdir(parents=True)
    (ws / "src" / "main.py").write_text("print('x')")
    resp = await dashboard_client.get("/api/workspaces/files-api/files?path=/src")
    assert resp.status_code == 200
    files = resp.json()
    assert any(f["name"] == "main.py" for f in files)


async def test_loops_endpoint(dashboard_client):
    resp = await dashboard_client.get("/api/loops")
    assert resp.status_code == 200
    templates = resp.json()
    assert len(templates) > 0
    # Sorted by success rate desc.
    rates = [t["success_rate"] for t in templates]
    assert rates == sorted(rates, reverse=True)


async def test_metrics_endpoint(dashboard_client, dashboard_harness):
    registry = dashboard_harness["registry"]
    await registry.register(AgentManifest.model_validate(make_manifest("m-api")))
    resp = await dashboard_client.get("/api/metrics")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total_agents"] >= 1
    assert body["uptime_seconds"] >= 0


async def test_views_crud(dashboard_client):
    # Create.
    body = {
        "name": "Test View",
        "description": "hello",
        "view_type": "custom",
        "data_sources": ["/api/agents"],
        "filters": {"status": "ready"},
        "refresh_interval_ms": 3000,
        "created_by": "test",
    }
    resp = await dashboard_client.post("/api/views", json=body)
    assert resp.status_code == 201
    created = resp.json()
    view_id = created["view_id"]
    assert created["name"] == "Test View"
    # List.
    resp = await dashboard_client.get("/api/views")
    assert resp.status_code == 200
    assert any(v["view_id"] == view_id for v in resp.json())
    # Update.
    body2 = {**body, "name": "Updated View"}
    resp = await dashboard_client.put(f"/api/views/{view_id}", json=body2)
    assert resp.status_code == 200
    assert resp.json()["name"] == "Updated View"
    # Get.
    resp = await dashboard_client.get(f"/api/views/{view_id}")
    assert resp.status_code == 200
    assert resp.json()["name"] == "Updated View"
    # Delete.
    resp = await dashboard_client.delete(f"/api/views/{view_id}")
    assert resp.status_code == 204
    resp = await dashboard_client.get(f"/api/views/{view_id}")
    assert resp.status_code == 404


async def test_layouts_crud(dashboard_client):
    body = {
        "name": "Default",
        "description": "Two-column layout",
        "panes": {"left": {"view_id": "v1"}, "right": {"view_id": "v2"}},
        "created_by": "test",
    }
    resp = await dashboard_client.post("/api/layouts", json=body)
    assert resp.status_code == 201
    layout_id = resp.json()["layout_id"]
    resp = await dashboard_client.get(f"/api/layouts/{layout_id}")
    assert resp.status_code == 200
    assert "left" in resp.json()["panes"]
    resp = await dashboard_client.delete(f"/api/layouts/{layout_id}")
    assert resp.status_code == 204
    resp = await dashboard_client.get(f"/api/layouts/{layout_id}")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# WebSocket
# ---------------------------------------------------------------------------


async def test_websocket_stream_receives_snapshot(dashboard_client, dashboard_harness):
    """Start add_client and cancel it after the snapshot is sent."""
    stream = dashboard_harness["stream"]
    ws = _FakeWebSocketForTest()
    task = asyncio.create_task(stream.add_client(ws, subscribe=None))
    await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):
        pass
    assert ws._accept_called
    assert len(ws.sent) >= 1
    first = json.loads(ws.sent[0])
    assert first["type"] == "snapshot"


async def test_websocket_stream_broadcasts_to_all_clients(dashboard_client, dashboard_harness):
    """Verifies broadcast() reaches all connected clients."""
    stream = dashboard_harness["stream"]
    bus = dashboard_harness["bus"]
    ws1 = _FakeWebSocketForTest()
    ws2 = _FakeWebSocketForTest()
    t1 = asyncio.create_task(stream.add_client(ws1))
    t2 = asyncio.create_task(stream.add_client(ws2))
    await asyncio.sleep(0.05)
    await bus.emit_event("agent_zombie", {"agent_id": "x"})
    await asyncio.sleep(0.1)
    t1.cancel()
    t2.cancel()
    try:
        await t1
    except (asyncio.CancelledError, Exception):
        pass
    try:
        await t2
    except (asyncio.CancelledError, Exception):  # noqa: BLE001:
        pass
    found1 = any(
        json.loads(m).get("type") == "envelope"
        and json.loads(m).get("event_name") == "agent_zombie"
        for m in ws1.sent
    )
    found2 = any(
        json.loads(m).get("type") == "envelope"
        and json.loads(m).get("event_name") == "agent_zombie"
        for m in ws2.sent
    )
    assert found1, "ws1 should have received the event"
    assert found2, "ws2 should have received the event"


async def test_websocket_subscription_filter(dashboard_client, dashboard_harness):
    """A client with subscribe=["queue_overflow"] only receives matching events."""
    stream = dashboard_harness["stream"]
    bus = dashboard_harness["bus"]
    ws_all = _FakeWebSocketForTest()
    ws_filter = _FakeWebSocketForTest()
    t_all = asyncio.create_task(stream.add_client(ws_all))
    t_filter = asyncio.create_task(stream.add_client(ws_filter, subscribe=["queue_overflow"]))
    await asyncio.sleep(0.05)
    await bus.emit_event("agent_zombie", {"agent_id": "x"})
    await bus.emit_event("queue_overflow", {"agent_id": "y"})
    await asyncio.sleep(0.1)
    t_all.cancel()
    t_filter.cancel()
    try:
        await t_all
    except (asyncio.CancelledError, Exception):  # noqa: BLE001:
        pass
    try:
        await t_filter
    except (asyncio.CancelledError, Exception):  # noqa: BLE001:
        pass
    found_zo_all = any(
        json.loads(m).get("event_name") == "agent_zombie" for m in ws_all.sent
    )
    found_zo_filter = any(
        json.loads(m).get("event_name") == "agent_zombie" for m in ws_filter.sent
    )
    found_qo_filter = any(
        json.loads(m).get("event_name") == "queue_overflow" for m in ws_filter.sent
    )
    assert found_zo_all
    assert not found_zo_filter
    assert found_qo_filter


class _FakeWebSocketForTest:
    """Minimal in-process stand-in for WebSocket used in unit tests."""

    def __init__(self) -> None:
        self.sent: list[str] = []
        self.closed = False
        self._accept_called = False

    async def accept(self) -> None:
        self._accept_called = True

    async def send_text(self, payload: str) -> None:
        if self.closed:
            raise RuntimeError("send on closed socket")
        self.sent.append(payload)

    async def receive_text(self) -> str:
        await asyncio.sleep(300.0)
        raise asyncio.CancelledError()

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.closed = True
