"""Tests for loop optimizer."""

import pytest

from loops.base_loop import LoopResult
from loops.model_router import LLMClient
from loops.optimizer import CriticScore, LoopOptimizer, LoopRecommendation
from loops.registry import create_registry


@pytest.fixture
def optimizer():
    """Create an optimizer with in-memory registry."""
    registry = create_registry(db_path=None)
    return LoopOptimizer(registry)


@pytest.fixture
def mock_model_client():
    """Create a mock model client."""
    return LLMClient(models=["gpt-4o-mini"], provider="openai")


class TestCriticScore:
    """Tests for CriticScore dataclass."""

    def test_critic_score_valid_score(self):
        """Test CriticScore clamps score to valid range."""
        score = CriticScore(score=15.0, critique="Too high")
        assert score.score == 10.0

        score = CriticScore(score=0.0, critique="Too low")
        assert score.score == 1.0

        score = CriticScore(score=7.5, critique="Just right")
        assert score.score == 7.5

    def test_critic_score_valid_confidence(self):
        """Test CriticScore clamps confidence to valid range."""
        score = CriticScore(score=5.0, critique="Test", confidence=1.5)
        assert score.confidence == 1.0

        score = CriticScore(score=5.0, critique="Test", confidence=-0.5)
        assert score.confidence == 0.0

    def test_critic_score_with_dimensions(self):
        """Test CriticScore with per-dimension scores."""
        score = CriticScore(
            score=8.0,
            critique="Good work",
            confidence=0.9,
            dimensions={
                "correctness": 9.0,
                "clarity": 7.5,
                "completeness": 8.0,
            },
        )

        assert score.score == 8.0
        assert score.confidence == 0.9
        assert score.dimensions["correctness"] == 9.0
        assert score.dimensions["clarity"] == 7.5


