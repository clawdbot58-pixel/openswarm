"""Tests for the Phase 10 trial store."""

import asyncio
import json

import pytest

from loops.base_loop import LoopResult
from loops.critic import CriticScore
from loops.trial_store import (
    DEFAULT_MIN_TRIALS,
    LeaderboardEntry,
    Trial,
    TrialStore,
)


def _make_score(quality: float = 7.0, cost: float = 0.005, latency: float = 200.0) -> CriticScore:
    return CriticScore(
        quality_score=quality,
        cost_usd=cost,
        latency_ms=latency,
        reasoning="ok",
    )


def _make_result(output: str = "ok", cost: float = 0.005, latency: float = 200.0) -> LoopResult:
    return LoopResult(
        output=output,
        confidence=0.7,
        tokens_used=100,
        cost_usd=cost,
        latency_ms=latency,
        iterations=1,
        intermediate_outputs=[],
    )


def _make_graph(loop_id: str = "reflection") -> dict:
    return {
        "loop_id": loop_id,
        "name": loop_id,
        "description": "test",
        "nodes": [
            {
                "node_id": "n1",
                "primitive": "generate",
                "model_override": None,
                "temperature": 0.7,
                "parameters": {},
            }
        ],
        "edges": [],
        "terminal_nodes": ["n1"],
        "entry_node": "n1",
    }


@pytest.fixture
def store() -> TrialStore:
    return TrialStore()


class TestTrialStoreRecord:
    """Trial insertion + immutability."""

    def test_record_trial_returns_uuid(self, store: TrialStore):
        tid = store.record_trial(
            loop_id="refl",
            task_type="code",
            loop_graph=_make_graph("refl"),
            score=_make_score(),
            result=_make_result(),
        )
        assert isinstance(tid, str)
        assert len(tid) == 36  # UUID4

    def test_record_trial_round_trips(self, store: TrialStore):
        tid = store.record_trial(
            loop_id="refl",
            task_type="code",
            loop_graph=_make_graph("refl"),
            score=_make_score(quality=8.0, cost=0.01, latency=100.0),
            result=_make_result(output="hello", cost=0.01, latency=100.0),
            task_preview="review this PR",
        )
        rows = store.get_trials(loop_id="refl")
        assert len(rows) == 1
        t = rows[0]
        assert t.trial_id == tid
        assert t.loop_id == "refl"
        assert t.task_type == "code"
        assert t.task_preview == "review this PR"
        assert t.output_preview == "hello"
        assert t.score.quality_score == 8.0
        assert t.score.composite_score > 0.0
        assert t.result.cost_usd == 0.01

    def test_record_persists_composite_in_json(self, store: TrialStore):
        """Stored score_json contains the composite even though it's derived."""
        score = _make_score(quality=9.0, cost=0.001, latency=10.0)
        expected_composite = score.composite_score
        store.record_trial(
            loop_id="refl",
            task_type=None,
            loop_graph=_make_graph(),
            score=score,
            result=_make_result(),
        )
        # Re-load and check the composite.
        loaded = store.get_trials()[0]
        assert loaded.score.composite_score == pytest.approx(expected_composite)

    def test_score_backfilled_with_loop_and_task(self, store: TrialStore):
        """The stored score's loop_id/task_type match the record args."""
        store.record_trial(
            loop_id="refl",
            task_type="math",
            loop_graph=_make_graph(),
            score=CriticScore(quality_score=5.0),
            result=_make_result(),
        )
        loaded = store.get_trials()[0]
        assert loaded.score.loop_id == "refl"
        assert loaded.score.task_type == "math"


