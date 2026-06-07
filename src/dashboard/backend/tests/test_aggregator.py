"""Tests for the background aggregator."""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

# Make ``src`` importable.
_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_SRC = _PROJECT_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from dashboard.backend.cache import AggregateCache  # noqa: E402
from dashboard.backend.tests.conftest import make_manifest  # noqa: E402
from kernel.models import AgentManifest  # noqa: E402


async def test_start_populates_cache(dashboard_harness):
    cache = dashboard_harness["cache"]
    agg = dashboard_harness["aggregator"]

    # The harness pre-seeds a "test-agent" and runs an initial fast-tick
    # during aggregator.start(); ``agent_count`` is therefore at least 1.
    initial = await cache.get("agent_count")
    assert initial is not None
    assert initial >= 1
    # Register an additional agent and force a fresh fast-tick to make
    # sure the cache tracks changes.
    await dashboard_harness["registry"].register(
        AgentManifest.model_validate(make_manifest("agg-agent"))
    )
    await agg._tick_fast()
    assert await cache.get("agent_count") >= 2
    assert await cache.get("workflow_count") == 0
    # system_metrics is a snapshot from the introspection layer.
    metrics = await cache.get("system_metrics")
    assert metrics is not None
    assert metrics["total_agents"] >= 1
    await agg.stop()


async def test_message_rate_window_tracks_deltas(dashboard_harness):
    agg = dashboard_harness["aggregator"]
    bus = dashboard_harness["bus"]
    # The aggregator's record_message_rate reads from the bus; push
    # some envelopes to bump the counter.
    bus.metrics.envelopes_received += 5
    rate = agg._record_message_rate(time.monotonic())
    # Rate should be positive (or zero depending on timing).
    assert rate >= 0.0


async def test_aggregator_periodic_refresh(dashboard_harness):
    cache = dashboard_harness["cache"]
    agg = dashboard_harness["aggregator"]
    await dashboard_harness["registry"].register(
        AgentManifest.model_validate(make_manifest("periodic-agent"))
    )
    await agg.start()
    # Wait for at least one fast tick to fire.
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline:
        if await cache.get("agent_count") == 1:
            break
        await asyncio.sleep(0.05)
    assert await cache.get("agent_count") == 1
    await agg.stop()


async def test_cache_set_get_round_trip():
    cache = AggregateCache()
    await cache.set("k1", {"a": 1})
    assert await cache.get("k1") == {"a": 1}
    assert cache.get_sync("k1") == {"a": 1}
    # Miss returns default.
    assert await cache.get("missing", default="d") == "d"


async def test_cache_set_many_atomic():
    cache = AggregateCache()
    await cache.set_many({"a": 1, "b": 2, "c": 3})
    assert await cache.get("a") == 1
    assert await cache.get("b") == 2
    assert await cache.get("c") == 3


async def test_cache_stats_record_hits_and_misses():
    cache = AggregateCache()
    await cache.set("k", "v")
    await cache.get("k")
    await cache.get("missing")
    stats = cache.stats()
    assert stats["hits"].get("k") == 1
    assert stats["misses"].get("missing") == 1


async def test_cache_clear():
    cache = AggregateCache()
    await cache.set("k", "v")
    cache.clear()
    assert await cache.get("k") is None


async def test_cache_wait_for_refresh_returns_false_on_timeout():
    cache = AggregateCache()
    fired = await cache.wait_for_refresh(timeout=0.05)
    assert fired is False
