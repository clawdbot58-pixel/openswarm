"""Loop optimizer - scores loop performance and recommends improvements."""

import time
from dataclasses import dataclass
from typing import Any

from .base_loop import LoopResult
from .model_router import LLMClient
from .registry import LoopRegistry


@dataclass
class CriticScore:
    """Score and critique from a critic LLM.

    Attributes:
        score: Overall quality score (1.0-10.0).
        critique: Detailed feedback text.
        confidence: Critic's confidence in the score (0.0-1.0).
        dimensions: Per-dimension scores (correctness, clarity, completeness, etc.).
    """
    score: float
    critique: str
    confidence: float = 0.8
    dimensions: dict[str, float] | None = None

    def __post_init__(self) -> None:
        """Validate and clamp score values."""
        self.score = max(1.0, min(10.0, self.score))
        self.confidence = max(0.0, min(1.0, self.confidence))


@dataclass
class LoopRecommendation:
    """A recommended loop with justification.

    Attributes:
        template_id: ID of recommended loop template.
        name: Human-readable name.
        score: Recommendation score (higher is better).
        reason: Why this loop is recommended.
        estimated_cost: Estimated cost in USD.
        estimated_latency: Estimated latency in ms.
    """
    template_id: str
    name: str
    score: float
    reason: str
    estimated_cost: float = 0.0
    estimated_latency: float = 0.0