class TestTrialStoreRead:
    """Read paths: get_trials, get_leaderboard, count."""

    def test_get_trials_filters_by_loop_id(self, store: TrialStore):
        for lid in ("a", "a", "b"):
            store.record_trial(
                loop_id=lid,
                task_type="code",
                loop_graph=_make_graph(),
                score=_make_score(),
                result=_make_result(),
            )
        a = store.get_trials(loop_id="a")
        b = store.get_trials(loop_id="b")
        assert len(a) == 2
        assert len(b) == 1

    def test_get_trials_filters_by_task_type(self, store: TrialStore):
        for tt in ("code", "code", "math"):
            store.record_trial(
                loop_id="x",
                task_type=tt,
                loop_graph=_make_graph(),
                score=_make_score(),
                result=_make_result(),
            )
        assert len(store.get_trials(task_type="code")) == 2
        assert len(store.get_trials(task_type="math")) == 1

    def test_get_trials_orders_newest_first(self, store: TrialStore):
        for i in range(3):
            store.record_trial(
                loop_id="x",
                task_type=None,
                loop_graph=_make_graph(),
                score=_make_score(quality=float(i)),
                result=_make_result(),
            )
        rows = store.get_trials()
        # Newest first → last inserted is index 0.
        assert rows[0].score.quality_score == 2.0
        assert rows[-1].score.quality_score == 0.0

    def test_count(self, store: TrialStore):
        assert store.count() == 0
        for _ in range(5):
            store.record_trial(
                loop_id="x",
                task_type=None,
                loop_graph=_make_graph(),
                score=_make_score(),
                result=_make_result(),
            )
        assert store.count() == 5
        assert store.count(loop_id="x") == 5
        assert store.count(loop_id="missing") == 0


class TestTrialStoreLeaderboard:
    """Aggregations: get_leaderboard and its min_trials filter."""

    def test_leaderboard_drops_low_evidence(self, store: TrialStore):
        for _ in range(2):  # < DEFAULT_MIN_TRIALS
            store.record_trial(
                loop_id="low",
                task_type="code",
                loop_graph=_make_graph(),
                score=_make_score(quality=9.0),
                result=_make_result(),
            )
        for _ in range(DEFAULT_MIN_TRIALS):
            store.record_trial(
                loop_id="high",
                task_type="code",
                loop_graph=_make_graph(),
                score=_make_score(quality=5.0),
                result=_make_result(),
            )
        lb = store.get_leaderboard(task_type="code")
        loop_ids = [e.loop_id for e in lb]
        assert "low" not in loop_ids
        assert "high" in loop_ids

    def test_leaderboard_min_trials_override(self, store: TrialStore):
        for _ in range(2):
            store.record_trial(
                loop_id="x",
                task_type=None,
                loop_graph=_make_graph(),
                score=_make_score(quality=5.0),
                result=_make_result(),
            )
        assert store.get_leaderboard(min_trials=1) != []
        assert store.get_leaderboard(min_trials=3) == []

    def test_leaderboard_sorted_by_score_desc(self, store: TrialStore):
        for i in range(3):
            store.record_trial(
                loop_id=f"loop-{i}",
                task_type="code",
                loop_graph=_make_graph(f"loop-{i}"),
                score=_make_score(quality=float(i + 1)),
                result=_make_result(),
            )
        lb = store.get_leaderboard(task_type="code", min_trials=1)
        assert lb[0].loop_id == "loop-2"
        assert lb[-1].loop_id == "loop-0"

    def test_leaderboard_sort_by_cost(self, store: TrialStore):
        # Three trials each so min_trials=1 still keeps them.
        for cost, q in [(0.10, 5.0), (0.01, 5.0), (0.05, 5.0)]:
            for _ in range(3):
                store.record_trial(
                    loop_id=f"loop-{cost}",
                    task_type="code",
                    loop_graph=_make_graph(),
                    score=_make_score(quality=q, cost=cost),
                    result=_make_result(cost=cost),
                )
        lb = store.get_leaderboard(task_type="code", min_trials=1, sort_by="cost")
        # Cheapest first.
        assert lb[0].loop_id == "loop-0.01"

    def test_leaderboard_sort_by_speed(self, store: TrialStore):
        for lat, q in [(500.0, 5.0), (100.0, 5.0), (1000.0, 5.0)]:
            for _ in range(3):
                store.record_trial(
                    loop_id=f"loop-{lat}",
                    task_type="code",
                    loop_graph=_make_graph(),
                    score=_make_score(quality=q, latency=lat),
                    result=_make_result(latency=lat),
                )
        lb = store.get_leaderboard(task_type="code", min_trials=1, sort_by="speed")
        # Fastest first.
        assert lb[0].loop_id == "loop-100.0"

    def test_leaderboard_sort_by_trials(self, store: TrialStore):
        for n, q in [(1, 9.0), (5, 5.0), (3, 5.0)]:
            for _ in range(n):
                store.record_trial(
                    loop_id=f"loop-{n}",
                    task_type="code",
                    loop_graph=_make_graph(),
                    score=_make_score(quality=q),
                    result=_make_result(),
                )
        lb = store.get_leaderboard(task_type="code", min_trials=1, sort_by="trials")
        # Most-evidence first.
        assert lb[0].loop_id == "loop-5"

    def test_leaderboard_invalid_sort_raises(self, store: TrialStore):
        for _ in range(3):
            store.record_trial(
                loop_id="x",
                task_type=None,
                loop_graph=_make_graph(),
                score=_make_score(),
                result=_make_result(),
            )
        with pytest.raises(ValueError):
            store.get_leaderboard(min_trials=1, sort_by="bogus")

    def test_leaderboard_best_variant_picked(self, store: TrialStore):
        for q in [3.0, 5.0, 9.0]:
            store.record_trial(
                loop_id="x",
                task_type="code",
                loop_graph=_make_graph(),
                score=_make_score(quality=q),
                result=_make_result(),
            )
        lb = store.get_leaderboard(min_trials=1)
        # The best (highest composite) trial's graph is the one stored
        # in best_variant.  The graph in the row should be the same as
        # the 9.0 trial's (we only have one shape, so it just checks
        # the wiring works).
        assert lb[0].best_variant


