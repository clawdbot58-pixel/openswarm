"""Tests for the MainAgent class.

These tests stub out the WebSocket transport by constructing the
agent with ``_ws=None`` after start(), so the test can inspect
outbound envelopes without a real kernel. The in-process kernel
fixture is used for the few tests that need end-to-end behaviour.
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

# Make ``src`` importable.
_SRC = Path(__file__).resolve().parents[3] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from kernel.config import reset_settings_for_tests  # noqa: E402
from kernel.main import create_app  # noqa: E402
from kernel.models import (  # noqa: E402
    AgentManifest,
    Envelope,
    KERNEL_EVENT_NAMES,
)

from agents.llm_client import LLMClient  # noqa: E402
from agents.main_agent import (  # noqa: E402
    CONDUCTOR_AGENT_ID,
    SPAWN_INITIAL_SWARM_ACTION,
    SUBSCRIBED_KERNEL_EVENTS,
    MainAgent,
    SwarmStatusSummary,
    UserReply,
    load_main_agent_manifest,
)
from agents.objective_parser import (  # noqa: E402
    StructuredObjective,
    objective_to_spawn_payload,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _main_manifest(agent_id: str = "main-agent") -> AgentManifest:
    return AgentManifest.model_validate(
        {
            "agent_id": agent_id,
            "version": "1.0.0",
            "role": "orchestrator",
            "intent": "Translate user goals into structured swarm objectives",
            "capabilities": {"inference": {"provider": "custom"}},
            "lifecycle": {"persistence": "persistent", "auto_restart": True},
            "registration_time": datetime.now(timezone.utc).isoformat().replace(
                "+00:00", "Z"
            ),
        }
    )


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture
def fake_main_agent(mock_llm: LLMClient) -> MainAgent:
    """A Main Agent whose WebSocket transport is stubbed out.

    The fixture replaces :meth:`BaseAgent.send` with a synchronous
    spy that records the envelope and adds it to the outbox snapshot
    without actually serialising it to a socket.
    """
    agent = MainAgent(
        _main_manifest(),
        llm=mock_llm,
        kernel_rest_url="http://127.0.0.1:65535",  # never contacted
    )
    # Mark the agent as "registered" so ``send`` does not block.
    agent._registered_event.set()
    agent._ws = object()  # truthy sentinel

    sent: list = []

    async def _spy_send(envelope):
        sent.append(envelope)
        async with agent._send_lock:
            agent._outbox.append(envelope)
        return envelope

    # Replace the send method. We keep a reference to the original for
    # tests that want to assert on real failures.
    agent._real_send = agent.send  # type: ignore[attr-defined]
    agent.send = _spy_send  # type: ignore[assignment]
    agent.sent = sent  # type: ignore[attr-defined]
    return agent


@pytest_asyncio.fixture
async def kernel_server(tmp_path):
    """A real uvicorn-served kernel for end-to-end checks."""
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
    assert server.started
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
# Manifest loader
# ---------------------------------------------------------------------------

def test_load_main_agent_manifest_reads_from_disk(tmp_path) -> None:
    raw = json.loads(Path("manifests/main-agent.json").read_text())
    # Round-trip the loader on a known good manifest.
    loaded = load_main_agent_manifest("manifests/main-agent.json")
    assert loaded.agent_id == raw["agent_id"]
    assert loaded.role == raw["role"]


# ---------------------------------------------------------------------------
# Status query
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_status_query_short_circuits(fake_main_agent: MainAgent) -> None:
    """A status query returns a :class:`UserReply` without dispatching to conductor."""
    reply = await fake_main_agent.handle_user_message("how is the swarm?")
    # With kernel_rest_url pointing at an unreachable host, fetch fails
    # — but the parser still routes the message as a status query.
    assert reply.is_status is True or reply.is_error is True
    assert reply.objective is not None
    assert reply.objective.is_status_query is True


@pytest.mark.asyncio
async def test_status_query_fetches_real_kernel(
    fake_main_agent: MainAgent, kernel_server
) -> None:
    """When the kernel is reachable, the status reply summarises the registry."""
    _, http = kernel_server
    fake_main_agent._kernel_rest_url = http  # type: ignore[attr-defined]
    # Pre-register the main-agent and a few peers via REST.
    import httpx

    async with httpx.AsyncClient(base_url=http, timeout=5.0) as c:
        for aid in ("main-agent", "coder", "reviewer"):
            r = await c.post(
                "/registry/agents",
                json={"manifest": _main_manifest(aid).model_dump(mode="json")},
            )
            assert r.status_code == 201
    reply = await fake_main_agent.handle_user_message("status?")
    assert reply.is_status is True
    assert reply.is_error is False
    # The summary should mention the 3 agents.
    assert "3" in reply.text or "three" in reply.text.lower()


# ---------------------------------------------------------------------------
# Goal dispatch
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_goal_message_dispatches_to_conductor(
    fake_main_agent: MainAgent,
) -> None:
    """A coding goal produces a spawn_initial_swarm envelope to the conductor."""
    reply = await fake_main_agent.handle_user_message(
        "implement a /healthz endpoint in the API"
    )
    assert reply.is_error is False
    assert reply.is_status is False
    assert reply.objective is not None
    assert reply.objective.primary_sector == "coding"
    assert "coding" in reply.objective.suggested_sectors
    # The outbox should now contain a request to the conductor.
    outbox = fake_main_agent.outbox_snapshot()
    assert any(
        e["receiver"]["agent_id"] == CONDUCTOR_AGENT_ID
        and e["payload"]["data"]["action"] == SPAWN_INITIAL_SWARM_ACTION
        for e in outbox
    )
    assert reply.sent_envelope_id is not None


@pytest.mark.asyncio
async def test_main_agent_never_sends_to_workers(fake_main_agent: MainAgent) -> None:
    """The Main Agent must never address a worker directly."""
    await fake_main_agent.handle_user_message(
        "build a complete e-commerce checkout flow with cart, payments, and tests"
    )
    outbox = fake_main_agent.outbox_snapshot()
    for env in outbox:
        receiver = env["receiver"]["agent_id"]
        assert not receiver.startswith("worker-"), (
            f"main agent must not address workers directly, got {receiver}"
        )


@pytest.mark.asyncio
async def test_main_agent_never_emits_tool_envelopes(
    fake_main_agent: MainAgent,
) -> None:
    """The Main Agent must never send a tool payload."""
    await fake_main_agent.handle_user_message("build a CLI todo app")
    await fake_main_agent.handle_user_message("research the best queue library")
    outbox = fake_main_agent.outbox_snapshot()
    for env in outbox:
        assert env["payload"]["content_type"] != "tool", (
            "main agent must never emit tool envelopes"
        )


# ---------------------------------------------------------------------------
# Confidence threshold
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_low_confidence_message_returns_error(
    fake_main_agent: MainAgent,
) -> None:
    """A message that the parser is unsure about does not spawn a workflow."""
    # Bump the threshold so even a normal-looking goal trips the gate.
    fake_main_agent._objective_min_confidence = 0.99  # type: ignore[attr-defined]
    reply = await fake_main_agent.handle_user_message("build a thing")
    assert reply.is_error is True
    assert "confidence" in reply.text.lower() or "rephrase" in reply.text.lower()
    # And the outbox is empty.
    assert fake_main_agent.outbox_snapshot() == []


# ---------------------------------------------------------------------------
# Cancellation
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_cancellation_sends_user_cancel_event(
    fake_main_agent: MainAgent,
) -> None:
    reply = await fake_main_agent.handle_user_message("stop everything")
    assert reply.is_error is False
    assert reply.objective is not None
    assert reply.objective.is_cancellation is True
    outbox = fake_main_agent.outbox_snapshot()
    assert any(
        e["envelope_type"] == "event"
        and e["payload"]["data"].get("event") == "user_cancel"
        for e in outbox
    )


# ---------------------------------------------------------------------------
# LLM parsing
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_llm_parser_uses_llm_response(fake_main_agent: MainAgent) -> None:
    """A well-formed LLM JSON is honoured by the Main Agent."""
    payload = {
        "goal": "Build a CLI todo app with categories",
        "verb": "create",
        "primary_sector": "coding",
        "suggested_sectors": ["coding", "testing"],
        "needs_approval": True,
        "is_status_query": False,
        "is_cancellation": False,
        "confidence": 0.91,
        "notes": [],
    }

    class _StubProvider:
        name = "mock"
        calls = 0

        async def complete(self, request):  # noqa: D401
            from agents.llm_client import CompletionResult

            _StubProvider.calls += 1
            return CompletionResult(
                text=json.dumps(payload),
                model=request.model,
                provider=self.name,
            )

    fake_main_agent._llm = LLMClient(  # type: ignore[attr-defined]
        router=__import__(
            "agents.llm_client", fromlist=["ModelRoute", "ModelRouter"]
        ).ModelRouter(
            [
                __import__(
                    "agents.llm_client", fromlist=["ModelRoute"]
                ).ModelRoute("mock", "m")
            ],
            providers={"mock": _StubProvider()},
        ),
    )
    reply = await fake_main_agent.handle_user_message("make me a todo app")
    assert reply.objective is not None
    assert reply.objective.goal == payload["goal"]
    assert reply.objective.confidence == pytest.approx(0.91)


# ---------------------------------------------------------------------------
# Kernel event surface
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_agent_zombie_event_surfaces_to_user(
    fake_main_agent: MainAgent,
) -> None:
    """A ``agent_zombie`` event becomes a user-facing warning."""
    await fake_main_agent.on_event(
        "agent_zombie", {"agent_id": "sector-manager-coding"}
    )
    # The reply should be queued.
    reply = await asyncio.wait_for(fake_main_agent._user_replies.get(), timeout=1.0)
    assert "sector-manager-coding" in reply.text


@pytest.mark.asyncio
async def test_permission_denied_surfaces_to_user(
    fake_main_agent: MainAgent,
) -> None:
    await fake_main_agent.on_event(
        "permission_denied",
        {"sender": "worker-x", "reason": "fs:read outside allowlist"},
    )
    reply = await asyncio.wait_for(fake_main_agent._user_replies.get(), timeout=1.0)
    assert "permission denied" in reply.text.lower() or "worker-x" in reply.text


@pytest.mark.asyncio
async def test_subscribed_events_set_matches_kernel_known(
    fake_main_agent: MainAgent,
) -> None:
    """Every event the Main Agent subscribes to is a kernel-known event."""
    for name in SUBSCRIBED_KERNEL_EVENTS:
        assert name in KERNEL_EVENT_NAMES, name


# ---------------------------------------------------------------------------
# SwarmStatusSummary rendering
# ---------------------------------------------------------------------------

def test_swarm_status_summary_renders_natural_language() -> None:
    summary = SwarmStatusSummary(
        main_agent_status="ready",
        main_agent_last_heartbeat="2026-06-04T12:00:00Z",
        total_agents=3,
        status_counts={"ready": 2, "busy": 1},
        connected=2,
        sample_agents=[{"agent_id": "a"}, {"agent_id": "b"}, {"agent_id": "c"}],
    )
    text = summary.to_user_text()
    assert "3" in text
    assert "ready=2" in text
    assert "Main Agent status = `ready`" in text


def test_swarm_status_summary_warns_when_main_is_zombie() -> None:
    summary = SwarmStatusSummary(
        main_agent_status="zombie",
        main_agent_last_heartbeat=None,
        total_agents=1,
        status_counts={"zombie": 1},
        connected=0,
        sample_agents=[],
    )
    text = summary.to_user_text()
    assert "zombie" in text.lower()
