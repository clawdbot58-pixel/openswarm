"""Shared pytest fixtures for the dashboard backend tests.

The fixtures wire a real kernel harness (registry + bus + settings)
plus an in-memory persistent memory store and a real loop registry.
Tests can use either:

* ``dashboard_harness`` — the raw collaborators;
* ``dashboard_client`` — an ``httpx.AsyncClient`` against a fully
  mounted FastAPI app (uses ``ASGITransport``).
"""
from __future__ import annotations

import sys
import uuid
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import pytest_asyncio

# Make ``src`` importable.
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_SRC = _PROJECT_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from dashboard.backend.aggregator import DataAggregator  # noqa: E402
from dashboard.backend.cache import AggregateCache  # noqa: E402
from dashboard.backend.config import ConfigAPI  # noqa: E402
from dashboard.backend.introspection import IntrospectionAPI  # noqa: E402
from dashboard.backend.main import create_dashboard_app  # noqa: E402
from dashboard.backend.stream import EventStream  # noqa: E402
from kernel.bus import MessageBus  # noqa: E402
from kernel.config import reset_settings_for_tests  # noqa: E402
from kernel.permissions import PermissionEnforcer  # noqa: E402
from kernel.registry import AgentRegistry  # noqa: E402
from loops.registry import create_registry  # noqa: E402
from memory.persistent import PersistentMemory  # noqa: E402

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def dashboard_harness(tmp_path) -> AsyncIterator[dict[str, Any]]:
    """Spin up a fully wired dashboard backend harness.

    Yields a dict with the registry, bus, settings, persistent
    memory, loop registry, introspection, config, cache, and the
    FastAPI app.
    """
    # Kernel settings with temp DB and fast heartbeats.
    settings = reset_settings_for_tests(
        db_path=tmp_path / "registry.db",
        heartbeat_interval_seconds=0.1,
        heartbeat_zombie_threshold_seconds=0.5,
        bus_router_poll_interval_seconds=0.005,
    )
    from kernel.config import _settings as _live

    _live.paths.heartbeats_dir = tmp_path / "hb"  # type: ignore[attr-defined]
    (tmp_path / "hb").mkdir(parents=True, exist_ok=True)

    # Kernel collaborators.
    registry = AgentRegistry(settings.db_path)
    await registry.initialize()
    permissions = PermissionEnforcer(registry)
    bus = MessageBus(registry, permissions, settings)
    registry._bus = bus  # type: ignore[attr-defined]
    await bus.start()

    # Persistent memory in temp file.
    memory = PersistentMemory(tmp_path / "memory.db")
    await memory.initialize()

    # Loop registry in temp file with premade templates.
    loop_registry = create_registry(str(tmp_path / "loops.db"))

    # Dashboard collaborators.
    introspection = IntrospectionAPI(
        registry=registry,
        bus=bus,
        settings=settings,
        persistent_memory=memory,
        loop_registry=loop_registry,
        workspaces_dir=tmp_path / "workspaces",
    )
    config = ConfigAPI(tmp_path / "dashboard.db")
    cache = AggregateCache()

    aggregator = DataAggregator(
        introspection=introspection,
        cache=cache,
        fast_interval_seconds=0.2,
        slow_interval_seconds=0.4,
    )
    stream = EventStream(introspection=introspection, heartbeat_interval_seconds=0.5)
    await stream.attach(bus)
    await stream.start()

    # Seed a minimal agent so get_agents() returns non-empty list.
    # Some snapshot tests fail with empty registries due to edge cases.
    await registry.register(
        __import__("kernel.models", fromlist=["AgentManifest"]).AgentManifest.model_validate(
            {
                "agent_id": "test-agent",
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

    # Start aggregator so the cache is pre-populated for snapshot tests.
    # Also seed the cache directly so tests don't depend on aggregator timing.
    from dashboard.backend.models import SystemMetrics

    await aggregator.start()
    await cache.set(  # type: ignore[attr-defined]
        "system_metrics",
        SystemMetrics(
            total_agents=1,
            active_agents=1,
            zombie_agents=0,
            busy_agents=0,
            idle_agents=1,
            total_workflows=0,
            running_workflows=0,
            completed_workflows=0,
            failed_workflows=0,
            messages_per_minute=0.0,
            avg_loop_latency_ms=0.0,
            total_cost_today_usd=0.0,
            uptime_seconds=0.0,
            queue_total=0,
            started_at=datetime.now(timezone.utc),
        ),
    )

    # Wrap the FastAPI app so the lifespan is accessible as a context
    # manager for use with TestClient and similar tools.

    async def _startup():
        pass

    async def _shutdown():
        await stream.stop()
        await aggregator.stop()
        await config.close()
        await memory.close()
        try:
            await bus.stop()
        except Exception:
            pass
        await registry.close()

    app = create_dashboard_app(
        introspection=introspection,
        config=config,
        cache=cache,
        aggregator=aggregator,
        stream=stream,
        enable_aggregator=False,
        enable_stream=False,
    )

    harness = {
        "settings": settings,
        "registry": registry,
        "bus": bus,
        "memory": memory,
        "loops": loop_registry,
        "introspection": introspection,
        "config": config,
        "cache": cache,
        "aggregator": aggregator,
        "stream": stream,
        "app": app,
        "workspaces_dir": tmp_path / "workspaces",
    }
    try:
        yield harness
    finally:
        await stream.stop()
        await aggregator.stop()
        await config.close()
        await memory.close()
        try:
            await bus.stop()
        except Exception:
            pass
        await registry.close()


@pytest_asyncio.fixture
async def dashboard_client(dashboard_harness) -> AsyncIterator[httpx.AsyncClient]:
    """An ``httpx.AsyncClient`` against the dashboard FastAPI app."""
    transport = httpx.ASGITransport(app=dashboard_harness["app"])
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        # Open the lifespan manually because ASGITransport does not.
        async with dashboard_harness["app"].router.lifespan_context(
            dashboard_harness["app"]
        ):
            yield client


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_manifest(
    agent_id: str,
    *,
    role: str = "executor",
    category: str = "coding",
    tags: list[str] | None = None,
    status: str = "ready",
) -> dict[str, Any]:
    """Build a valid manifest dict for tests."""
    return {
        "agent_id": agent_id,
        "version": "1.0.0",
        "role": role,
        "human_readable_name": f"Test {agent_id}",
        "intent": f"test agent {agent_id}",
        "category": category,
        "tags": tags or [],
        "capabilities": {"inference": {"provider": "anthropic"}},
        "lifecycle": {"persistence": "ephemeral"},
        "registration_time": datetime.now(timezone.utc).isoformat().replace(
            "+00:00", "Z"
        ),
        "status": status,
    }


def make_envelope(
    sender_id: str = "main-agent",
    receiver_id: str = "coder",
    *,
    envelope_type: str = "request",
    content_type: str = "text",
    content: str = "hello",
) -> dict[str, Any]:
    """Build a valid envelope dict for tests."""
    payload: dict[str, Any]
    if content_type == "data":
        payload = {"content_type": "data", "data": {"hello": content}}
    else:
        payload = {"content_type": content_type, "content": content}
    return {
        "envelope_id": str(uuid.uuid4()),
        "created_at": datetime.now(timezone.utc).isoformat().replace(
            "+00:00", "Z"
        ),
        "envelope_type": envelope_type,
        "sender": {"agent_id": sender_id, "role": "orchestrator"},
        "receiver": {"agent_id": receiver_id, "role": "executor"},
        "preamble": {"intent": {"goal": "test", "phase": "execution"}},
        "payload": payload,
    }
