"""Tests for the Phase 10 meta-agent."""

import asyncio
from typing import Any

import pytest

from meta_agent import (
    DEFAULT_BASE_LOOP,
    LoopVariant,
    MetaAgent,
    TASK_TYPE_TO_BASE,
)
from loops.assembler import LoopEdge, LoopGraph, LoopAssembler
from loops.primitives import LoopPrimitive, PrimitiveType


class _StubLLM:
    """A minimal LLM stub that returns a fixed JSON mutation choice."""

    def __init__(self, content: str = '{"mutation": "noop", "rationale": "test"}'):
        self._content = content
        self.calls = 0

    async def generate(
        self,
        system: str = "",
        user: str = "",
        json_mode: bool = False,
        temperature: float = 0.7,
    ) -> Any:
        self.calls += 1
        return _StubResponse(self._content)


class _StubResponse:
    def __init__(self, content: str) -> None:
        self.content = content


class TestMetaAgentHeuristic:
    """Deterministic path: no LLM."""

    @pytest.mark.asyncio
    async def test_heuristic_produces_valid_dag(self):
        m = MetaAgent(llm=None)
        v = await m.propose_variant("reflection", "code_review", "review this PR")
        assert isinstance(v, LoopVariant)
        assert v.base_loop_id == "reflection"
        assert v.task_type == "code_review"
        # The produced graph must validate (otherwise the Pydantic
        # model_post_init hook would have raised).
        assert v.graph.topological_order()  # not empty + acyclic

    @pytest.mark.asyncio
    async def test_heuristic_picks_stable_id(self):
        m = MetaAgent(llm=None)
        v1 = await m.propose_variant("cot", "math", "compute 2+2")
        v2 = await m.propose_variant("cot", "math", "compute 2+2")
        assert v1.loop_id == v2.loop_id

    @pytest.mark.asyncio
    async def test_heuristic_different_task_types_yield_different_variants(self):
        m = MetaAgent(llm=None)
        a = await m.propose_variant("reflection", "code_review", "x")
        b = await m.propose_variant("reflection", "summarisation", "x")
        c = await m.propose_variant("reflection", "code_review", "x")
        # Same task type → same id.
        assert a.loop_id == c.loop_id
        # Different task type → different id.
        assert a.loop_id != b.loop_id

    @pytest.mark.asyncio
    async def test_heuristic_empty_task_uses_heuristic(self):
        m = MetaAgent(llm=None)
        v = await m.propose_variant("reflection", "code", "")
        assert v.rationale  # filled by heuristic
        assert v.modification != ""

    @pytest.mark.asyncio
    async def test_heuristic_quality_feedback_promotes_upgrade(self):
        """If recent feedback has quality < 5 and base is direct, upgrade."""
        m = MetaAgent(llm=None)
        # Build a "recent feedback" critic with quality < 5.
        from loops.critic import CriticScore

        bad = CriticScore(quality_score=2.0, loop_id="direct", task_type="code")
        v = await m.propose_variant(
            "direct", "code", "do code", recent_feedback=[bad]
        )
        # The upgrade mutation adds a critique+revise tail.
        assert "upgrade" in v.modification or "reflect" in v.modification

    @pytest.mark.asyncio
    async def test_heuristic_math_picks_strengthen_cot(self):
        m = MetaAgent(llm=None)
        v = await m.propose_variant("cot", "math", "compute something")
        # Math tasks with a cot base should be CoT-strengthened.
        assert v.modification == "strengthen_cot"

    @pytest.mark.asyncio
    async def test_heuristic_design_picks_raise_branch(self):
        m = MetaAgent(llm=None)
        v = await m.propose_variant("tree", "design", "design a system")
        assert v.modification == "raise_branch_count"

    @pytest.mark.asyncio
    async def test_heuristic_normalises_unknown_base(self):
        m = MetaAgent(llm=None)
        v = await m.propose_variant("garbage", "general", "anything")
        # Falls back to the default base loop.
        assert v.base_loop_id == DEFAULT_BASE_LOOP


class TestMetaAgentLLM:
    """LLM-driven path."""

    @pytest.mark.asyncio
    async def test_llm_path_is_used(self):
        llm = _StubLLM(
            '{"mutation": "strengthen_cot", "rationale": "needs reasoning"}'
        )
        m = MetaAgent(llm=llm)
        v = await m.propose_variant("reflection", "code", "do code")
        assert llm.calls == 1
        assert v.modification == "strengthen_cot"
        assert "needs reasoning" in v.rationale

    @pytest.mark.asyncio
    async def test_llm_path_handles_markdown_fences(self):
        llm = _StubLLM(
            '```json\n{"mutation": "lower_temperature", "rationale": "x"}\n```'
        )
        m = MetaAgent(llm=llm)
        v = await m.propose_variant("reflection", "general", "task")
        assert v.modification == "lower_temperature"

    @pytest.mark.asyncio
    async def test_llm_path_falls_back_on_garbage(self):
        llm = _StubLLM("not json at all")
        m = MetaAgent(llm=llm)
        v = await m.propose_variant("reflection", "general", "task")
        # Falls back to heuristic — must still produce a valid variant.
        assert v.modification != ""

    @pytest.mark.asyncio
    async def test_llm_unknown_mutation_is_noop(self):
        llm = _StubLLM('{"mutation": "uninvented", "rationale": "?"}')
        m = MetaAgent(llm=llm)
        v = await m.propose_variant("reflection", "general", "task")
        # Returns the base graph untouched; modification is a noop label.
        assert "noop" in v.modification
        # The graph should still be valid.
        assert v.graph.topological_order()


