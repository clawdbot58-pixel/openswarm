"""End-to-end integration tests for the agent swarm.

These tests verify kernel + WebSocket agent registration and message routing.
"""
from __future__ import annotations

import asyncio
import json
import socket
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
import websockets

_SRC = Path(__file__).resolve().parents[4] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from kernel.config import reset_settings_for_tests  # noqa: E402
from kernel.main import create_app  # noqa: E402
from kernel.models import AgentManifest, Endpoint, Preamble, Envelope  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _manifest(agent_id: str, role: str) -> AgentManifest:
    return AgentManifest.model_validate({
        "agent_id": agent_id,
        "version": "1.0.0",
        "role": role,
        "intent": f"Test agent {agent_id}",
        "capabilities": {"inference": {"provider": "custom"}},
        "lifecycle": {"persistence": "session"},
        "registration_time": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    })


async def ws_connect_and_register(ws_url: str, manifest: AgentManifest) -> websockets.WebSocketClientProtocol:
    ws = await websockets.connect(ws_url)
    reg = Envelope(
        envelope_id=str(uuid.uuid4()),
        created_at=datetime.now(timezone.utc),
        envelope_type="request",
        sender=Endpoint(agent_id=manifest.agent_id, role=manifest.role),
        receiver=Endpoint(agent_id=manifest.agent_id, role=manifest.role),
        preamble=Preamble(intent={"goal": "register", "phase": "execution"}),
        payload={"content_type": "data", "data": {"manifest": manifest.model_dump()}},
    )
    await ws.send(reg.model_dump_json())
    ack_raw = await ws.recv()
    ack = json.loads(ack_raw)
    assert ack.get("type") == "registered", f"expected registered ack, got {ack}"
    return ws


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def kernel(tmp_path):
    import uvicorn

    settings = reset_settings_for_tests(
        db_path=tmp_path / "registry.db",
        heartbeat_interval_seconds=0.1,
        heartbeat_zombie_threshold_seconds=0.3,
        bus_router_poll_interval_seconds=0.005,
        bus_max_queue_size=1000,
    )
    from kernel.config import _settings as _live
    _live.paths.heartbeats_dir = tmp_path / "hb"
    (tmp_path / "hb").mkdir(parents=True, exist_ok=True)

    app = create_app(settings)
    port = _free_port()
    config = uvicorn.Config(
        app, host="127.0.0.1", port=port, log_level="error", lifespan="on"
    )
    server = uvicorn.Server(config)
    runner = asyncio.create_task(server.serve())
    for _ in range(50):
        if server.started:
            break
        await asyncio.sleep(0.05)
    assert server.started
    yield f"ws://127.0.0.1:{port}/ws", f"http://127.0.0.1:{port}"
    server.should_exit = True
    try:
        await runner
    except Exception:
        pass


@pytest_asyncio.fixture
async def kernel_with_agents(kernel):
    ws_url, http_url = kernel

    main_manifest = _manifest("main-agent", "orchestrator")
    main_ws = await ws_connect_and_register(ws_url, main_manifest)

    cond_manifest = _manifest("conductor", "orchestrator")
    cond_ws = await ws_connect_and_register(ws_url, cond_manifest)

    sm_manifest = _manifest("sector-manager-coding", "specialist")
    sm_ws = await ws_connect_and_register(ws_url, sm_manifest)

    yield {
        "ws_url": ws_url,
        "http_url": http_url,
        "main_ws": main_ws,
        "cond_ws": cond_ws,
        "sm_ws": sm_ws,
    }

    await main_ws.close()
    await cond_ws.close()
    await sm_ws.close()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_kernel_registers_all_agents(kernel_with_agents):
    """All three agents successfully register with the kernel."""
    import httpx
    _ws_url, http_url = kernel_with_agents["ws_url"], kernel_with_agents["http_url"]
    async with httpx.AsyncClient() as client:
        resp = await client.get(f"{http_url}/registry/agents")
        assert resp.status_code == 200
        agents = resp.json()
        ids = {a["agent_id"] for a in agents}
        assert "main-agent" in ids
        assert "conductor" in ids
        assert "sector-manager-coding" in ids


@pytest.mark.asyncio
async def test_kernel_health_endpoint_responds(kernel):
    """Kernel health endpoint works."""
    import httpx
    _ws_url, http_url = kernel
    async with httpx.AsyncClient() as client:
        resp = await client.get(f"{http_url}/health")
        assert resp.status_code == 200
        assert "status" in resp.json()


@pytest.mark.asyncio
async def test_ws_message_broadcast(kernel_with_agents):
    """A message sent by one agent is broadcast to others."""
    ws_url = kernel_with_agents["ws_url"]
    main_ws = kernel_with_agents["main_ws"]
    cond_ws = kernel_with_agents["cond_ws"]

    msg = {
        "envelope_id": str(uuid.uuid4()),
        "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "envelope_type": "request",
        "sender": {"agent_id": "main-agent", "role": "orchestrator"},
        "receiver": {"agent_id": "conductor", "role": "orchestrator"},
        "preamble": {"intent": {"goal": "ping", "phase": "execution"}},
        "payload": {"content_type": "data", "data": {"text": "hello"}},
    }
    await main_ws.send(json.dumps(msg))

    received = False
    for _ in range(20):
        try:
            raw = await asyncio.wait_for(cond_ws.recv(), timeout=0.5)
            data = json.loads(raw)
            if data.get("type") == "envelope":
                env = data.get("envelope", {})
                if env.get("sender", {}).get("agent_id") == "main-agent":
                    received = True
                    break
        except asyncio.TimeoutError:
            continue
    assert received, "Conductor never received message from MainAgent"