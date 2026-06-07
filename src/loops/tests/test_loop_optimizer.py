"""Tests for the Phase 10 loop optimizer."""

import asyncio
from typing import Any

import pytest

from loop_optimizer import (
    CycleReport,
    LoopOptimizer,
    OptimizationConfig,
)
from loops.base_loop import LoopResult
from loops.critic import LoopCritic
from loops.trial_store import DEFAULT_MIN_TRIALS, LeaderboardEntry, Trial, TrialStore
from meta_agent import MetaAgent


@pytest.fixture
def store() -> TrialStore:
    return TrialStore()


@pytest.fixture
def optimizer(store) -> LoopOptimizer:
    return LoopOptimizer(trial_store=store)


class TestLoopOptimizerCycle:
    """run_optimization_cycle happy path."""

    @pytest.mark.asyncio
    async def test_cycle_records_n_trials(self, store: TrialStore):
        """Use an LLM-stub meta-agent that returns diverse mutations."""
        from meta_agent import MetaAgent

        # The stub rotates through the catalog so each call yields
        # a different variant.
        catalog = [
            "noop",
            "upgrade_to_reflection",
            "strengthen_cot",
            "raise_branch_count",
            "add_critique",
            "swap_to_cot",
            "lower_temperature",
        ]
        call_count = {"i": 0}

        class RotatingLLM:
            async def generate(self, *a, **k):
                idx = call_count["i"] % len(catalog)
                call_count["i"] += 1
                return _StubResp(
                    f'{{"mutation": "{catalog[idx]}", "rationale": "rotating"}}'
                )

        class _StubResp:
            def __init__(self, c):
                self.content = c

        meta = MetaAgent(llm=RotatingLLM())
        opt = LoopOptimizer(meta_agent=meta, trial_store=store)
        report = await opt.run_optimization_cycle(
            task_type="code_review",
            task_sample="Review this PR",
            n_trials=3,
            base_loop="reflection",
        )
        assert isinstance(report, CycleReport)
        assert report.task_type == "code_review"
        assert report.base_loop == "reflection"
        assert len(report.trials) == 3
        # Each trial has a unique id, a real composite score, and a graph.
        ids = {t.trial_id for t in report.trials}
        assert len(ids) == 3
        for t in report.trials:
            assert t.score.composite_score > 0.0
            assert t.loop_graph
            assert t.task_type == "code_review"

    @pytest.mark.asyncio
    async def test_cycle_picks_best(self, optimizer: LoopOptimizer):
        report = await optimizer.run_optimization_cycle(
            task_type="code_review",
            task_sample="Review this PR",
            n_trials=3,
            base_loop="reflection",
        )
        assert report.best_loop_id is not None
        best = next(t for t in report.trials if t.loop_id == report.best_loop_id)
        assert report.best_score == pytest.approx(best.score.composite_score)

    @pytest.mark.asyncio
    async def test_cycle_includes_baseline(self, optimizer: LoopOptimizer):
        report = await optimizer.run_optimization_cycle(
            task_type="code_review",
            task_sample="x",
            n_trials=3,
            base_loop="reflection",
            include_builtins=True,
        )
        loop_ids = [t.loop_id for t in report.trials]
        assert any("baseline" in lid for lid in loop_ids)

    @pytest.mark.asyncio
    async def test_cycle_skip_baseline(self, optimizer: LoopOptimizer):
        report = await optimizer.run_optimization_cycle(
            task_type="code_review",
            task_sample="x",
            n_trials=2,
            base_loop="reflection",
            include_builtins=False,
        )
        loop_ids = [t.loop_id for t in report.trials]
        assert not any("baseline" in lid for lid in loop_ids)


