"""Loop critic - scores loop output quality.

Phase 10 introduces a dedicated critic LLM call that grades a loop's
output on a 0-10 scale, decomposes the score into named criteria, and
combines quality with cost and latency into a single
:attr:`CriticScore.composite_score` number.  The composite formula is
fixed by ``vision/thinking-loops.md``::

    score = (quality * 0.6) + (1 / cost_usd * 0.3) + (1 / latency_sec * 0.1)

and any change to those weights requires a version bump of the
``thinking-loops`` spec.

The Phase 4 ``loops.optimizer.CriticScore`` dataclass is still
exported and used by the registry, but it has a different shape (it
holds ``score`` / ``critique`` / ``confidence`` / ``dimensions``).
The Phase 10 :class:`CriticScore` here is the new public type the
meta-agent, loop-optimizer and trial-store all use.
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any, Iterable, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Scoring constants — frozen by the thinking-loops spec.
# ---------------------------------------------------------------------------

#: Weight applied to the normalised quality score in the composite.
QUALITY_WEIGHT: float = 0.6

#: Weight applied to the inverse-cost term in the composite.
COST_WEIGHT: float = 0.3

#: Weight applied to the inverse-latency term in the composite.
LATENCY_WEIGHT: float = 0.1

#: Minimum quality score to consider a trial "successful".
SUCCESS_THRESHOLD: float = 5.0

#: Default cost floor for the inverse-cost term (matches the spec).
_COST_FLOOR: float = 0.001

#: Default latency floor (seconds) for the inverse-latency term.
_LATENCY_FLOOR_SEC: float = 0.1

#: Names of the criteria the critic always tries to surface.  Loop
#: critic callers can add or rename criteria, but these four are the
#: baseline the dashboard surfaces.
DEFAULT_CRITERIA: tuple[str, ...] = (
    "accuracy",
    "completeness",
    "clarity",
    "relevance",
)


# ---------------------------------------------------------------------------
# Public LLM-client protocol — the critic only needs ``.generate``
# ---------------------------------------------------------------------------


@runtime_checkable
class _TextGenClient(Protocol):
    """Minimum LLM-client surface the :class:`LoopCritic` needs.

    The Phase 4 ``loops.model_router.LLMClient`` and the Phase 10
    ``agents.llm_client.LLMClient`` both expose ``.generate`` with a
    compatible shape; the loop-optimizer wires whichever it has.  The
    critic only ever calls ``.generate`` (never ``.stream``), so the
    protocol is deliberately small.
    """

    async def generate(  # pragma: no cover — protocol
        self,
        system: str,
        user: str,
        json_mode: bool = False,
        temperature: float = 0.3,
    ) -> Any:
        ...


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class CriticScore(BaseModel):
    """Quality assessment of a loop's output.

    The composite score is a derived property; it is recomputed from
    ``quality_score`` / ``cost_usd`` / ``latency_ms`` on every access,
    so callers can mutate the underlying fields without worrying about
    keeping the composite in sync.

    Attributes:
        quality_score: Overall quality in [0, 10].
        reasoning: Free-text explanation from the critic.
        criteria_scores: Per-criterion sub-scores (``accuracy``,
            ``clarity``, ...).  Always present but may be empty.
        cost_usd: USD cost of the loop that produced the output.
        latency_ms: Wall-clock latency of the loop in milliseconds.
        loop_id: Optional identifier of the loop that produced the
            output (filled in by the trial store).
        task_type: Optional task type tag (filled in by the
            trial store / meta-agent).
    """

    model_config = ConfigDict(extra="forbid")

    quality_score: float = Field(ge=0.0, le=10.0)
    reasoning: str = ""
    criteria_scores: dict[str, float] = Field(default_factory=dict)
    cost_usd: float = 0.0
    latency_ms: float = 0.0
    loop_id: str | None = None
    task_type: str | None = None

    @property
    def composite_score(self) -> float:
        """Composite quality / cost / latency score.

        Implements the fixed formula from ``vision/thinking-loops.md``::

            score = (quality / 10) * 0.6
                  + (1 / max(cost, 0.001)) * 0.3
                  + (1 / max(latency_sec, 0.1)) * 0.1

        Higher is better.  The two floor terms (0.001 USD, 0.1 s)
        prevent a zero-cost / zero-latency loop from swamping the
        composite, which would otherwise be a degenerate
        ranking signal.
        """
        quality_term = (float(self.quality_score) / 10.0) * QUALITY_WEIGHT
        cost_term = (1.0 / max(float(self.cost_usd), _COST_FLOOR)) * COST_WEIGHT
        latency_sec = float(self.latency_ms) / 1000.0
        latency_term = (1.0 / max(latency_sec, _LATENCY_FLOOR_SEC)) * LATENCY_WEIGHT
        return quality_term + cost_term + latency_term

    @property
    def is_success(self) -> bool:
        """True if the quality score clears the success threshold."""
        return self.quality_score >= SUCCESS_THRESHOLD

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a JSON-safe dict (includes the composite)."""
        data = self.model_dump()
        data["composite_score"] = self.composite_score
        return data