class LoopOptimizer:
    """Optimizes loop selection using critic feedback.

    Uses a dedicated critic LLM to score outputs and update
    template statistics for better recommendations.
    """

    def __init__(self, registry: LoopRegistry, critic_client: LLMClient | None = None):
        """Initialize the loop optimizer.

        Args:
            registry: The loop template registry.
            critic_client: Optional dedicated LLM client for criticism.
                         If None, uses the default model with critic prompt.
        """
        self.registry = registry
        self.critic_client = critic_client

    async def score_output(
        self,
        task: str,
        output: str,
        expected_format: str = "text",
    ) -> CriticScore:
        """Score an output using a critic LLM.

        Args:
            task: The original task description.
            output: The output to score.
            expected_format: Expected output format (text, json, code, etc.).

        Returns:
            CriticScore with score and feedback.
        """
        score_prompt = self._build_critic_prompt(task, output, expected_format)

        if self.critic_client:
            response = await self.critic_client.generate(
                system=self._get_critic_system_prompt(),
                user=score_prompt,
                json_mode=False,
                temperature=0.3,
            )
        else:
            raise RuntimeError("No critic client configured")

        return self._parse_critic_response(response.content, output)

    def _get_critic_system_prompt(self) -> str:
        """Get the critic system prompt.

        Returns:
            System prompt for the critic LLM.
        """
        return """You are a quality critic. Your job is to evaluate the quality of
outputs produced by AI agents.

Score outputs on a scale of 1-10 based on:
- Correctness: Is the output accurate and free from errors?
- Clarity: Is the output clear and well-organized?
- Completeness: Does the output fully address the task?
- Quality: Overall production value

Provide both an overall score and per-dimension scores.
Be honest and constructive in your critique."""

    def _build_critic_prompt(
        self,
        task: str,
        output: str,
        expected_format: str,
    ) -> str:
        """Build the critic evaluation prompt.

        Args:
            task: Original task description.
            output: Output to evaluate.
            expected_format: Expected output format.

        Returns:
            Formatted critic prompt.
        """
        return f"""Task: {task}

Expected format: {expected_format}

Output to evaluate:
---
{output}
---

Evaluate this output and provide:
1. Overall score (1-10)
2. Per-dimension scores (correctness, clarity, completeness)
3. Detailed critique

Format your response as:
SCORE: [overall 1-10]
DIMENSIONS: correctness=[1-10], clarity=[1-10], completeness=[1-10]
CRITIQUE: [your detailed feedback]
"""

    def _parse_critic_response(self, content: str, output: str) -> CriticScore:
        """Parse critic response into CriticScore.

        Args:
            content: Raw critic LLM response.
            output: Original output (for fallback).

        Returns:
            CriticScore instance.
        """
        import re

        dimensions: dict[str, float] = {}
        critique = content

        score_match = re.search(r"SCORE[:\s]+(\d+(?:\.\d+)?)", content, re.IGNORECASE)
        overall_score = float(score_match.group(1)) if score_match else 5.0

        dim_patterns = {
            "correctness": r"correctness[:=]\s*(\d+(?:\.\d+)?)",
            "clarity": r"clarity[:=]\s*(\d+(?:\.\d+)?)",
            "completeness": r"completeness[:=]\s*(\d+(?:\.\d+)?)",
        }

        for dim_name, pattern in dim_patterns.items():
            match = re.search(pattern, content, re.IGNORECASE)
            if match:
                dimensions[dim_name] = float(match.group(1))

        critique_match = re.search(r"CRITIQUE[:\s]+(.*)", content, re.IGNORECASE | re.DOTALL)
        if critique_match:
            critique = critique_match.group(1).strip()

        confidence = 0.8
        if len(dimensions) >= 3:
            confidence = 0.95
        elif len(dimensions) >= 1:
            confidence = 0.85

        return CriticScore(
            score=overall_score,
            critique=critique,
            confidence=confidence,
            dimensions=dimensions if dimensions else None,
        )

    async def record_result(
        self,
        template_id: str,
        loop_result: LoopResult,
        critic_score: CriticScore,
    ) -> None:
        """Record a loop execution result and update registry stats.

        Args:
            template_id: Template that was used.
            loop_result: Result from loop execution.
            critic_score: Score from critic.
        """
        success = critic_score.score >= 5.0

        self.registry.update_stats(
            template_id=template_id,
            score=critic_score.score,
            cost=loop_result.cost_usd,
            latency=loop_result.latency_ms,
            success=success,
        )

    def recommend(
        self,
        task_type: str,
        min_score: float = 7.0,
        budget_usd: float | None = None,
    ) -> list[LoopRecommendation]:
        """Recommend loops for a task type.

        Score = (avg_score * 0.6) + (1/avg_cost * 0.3) + (1/avg_latency * 0.1)

        Args:
            task_type: Type of task (coding, review, research, etc.).
            min_score: Minimum average score threshold.
            budget_usd: Optional maximum cost budget.

        Returns:
            List of LoopRecommendations, sorted by score.
        """
        recommendations = self.registry.get_recommendation(
            task_type=task_type,
            budget_usd=budget_usd,
            limit=10,
        )

        result: list[LoopRecommendation] = []

        for rec in recommendations:
            if rec.get("avg_score", 0) < min_score:
                continue

            reason = f"High success rate ({rec.get('success_rate', 0):.0%})"
            if rec.get("avg_score", 0) >= 8.0:
                reason += f", excellent avg score ({rec.get('avg_score', 0):.1f}/10)"
            elif rec.get("avg_score", 0) >= 7.0:
                reason += f", good avg score ({rec.get('avg_score', 0):.1f}/10)"

            if rec.get("usage_count", 0) >= 5:
                reason += f", tried {rec.get('usage_count')} times"

            result.append(LoopRecommendation(
                template_id=rec["id"],
                name=rec["name"],
                score=rec["recommendation_score"],
                reason=reason,
                estimated_cost=rec.get("avg_cost_usd", 0.0),
                estimated_latency=rec.get("avg_latency_ms", 0.0),
            ))

        result.sort(key=lambda x: x.score, reverse=True)

        return result[:3]

    def analyze_loop_performance(self, template_id: str) -> dict[str, Any] | None:
        """Get detailed performance analysis for a template.

        Args:
            template_id: Template ID to analyze.

        Returns:
            Dict with performance metrics or None if not found.
        """
        stats = self.registry.get_stats(template_id)
        if stats is None:
            return None

        performance_score = (
            stats.avg_score / 10.0 * 0.6 +
            1.0 / (stats.avg_cost_usd + 0.001) * 0.3 +
            1.0 / (stats.avg_latency_ms + 1.0) * 0.1
        )

        return {
            "template_id": template_id,
            "success_rate": stats.success_rate,
            "avg_score": stats.avg_score,
            "avg_cost_usd": stats.avg_cost_usd,
            "avg_latency_ms": stats.avg_latency_ms,
            "usage_count": stats.usage_count,
            "performance_score": performance_score,
            "is_reliable": stats.success_rate >= 0.8 and stats.usage_count >= 5,
            "is_fast": stats.avg_latency_ms < 5000,
            "is_cheap": stats.avg_cost_usd < 0.01,
        }

    def compare_templates(
        self,
        template_id_a: str,
        template_id_b: str,
    ) -> dict[str, Any]:
        """Compare two templates side by side.

        Args:
            template_id_a: First template ID.
            template_id_b: Second template ID.

        Returns:
            Dict with comparison metrics.
        """
        stats_a = self.registry.get_stats(template_id_a)
        stats_b = self.registry.get_stats(template_id_b)

        if stats_a is None and stats_b is None:
            return {"winner": None, "reason": "No data for either template"}

        if stats_a is None:
            return {"winner": template_id_b, "reason": f"{template_id_a} has no data"}
        if stats_b is None:
            return {"winner": template_id_a, "reason": f"{template_id_b} has no data"}

        comparisons: dict[str, Any] = {
            "template_a": template_id_a,
            "template_b": template_id_b,
            "scores": {
                template_id_a: stats_a.avg_score,
                template_id_b: stats_b.avg_score,
            },
            "costs": {
                template_id_a: stats_a.avg_cost_usd,
                template_id_b: stats_b.avg_cost_usd,
            },
            "latencies": {
                template_id_a: stats_a.avg_latency_ms,
                template_id_b: stats_b.avg_latency_ms,
            },
            "success_rates": {
                template_id_a: stats_a.success_rate,
                template_id_b: stats_b.success_rate,
            },
        }

        perf_a = (
            stats_a.avg_score / 10.0 * 0.6 +
            1.0 / (stats_a.avg_cost_usd + 0.001) * 0.3 +
            1.0 / (stats_a.avg_latency_ms + 1.0) * 0.1
        )
        perf_b = (
            stats_b.avg_score / 10.0 * 0.6 +
            1.0 / (stats_b.avg_cost_usd + 0.001) * 0.3 +
            1.0 / (stats_b.avg_latency_ms + 1.0) * 0.1
        )

        comparisons["performance_scores"] = {
            template_id_a: perf_a,
            template_id_b: perf_b,
        }
        comparisons["winner"] = template_id_a if perf_a > perf_b else template_id_b

        reasons = []
        if stats_a.avg_score > stats_b.avg_score:
            reasons.append(f"{template_id_a} has higher quality ({stats_a.avg_score:.1f} vs {stats_b.avg_score:.1f})")
        elif stats_b.avg_score > stats_a.avg_score:
            reasons.append(f"{template_id_b} has higher quality ({stats_b.avg_score:.1f} vs {stats_a.avg_score:.1f})")

        if stats_a.avg_cost_usd < stats_b.avg_cost_usd:
            reasons.append(f"{template_id_a} is cheaper (${stats_a.avg_cost_usd:.4f} vs ${stats_b.avg_cost_usd:.4f})")
        elif stats_b.avg_cost_usd < stats_a.avg_cost_usd:
            reasons.append(f"{template_id_b} is cheaper (${stats_b.avg_cost_usd:.4f} vs ${stats_a.avg_cost_usd:.4f})")

        if stats_a.avg_latency_ms < stats_b.avg_latency_ms:
            reasons.append(f"{template_id_a} is faster ({stats_a.avg_latency_ms:.0f}ms vs {stats_b.avg_latency_ms:.0f}ms)")
        elif stats_b.avg_latency_ms < stats_a.avg_latency_ms:
            reasons.append(f"{template_id_b} is faster ({stats_b.avg_latency_ms:.0f}ms vs {stats_a.avg_latency_ms:.0f}ms)")

        comparisons["reasons"] = reasons

        return comparisons