class TestLoopOptimizerSelect:
    """select_for_task: leaderboard → fallback to premade loop."""

    @pytest.mark.asyncio
    async def test_select_falls_back_to_premade_when_empty(
        self, optimizer: LoopOptimizer
    ):
        graph = await optimizer.select_for_task("code_review", min_trials=1)
        # No trials → falls back to the premade loop for the task type.
        assert graph.loop_id in {"reflection", "reflection-baseline"}
        # The graph is a valid Pydantic LoopGraph.
        graph.validate_dag()

    @pytest.mark.asyncio
    async def test_select_returns_top_leaderboard_entry(self, store: TrialStore):
        opt = LoopOptimizer(trial_store=store)
        # Seed the leaderboard with one clear winner.
        from loops.critic import CriticScore
        from datetime import datetime, timezone

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
            await store.arecord_trial(
                loop_id="winner",
                task_type="code",
                loop_graph={"loop_id": "winner", "name": "winner",
                            "nodes": [{"node_id": "n", "primitive": "generate",
                                       "model_override": None, "temperature": 0.7,
                                       "parameters": {}}],
                            "edges": [], "terminal_nodes": ["n"],
                            "entry_node": "n"},
                score=score,
                result=result,
            )
        chosen = await opt.select_for_task("code", min_trials=1)
        assert chosen.loop_id == "winner"

    @pytest.mark.asyncio
    async def test_select_falls_back_global(self, store: TrialStore):
        """When the per-task leaderboard is empty, look at the global one."""
        from loops.critic import CriticScore

        opt = LoopOptimizer(trial_store=store)
        # Seed the global leaderboard (task_type=None) with a winner.
        valid_graph = {
            "loop_id": "global-winner",
            "name": "global-winner",
            "description": "",
            "nodes": [
                {
                    "node_id": "n",
                    "primitive": "generate",
                    "model_override": None,
                    "temperature": 0.7,
                    "parameters": {},
                }
            ],
            "edges": [],
            "terminal_nodes": ["n"],
            "entry_node": "n",
        }
        for _ in range(3):
            await store.arecord_trial(
                loop_id="global-winner",
                task_type=None,
                loop_graph=valid_graph,
                score=CriticScore(quality_score=5.0),
                result=LoopResult(
                    output="x", confidence=0.5, tokens_used=0,
                    cost_usd=0.001, latency_ms=10.0, iterations=0,
                    intermediate_outputs=[],
                ),
            )
        chosen = await opt.select_for_task("does-not-exist", min_trials=1)
        assert chosen.loop_id == "global-winner"


class TestLoopOptimizerLeaderboard:
    """get_leaderboard delegates to the store."""

    @pytest.mark.asyncio
    async def test_get_leaderboard_empty(self, optimizer: LoopOptimizer):
        lb = await optimizer.get_leaderboard()
        assert lb == []

    @pytest.mark.asyncio
    async def test_get_leaderboard_min_trials(self, optimizer: LoopOptimizer):
        # Cycle once with 2 trials, then ask for the leaderboard with
        # min_trials=3 (the default) — should be empty.
        await optimizer.run_optimization_cycle(
            task_type="code", task_sample="x", n_trials=2
        )
        lb = await optimizer.get_leaderboard(min_trials=3)
        assert lb == []


class TestLoopOptimizerRecord:
    """arecord_trial: single trial insertion path."""

    @pytest.mark.asyncio
    async def test_arecord_trial_uses_optimizer_critic(
        self, store: TrialStore
    ):
        opt = LoopOptimizer(trial_store=store)
        graph = opt.meta_agent._assembler.assemble_builtin("reflection")
        result = LoopResult(
            output="a long thoughtful response with several words.",
            confidence=0.7,
            tokens_used=10,
            cost_usd=0.001,
            latency_ms=100.0,
            iterations=1,
            intermediate_outputs=[],
        )
        trial = await opt.arecord_trial(
            loop_id="refl",
            task_type="code",
            graph=graph,
            result=result,
            task_sample="write something thoughtful",
        )
        assert trial.loop_id == "refl"
        assert trial.task_type == "code"
        assert trial.score.composite_score > 0.0


class TestLoopOptimizerSync:
    """Sync wrappers."""

    def test_run_optimization_cycle_sync(self, store: TrialStore):
        opt = LoopOptimizer(trial_store=store)
        report = opt.run_optimization_cycle_sync(
            task_type="code", task_sample="x", n_trials=2
        )
        assert len(report.trials) == 2

    def test_select_for_task_sync(self, store: TrialStore):
        opt = LoopOptimizer(trial_store=store)
        graph = opt.select_for_task_sync("code", min_trials=1)
        assert graph.loop_id


class TestLoopOptimizerCustomExecutor:
    """Plugging a custom executor in."""

    @pytest.mark.asyncio
    async def test_custom_executor_receives_loop_id(self, store: TrialStore):
        opt = LoopOptimizer(trial_store=store)

        seen: list[tuple[str, str]] = []

        async def my_exec(graph, task, *, loop_id):
            seen.append((loop_id, task))
            return LoopResult(
                output=f"custom for {loop_id}",
                confidence=0.5,
                tokens_used=10,
                cost_usd=0.001,
                latency_ms=10.0,
                iterations=1,
                intermediate_outputs=[],
            )

        opt.set_executor(my_exec)
        await opt.run_optimization_cycle(
            task_type="code", task_sample="hello", n_trials=2
        )
        assert seen
        for loop_id, task in seen:
            assert task == "hello"


class TestOptimizationConfig:
    """The dataclass is just a bag of tunables."""

    def test_defaults(self):
        c = OptimizationConfig()
        assert c.n_trials == 3
        assert c.base_loop == "reflection"
        assert c.task_type == "general"
        assert c.task_sample == ""
        assert c.include_builtins is True
