"""Phase 10 dashboard backend integration tests.

The dashboard gains four new endpoints that surface the trial/error
cycle:

* ``GET /api/loops/leaderboard``
* ``GET /api/loops/leaderboard/{task_type}``
* ``GET /api/loops/{loop_id}/trials``
* ``POST /api/loops/optimize``

These tests stand up a full dashboard harness (see
:mod:`src.dashboard.backend.tests.conftest`) and wire a real
:class:`loops.trial_store.TrialStore` + :class:`loop_optimizer.LoopOptimizer`
into the introspection layer.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import pytest_asyncio

# Make ``src`` importable.
_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from loop_optimizer import LoopOptimizer  # noqa: E402
from loops.trial_store import TrialStore  # noqa: E402


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def phase10_harness(tmp_path):
    """Like the standard dashboard harness, but with trial_store + loop_optimizer."""
    from kernel.bus import MessageBus
    from kernel.config import reset_settings_for_tests
    from kernel.permissions import PermissionEnforcer
    from kernel.registry import AgentRegistry
    from loops.registry import create_registry
    from memory.persistent import PersistentMemory

    from dashboard.backend.cache import AggregateCache
    from dashboard.backend.config import ConfigAPI
    from dashboard.backend.introspection import IntrospectionAPI
    from dashboard.backend.main import create_dashboard_app

    settings = reset_settings_for_tests(
        db_path=tmp_path / "registry.db",
        heartbeat_interval_seconds=0.1,
        heartbeat_zombie_threshold_seconds=0.5,
        bus_router_poll_interval_seconds=0.005,
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

    memory = PersistentMemory(tmp_path / "memory.db")
    await memory.initialize()
    loop_registry = create_registry(str(tmp_path / "loops.db"))

    # Phase 10 collaborators.
    trial_store = TrialStore()
    loop_optimizer = LoopOptimizer(trial_store=trial_store)

    introspection = IntrospectionAPI(
        registry=registry,
        bus=bus,
        settings=settings,
        persistent_memory=memory,
        loop_registry=loop_registry,
        workspaces_dir=tmp_path / "workspaces",
        trial_store=trial_store,
        loop_optimizer=loop_optimizer,
    )
    config = ConfigAPI(tmp_path / "dashboard.db")
    cache = AggregateCache()
    app = create_dashboard_app(
        introspection=introspection,
        config=config,
        cache=cache,
        enable_aggregator=False,
        enable_stream=False,
    )
    try:
        yield {
            "app": app,
            "trial_store": trial_store,
            "loop_optimizer": loop_optimizer,
        }
    finally:
        await config.close()
        await memory.close()
        try:
            await bus.stop()
        except Exception:
            pass
        await registry.close()


@pytest_asyncio.fixture
async def phase10_client(phase10_harness):
    import httpx

    transport = httpx.ASGITransport(app=phase10_harness["app"])
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        async with phase10_harness["app"].router.lifespan_context(
            phase10_harness["app"]
        ):
            yield client


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestLeaderboardEndpoint:
    """``GET /api/loops/leaderboard``."""

    @pytest.mark.asyncio
    async def test_empty_leaderboard(self, phase10_client):
        resp = await phase10_client.get("/api/loops/leaderboard")
        assert resp.status_code == 200
        assert resp.json() == []

    @pytest.mark.asyncio
    async def test_leaderboard_with_seeded_trials(self, phase10_client, phase10_harness):
        # Seed three trials so the leaderboard surfaces an entry.
        from datetime import datetime, timezone
        from loops.base_loop import LoopResult
        from loops.critic import CriticScore

        for i in range(3):
            score = CriticScore(quality_score=8.0, cost_usd=0.001, latency_ms=100.0)
            result = LoopResult(
                output=f"trial {i}",
                confidence=0.8,
                tokens_used=10,
                cost_usd=0.001,
                latency_ms=100.0,
                iterations=1,
                intermediate_outputs=[],
            )
            await phase10_harness["trial_store"].arecord_trial(
                loop_id="winner",
                task_type="code",
                loop_graph={"loop_id": "winner", "name": "winner",
                            "nodes": [], "edges": [],
                            "terminal_nodes": [], "entry_node": None},
                score=score,
                result=result,
            )
        resp = await phase10_client.get("/api/loops/leaderboard?min_trials=1")
        assert resp.status_code == 200
        body = resp.json()
        assert len(body) == 1
        assert body[0]["loop_id"] == "winner"
        assert body[0]["trial_count"] == 3

    @pytest.mark.asyncio
    async def test_leaderboard_by_task_type(self, phase10_client, phase10_harness):
        from loops.critic import CriticScore
        from loops.base_loop import LoopResult

        for _ in range(3):
            await phase10_harness["trial_store"].arecord_trial(
                loop_id="math-loop",
                task_type="math",
                loop_graph={"loop_id": "math-loop", "name": "math",
                            "nodes": [], "edges": [],
                            "terminal_nodes": [], "entry_node": None},
                score=CriticScore(quality_score=5.0),
                result=LoopResult(
                    output="x", confidence=0.5, tokens_used=0,
                    cost_usd=0.001, latency_ms=10.0, iterations=0,
                    intermediate_outputs=[],
                ),
            )
        resp = await phase10_client.get("/api/loops/leaderboard/math?min_trials=1")
        assert resp.status_code == 200
        body = resp.json()
        assert len(body) == 1
        assert body[0]["task_type"] == "math"


class TestTrialsEndpoint:
    """``GET /api/loops/{loop_id}/trials``."""

    @pytest.mark.asyncio
    async def test_trials_empty(self, phase10_client):
        resp = await phase10_client.get("/api/loops/never-existed/trials")
        assert resp.status_code == 200
        assert resp.json() == []

    @pytest.mark.asyncio
    async def test_trials_returns_seeded(self, phase10_client, phase10_harness):
        from loops.critic import CriticScore
        from loops.base_loop import LoopResult

        for i in range(2):
            await phase10_harness["trial_store"].arecord_trial(
                loop_id="my-loop",
                task_type="code",
                loop_graph={"loop_id": "my-loop", "name": "my-loop",
                            "nodes": [], "edges": [],
                            "terminal_nodes": [], "entry_node": None},
                score=CriticScore(quality_score=5.0 + i),
                result=LoopResult(
                    output=f"o{i}", confidence=0.5, tokens_used=0,
                    cost_usd=0.001, latency_ms=10.0, iterations=0,
                    intermediate_outputs=[],
                ),
            )
        resp = await phase10_client.get("/api/loops/my-loop/trials")
        assert resp.status_code == 200
        body = resp.json()
        assert len(body) == 2
        # Newest first → higher score first.
        assert body[0]["score"]["quality_score"] == 6.0
        assert body[1]["score"]["quality_score"] == 5.0


class TestOptimizeEndpoint:
    """``POST /api/loops/optimize``."""

    @pytest.mark.asyncio
    async def test_optimize_runs_a_cycle(self, phase10_client):
        resp = await phase10_client.post(
            "/api/loops/optimize",
            json={
                "task_type": "code_review",
                "task_sample": "Review this PR",
                "n_trials": 2,
                "base_loop": "reflection",
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["task_type"] == "code_review"
        assert body["base_loop"] == "reflection"
        assert body["trial_count"] == 2
        assert len(body["trials"]) == 2
        # Each trial has a composite score.
        for t in body["trials"]:
            assert t["score"]["composite_score"] > 0.0

    @pytest.mark.asyncio
    async def test_optimize_invalid_input_rejected(self, phase10_client):
        resp = await phase10_client.post(
            "/api/loops/optimize",
            json={"n_trials": 0},  # below ge=1
        )
        # Pydantic returns 422.
        assert resp.status_code == 422
