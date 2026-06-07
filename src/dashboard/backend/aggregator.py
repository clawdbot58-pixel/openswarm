"""Background data aggregator.

The :class:`DataAggregator` pre-computes the most-asked dashboard
queries on a timer so the FastAPI handlers can answer in O(1) without
hitting SQLite or scanning the workspace tree on every request.

Two cadences are maintained:

* **Fast loop (default 5s)** — agent counts, workflow counts, queue
  totals, messages/minute, cost_today.  These power the dashboard
  header widgets and must stay fresh.
* **Slow loop (default 60s)** — loop-registry performance, per-agent
  metrics, audit log aggregates.  These power the laboratory view and
  tolerate more staleness.

The aggregator writes into the :class:`AggregateCache`; consumers
(``GET /api/metrics``, the WebSocket pusher) read from it.  The
aggregator never reads from the cache.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any

from .cache import AggregateCache
from .introspection import IntrospectionAPI

if TYPE_CHECKING:  # pragma: no cover
    pass

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------


DEFAULT_FAST_INTERVAL: float = 5.0
DEFAULT_SLOW_INTERVAL: float = 60.0
# Sliding window for "messages per minute" — covers a couple of
# fast-loop ticks so the metric is meaningful even when traffic is
# bursty.
MESSAGE_RATE_WINDOW_SECONDS: float = 60.0


# ---------------------------------------------------------------------------
# DataAggregator
# ---------------------------------------------------------------------------


class DataAggregator:
    """Periodic background aggregator.

    Args:
        introspection: The read-only API to query.
        cache: The in-memory cache to populate.
        fast_interval_seconds: How often to refresh fast-loop values.
        slow_interval_seconds: How often to refresh slow-loop values.
    """

    def __init__(
        self,
        introspection: IntrospectionAPI,
        cache: AggregateCache,
        *,
        fast_interval_seconds: float = DEFAULT_FAST_INTERVAL,
        slow_interval_seconds: float = DEFAULT_SLOW_INTERVAL,
        history_size: int = 60,
    ) -> None:
        self._introspection = introspection
        self._cache = cache
        self._fast = max(1.0, fast_interval_seconds)
        self._slow = max(5.0, slow_interval_seconds)
        self._history_size = max(10, history_size)
        self._fast_task: asyncio.Task[None] | None = None
        self._slow_task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()
        self._started = False
        # Sliding-window message-rate buffer.
        self._message_window: list[tuple[float, int]] = []
        self._last_message_count: int = 0

    # -- lifecycle --------------------------------------------------------

    async def start(self) -> None:
        """Spawn the two background tasks.  Idempotent."""
        if self._started:
            return
        self._stop_event.clear()
        # Run the first iterations synchronously so the cache is warm
        # before the first request lands.
        try:
            await self._tick_fast()
        except Exception:
            logger.exception("aggregator initial fast-tick failed")
        try:
            await self._tick_slow()
        except Exception:
            logger.exception("aggregator initial slow-tick failed")
        self._fast_task = asyncio.create_task(self._fast_loop(), name="agg-fast")
        self._slow_task = asyncio.create_task(self._slow_loop(), name="agg-slow")
        self._started = True
        logger.info(
            "DataAggregator started fast=%.1fs slow=%.1fs",
            self._fast,
            self._slow,
        )

    async def stop(self) -> None:
        """Cancel the background tasks.  Idempotent."""
        if not self._started:
            return
        self._stop_event.set()
        for task in (self._fast_task, self._slow_task):
            if task is not None:
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
        self._fast_task = None
        self._slow_task = None
        self._started = False
        logger.info("DataAggregator stopped")

    # -- main loops -------------------------------------------------------

    async def _fast_loop(self) -> None:
        """Run :meth:`_tick_fast` every ``fast_interval_seconds``."""
        try:
            while not self._stop_event.is_set():
                end = time.monotonic() + self._fast
                try:
                    await self._tick_fast()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("fast tick failed")
                while not self._stop_event.is_set() and time.monotonic() < end:
                    await asyncio.sleep(min(0.5, end - time.monotonic()))
        except asyncio.CancelledError:
            raise

    async def _slow_loop(self) -> None:
        """Run :meth:`_tick_slow` every ``slow_interval_seconds``."""
        try:
            # Wait at least one fast tick before kicking off so the
            # metrics endpoint isn't racing the first slow pass.
            end = time.monotonic() + max(self._slow, self._fast)
            while not self._stop_event.is_set() and time.monotonic() < end:
                await asyncio.sleep(min(0.5, end - time.monotonic()))
            while not self._stop_event.is_set():
                end = time.monotonic() + self._slow
                try:
                    await self._tick_slow()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("slow tick failed")
                while not self._stop_event.is_set() and time.monotonic() < end:
                    await asyncio.sleep(min(0.5, end - time.monotonic()))
        except asyncio.CancelledError:
            raise

    # -- ticks ------------------------------------------------------------

    async def _tick_fast(self) -> None:
        """Refresh the fast-loop entries."""
        # 1. Agent counts.
        agents = await self._introspection.get_agents()
        by_status: dict[str, int] = {}
        by_category: dict[str, int] = {}
        for agent in agents:
            by_status[agent.status] = by_status.get(agent.status, 0) + 1
            by_category[agent.category] = by_category.get(agent.category, 0) + 1

        # 2. Workflow counts.
        workflows = await self._introspection.get_workflows()
        wf_by_status: dict[str, int] = {}
        for wf in workflows:
            wf_by_status[wf.status] = wf_by_status.get(wf.status, 0) + 1

        # 3. Message rate (sliding window).
        now = time.monotonic()
        rate = self._record_message_rate(now)

        # 4. System metrics (composed locally for speed).
        metrics = await self._introspection.get_system_metrics()
        # The sliding-window rate is exposed via cache["messages_per_minute"]
        # and the header widget reads that key directly; the metrics
        # payload's ``messages_per_minute`` field is the lifetime
        # average from the bus and is intentionally different.

        # 5. Workspace count.
        workspaces = await self._introspection.get_workspaces()

        await self._cache.set_many(
            {
                "agents_by_status": by_status,
                "agents_by_category": by_category,
                "agent_count": len(agents),
                "workflows_by_status": wf_by_status,
                "workflow_count": len(workflows),
                "workspace_count": len(workspaces),
                "system_metrics": metrics.model_dump(mode="json"),
                "messages_per_minute": rate,
            }
        )

    async def _tick_slow(self) -> None:
        """Refresh the slow-loop entries."""
        # Loop templates + recommendations.
        templates = await self._introspection.get_loop_templates()
        await self._cache.set("loop_templates", [t.model_dump(mode="json") for t in templates])
        # Per-template performance (only the top 10 by success rate to
        # bound the work; the API will fetch the rest on demand).
        perf: dict[str, Any] = {}
        for tpl in templates[:10]:
            try:
                p = await self._introspection.get_loop_performance(tpl.id)
                perf[tpl.id] = p.model_dump(mode="json")
            except Exception:
                continue
        await self._cache.set("loop_performance", perf)

        # Cost totals (24h + 1h windows).
        cost_24h = await self._introspection._cost_today()  # type: ignore[attr-defined]
        # Reuse the same function — it already returns 24h.  When
        # Phase 10 wires in real cost tracking this will become a
        # proper breakdown.
        await self._cache.set("cost_totals", {"last_24h_usd": cost_24h})

    # -- helpers ----------------------------------------------------------

    def _record_message_rate(self, now: float) -> float:
        """Maintain the sliding-window message-rate counter.

        The bus's ``metrics.envelopes_received`` is monotonic; we
        sample it every fast-tick and convert the delta over the
        configured window into messages/minute.

        Returns the latest rate (messages/minute).
        """
        bus = self._introspection._bus  # type: ignore[attr-defined]
        current = int(bus.metrics.envelopes_received)
        delta = max(0, current - self._last_message_count)
        self._last_message_count = current
        self._message_window.append((now, delta))
        # Drop entries outside the window.
        cutoff = now - MESSAGE_RATE_WINDOW_SECONDS
        self._message_window = [(t, d) for t, d in self._message_window if t >= cutoff]
        if not self._message_window:
            return 0.0
        # Rate = total messages in window / window duration (min).
        total = sum(d for _, d in self._message_window)
        window_span = max(1.0, self._message_window[-1][0] - self._message_window[0][0])
        minutes = window_span / 60.0
        return round(total / minutes, 2) if minutes > 0 else 0.0

    # -- diagnostics ------------------------------------------------------

    def stats(self) -> dict[str, Any]:
        """Return a small stats dict for diagnostics."""
        return {
            "fast_interval": self._fast,
            "slow_interval": self._slow,
            "started": self._started,
            "message_window_size": len(self._message_window),
            "last_message_count": self._last_message_count,
        }


__all__ = [
    "DEFAULT_FAST_INTERVAL",
    "DEFAULT_SLOW_INTERVAL",
    "MESSAGE_RATE_WINDOW_SECONDS",
    "DataAggregator",
]
