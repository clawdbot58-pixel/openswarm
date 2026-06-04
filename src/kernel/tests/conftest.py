"""Shared pytest fixtures for the kernel test suite.

Every test gets a temporary directory for SQLite + heartbeats so the
real ``data/`` and ``heartbeats/`` trees are never touched.  The
``kernel_test`` fixture returns a fully-wired :class:`KernelHarness`
that bundles the registry, bus, permissions, settings, and an in-process
helper for sending envelopes.
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncIterator

import pytest
import pytest_asyncio

# Make ``src`` importable when tests are run from the project root.
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_SRC = _PROJECT_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from kernel.bus import MessageBus  # noqa: E402
from kernel.config import KernelSettings, reset_settings_for_tests  # noqa: E402
from kernel.models import (  # noqa: E402
    AgentManifest,
    Endpoint,
    Envelope,
    Preamble,
)
from kernel.permissions import PermissionEnforcer  # noqa: E402
from kernel.registry import AgentRegistry  # noqa: E402


# pytest-asyncio configuration: each async test gets its own loop.
pytest_plugins = ["pytest_asyncio"]


def pytest_collection_modifyitems(config, items):
    """Mark async test functions for pytest-asyncio."""
    for item in items:
        if "asyncio" in item.keywords:
            continue
        if asyncio.iscoroutinefunction(getattr(item, "function", None)):
            item.add_marker(pytest.mark.asyncio)


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------

@dataclass
class KernelHarness:
    """Bundle of the kernel's collaborators for one test."""

    settings: KernelSettings
    registry: AgentRegistry
    permissions: PermissionEnforcer
    bus: MessageBus

    async def aclose(self) -> None:
        """Tear down in reverse order."""
        try:
            await self.bus.stop()
        except Exception:
            pass
        await self.registry.close()

    def make_manifest(
        self,
        agent_id: str = "main-agent",
        *,
        role: str = "orchestrator",
        tools: list[dict] | None = None,
        permissions: dict | None = None,
        auto_restart: bool = False,
        version: str = "1.0.0",
    ) -> AgentManifest:
        """Build a minimal valid manifest for tests."""
        return AgentManifest.model_validate(
            {
                "agent_id": agent_id,
                "version": version,
                "role": role,
                "intent": f"test agent {agent_id}",
                "capabilities": {
                    "inference": {"provider": "anthropic"},
                    "tools": tools or [],
                },
                "permissions": permissions,
                "lifecycle": {
                    "persistence": "ephemeral",
                    "auto_restart": auto_restart,
                },
                "registration_time": datetime.now(timezone.utc).isoformat().replace(
                    "+00:00", "Z"
                ),
            }
        )

    def make_envelope(
        self,
        sender_id: str = "main-agent",
        receiver_id: str = "coder",
        content: str = "hello",
        *,
        envelope_type: str = "request",
        priority: int = 5,
        content_type: str = "text",
        payload_extra: dict | None = None,
        reply_to: str | None = None,
        tool_name: str | None = None,
        tool_action: str = "invoke",
        tool_params: dict | None = None,
    ) -> Envelope:
        """Build a valid envelope for tests."""
        if content_type == "tool":
            payload: dict = {
                "content_type": "tool",
                "tool_name": tool_name or "fs.read",
                "action": tool_action,
                "parameters": tool_params or {},
            }
        else:
            payload = {"content_type": "text", "content": content}
            if payload_extra:
                payload.update(payload_extra)
        return Envelope(
            envelope_id=str(uuid.uuid4()),
            created_at=datetime.now(timezone.utc),
            envelope_type=envelope_type,  # type: ignore[arg-type]
            sender=Endpoint(agent_id=sender_id, role="orchestrator"),
            receiver=Endpoint(agent_id=receiver_id, role="executor"),
            reply_to=reply_to,
            preamble=Preamble(
                intent={"goal": "test", "phase": "execution"},
            ),
            payload=payload,  # type: ignore[arg-type]
            metadata={"priority": priority},
        )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def kernel_test() -> AsyncIterator[KernelHarness]:
    """Spin up an isolated kernel harness.

    Yields a :class:`KernelHarness` and tears it down on exit. The bus
    is started so router-level behavior is testable.
    """
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        # Heartbeats go to a per-test directory so file polling is
        # isolated from any other test.
        hb_dir = tmp_path / "hb"
        hb_dir.mkdir()
        # Allow env override for fast polling.
        interval = float(os.environ.get("KERNEL_TEST_HB_INTERVAL", "0.05"))
        threshold = float(os.environ.get("KERNEL_TEST_HB_THRESHOLD", "0.2"))
        router_poll = float(os.environ.get("KERNEL_TEST_ROUTER_POLL", "0.005"))
        settings = reset_settings_for_tests(
            db_path=tmp_path / "registry.db",
            heartbeat_interval_seconds=interval,
            heartbeat_zombie_threshold_seconds=threshold,
            bus_router_poll_interval_seconds=router_poll,
            bus_max_queue_size=int(os.environ.get("KERNEL_TEST_QUEUE_MAX", "1000")),
        )
        # Point the heartbeats dir at the per-test location.
        from kernel.config import _settings as _live_settings  # noqa: WPS433

        # Patch the heartbeats_dir on the live settings.
        _live_settings.paths.heartbeats_dir = hb_dir  # type: ignore[attr-defined]
        # Ensure it exists (the property only creates the data dir).
        hb_dir.mkdir(parents=True, exist_ok=True)

        registry = AgentRegistry(settings.db_path)
        await registry.initialize()
        permissions = PermissionEnforcer(registry)
        bus = MessageBus(registry, permissions, settings)
        registry._bus = bus  # type: ignore[attr-defined]
        await bus.start()

        harness = KernelHarness(
            settings=settings,
            registry=registry,
            permissions=permissions,
            bus=bus,
        )
        try:
            yield harness
        finally:
            await harness.aclose()
