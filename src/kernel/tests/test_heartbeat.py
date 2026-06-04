"""Tests for the :class:`~kernel.heartbeat.HeartbeatMonitor`.

Covers:

* write a heartbeat file → registry status updates
* age the file past the zombie threshold → agent is marked zombie and
  a ``agent_zombie`` event is emitted to the main agent
* auto-restart manifests emit ``auto_restart_triggered``
* missing file for a registered agent is treated as a zombie
* cleanup removes files for unregistered agents
* inbound WS heartbeats refresh the timestamp
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from kernel.heartbeat import HeartbeatMonitor
from kernel.models import AgentManifest


def _manifest(agent_id: str = "coder", auto_restart: bool = False) -> AgentManifest:
    return AgentManifest.model_validate(
        {
            "agent_id": agent_id,
            "version": "1.0.0",
            "role": "executor",
            "intent": f"test {agent_id}",
            "capabilities": {"inference": {"provider": "anthropic"}},
            "lifecycle": {
                "persistence": "ephemeral",
                "auto_restart": auto_restart,
            },
            "registration_time": "2026-06-04T10:00:00Z",
        }
    )


def _write_hb(dir_: Path, agent_id: str, status: str = "ready") -> Path:
    p = dir_ / f"{agent_id}.json"
    p.write_text(
        json.dumps(
            {
                "agent_id": agent_id,
                "timestamp": datetime.now(timezone.utc).isoformat().replace(
                    "+00:00", "Z"
                ),
                "status": status,
            }
        )
    )
    return p


def _make_monitor(kernel_test) -> HeartbeatMonitor:
    """Construct a monitor pointing at the per-test heartbeats dir."""
    from kernel.config import _settings as _live

    mon = HeartbeatMonitor(kernel_test.registry, kernel_test.bus, kernel_test.settings)
    mon._dir = _live.paths.heartbeats_dir  # type: ignore[attr-defined]
    return mon


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_heartbeat_file_updates_registry_status(kernel_test):
    mon = _make_monitor(kernel_test)
    await kernel_test.registry.register(_manifest("coder"))
    p = _write_hb(mon._dir, "coder", status="busy")  # type: ignore[attr-defined]
    summary = await mon.tick()
    assert "coder" in summary["alive"]
    st = await kernel_test.registry.get_status("coder")
    assert st["status"] == "busy"
    assert st["last_heartbeat"] is not None


@pytest.mark.asyncio
async def test_stale_heartbeat_marks_zombie_and_emits_event(kernel_test):
    mon = _make_monitor(kernel_test)
    # Use a very short threshold for the test.
    kernel_test.settings.heartbeat_zombie_threshold_seconds = 0.1
    await kernel_test.registry.register(_manifest("coder"))
    p = _write_hb(mon._dir, "coder")
    # Make the file old.
    old = time.time() - 1.0
    os.utime(p, (old, old))
    received: list = []
    kernel_test.bus.add_event_listener(received.append)
    summary = await mon.tick()
    assert "coder" in summary["zombies"]
    st = await kernel_test.registry.get_status("coder")
    assert st["status"] == "zombie"
    # Wait for the kernel event to be delivered to listeners.
    for _ in range(20):
        if any(
            e.payload.data.get("event") == "agent_zombie"  # type: ignore[union-attr]
            for e in received
        ):
            break
        await asyncio.sleep(0.02)
    events = [
        e
        for e in received
        if e.payload.data.get("event") == "agent_zombie"  # type: ignore[union-attr]
    ]
    assert events
    assert events[0].payload.data.get("agent_id") == "coder"  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_auto_restart_manifest_triggers_event(kernel_test):
    mon = _make_monitor(kernel_test)
    kernel_test.settings.heartbeat_zombie_threshold_seconds = 0.1
    await kernel_test.registry.register(_manifest("coder", auto_restart=True))
    p = _write_hb(mon._dir, "coder")
    os.utime(p, (time.time() - 1.0, time.time() - 1.0))
    received: list = []
    kernel_test.bus.add_event_listener(received.append)
    await mon.tick()
    for _ in range(20):
        if any(
            e.payload.data.get("event") == "auto_restart_triggered"  # type: ignore[union-attr]
            for e in received
        ):
            break
        await asyncio.sleep(0.02)
    events = [
        e
        for e in received
        if e.payload.data.get("event") == "auto_restart_triggered"  # type: ignore[union-attr]
    ]
    assert events


@pytest.mark.asyncio
async def test_missing_heartbeat_file_marks_zombie(kernel_test):
    mon = _make_monitor(kernel_test)
    kernel_test.settings.heartbeat_zombie_threshold_seconds = 0.0
    await kernel_test.registry.register(_manifest("ghost"))
    # No file on disk for "ghost".
    summary = await mon.tick()
    assert "ghost" in summary["zombies"]
    st = await kernel_test.registry.get_status("ghost")
    assert st["status"] == "zombie"


@pytest.mark.asyncio
async def test_cleanup_removes_orphan_files(kernel_test):
    mon = _make_monitor(kernel_test)
    # File for a non-registered agent.
    p = _write_hb(mon._dir, "orphan")
    assert p.exists()
    summary = await mon.tick()
    assert "orphan" in summary["cleanups"]
    assert not p.exists()


@pytest.mark.asyncio
async def test_inbound_ws_heartbeat_updates_registry(kernel_test):
    mon = _make_monitor(kernel_test)
    await kernel_test.registry.register(_manifest("coder"))
    # Mark zombie first.
    await kernel_test.registry.update_status("coder", "zombie")
    await mon.process_inbound_heartbeat("coder")
    st = await kernel_test.registry.get_status("coder")
    assert st["status"] == "ready"
    assert st["last_heartbeat"] is not None


@pytest.mark.asyncio
async def test_invalid_heartbeat_file_is_skipped(kernel_test):
    mon = _make_monitor(kernel_test)
    await kernel_test.registry.register(_manifest("coder"))
    bad = mon._dir / "coder.json"  # type: ignore[attr-defined]
    bad.write_text("not valid json")
    summary = await mon.tick()
    # No exception; coder is not marked alive because the file was bad.
    assert "coder" not in summary["alive"]


@pytest.mark.asyncio
async def test_repeated_zombie_does_not_duplicate_event(kernel_test):
    """A second tick on an already-zombie agent should not re-emit events."""
    mon = _make_monitor(kernel_test)
    kernel_test.settings.heartbeat_zombie_threshold_seconds = 0.0
    await kernel_test.registry.register(_manifest("coder"))
    received: list = []
    kernel_test.bus.add_event_listener(received.append)
    await mon.tick()  # marks zombie
    await asyncio.sleep(0.05)
    n_events_after_first = sum(
        1
        for e in received
        if e.payload.data.get("event") == "agent_zombie"  # type: ignore[union-attr]
    )
    await mon.tick()  # second tick; should not re-emit
    await asyncio.sleep(0.05)
    n_events_after_second = sum(
        1
        for e in received
        if e.payload.data.get("event") == "agent_zombie"  # type: ignore[union-attr]
    )
    assert n_events_after_first == n_events_after_second
