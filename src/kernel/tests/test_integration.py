"""End-to-end integration test for the kernel.

Exercises the FastAPI app (REST + WebSocket) over real HTTP/WS using
``httpx.ASGITransport`` and the ``websockets`` library. The test:

1. Boots a kernel app with a temp DB.
2. Connects three agents over WebSocket.
3. Sends envelopes between them.
4. Kills one client mid-test.
5. Verifies zombie detection within the configured threshold.
6. Verifies the surviving clients can still exchange messages.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import pytest
import websockets

# Make ``src`` importable.
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_SRC = _PROJECT_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from kernel.config import KernelSettings, reset_settings_for_tests  # noqa: E402
from kernel.main import create_app  # noqa: E402
from kernel.models import AgentManifest, Endpoint, Envelope, Preamble  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def fast_app_url(tmp_path):
    """Build an app bound to a temp DB and an ephemeral port via ASGITransport."""
    settings = reset_settings_for_tests(
        db_path=tmp_path / "registry.db",
        heartbeat_interval_seconds=0.1,
        heartbeat_zombie_threshold_seconds=0.3,
        bus_router_poll_interval_seconds=0.005,
    )
    from kernel.config import _settings as _live

    _live.paths.heartbeats_dir = tmp_path / "hb"  # type: ignore[attr-defined]
    (tmp_path / "hb").mkdir(parents=True, exist_ok=True)
    app = create_app(settings)
    return app, settings


def _manifest(agent_id: str, role: str = "executor") -> dict:
    return {
        "agent_id": agent_id,
        "version": "1.0.0",
        "role": role,
        "intent": f"test {agent_id}",
        "capabilities": {"inference": {"provider": "anthropic"}},
        "lifecycle": {"persistence": "ephemeral"},
        "registration_time": datetime.now(timezone.utc).isoformat().replace(
            "+00:00", "Z"
        ),
    }


def _register_envelope(manifest: dict) -> dict:
    return {
        "envelope_id": str(uuid.uuid4()),
        "created_at": datetime.now(timezone.utc).isoformat().replace(
            "+00:00", "Z"
        ),
        "envelope_type": "request",
        "sender": {"agent_id": manifest["agent_id"], "role": manifest["role"]},
        "receiver": {"agent_id": manifest["agent_id"], "role": manifest["role"]},
        "preamble": {"intent": {"goal": "register", "phase": "execution"}},
        "payload": {"content_type": "data", "data": {"manifest": manifest}},
    }


def _msg_envelope(
    sender: str, receiver: str, content: str, sender_role: str = "executor"
) -> dict:
    return {
        "envelope_id": str(uuid.uuid4()),
        "created_at": datetime.now(timezone.utc).isoformat().replace(
            "+00:00", "Z"
        ),
        "envelope_type": "request",
        "sender": {"agent_id": sender, "role": sender_role},
        "receiver": {"agent_id": receiver, "role": "executor"},
        "preamble": {"intent": {"goal": "chat", "phase": "execution"}},
        "payload": {"content_type": "text", "content": content},
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_full_lifecycle_three_clients_zombie_and_survivors(
    fast_app_url, monkeypatch
):
    """Boot the app, run 3 clients, kill one, verify zombie + survivors."""
    app, settings = fast_app_url

    # Override the websocket endpoint URL construction by using the ASGI
    # transport's WebSocket support via httpx? httpx doesn't ship WS. We
    # need a real socket. So we use uvicorn in a background thread.
    import socket
    import threading

    import uvicorn

    # Pick a free port.
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=port,
        log_level="warning",
        lifespan="on",
    )
    server = uvicorn.Server(config)

    runner_task = asyncio.create_task(server.serve())
    # Wait for the server to be ready.
    for _ in range(50):
        if server.started:
            break
        await asyncio.sleep(0.05)
    assert server.started, "uvicorn did not start"

    base_url = f"ws://127.0.0.1:{port}/ws"
    http_url = f"http://127.0.0.1:{port}"

    try:
        # Pre-register the main-agent via the REST API so the bus has a
        # valid sender for orchestrator-originated envelopes.
        async with httpx.AsyncClient(base_url=http_url, timeout=5.0) as client:
            r = await client.post(
                "/registry/agents",
                json={"manifest": _manifest("main-agent", "orchestrator")},
            )
            assert r.status_code == 201, r.text
            r = await client.post(
                "/registry/agents",
                json={"manifest": _manifest("coder", "executor")},
            )
            assert r.status_code == 201
            r = await client.post(
                "/registry/agents",
                json={"manifest": _manifest("reviewer", "executor")},
            )
            assert r.status_code == 201

        # Connect three WS clients.
        async def connect_and_register(agent_id: str) -> Any:
            ws = await websockets.connect(base_url)
            await ws.send(json.dumps(_register_envelope(_manifest(agent_id))))
            ack_raw = await asyncio.wait_for(ws.recv(), timeout=3.0)
            ack = json.loads(ack_raw)
            assert ack["type"] == "registered", ack
            return ws

        ws_coder, ws_reviewer, ws_main = await asyncio.gather(
            connect_and_register("coder"),
            connect_and_register("reviewer"),
            connect_and_register("main-agent"),
        )

        try:
            # Drain the initial ack for main-agent (it received its own
            # register envelope via the bus echo). Actually we excluded
            # the sender from broadcast, but each agent's subscriber
            # is the WS itself, so they will receive the envelopes the
            # bus is about to send. Each WS handler returns to the read
            # loop immediately after registering. There are no messages
            # in flight yet, so recv() will block until we send.
            # We send a message from main-agent → coder.
            await ws_main.send(
                json.dumps(_msg_envelope("main-agent", "coder", "ping 1"))
            )
            # Wait for coder to receive the message.
            msg_raw = await asyncio.wait_for(ws_coder.recv(), timeout=3.0)
            msg = json.loads(msg_raw)
            assert msg["type"] == "envelope"
            assert msg["envelope"]["payload"]["content"] == "ping 1"

            # And a broadcast from main-agent.
            await ws_main.send(
                json.dumps(
                    {
                        "envelope_id": str(uuid.uuid4()),
                        "created_at": datetime.now(timezone.utc)
                        .isoformat()
                        .replace("+00:00", "Z"),
                        "envelope_type": "request",
                        "sender": {
                            "agent_id": "main-agent",
                            "role": "orchestrator",
                        },
                        "receiver": {"agent_id": "*", "role": "executor"},
                        "preamble": {
                            "intent": {"goal": "broadcast", "phase": "execution"}
                        },
                        "payload": {
                            "content_type": "text",
                            "content": "hello all",
                        },
                    }
                )
            )
            # Both coder and reviewer should receive it.
            received = []
            for ws in (ws_coder, ws_reviewer):
                raw = await asyncio.wait_for(ws.recv(), timeout=3.0)
                m = json.loads(raw)
                assert m["type"] == "envelope"
                received.append(m["envelope"]["payload"]["content"])
            assert received == ["hello all", "hello all"]

            # Now kill the reviewer.
            await ws_reviewer.close()
            # Poll the registry for zombie status. Threshold is 0.3s;
            # the monitor polls every 0.1s.
            zombie_seen = False
            for _ in range(40):  # up to 4s
                async with httpx.AsyncClient(
                    base_url=http_url, timeout=5.0
                ) as client:
                    r = await client.get(
                        "/registry/agents/reviewer/status"
                    )
                    if r.status_code == 200:
                        st = r.json()
                        if st["status"] == "zombie":
                            zombie_seen = True
                            break
                await asyncio.sleep(0.1)
            assert zombie_seen, "reviewer was not marked zombie in time"

            # Surviving clients still work: send a message from main → coder.
            await ws_main.send(
                json.dumps(_msg_envelope("main-agent", "coder", "still alive"))
            )
            raw = await asyncio.wait_for(ws_coder.recv(), timeout=3.0)
            m = json.loads(raw)
            assert m["envelope"]["payload"]["content"] == "still alive"
        finally:
            for ws in (ws_coder, ws_reviewer, ws_main):
                try:
                    await ws.close()
                except Exception:  # noqa: BLE001
                    pass
    finally:
        server.should_exit = True
        await runner_task


@pytest.mark.asyncio
async def test_api_returns_422_not_500_on_custom_validator_error(
    fast_app_url,
):
    """Regression: a custom field_validator raising ValueError used to 500
    because Pydantic's ``ctx.error`` carries an unserializable exception
    object. The API must coerce it and return 422."""
    import socket
    import uvicorn

    app, _settings = fast_app_url
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    config = uvicorn.Config(
        app, host="127.0.0.1", port=port, log_level="error", lifespan="on"
    )
    server = uvicorn.Server(config)
    runner = asyncio.create_task(server.serve())
    for _ in range(50):
        if server.started: break
        await asyncio.sleep(0.05)
    try:
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{port}", timeout=5.0
        ) as c:
            # Bad agent_id triggers the custom field_validator that raises ValueError.
            env = {
                "envelope_id": "11111111-1111-1111-1111-111111111111",
                "created_at": datetime.now(timezone.utc).isoformat().replace(
                    "+00:00", "Z"
                ),
                "envelope_type": "request",
                "sender": {"agent_id": "main-agent", "role": "orchestrator"},
                "receiver": {"agent_id": "BadId", "role": "executor"},
                "preamble": {"intent": {"goal": "x", "phase": "execution"}},
                "payload": {"content_type": "text", "content": "hi"},
            }
            r = await c.post("/bus/send", json={"envelope": env})
            assert r.status_code == 422, f"got {r.status_code}: {r.text}"
            body = r.json()
            # Body must be JSON-parseable and include the rejection code.
            assert body["detail"]["code"] == "envelope_rejected"
    finally:
        server.should_exit = True
        await runner
