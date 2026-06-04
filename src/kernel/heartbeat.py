"""File-based heartbeat monitor.

Each agent writes a small JSON file to ``heartbeats/{agent_id}.json``
periodically. The monitor polls the directory every
:attr:`KernelSettings.heartbeat_interval_seconds` and:

* updates the registry's ``last_heartbeat`` and ``status`` columns;
* marks agents as ``zombie`` when their heartbeat file is older than
  :attr:`KernelSettings.heartbeat_zombie_threshold_seconds` or missing;
* emits a ``agent_zombie`` event to the main agent;
* emits an ``auto_restart_triggered`` event if the agent's manifest
  declares ``lifecycle.auto_restart = True`` (the main agent decides
  whether to actually restart);
* cleans up heartbeat files for agents that have been unregistered.

The monitor is a single :class:`asyncio.Task` started by the FastAPI
lifespan and stopped on shutdown.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .bus import MessageBus
from .config import KernelSettings
from .exceptions import HeartbeatError
from .models import AgentIdStr, AgentManifest, HeartbeatFile
from .registry import AgentRegistry

logger = logging.getLogger(__name__)


# How often the monitor should re-evaluate the file system.
_POLL_INTERVAL_FALLBACK: float = 10.0


class HeartbeatMonitor:
    """Polls ``heartbeats/`` and synchronizes liveness into the registry."""

    def __init__(
        self,
        registry: AgentRegistry,
        bus: MessageBus,
        settings: KernelSettings,
    ) -> None:
        self._registry = registry
        self._bus = bus
        self._settings = settings
        self._dir: Path = settings.paths.heartbeats_dir
        self._task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()
        self._started = False
        # Cache the last seen mtime per agent to avoid rereading unchanged
        # files every tick. The cache is best-effort: any read failure
        # invalidates the entry.
        self._last_seen_mtime: dict[AgentIdStr, float] = {}

    # -- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        """Spawn the background polling task. Idempotent."""
        if self._started:
            return
        self._dir.mkdir(parents=True, exist_ok=True)
        self._stop_event.clear()
        self._task = asyncio.create_task(self._loop(), name="kernel-heartbeat")
        self._started = True
        logger.info(
            "heartbeat monitor started dir=%s interval=%s threshold=%s",
            self._dir,
            self._settings.heartbeat_interval_seconds,
            self._settings.heartbeat_zombie_threshold_seconds,
        )

    async def stop(self) -> None:
        """Cancel the polling task. Idempotent."""
        if not self._started:
            return
        self._stop_event.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._task = None
        self._started = False
        logger.info("heartbeat monitor stopped")

    # -- main loop ---------------------------------------------------------

    async def _loop(self) -> None:
        """Periodically run :meth:`tick` until cancelled."""
        try:
            while not self._stop_event.is_set():
                try:
                    await self.tick()
                except Exception:  # noqa: BLE001 — never let the monitor die
                    logger.exception("heartbeat tick failed")
                # Sleep in small slices so stop() returns quickly.
                interval = self._settings.heartbeat_interval_seconds or _POLL_INTERVAL_FALLBACK
                end = time.monotonic() + interval
                while not self._stop_event.is_set() and time.monotonic() < end:
                    await asyncio.sleep(min(0.5, end - time.monotonic()))
        except asyncio.CancelledError:
            raise

    # -- one tick ----------------------------------------------------------

    async def tick(self) -> dict[str, Any]:
        """Run a single monitoring pass. Exposed for tests.

        Returns a small summary dict with the actions taken during the
        tick. Useful for assertions in unit tests.
        """
        summary: dict[str, Any] = {
            "alive": [],
            "zombies": [],
            "auto_restarts": [],
            "cleanups": [],
        }
        now = time.time()
        registered_ids = set(await self._registry.all_ids())
        # 1. Inspect every heartbeat file present on disk.
        on_disk_ids: set[AgentIdStr] = set()
        for path in self._dir.glob("*.json"):
            agent_id = path.stem
            on_disk_ids.add(agent_id)
            try:
                stat = path.stat()
            except FileNotFoundError:
                continue
            mtime = stat.st_mtime
            age = now - mtime
            # Skip the file if it was already up-to-date last tick.
            if self._last_seen_mtime.get(agent_id) == mtime:
                # Even if unchanged, the agent may have become zombie due
                # to staleness on a previous tick — re-evaluate age below.
                pass
            self._last_seen_mtime[agent_id] = mtime
            # Read & parse.
            try:
                hb = self._parse_file(path)
            except HeartbeatError as exc:
                logger.warning("heartbeat file %s invalid: %s", path, exc)
                continue
            if agent_id not in registered_ids:
                # Cleanup: heartbeat for a not-registered agent.
                try:
                    path.unlink(missing_ok=True)
                    summary["cleanups"].append(agent_id)
                except OSError:
                    logger.debug("cleanup unlink failed for %s", path)
                continue
            # Update registry.
            try:
                await self._registry.update_heartbeat(
                    agent_id, hb.timestamp
                )
                await self._registry.update_status(agent_id, hb.status)
            except Exception:  # noqa: BLE001 — AgentNotFound etc.
                logger.debug("heartbeat update failed for %s", agent_id)
                continue
            # Zombie check.
            if age > self._settings.heartbeat_zombie_threshold_seconds:
                await self._mark_zombie(agent_id, age, summary)
            else:
                summary["alive"].append(agent_id)
        # 2. Detect missing heartbeat files for registered agents.
        for agent_id in registered_ids - on_disk_ids:
            # If we never saw a file, last_seen_mtime is absent and the
            # agent is definitely zombie.
            last_mtime = self._last_seen_mtime.get(agent_id)
            age = (now - last_mtime) if last_mtime else float("inf")
            await self._mark_zombie(agent_id, age, summary)
        return summary

    # -- helpers -----------------------------------------------------------

    def _parse_file(self, path: Path) -> HeartbeatFile:
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise HeartbeatError(
                f"could not read heartbeat file: {exc}", path=str(path)
            ) from exc
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise HeartbeatError(
                f"heartbeat file is not valid JSON: {exc}", path=str(path)
            ) from exc
        try:
            return HeartbeatFile.model_validate(data)
        except Exception as exc:  # noqa: BLE001
            raise HeartbeatError(
                f"heartbeat file failed schema validation: {exc}", path=str(path)
            ) from exc

    async def _mark_zombie(
        self,
        agent_id: AgentIdStr,
        age: float,
        summary: dict[str, Any],
    ) -> None:
        """Mark ``agent_id`` zombie (idempotent) and emit events."""
        try:
            current = await self._registry.get_status(agent_id)
        except Exception:  # noqa: BLE001
            return
        if current["status"] == "zombie":
            # Already handled this tick or a previous one; avoid spam.
            summary["zombies"].append(agent_id)
            return
        await self._registry.update_status(agent_id, "zombie")
        self._bus.metrics.zombies_detected += 1
        summary["zombies"].append(agent_id)
        logger.warning("agent zombie agent_id=%s age=%.1fs", agent_id, age)
        await self._bus._emit_to_main(  # type: ignore[attr-defined]
            "agent_zombie",
            {"agent_id": agent_id, "age_seconds": age},
        )
        # Check the manifest for auto_restart.
        try:
            manifest: AgentManifest = await self._registry.get(agent_id)
        except Exception:  # noqa: BLE001
            return
        if manifest.lifecycle.auto_restart:
            summary["auto_restarts"].append(agent_id)
            await self._bus._emit_to_main(  # type: ignore[attr-defined]
                "auto_restart_triggered",
                {
                    "agent_id": agent_id,
                    "restart_policy": manifest.lifecycle.restart_policy,
                    "max_restarts": manifest.lifecycle.max_restarts,
                },
            )

    # -- external API for the WS layer -------------------------------------

    async def process_inbound_heartbeat(
        self,
        agent_id: AgentIdStr,
        timestamp: datetime | None = None,
    ) -> None:
        """Update the registry from a WebSocket-borne heartbeat envelope.

        Validates that the agent is registered; otherwise this is a no-op
        (the agent should send a registration envelope first).
        """
        try:
            await self._registry.get_status(agent_id)
        except Exception:  # noqa: BLE001
            return
        ts = timestamp or datetime.now(timezone.utc)
        await self._registry.update_heartbeat(agent_id, ts)
        # Also clear any zombie flag.
        try:
            current = await self._registry.get_status(agent_id)
            if current["status"] == "zombie":
                await self._registry.update_status(agent_id, "ready")
        except Exception:  # noqa: BLE001
            return


__all__ = ["HeartbeatMonitor"]
