"""Shared pytest fixtures for the Phase 2 user-facing agent tests.

Every test gets:

* a fresh in-process kernel (registry + bus + permissions +
  heartbeat) running on a temp DB;
* a small set of helper functions to build manifests and envelopes
  matching the kernel's contract;
* an LLMClient wired to the in-process ``mock`` provider so
  parsing tests are deterministic.

The kernel runs as a real FastAPI app under uvicorn (in a background
thread) when the test needs WebSocket connectivity, and as an
in-process harness when only the bus / registry is needed.
"""
from __future__ import annotations

import asyncio
import json
import os
import socket
import sys
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator, Iterator

import pytest
import pytest_asyncio

# Make ``src`` importable.
_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_SRC = _PROJECT_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from kernel.bus import MessageBus  # noqa: E402
from kernel.config import (  # noqa: E402
    KernelSettings,
    reset_settings_for_tests,
)
from kernel.models import (  # noqa: E402
    AgentManifest,
    Endpoint,
    Envelope,
    Preamble,
)
from kernel.permissions import PermissionEnforcer  # noqa: E402
from kernel.registry import AgentRegistry  # noqa: E402

from agents.llm_client import LLMClient, ModelRoute  # noqa: E402


# ---------------------------------------------------------------------------
# In-process kernel harness (no HTTP / WS)
# ---------------------------------------------------------------------------

class InProcessKernel:
    """Bundle of the kernel's in-process collaborators for one test."""

    def __init__(
        self,
        settings: KernelSettings,
        registry: AgentRegistry,
        permissions: PermissionEnforcer,
        bus: MessageBus,
    ) -> None:
        self.settings = settings
        self.registry = registry
        self.permissions = permissions
        self.bus = bus

    async def aclose(self) -> None:
        try:
            await self.bus.stop()
        except Exception:
            pass
        try:
            await self.registry.close()
        except Exception:
            pass


@pytest_asyncio.fixture
async def inproc_kernel() -> AsyncIterator[InProcessKernel]:
    """Spin up an in-process kernel harness (no HTTP/WS)."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        settings = reset_settings_for_tests(
            db_path=tmp_path / "registry.db",
            heartbeat_interval_seconds=0.05,
            heartbeat_zombie_threshold_seconds=0.2,
            bus_router_poll_interval_seconds=0.005,
            bus_max_queue_size=1000,
        )
        from kernel.config import _settings as _live

        _live.paths.heartbeats_dir = tmp_path / "hb"  # type: ignore[attr-defined]
        (tmp_path / "hb").mkdir(parents=True, exist_ok=True)
        registry = AgentRegistry(settings.db_path)
        await registry.initialize()
        permissions = PermissionEnforcer(registry)
        bus = MessageBus(registry, permissions, settings)
        registry._bus = bus  # type: ignore[attr-defined]
        await bus.start()
        try:
            yield InProcessKernel(
                settings=settings,
                registry=registry,
                permissions=permissions,
                bus=bus,
            )
        finally:
            await InProcessKernel(
                settings, registry, permissions, bus
            ).aclose()


# ---------------------------------------------------------------------------
# Real kernel app (HTTP + WS) for integration tests
# ---------------------------------------------------------------------------

def _free_port() -> int:
    """Return an unused TCP port on localhost."""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture
def kernel_app(tmp_path):
    """Return a (FastAPI app, settings) pair bound to a temp DB."""
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
    from kernel.main import create_app

    app = create_app(settings)
    return app, settings


# ---------------------------------------------------------------------------
# Manifest / envelope helpers
# ---------------------------------------------------------------------------

def make_manifest(
    agent_id: str,
    *,
    role: str = "executor",
    intent: str | None = None,
    category: str = "custom",
    tools: list[dict[str, Any]] | None = None,
    auto_restart: bool = False,
    persistence: str = "ephemeral",
    permissions: dict[str, Any] | None = None,
) -> AgentManifest:
    """Build a minimal valid manifest for tests."""
    payload: dict[str, Any] = {
        "agent_id": agent_id,
        "version": "1.0.0",
        "role": role,
        "intent": intent or f"test {agent_id}",
        "category": category,
        "tags": ["test"],
        "capabilities": {
            "inference": {"provider": "custom", "default_model": "test-model"},
            "tools": tools or [],
        },
        "lifecycle": {
            "persistence": persistence,
            "auto_restart": auto_restart,
        },
        "registration_time": datetime.now(timezone.utc).isoformat().replace(
            "+00:00", "Z"
        ),
    }
    if permissions is not None:
        payload["permissions"] = permissions
    return AgentManifest.model_validate(payload)


def make_envelope(
    sender_id: str,
    receiver_id: str,
    payload: dict[str, Any] | None = None,
    *,
    sender_role: str = "executor",
    receiver_role: str = "executor",
    envelope_type: str = "request",
    reply_to: str | None = None,
) -> Envelope:
    """Build a valid Envelope for tests."""
    return Envelope(
        envelope_id=str(uuid.uuid4()),
        created_at=datetime.now(timezone.utc),
        envelope_type=envelope_type,  # type: ignore[arg-type]
        sender=Endpoint(agent_id=sender_id, role=sender_role),  # type: ignore[arg-type]
        receiver=Endpoint(agent_id=receiver_id, role=receiver_role),  # type: ignore[arg-type]
        reply_to=reply_to,
        preamble=Preamble(intent={"goal": "test", "phase": "execution"}),
        payload=payload or {"content_type": "data", "data": {}},  # type: ignore[arg-type]
    )


# ---------------------------------------------------------------------------
# LLM client fixture (mock provider)
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def mock_llm() -> AsyncIterator[LLMClient]:
    """LLMClient backed by the in-process mock provider."""
    client = LLMClient.from_config(
        {
            "routes": [
                {"provider": "mock", "model": "mock-model"},
            ],
        }
    )
    yield client


# ---------------------------------------------------------------------------
# pytest-asyncio config
# ---------------------------------------------------------------------------

def pytest_collection_modifyitems(config, items):  # noqa: D401
    """Mark async test functions for pytest-asyncio."""
    for item in items:
        if "asyncio" in item.keywords:
            continue
        if asyncio.iscoroutinefunction(getattr(item, "function", None)):
            item.add_marker(pytest.mark.asyncio)
