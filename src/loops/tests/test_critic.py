"""Tests for the Phase 10 loop critic."""

import pytest

from loops.critic import (
    COST_WEIGHT,
    CriticScore,
    DEFAULT_CRITERIA,
    LATENCY_WEIGHT,
    LoopCritic,
    QUALITY_WEIGHT,
    SUCCESS_THRESHOLD,
    build_critic,
)


class TestCriticScore:
    """Tests for the :class:`CriticScore` Pydantic model."""

    def test_composite_score_formula(self):
        """The composite must follow the spec's formula exactly."""
        s = CriticScore(
            quality_score=8.0,
            cost_usd=0.01,
            latency_ms=1000.0,
        )
        quality_term = (8.0 / 10.0) * QUALITY_WEIGHT
        cost_term = (1.0 / 0.01) * COST_WEIGHT
        latency_sec = 1.0
        latency_term = (1.0 / latency_sec) * LATENCY_WEIGHT
        expected = quality_term + cost_term + latency_term
        assert s.composite_score == pytest.approx(expected, rel=1e-6)

    def test_composite_score_floors_cost_and_latency(self):
        """Tiny or zero cost/latency should be floored to avoid blow-up."""
        s = CriticScore(quality_score=5.0, cost_usd=0.0, latency_ms=0.0)
        # cost floor 0.001 → 1/0.001 = 1000 → * 0.3 = 300
        # latency floor 0.1s → 1/0.1 = 10 → * 0.1 = 1
        # quality: 5/10 * 0.6 = 0.3
        assert s.composite_score == pytest.approx(301.3, rel=1e-6)

    def test_is_success_threshold(self):
        assert CriticScore(quality_score=5.0).is_success
        assert CriticScore(quality_score=SUCCESS_THRESHOLD + 0.1).is_success
        assert not CriticScore(quality_score=4.9).is_success
        assert not CriticScore(quality_score=0.0).is_success

    def test_quality_clamped_to_range(self):
        # Pydantic Field(ge=0, le=10) should reject out-of-range.
        with pytest.raises(Exception):
            CriticScore(quality_score=11.0)
        with pytest.raises(Exception):
            CriticScore(quality_score=-0.1)

    def test_to_dict_includes_composite(self):
        s = CriticScore(quality_score=7.0, cost_usd=0.005, latency_ms=500.0)
        d = s.to_dict()
        assert "composite_score" in d
        assert d["composite_score"] == pytest.approx(s.composite_score)
        assert d["quality_score"] == 7.0

    def test_extra_forbidden(self):
        with pytest.raises(Exception):
            CriticScore(quality_score=5.0, unknown=1)


class TestLoopCritic:
    """Tests for the :class:`LoopCritic` LLM-backed scorer."""

    @pytest.mark.asyncio
    async def test_heuristic_when_no_model(self):
        critic = LoopCritic(model=None)
        score = await critic.score(
            task="write hello world",
            output="Hello, world! This is a simple greeting.",
            task_type="general",
            cost_usd=0.001,
            latency_ms=100.0,
        )
        assert 0.0 <= score.quality_score <= 9.0
        # Heuristic must always return a composite in valid range.
        assert score.composite_score > 0.0
        # Reasoning should mention the heuristic path.
        assert "heuristic" in score.reasoning.lower()

    @pytest.mark.asyncio
    async def test_heuristic_with_reference_answer(self):
        critic = LoopCritic(model=None)
        score = await critic.score(
            task="compute fib(10)",
            output=(
                "The answer is 55. 55 is the tenth Fibonacci number. "
                "So 55 is what we wanted."
            ),
            expected="The tenth Fibonacci number is 55.",
            cost_usd=0.001,
            latency_ms=50.0,
        )
        # Overlap with the reference should boost the quality above the
        # base of ~4 for a 16-word output.
        assert score.quality_score >= 5.0

    @pytest.mark.asyncio
    async def test_llm_path_parses_json(self):
        class FakeResponse:
            def __init__(self, content):
                self.content = content

        class FakeLLM:
            async def generate(self, system, user, json_mode=False, temperature=0.2):
                return FakeResponse(
                    '{"score": 7.5, "criteria": {"accuracy": 8, "completeness": 7, '
                    '"clarity": 8, "relevance": 7}, "reasoning": "Good."}'
                )

        critic = LoopCritic(model=FakeLLM())
        score = await critic.score(
            task="test",
            output="An output.",
            cost_usd=0.01,
            latency_ms=200.0,
        )
        assert score.quality_score == pytest.approx(7.5)
        assert score.criteria_scores["accuracy"] == 8.0
        assert "Good" in score.reasoning

    @pytest.mark.asyncio
    async def test_llm_path_handles_code_fences(self):
        class FakeResponse:
            def __init__(self, content):
                self.content = content

        class FakeLLM:
            async def generate(self, system, user, json_mode=False, temperature=0.2):
                return FakeResponse(
                    '```json\n{"score": 6.0, "criteria": {}, "reasoning": "fine"}\n```'
                )

        critic = LoopCritic(model=FakeLLM())
        score = await critic.score(task="t", output="o")
        assert score.quality_score == pytest.approx(6.0)

    @pytest.mark.asyncio
    async def test_llm_path_falls_back_on_garbage(self):
        class FakeResponse:
            def __init__(self, content):
                self.content = content

        class FakeLLM:
            async def generate(self, system, user, json_mode=False, temperature=0.2):
                return FakeResponse("not json at all")

        critic = LoopCritic(model=FakeLLM())
        score = await critic.score(task="t", output="a non-empty output")
        # No JSON found → default 5.0 from the regex fallback.
        assert score.quality_score == 5.0

    @pytest.mark.asyncio
    async def test_llm_path_falls_back_on_exception(self):
        class FailingLLM:
            async def generate(self, system, user, json_mode=False, temperature=0.2):
                raise RuntimeError("no API key")

        critic = LoopCritic(model=FailingLLM())
        score = await critic.score(task="t", output="an output")
        # Should fall through to heuristic — non-zero, non-10.
        assert 0.0 < score.quality_score <= 9.0


def test_build_critic_none_returns_offline():
    c = build_critic(None)
    assert isinstance(c, LoopCritic)
    assert c._model is None  # type: ignore[attr-defined]


def test_build_critic_wraps_model():
    class M:
        async def generate(self, *a, **k):
            return None

    c = build_critic(M())
    assert c._model is M() or c._model is not None  # type: ignore[attr-defined]


def test_default_criteria_is_baseline():
    assert "accuracy" in DEFAULT_CRITERIA
    assert "completeness" in DEFAULT_CRITERIA
