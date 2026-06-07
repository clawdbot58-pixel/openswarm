"""Tests for the WebSocket event stream."""
from __future__ import annotations

import asyncio
import json
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Make ``src`` importable.
_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_SRC = _PROJECT_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from kernel.models import (  # noqa: E402
    Endpoint,
    Envelope,
    EnvelopeMetadata,
    Preamble,
)


def _make_envelope(event: str = "agent_status_changed", **details: Any) -> Envelope:
    """Build a valid event envelope for tests."""
    payload_data = {"event": event, **details}
    return Envelope(
        envelope_id=str(uuid.uuid4()),
        created_at=datetime.now(timezone.utc),
        envelope_type="event",
        sender=Endpoint(agent_id="kernel", role="kernel"),
        receiver=Endpoint(agent_id="main-agent", role="orchestrator"),
        preamble=Preamble(intent={"goal": "system_event", "phase": "execution"}),
        payload={"content_type": "data", "data": payload_data},
        metadata=EnvelopeMetadata(priority=5),
    )


class _FakeWebSocket:
    """Minimal WebSocket stand-in for testing.

    Captures every ``send_text`` call so the test can assert on the
    serialized stream.
    """

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

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.closed = True

    async def receive_text(self) -> str:
        # Return a no-op pong-style message.
        await asyncio.sleep(0.01)
        return json.dumps({"type": "pong"})


async def test_connect_receives_initial_snapshot(dashboard_harness):
    stream = dashboard_harness["stream"]
    # Seed an agent so the snapshot is non-empty.
    await dashboard_harness["registry"].register(
        __import__("kernel.models", fromlist=["AgentManifest"]).AgentManifest.model_validate(
            {
                "agent_id": "snap-agent",
                "version": "1.0.0",
                "role": "executor",
                "intent": "test",
                "capabilities": {"inference": {"provider": "anthropic"}},
                "lifecycle": {"persistence": "ephemeral"},
                "registration_time": datetime.now(timezone.utc).isoformat().replace(
                    "+00:00", "Z"
                ),
                "status": "ready",
            }
        )
    )
    ws = _FakeWebSocket()
    # Run add_client in the background and cancel after a short delay.
    task = asyncio.create_task(stream.add_client(ws))
    await asyncio.sleep(0.1)
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):
        pass
    assert ws._accept_called
    assert len(ws.sent) >= 1
    first = json.loads(ws.sent[0])
    assert first["type"] == "snapshot"
    assert "agents" in first["data"]
    assert "metrics" in first["data"]


async def _add_and_cancel(stream, ws, *, subscribe=None, sleep=0.05):
    """Start ``add_client`` in a background task, yield briefly, then cancel."""
    task = asyncio.create_task(stream.add_client(ws, subscribe=subscribe))
    await asyncio.sleep(sleep)
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):
        pass


async def test_broadcast_reaches_all_clients(dashboard_harness):
    stream = dashboard_harness["stream"]
    ws1 = _FakeWebSocket()
    ws2 = _FakeWebSocket()
    t1 = asyncio.create_task(stream.add_client(ws1))
    t2 = asyncio.create_task(stream.add_client(ws2))
    await asyncio.sleep(0.05)
    assert stream.client_count == 2
    await stream.broadcast({"type": "ping", "ts": 1.0})
    # Cancel the read loops so the test doesn't hang.
    t1.cancel()
    t2.cancel()
    try:
        await t1
    except (asyncio.CancelledError, Exception):
        pass
    try:
        await t2
    except (asyncio.CancelledError, Exception):
        pass
    assert any(json.loads(m).get("type") == "ping" for m in ws1.sent)
    assert any(json.loads(m).get("type") == "ping" for m in ws2.sent)


async def test_filter_and_broadcast_only_targeted(dashboard_harness):
    stream = dashboard_harness["stream"]
    ws_all = _FakeWebSocket()
    ws_filter = _FakeWebSocket()
    t1 = asyncio.create_task(stream.add_client(ws_all))
    t2 = asyncio.create_task(stream.add_client(ws_filter, subscribe=["agent_zombie"]))
    await asyncio.sleep(0.05)
    envelope = _make_envelope(event="agent_zombie", agent_id="a")
    await stream.filter_and_broadcast(envelope)
    # Cancel read loops so the test doesn't hang.
    t1.cancel()
    t2.cancel()
    try:
        await t1
    except (asyncio.CancelledError, Exception):
        pass
    try:
        await t2
    except (asyncio.CancelledError, Exception):
        pass
    # Both should see the envelope because all-clients is unfiltered.
    envelope_ws_all = any(
        json.loads(m).get("type") == "envelope" for m in ws_all.sent
    )
    envelope_ws_filter = any(
        json.loads(m).get("type") == "envelope" for m in ws_filter.sent
    )
    assert envelope_ws_all
    assert envelope_ws_filter


async def test_remove_client_graceful(dashboard_harness):
    stream = dashboard_harness["stream"]
    ws = _FakeWebSocket()
    task = asyncio.create_task(stream.add_client(ws))
    await asyncio.sleep(0.05)
    assert stream.client_count == 1
    await stream.remove_client(ws)
    assert stream.client_count == 0
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):
        pass


async def test_kernel_event_reaches_dashboard(dashboard_harness):
    """End-to-end: kernel emits an event → stream broadcasts to clients."""
    stream = dashboard_harness["stream"]
    bus = dashboard_harness["bus"]
    ws = _FakeWebSocket()
    task = asyncio.create_task(stream.add_client(ws))
    await asyncio.sleep(0.05)
    # Emit a real event envelope through the bus.
    await bus.emit_event("agent_zombie", {"agent_id": "x", "age_seconds": 99.0})
    # The bus calls event listeners synchronously; the stream then
    # serializes. Give the websocket a moment.
    await asyncio.sleep(0.05)
    # Cancel the read loop so the test doesn't hang.
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):
        pass
    found = False
    for raw in ws.sent:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if data.get("type") == "envelope" and data.get("event_name") == "agent_zombie":
            found = True
            break
    assert found