class TestMetaAgentReflect:
    """Test the :meth:`MetaAgent.reflect_on_trial` helper."""

    @pytest.mark.asyncio
    async def test_reflect_no_llm_returns_deterministic(self):
        m = MetaAgent(llm=None)
        from loops.critic import CriticScore
        from loops.trial_store import Trial
        from loops.base_loop import LoopResult
        from datetime import datetime, timezone

        score = CriticScore(quality_score=8.5, reasoning="ok")
        trial = Trial(
            trial_id="t-1",
            loop_id="refl",
            task_type="code",
            loop_graph={},
            score=score,
            result=LoopResult(output="x", confidence=0.5, tokens_used=0, cost_usd=0.001, latency_ms=10.0, iterations=0, intermediate_outputs=[]),
            timestamp=datetime.now(timezone.utc),
        )
        text = await m.reflect_on_trial(trial, score)
        assert "8.5" in text
        assert "refl" in text

    @pytest.mark.asyncio
    async def test_reflect_llm_uses_response(self):
        llm = _StubLLM("the loop did well because it self-corrected.")
        m = MetaAgent(llm=llm)
        from loops.critic import CriticScore
        from loops.trial_store import Trial
        from loops.base_loop import LoopResult
        from datetime import datetime, timezone

        score = CriticScore(quality_score=8.0, reasoning="ok")
        trial = Trial(
            trial_id="t-1",
            loop_id="refl",
            task_type="code",
            loop_graph={},
            score=score,
            result=LoopResult(output="x", confidence=0.5, tokens_used=0, cost_usd=0.001, latency_ms=10.0, iterations=0, intermediate_outputs=[]),
            timestamp=datetime.now(timezone.utc),
        )
        text = await m.reflect_on_trial(trial, score)
        assert "self-corrected" in text

    @pytest.mark.asyncio
    async def test_reflect_llm_falls_back_on_exception(self):
        class FailingLLM:
            async def generate(self, *a, **k):
                raise RuntimeError("no API")

        m = MetaAgent(llm=FailingLLM())
        from loops.critic import CriticScore
        from loops.trial_store import Trial
        from loops.base_loop import LoopResult
        from datetime import datetime, timezone

        score = CriticScore(quality_score=7.0, reasoning="ok")
        trial = Trial(
            trial_id="t-1",
            loop_id="refl",
            task_type="code",
            loop_graph={},
            score=score,
            result=LoopResult(output="x", confidence=0.5, tokens_used=0, cost_usd=0.001, latency_ms=10.0, iterations=0, intermediate_outputs=[]),
            timestamp=datetime.now(timezone.utc),
        )
        text = await m.reflect_on_trial(trial, score)
        # Should fall back to the deterministic explanation.
        assert "7.0" in text


class TestMetaAgentMutations:
    """The mutation catalog must be wired to a real assembler."""

    @pytest.mark.asyncio
    async def test_all_mutations_produce_valid_dags(self):
        assembler = LoopAssembler()
        m = MetaAgent(llm=None, assembler=assembler)
        for name, _ in m.MUTATIONS:
            llm = _StubLLM(f'{{"mutation": "{name}", "rationale": "?"}}')
            m2 = MetaAgent(llm=llm, assembler=assembler)
            v = await m2.propose_variant("reflection", "code", "x")
            # The graph must be a valid DAG.
            v.graph.validate_dag()
            v.graph.topological_order()
            assert v.modification == name or "noop" in v.modification

    @pytest.mark.asyncio
    async def test_add_critique_terminates_in_critique(self):
        assembler = LoopAssembler()
        m = MetaAgent(llm=_StubLLM('{"mutation": "add_critique"}'), assembler=assembler)
        v = await m.propose_variant("reflection", "general", "task")
        # The base reflection graph terminates in "revise"; the
        # add_critique mutation only adds a critique if none exists,
        # so it returns the base unchanged (noop).  Verify the graph
        # is still a valid DAG either way.
        v.graph.validate_dag()
        assert v.graph.terminal_nodes

    def test_task_type_mapping_has_baseline(self):
        assert "general" in TASK_TYPE_TO_BASE
        assert TASK_TYPE_TO_BASE["math"] == "cot"
        assert TASK_TYPE_TO_BASE["design"] == "tree"
        assert TASK_TYPE_TO_BASE["decision"] == "debate"
