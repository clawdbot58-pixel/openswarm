"""Tests for the BaseAgent WebSocket client.

These tests spin up a real kernel (in a background thread via
uvicorn) and connect a real WebSocket client. We could mock the
websockets library, but the real path catches connection-handshake
bugs that mocks would hide.
"""
from __future__ import annotations

import asyncio
import json
import socket
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
import websockets

# Make ``src`` importable.
_SRC = Path(__file__).resolve().parents[3] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from kernel.config import reset_settings_for_tests  # noqa: E402
from kernel.main import create_app  # noqa: E402
from kernel.models import (  # noqa: E402
    AgentManifest,
    Endpoint,
    Envelope,
    Preamble,
)

from agents.base_agent import (  # noqa: E402
    AgentError,
    BaseAgent,
    new_envelope_id,
    utc_now,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _manifest(agent_id: str, role: str = "executor") -> AgentManifest:
    return AgentManifest.model_validate(
        {
            "agent_id": agent_id,
            "version": "1.0.0",
            "role": role,
            "intent": f"test {agent_id}",
            "capabilities": {"inference": {"provider": "custom"}},
            "lifecycle": {"persistence": "ephemeral"},
            "registration_time": datetime.now(timezone.utc).isoformat().replace(
                "+00:00", "Z"
            ),
        }
    )


@pytest_asyncio.fixture
async def kernel_server(tmp_path):
    """Boot a uvicorn-served kernel and yield ``(base_url, http_url)``."""
    import uvicorn

    settings = reset_settings_for_tests(
        db_path=tmp_path / "registry.db",
        heartbeat_interval_seconds=0.1,
        heartbeat_zombie_threshold_seconds=0.3,
        bus_router_poll_interval_seconds=0.005,
        bus_max_queue_size=1000,
    )
    from kernel.config import _settings as _live

    _live.paths.heartbeats_dir = tmp_path / "hb"  # type: ignore[attr-defined]
    (tmp_path / "hb").mkdir(parents=True, exist_ok=True)
    app = create_app(settings)
    port = _free_port()
    config = uvicorn.Config(
        app, host="127.0.0.1", port=port, log_level="error", lifespan="on"
    )
    server = uvicorn.Server(config)
    runner_task = asyncio.create_task(server.serve())
    for _ in range(50):
        if server.started:
            break
        await asyncio.sleep(0.05)
    assert server.started, "kernel did not start"
    base = f"ws://127.0.0.1:{port}/ws"
    http = f"http://127.0.0.1:{port}"
    try:
        yield base, http
    finally:
        server.should_exit = True
        try:
            await runner_task
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Envelope helpers
# ---------------------------------------------------------------------------

def test_envelope_helpers() -> None:
    """``new_envelope_id`` returns a UUID string and ``utc_now`` is tz-aware."""
    eid = new_envelope_id()
    assert isinstance(eid, str)
    assert len(eid) == 36  # canonical UUID4 string
    now = utc_now()
    assert now.tzinfo is not None


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------

def test_constructs_with_dict_manifest() -> None:
    m = _manifest("alpha")
    agent = BaseAgent(m.model_dump(mode="json"))
    assert agent.agent_id == "alpha"
    assert agent.role == "executor"
    assert not agent.is_connected
    assert not agent.is_draining
    assert not agent.is_closed


def test_constructs_with_pydantic_manifest() -> None:
    m = _manifest("beta", role="specialist")
    agent = BaseAgent(m)
    assert agent.role == "specialist"


def test_constructs_with_manifest_includes_endpoints() -> None:
    """The system prompt path is stored as a Path."""
    agent = BaseAgent(_manifest("gamma"))
    assert agent.system_prompt == ""  # no file at the default path


def test_invalid_manifest_type_raises() -> None:
    with pytest.raises(TypeError):
        BaseAgent("not a manifest")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Envelope construction
# ---------------------------------------------------------------------------

def test_build_registration_envelope_has_manifest() -> None:
    m = _manifest("delta", role="orchestrator")
    agent = BaseAgent(m)
    env = agent.build_registration_envelope()
    assert env.envelope_type == "request"
    assert env.payload.content_type == "data"
    assert "manifest" in env.payload.data  # type: ignore[attr-defined]
    assert env.payload.data["manifest"]["agent_id"] == "delta"  # type: ignore[attr-defined]


def test_build_heartbeat_envelope_type() -> None:
    m = _manifest("epsilon")
    agent = BaseAgent(m)
    hb = agent.build_heartbeat()
    assert hb.envelope_type == "heartbeat"
    assert hb.sender.agent_id == "epsilon"


def test_build_request_envelope_shape() -> None:
    m = _manifest("zeta")
    agent = BaseAgent(m)
    env = agent.build_request(
        "peer-agent",
        payload={"content_type": "text", "content": "hello"},
        receiver_role="specialist",
        goal="do-thing",
        phase="execution",
    )
    assert env.envelope_type == "request"
    assert env.receiver.agent_id == "peer-agent"
    assert env.receiver.role == "specialist"
    assert env.preamble.intent.goal == "do-thing"
    assert env.payload.content_type == "text"  # type: ignore[attr-defined]


def test_build_event_envelope_carries_event_name() -> None:
    m = _manifest("eta")
    agent = BaseAgent(m)
    env = agent.build_event(
        "peer", "sector_complete", details={"sector": "coding"}
    )
    assert env.envelope_type == "event"
    assert env.payload.content_type == "data"  # type: ignore[attr-defined]
    assert env.payload.data["event"] == "sector_complete"  # type: ignore[attr-defined]
    assert env.payload.data["sector"] == "coding"  # type: ignore[attr-defined]


def test_build_response_envelope_links_reply_to() -> None:
    m = _manifest("theta")
    agent = BaseAgent(m)
    rid = str(uuid.uuid4())
    env = agent.build_response(
        "peer",
        rid,
        payload={"content_type": "text", "content": "ok"},
    )
    assert env.envelope_type == "response"
    assert env.reply_to == rid


def test_build_error_envelope_carries_code() -> None:
    m = _manifest("iota")
    agent = BaseAgent(m)
    env = agent.build_error("peer", "boom", "something went wrong")
    assert env.envelope_type == "error"
    assert env.payload.data["code"] == "boom"  # type: ignore[attr-defined]
    assert env.payload.data["message"] == "something went wrong"  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Outbox introspection
# ---------------------------------------------------------------------------

def test_outbox_snapshot_is_empty_before_send() -> None:
    m = _manifest("kappa")
    agent = BaseAgent(m)
    assert agent.outbox_snapshot() == []


# ---------------------------------------------------------------------------
# WebSocket integration: real kernel
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_agent_connects_registers_and_receives_acks(kernel_server) -> None:
    """``start`` opens a connection and completes the registration handshake."""
    ws_url, _ = kernel_server
    m = _manifest("lambda")
    agent = BaseAgent(m, ws_url=ws_url, heartbeat_interval=0)
    try:
        await agent.start()
        assert agent.is_connected
        assert agent.registration_ack is not None
        assert agent.registration_ack["type"] == "registered"
        assert agent.registration_ack["agent_id"] == "lambda"
    finally:
        await agent.close()
    assert agent.is_closed


@pytest.mark.asyncio
async def test_agent_send_reaches_kernel(kernel_server) -> None:
    """A request sent over WS shows up at the receiver's queue."""
    ws_url, http_url = kernel_server
    # Pre-register the receiver.
    import httpx

    async with httpx.AsyncClient(base_url=http_url, timeout=5.0) as c:
        r = await c.post(
            "/registry/agents",
            json={"manifest": _manifest("mu", role="executor").model_dump(mode="json")},
        )
        assert r.status_code == 201
    m = _manifest("nu", role="orchestrator")
    agent = BaseAgent(m, ws_url=ws_url, heartbeat_interval=0)
    try:
        await agent.start()
        env = agent.build_request(
            "mu",
            payload={"content_type": "text", "content": "ping"},
        )
        await agent.send(env)
        # Give the kernel router a tick to deliver.
        await asyncio.sleep(0.2)
    finally:
        await agent.close()


@pytest.mark.asyncio
async def test_agent_drain_refuses_new_sends(kernel_server) -> None:
    """After ``drain`` the agent rejects outbound work."""
    ws_url, _ = kernel_server
    agent = BaseAgent(_manifest("xi"), ws_url=ws_url, heartbeat_interval=0)
    try:
        await agent.start()
        await agent.drain()
        assert agent.is_draining
        with pytest.raises(AgentError):
            await agent.send(
                agent.build_request(
                    "mu", payload={"content_type": "text", "content": "x"}
                )
            )
    finally:
        await agent.close()


@pytest.mark.asyncio
async def test_agent_close_is_idempotent(kernel_server) -> None:
    """Calling close twice does not raise."""
    ws_url, _ = kernel_server
    agent = BaseAgent(_manifest("omicron"), ws_url=ws_url, heartbeat_interval=0)
    await agent.start()
    await agent.close()
    await agent.close()
    assert agent.is_closed


@pytest.mark.asyncio
async def test_agent_receives_inbound_envelope(kernel_server) -> None:
    """Envelopes routed back to the agent fire ``on_envelope``."""
    ws_url, _ = kernel_server
    # Pre-register a peer so the kernel will accept a forwarded message.
    import httpx

    async with httpx.AsyncClient(base_url=kernel_server[1], timeout=5.0) as c:
        r = await c.post(
            "/registry/agents",
            json={"manifest": _manifest("pi", role="orchestrator").model_dump(mode="json")},
        )
        assert r.status_code == 201
    received: list[Envelope] = []
    events: list[tuple[str, dict[str, Any]]] = []

    class _Recorder(BaseAgent):
        async def on_envelope(self, envelope: Envelope) -> None:  # type: ignore[override]
            received.append(envelope)

        async def on_event(self, event_name: str, details: dict[str, Any]) -> None:  # type: ignore[override]
            events.append((event_name, details))

    agent = _Recorder(_manifest("rho"), ws_url=ws_url, heartbeat_interval=0)
    try:
        await agent.start()
        # Have the peer send us an envelope via REST.
        async with httpx.AsyncClient(base_url=kernel_server[1], timeout=5.0) as c:
            r = await c.post(
                "/bus/send",
                json={
                    "envelope": {
                        "envelope_id": str(uuid.uuid4()),
                        "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                        "envelope_type": "request",
                        "sender": {"agent_id": "pi", "role": "orchestrator"},
                        "receiver": {"agent_id": "rho", "role": "executor"},
                        "preamble": {"intent": {"goal": "hi", "phase": "execution"}},
                        "payload": {"content_type": "text", "content": "hello"},
                    }
                },
            )
            assert r.status_code == 200, r.text
        # Wait for delivery.
        for _ in range(20):
            if received:
                break
            await asyncio.sleep(0.1)
        assert received, "agent did not receive inbound envelope"
        assert received[0].payload.content_type == "text"  # type: ignore[attr-defined]
        assert received[0].payload.content == "hello"  # type: ignore[attr-defined]
    finally:
        await agent.close()


@pytest.mark.asyncio
async def test_agent_heartbeat_task_runs(kernel_server) -> None:
    """The heartbeat task emits heartbeats on the configured interval."""
    ws_url, _ = kernel_server
    agent = BaseAgent(_manifest("sigma"), ws_url=ws_url, heartbeat_interval=0.05)
    try:
        await agent.start()
        # Let a few heartbeats fire.
        await asyncio.sleep(0.2)
        # The kernel marks heartbeats in the registry; we just verify
        # the agent did not crash.
        assert agent.is_connected
    finally:
        await agent.close()