class TestLoopOptimizer:
    """Tests for LoopOptimizer."""

    @pytest.mark.asyncio
    async def test_score_output_requires_critic_client(self, optimizer):
        """Test that score_output requires a critic client."""
        optimizer.critic_client = None

        with pytest.raises(RuntimeError, match="No critic client"):
            await optimizer.score_output("Task", "Output")

    def test_critic_system_prompt(self, optimizer):
        """Test critic system prompt is set correctly."""
        prompt = optimizer._get_critic_system_prompt()
        assert "quality critic" in prompt.lower()
        assert "1-10" in prompt

    def test_build_critic_prompt(self, optimizer):
        """Test critic prompt building."""
        prompt = optimizer._build_critic_prompt(
            task="Write a function",
            output="def foo(): pass",
            expected_format="code",
        )

        assert "Write a function" in prompt
        assert "def foo(): pass" in prompt
        assert "code" in prompt

    def test_parse_critic_response_with_score(self, optimizer):
        """Test parsing critic response with explicit score."""
        content = """
SCORE: 8.5
DIMENSIONS: correctness=9.0, clarity=8.0, completeness=8.5
CRITIQUE: The output is well-structured and correct.
        """

        score = optimizer._parse_critic_response(content, "original output")

        assert score.score == 8.5
        assert "well-structured" in score.critique.lower()
        assert score.dimensions is not None
        assert score.dimensions["correctness"] == 9.0

    def test_parse_critic_response_without_score(self, optimizer):
        """Test parsing critic response without explicit score."""
        content = "This output looks fine overall."

        score = optimizer._parse_critic_response(content, "original output")

        assert score.score == 5.0

    def test_parse_critic_response_no_dimensions(self, optimizer):
        """Test parsing critic response without dimensions."""
        content = "SCORE: 7.0\nGood output."

        score = optimizer._parse_critic_response(content, "original output")

        assert score.score == 7.0
        assert score.dimensions is None
        assert score.confidence < 0.9

    @pytest.mark.asyncio
    async def test_record_result(self, optimizer):
        """Test recording a loop result."""
        loop_result = LoopResult(
            output="Test output",
            confidence=0.8,
            tokens_used=100,
            cost_usd=0.001,
            latency_ms=200,
            iterations=1,
            intermediate_outputs=[],
        )

        critic_score = CriticScore(score=8.0, critique="Good")

        await optimizer.record_result("direct", loop_result, critic_score)

        stats = optimizer.registry.get_stats("direct")
        assert stats is not None
        assert stats.usage_count == 1
        assert stats.avg_score == 8.0

    def test_recommend(self, optimizer):
        """Test recommendation generation."""
        optimizer.registry.update_stats("direct", score=7.0, cost=0.001, latency=100, success=True)
        optimizer.registry.update_stats("reflection", score=8.0, cost=0.003, latency=300, success=True)
        optimizer.registry.update_stats("cot", score=6.0, cost=0.001, latency=100, success=True)

        recs = optimizer.recommend("general", min_score=6.0)

        assert len(recs) <= 3
        assert all(isinstance(r, LoopRecommendation) for r in recs)

        for rec in recs:
            assert rec.template_id
            assert rec.name
            assert rec.score > 0

    def test_recommend_respects_min_score(self, optimizer):
        """Test recommendations respect min_score threshold."""
        optimizer.registry.update_stats("direct", score=5.0, cost=0.001, latency=100, success=True)
        optimizer.registry.update_stats("reflection", score=8.0, cost=0.003, latency=300, success=True)

        recs = optimizer.recommend("coding", min_score=7.0)

        assert all(r.score >= 7.0 or optimizer.registry.get_stats(r.template_id).avg_score >= 7.0
                   for r in recs)

    def test_recommend_respects_budget(self, optimizer):
        """Test recommendations respect budget."""
        optimizer.registry.update_stats("direct", score=7.0, cost=0.001, latency=100, success=True)
        optimizer.registry.update_stats("ensemble", score=9.0, cost=0.100, latency=500, success=True)

        recs = optimizer.recommend("general", budget_usd=0.01)

        for rec in recs:
            assert rec.estimated_cost <= 0.01

    def test_analyze_loop_performance(self, optimizer):
        """Test performance analysis."""
        optimizer.registry.update_stats("reflection", score=8.0, cost=0.003, latency=300, success=True)
        optimizer.registry.update_stats("reflection", score=8.0, cost=0.003, latency=300, success=True)
        optimizer.registry.update_stats("reflection", score=8.0, cost=0.003, latency=300, success=True)
        optimizer.registry.update_stats("reflection", score=8.0, cost=0.003, latency=300, success=True)
        optimizer.registry.update_stats("reflection", score=8.0, cost=0.003, latency=300, success=True)

        analysis = optimizer.analyze_loop_performance("reflection")

        assert analysis is not None
        assert "template_id" in analysis
        assert "performance_score" in analysis
        assert "is_reliable" in analysis
        assert "is_fast" in analysis
        assert "is_cheap" in analysis

    def test_analyze_loop_performance_not_found(self, optimizer):
        """Test performance analysis for nonexistent template."""
        analysis = optimizer.analyze_loop_performance("nonexistent")
        assert analysis is None

    def test_compare_templates(self, optimizer):
        """Test comparing two templates."""
        optimizer.registry.update_stats("direct", score=7.0, cost=0.001, latency=100, success=True)
        optimizer.registry.update_stats("reflection", score=8.0, cost=0.003, latency=300, success=True)

        comparison = optimizer.compare_templates("direct", "reflection")

        assert "template_a" in comparison
        assert "template_b" in comparison
        assert "winner" in comparison
        assert comparison["template_a"] == "direct"
        assert comparison["template_b"] == "reflection"

    def test_compare_templates_one_not_found(self, optimizer):
        """Test comparing when one template not found."""
        optimizer.registry.update_stats("direct", score=7.0, cost=0.001, latency=100, success=True)

        comparison = optimizer.compare_templates("direct", "nonexistent")

        assert comparison["winner"] == "direct"
        assert "has no data" in comparison["reason"]

    def test_compare_templates_neither_found(self, optimizer):
        """Test comparing when neither template found."""
        comparison = optimizer.compare_templates("a", "b")

        assert comparison["winner"] is None


class TestLoopRecommendation:
    """Tests for LoopRecommendation dataclass."""

    def test_recommendation_creation(self):
        """Test LoopRecommendation can be created."""
        rec = LoopRecommendation(
            template_id="test-loop",
            name="Test Loop",
            score=8.5,
            reason="High quality",
            estimated_cost=0.002,
            estimated_latency=200,
        )

        assert rec.template_id == "test-loop"
        assert rec.name == "Test Loop"
        assert rec.score == 8.5
        assert rec.reason == "High quality"
        assert rec.estimated_cost == 0.002
        assert rec.estimated_latency == 200