class TestTrialStoreAsync:
    """Async wrappers are thin ``asyncio.to_thread`` shims."""

    @pytest.mark.asyncio
    async def test_arecord_trial(self, store: TrialStore):
        tid = await store.arecord_trial(
            loop_id="x",
            task_type=None,
            loop_graph=_make_graph(),
            score=_make_score(),
            result=_make_result(),
        )
        assert isinstance(tid, str)
        assert (await store.acount()) == 1

    @pytest.mark.asyncio
    async def test_aget_trials(self, store: TrialStore):
        await store.arecord_trial(
            loop_id="x",
            task_type=None,
            loop_graph=_make_graph(),
            score=_make_score(),
            result=_make_result(),
        )
        rows = await store.aget_trials()
        assert len(rows) == 1

    @pytest.mark.asyncio
    async def test_aget_leaderboard(self, store: TrialStore):
        for _ in range(3):
            await store.arecord_trial(
                loop_id="x",
                task_type=None,
                loop_graph=_make_graph(),
                score=_make_score(),
                result=_make_result(),
            )
        lb = await store.aget_leaderboard(min_trials=1)
        assert len(lb) == 1


class TestTrialStoreModels:
    """Trial + LeaderboardEntry Pydantic models."""

    def test_trial_to_dict_round_trip(self):
        score = _make_score(quality=8.0, cost=0.01, latency=100.0)
        result = _make_result(output="hello", cost=0.01, latency=100.0)
        t = Trial(
            trial_id="t-1",
            loop_id="refl",
            task_type="code",
            loop_graph=_make_graph(),
            score=score,
            result=result,
            timestamp=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
        )
        d = t.to_dict()
        assert d["loop_id"] == "refl"
        assert d["score"]["composite_score"] == pytest.approx(score.composite_score)
        assert d["result"]["output"] == "hello"
        # Timestamp is ISO 8601 with Z suffix.
        assert d["timestamp"].endswith("Z")

    def test_leaderboard_entry_to_dict(self):
        e = LeaderboardEntry(
            loop_id="refl",
            avg_score=0.5,
            avg_quality=8.0,
            avg_cost_usd=0.001,
            avg_latency_ms=100.0,
            trial_count=3,
            last_trial=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
        )
        d = e.to_dict()
        assert d["loop_id"] == "refl"
        assert d["last_trial"].endswith("Z")