# ---------------------------------------------------------------------------
# The critic
# ---------------------------------------------------------------------------


# A small rubric the critic applies by default; callers can override
# at construction time or per-call.
_DEFAULT_RUBRIC: str = (
    "Rate the output on:\n"
    "- accuracy: is the output factually correct and free of errors?\n"
    "- completeness: does the output fully address the task?\n"
    "- clarity: is the output well-organised, readable, and unambiguous?\n"
    "- relevance: does the output stay on-task without filler?\n\n"
    "Output a JSON object with these keys:\n"
    '  "score": float between 0 and 10 (overall quality)\n'
    '  "criteria": { "accuracy": float, "completeness": float, "clarity": float, "relevance": float }\n'
    '  "reasoning": short string explaining the score\n'
    "Do not wrap the JSON in code fences. Do not add commentary outside the JSON."
)


class LoopCritic:
    """LLM-backed scorer for loop outputs.

    The critic calls an LLM (any object with an async
    ``.generate(system, user, json_mode, temperature)`` method) to
    grade an output, parses the JSON it returns, and wraps the result
    in a :class:`CriticScore`.  When the LLM call fails, returns
    non-JSON, or is unavailable, the critic falls back to a
    deterministic heuristic so the trial/error cycle can keep running
    offline.

    Args:
        model: An LLM client (anything implementing
            :class:`_TextGenClient`).  ``None`` is allowed and yields
            the deterministic path only — handy for tests and for the
            first deploy before the LLM key is configured.
        rubric: Custom scoring rubric forwarded to the LLM.
        criteria: Names of the criteria the critic should report.
    """

    def __init__(
        self,
        model: _TextGenClient | None = None,
        *,
        rubric: str | None = None,
        criteria: Iterable[str] | None = None,
    ) -> None:
        self._model = model
        self._rubric = rubric or _DEFAULT_RUBRIC
        self._criteria: tuple[str, ...] = tuple(criteria or DEFAULT_CRITERIA)

    @property
    def rubric(self) -> str:
        """The rubric the critic hands to the LLM."""
        return self._rubric

    @property
    def criteria(self) -> tuple[str, ...]:
        """The criterion names the critic expects in the response."""
        return self._criteria

    async def score(
        self,
        task: str,
        output: str,
        expected: str | None = None,
        task_type: str | None = None,
        *,
        cost_usd: float = 0.0,
        latency_ms: float = 0.0,
        loop_id: str | None = None,
    ) -> CriticScore:
        """Score a loop's output.

        Args:
            task: The original user task.
            output: The text the loop produced.
            expected: Optional reference answer (used to improve the
                rubric when present).
            task_type: Optional tag (e.g. ``"code_review"``).  Stored
                on the returned :class:`CriticScore` for later
                leaderboard filtering.
            cost_usd: USD cost of the loop execution that produced
                ``output``.  Folded into the composite score.
            latency_ms: Latency of the loop execution in ms.  Folded
                into the composite score.
            loop_id: Optional loop identifier (forwarded to the
                returned :class:`CriticScore`).

        Returns:
            A :class:`CriticScore` with quality, criteria, reasoning,
            and composite score.
        """
        if self._model is None:
            return self._heuristic_score(
                task, output, expected, task_type, cost_usd, latency_ms, loop_id
            )

        system = (
            "You are an impartial quality critic for AI-generated "
            "outputs. Score fairly, never pad scores, never penalise "
            "for being concise. Respond with JSON only."
        )
        user = self._build_user_prompt(task, output, expected)

        try:
            response = await self._model.generate(
                system=system,
                user=user,
                json_mode=True,
                temperature=0.2,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("critic LLM call failed (%s); using heuristic", exc)
            return self._heuristic_score(
                task, output, expected, task_type, cost_usd, latency_ms, loop_id
            )

        content = getattr(response, "content", None) or getattr(response, "text", "")
        parsed = self._parse_response(content)
        return CriticScore(
            quality_score=parsed["quality_score"],
            reasoning=parsed["reasoning"],
            criteria_scores=parsed["criteria_scores"],
            cost_usd=float(cost_usd),
            latency_ms=float(latency_ms),
            loop_id=loop_id,
            task_type=task_type,
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _build_user_prompt(
        self,
        task: str,
        output: str,
        expected: str | None,
    ) -> str:
        pieces = [
            "# TASK",
            task.strip() or "(empty task)",
            "",
            "# OUTPUT TO EVALUATE",
            output.strip() if output else "(empty output)",
        ]
        if expected:
            pieces.extend(["", "# REFERENCE ANSWER", expected.strip()])
        pieces.extend(
            [
                "",
                "# RUBRIC",
                self._rubric,
                "",
                "Respond with the JSON object described above.",
            ]
        )
        return "\n".join(pieces)

    def _parse_response(self, content: str) -> dict[str, Any]:
        """Parse the LLM's JSON-ish response into score fields.

        Tolerant of code fences and small format deviations; falls back
        to a regex scan for ``"score": <number>`` and the per-criterion
        sub-scores when the JSON itself is malformed.
        """
        text = (content or "").strip()
        # Strip code fences if the model wrapped the JSON in markdown.
        if text.startswith("```"):
            text = text.strip("`")
            if text.lower().startswith("json"):
                text = text[4:]
            text = text.strip()

        # First try a strict JSON parse.
        data: Any = None
        if text:
            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                data = None

        if isinstance(data, dict):
            return self._extract_from_dict(data)

        # Heuristic: regex over freeform text.
        return self._extract_from_text(text)

    def _extract_from_dict(self, data: dict[str, Any]) -> dict[str, Any]:
        quality = self._coerce_quality(data.get("score"))
        reasoning = str(data.get("reasoning") or "").strip()
        raw_criteria = data.get("criteria") or data.get("criteria_scores") or {}
        criteria_scores: dict[str, float] = {}
        if isinstance(raw_criteria, dict):
            for name in self._criteria:
                if name in raw_criteria:
                    criteria_scores[name] = self._coerce_quality(raw_criteria[name])
        return {
            "quality_score": quality,
            "reasoning": reasoning,
            "criteria_scores": criteria_scores,
        }

    def _extract_from_text(self, text: str) -> dict[str, Any]:
        quality = 5.0
        score_match = re.search(
            r"\"?score\"?\s*[:=]\s*(\d+(?:\.\d+)?)", text, re.IGNORECASE
        )
        if score_match:
            quality = self._coerce_quality(score_match.group(1))

        criteria_scores: dict[str, float] = {}
        for name in self._criteria:
            pat = rf"\"?{re.escape(name)}\"?\s*[:=]\s*(\d+(?:\.\d+)?)"
            match = re.search(pat, text, re.IGNORECASE)
            if match:
                criteria_scores[name] = self._coerce_quality(match.group(1))

        reasoning = ""
        reason_match = re.search(
            r"\"?reasoning\"?\s*[:=]\s*\"([^\"]*)\"", text, re.IGNORECASE
        )
        if reason_match:
            reasoning = reason_match.group(1).strip()

        return {
            "quality_score": quality,
            "reasoning": reasoning,
            "criteria_scores": criteria_scores,
        }

    @staticmethod
    def _coerce_quality(value: Any) -> float:
        """Clamp a value to the [0, 10] interval."""
        try:
            v = float(value)
        except (TypeError, ValueError):
            return 5.0
        return max(0.0, min(10.0, v))

    def _heuristic_score(
        self,
        task: str,
        output: str,
        expected: str | None,
        task_type: str | None,
        cost_usd: float,
        latency_ms: float,
        loop_id: str | None,
    ) -> CriticScore:
        """Deterministic offline scorer.

        Used when the LLM is unavailable or returns garbage.  The
        heuristic is intentionally simple — it rewards length,
        coherence, and overlap with a reference answer if one is
        supplied — and never returns a 10.  This keeps the
        trial/error cycle honest even without a live critic.
        """
        text = (output or "").strip()
        words = text.split()
        word_count = len(words)

        # Base: 4.0 for any non-empty output, scaled up to 8.0.
        if word_count == 0:
            base = 0.0
        else:
            base = 4.0 + min(4.0, word_count / 50.0)

        # Reward very long, well-structured outputs slightly.
        sentence_count = max(1, text.count(".") + text.count("!") + text.count("?"))
        if sentence_count >= 3:
            base += 0.5

        # Reward overlap with reference answer (if given).
        if expected:
            expected_words = set(expected.lower().split())
            output_words = set(word.lower() for word in words)
            if expected_words:
                overlap = len(expected_words & output_words) / len(expected_words)
                base += 2.0 * overlap
                base = min(9.0, base)

        quality = self._coerce_quality(base)
        criteria = {
            name: self._coerce_quality(quality - 0.5 if word_count < 10 else quality)
            for name in self._criteria
        }
        reasoning = (
            "Heuristic score (no critic LLM available). "
            f"word_count={word_count}, sentence_count={sentence_count}, "
            f"task_type={task_type or 'n/a'}."
        )
        return CriticScore(
            quality_score=quality,
            reasoning=reasoning,
            criteria_scores=criteria,
            cost_usd=float(cost_usd),
            latency_ms=float(latency_ms),
            loop_id=loop_id,
            task_type=task_type,
        )


# ---------------------------------------------------------------------------
# Convenience: build a critic from any LLMClient the caller has.
# ---------------------------------------------------------------------------


def build_critic(model: Any) -> LoopCritic:
    """Return a :class:`LoopCritic` for ``model``.

    Accepts both the ``loops.model_router.LLMClient`` and the
    ``agents.llm_client.LLMClient`` shapes; both expose the async
    ``.generate(system, user, json_mode, temperature)`` surface the
    critic needs.

    Args:
        model: Any LLM client implementing the surface above.

    Returns:
        A :class:`LoopCritic` configured to use ``model``.
    """
    if model is None:
        return LoopCritic(model=None)
    return LoopCritic(model=model)


__all__ = [
    "COST_WEIGHT",
    "CriticScore",
    "DEFAULT_CRITERIA",
    "LATENCY_WEIGHT",
    "LoopCritic",
    "QUALITY_WEIGHT",
    "SUCCESS_THRESHOLD",
    "build_critic",
]


def _now_iso() -> str:  # pragma: no cover — debugging helper
    """Return the current UTC time as ISO 8601 with ``Z`` suffix."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
