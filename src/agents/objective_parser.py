"""Translate a user's natural-language message into a structured objective.

The Main Agent is the only agent that talks to the user. When the user
types a message, the Main Agent must decide what they actually meant
and emit a structured JSON objective that the Conductor can act on.

This module isolates that translation in one place. The public surface
is two functions:

* :func:`parse_objective` — full LLM-driven parse. Returns a
  :class:`StructuredObjective` plus a short confidence score and a
  trace of the LLM call.
* :func:`parse_objective_heuristic` — fast keyword-based fallback that
  needs no LLM. Used as a graceful degradation when the LLM is
  unreachable and as a sanity check in tests.

The structured shape is deliberately small and aligned with what the
Conductor already needs: a goal, a verb/noun/sector classification,
suggested sector managers, a plan-readiness flag, and the user text
preserved verbatim. Anything richer belongs in the workflow JSON the
Conductor builds downstream.
"""
from __future__ import annotations

import json
import logging
import re
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any

from .llm_client import LLMClient, LLMError

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Domain enum (the canonical sector names)
# ---------------------------------------------------------------------------

# The Conductor recognises these sector keys when deciding which
# sector managers to spawn. Adding a new sector here is a one-line
# change because the Conductor's sector list is also derived from
# this constant (see conductor.py).

KNOWN_SECTORS: tuple[str, ...] = (
    "research",
    "coding",
    "testing",
    "review",
    "deployment",
    "analysis",
    "documentation",
    "planning",
)

# Aliases we accept in user text. The parser normalises them into a
# canonical sector name. Order matters: longest match wins.
SECTOR_ALIASES: dict[str, str] = {
    "research": "research",
    "investigate": "research",
    "investigation": "research",
    "look up": "research",
    "find out": "research",
    "find": "research",
    "search": "research",
    "code": "coding",
    "coding": "coding",
    "implement": "coding",
    "build": "coding",
    "develop": "coding",
    "write code": "coding",
    "fix": "coding",
    "bug": "coding",
    "refactor": "coding",
    "test": "testing",
    "tests": "testing",
    "qa": "testing",
    "verify": "testing",
    "review": "review",
    "audit": "review",
    "inspect": "review",
    "deploy": "deployment",
    "deployment": "deployment",
    "release": "deployment",
    "ship": "deployment",
    "analyse": "analysis",
    "analyze": "analysis",
    "analysis": "analysis",
    "evaluate": "analysis",
    "document": "documentation",
    "documentation": "documentation",
    "docs": "documentation",
    "readme": "documentation",
    "plan": "planning",
    "design": "planning",
    "architect": "planning",
    "architecture": "planning",
}


# Verb / intent heuristics. Used only by the fallback parser; the LLM
# parser emits its own verb.
INTENT_VERBS: dict[str, str] = {
    "create": "create",
    "build": "create",
    "make": "create",
    "write": "create",
    "implement": "create",
    "add": "create",
    "fix": "repair",
    "repair": "repair",
    "debug": "repair",
    "resolve": "repair",
    "patch": "repair",
    "refactor": "restructure",
    "restructure": "restructure",
    "reorganise": "restructure",
    "reorganize": "restructure",
    "investigate": "research",
    "research": "research",
    "analyse": "analyse",
    "analyze": "analyse",
    "evaluate": "analyse",
    "compare": "analyse",
    "deploy": "deploy",
    "ship": "deploy",
    "release": "deploy",
    "test": "verify",
    "verify": "verify",
    "validate": "verify",
    "review": "review",
    "audit": "review",
    "explain": "explain",
    "describe": "explain",
    "summarise": "explain",
    "summarize": "explain",
    "plan": "plan",
    "design": "plan",
}


# Status-query cues: a user asking "how is it going?" / "status?"
# should not spawn any sector managers. The parser tags these with
# ``is_status_query=True`` so the Main Agent can short-circuit to a
# registry read.
STATUS_QUERY_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^\s*status\s*\?*$", re.IGNORECASE),
    re.compile(r"\bhow'?s?\s+it\s+going\b", re.IGNORECASE),
    re.compile(r"\bhow\s+is\s+the\s+swarm\b", re.IGNORECASE),
    re.compile(r"\bhow\s+are\s+things\b", re.IGNORECASE),
    re.compile(r"\bwhat'?s\s+happening\b", re.IGNORECASE),
    re.compile(r"\bwhat\s+is\s+happening\b", re.IGNORECASE),
    re.compile(r"\bswarm\s+status\b", re.IGNORECASE),
    re.compile(r"\bagents?\s+running\b", re.IGNORECASE),
    re.compile(r"\bhow\s+many\s+agents?\b", re.IGNORECASE),
    re.compile(r"\blist\s+agents?\b", re.IGNORECASE),
    re.compile(r"\bshow\s+agents?\b", re.IGNORECASE),
    re.compile(r"\bwho'?s?\s+online\b", re.IGNORECASE),
    re.compile(r"\bwhat\s+are\s+you\s+doing\b", re.IGNORECASE),
    re.compile(r"\bprogress(?:\s+report)?\b", re.IGNORECASE),
    re.compile(r"\bupdate\s+me\b", re.IGNORECASE),
    re.compile(r"\bstate\s+of\s+the\s+swarm\b", re.IGNORECASE),
    re.compile(r"\bhow\s+(?:is|are)\s+(?:it|things|everything)\b", re.IGNORECASE),
)


# Cancellation / interruption cues. The Main Agent may choose to
# forward these to the Conductor as workflow-control events.
CANCELLATION_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bstop\s+everything\b", re.IGNORECASE),
    re.compile(r"\bhalt\b", re.IGNORECASE),
    re.compile(r"\babort\b", re.IGNORECASE),
    re.compile(r"\bcancel\s+(?:the\s+)?(?:workflow|job|task)\b", re.IGNORECASE),
    re.compile(r"\bnever\s*mind\b", re.IGNORECASE),
    re.compile(r"\bforget\s+it\b", re.IGNORECASE),
)


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class StructuredObjective:
    """The shape every objective must conform to.

    Stable, JSON-serialisable, and consumable by the Conductor
    without further parsing. The Conductor inspects
    ``suggested_sectors`` to decide which sector managers to spawn.
    """

    objective_id: str
    """UUID for tracing this objective through the swarm."""

    user_text: str
    """The original user message, preserved verbatim."""

    goal: str
    """A clean one-sentence restatement of what the user wants."""

    verb: str
    """High-level intent verb (create / repair / research / etc.)."""

    primary_sector: str
    """The dominant sector the workflow should start from."""

    suggested_sectors: list[str] = field(default_factory=list)
    """All sectors the Conductor should consider spawning."""

    needs_approval: bool = True
    """True if the Conductor should request user approval before executing."""

    is_status_query: bool = False
    """True if the user is asking about swarm state, not giving a goal."""

    is_cancellation: bool = False
    """True if the user is asking to stop the current workflow."""

    confidence: float = 0.5
    """0-1. Low values should surface a clarifying question to the user."""

    notes: list[str] = field(default_factory=list)
    """Free-form caveats / assumptions the parser flagged."""

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a JSON-friendly dict."""
        return asdict(self)

    def to_json(self) -> str:
        """Serialise to a JSON string."""
        return json.dumps(self.to_dict())


@dataclass(slots=True)
class ObjectiveParseResult:
    """A :class:`StructuredObjective` plus provenance."""

    objective: StructuredObjective
    """The structured objective itself."""

    source: str
    """Which parser produced this (``"llm"`` or ``"heuristic"``)."""

    raw_response: str | None = None
    """The raw LLM response text, for debugging / replay."""

    def to_dict(self) -> dict[str, Any]:
        return {
            "objective": self.objective.to_dict(),
            "source": self.source,
            "raw_response": self.raw_response,
        }


# ---------------------------------------------------------------------------
# LLM-driven parser
# ---------------------------------------------------------------------------

# The system prompt instructs the model to output a single JSON object
# matching the StructuredObjective schema. The schema is repeated
# verbatim so the model has a concrete template to follow.
_OBJECTIVE_SYSTEM_PROMPT = """You are the OpenSwarm objective parser.
Your job: read the user's natural-language message and emit a single
JSON object describing the structured objective.

The JSON MUST match this schema (additional keys are FORBIDDEN):

{
  "goal": "string — one-sentence restatement of what the user wants",
  "verb": "string — one of: create, repair, restructure, research, analyse, deploy, verify, review, explain, plan",
  "primary_sector": "string — one of: research, coding, testing, review, deployment, analysis, documentation, planning",
  "suggested_sectors": ["array of the same enum, may be empty"],
  "needs_approval": "boolean — true for non-trivial work, false for trivial one-liners",
  "is_status_query": "boolean — true if user is asking about swarm state, not giving a goal",
  "is_cancellation": "boolean — true if user wants to stop the current workflow",
  "confidence": "number 0.0-1.0 — your own certainty in the parse",
  "notes": ["array of strings — caveats, assumptions, things to clarify"]
}

Rules:
- Preserve the user's intent. Do not over-interpret.
- If the message is a status query (e.g. "how is the swarm doing?"),
  set is_status_query=true and suggested_sectors=[].
- If the message is a cancellation ("stop", "halt", "abort"),
  set is_cancellation=true.
- needs_approval should be true unless the user is asking for something
  trivial (a single read, a single fact lookup, a status check).
- suggested_sectors should include every sector the workflow will likely
  need, including dependencies (e.g. coding + testing for "build and
  test X"). Order does not matter.
- Output JSON only. No prose, no markdown fences, no commentary."""


async def parse_objective(
    user_text: str,
    llm: LLMClient,
    *,
    model: str | None = None,
    temperature: float = 0.0,
) -> ObjectiveParseResult:
    """Run the LLM-driven parser.

    Falls back to :func:`parse_objective_heuristic` on any LLM error
    so the user always gets a structured answer.
    """
    text = (user_text or "").strip()
    if not text:
        # Empty input → trivial status query, don't even bother the LLM.
        return ObjectiveParseResult(
            objective=_empty_objective(text),
            source="heuristic",
        )
    try:
        result = await llm.complete_json(
            system=_OBJECTIVE_SYSTEM_PROMPT,
            user=text,
            model=model,
            temperature=temperature,
        )
    except LLMError as exc:
        logger.warning("LLM objective parse failed, falling back: %s", exc)
        return ObjectiveParseResult(
            objective=parse_objective_heuristic(text),
            source="heuristic",
            raw_response=None,
        )
    objective = _objective_from_llm(result, text)
    return ObjectiveParseResult(
        objective=objective,
        source="llm",
        raw_response=json.dumps(result),
    )


def _objective_from_llm(
    parsed: dict[str, Any], user_text: str
) -> StructuredObjective:
    """Validate and normalise the LLM's JSON into a :class:`StructuredObjective`."""
    goal = _safe_str(parsed.get("goal"), fallback=user_text.strip())
    verb = _safe_str(parsed.get("verb"), fallback="create").lower()
    if verb not in INTENT_VERBS.values():
        verb = "create"
    primary_sector = _normalise_sector(parsed.get("primary_sector"))
    if primary_sector is None:
        primary_sector = _infer_sector_from_text(user_text) or "coding"
    raw_sectors = parsed.get("suggested_sectors") or []
    if not isinstance(raw_sectors, list):
        raw_sectors = []
    suggested: list[str] = []
    for s in raw_sectors:
        norm = _normalise_sector(s)
        if norm and norm not in suggested:
            suggested.append(norm)
    if primary_sector and primary_sector not in suggested:
        suggested.insert(0, primary_sector)
    needs_approval = bool(parsed.get("needs_approval", True))
    is_status = bool(parsed.get("is_status_query", False))
    is_cancel = bool(parsed.get("is_cancellation", False))
    try:
        confidence = float(parsed.get("confidence", 0.7))
    except (TypeError, ValueError):
        confidence = 0.7
    confidence = max(0.0, min(1.0, confidence))
    raw_notes = parsed.get("notes") or []
    if not isinstance(raw_notes, list):
        raw_notes = []
    notes = [str(n) for n in raw_notes if n is not None]
    return StructuredObjective(
        objective_id=str(uuid.uuid4()),
        user_text=user_text,
        goal=goal,
        verb=verb,
        primary_sector=primary_sector or "coding",
        suggested_sectors=suggested,
        needs_approval=needs_approval,
        is_status_query=is_status,
        is_cancellation=is_cancel,
        confidence=confidence,
        notes=notes,
    )


# ---------------------------------------------------------------------------
# Heuristic parser (no LLM)
# ---------------------------------------------------------------------------

def parse_objective_heuristic(user_text: str) -> StructuredObjective:
    """Produce a :class:`StructuredObjective` from regex heuristics.

    Used as a fallback when the LLM is unavailable and as a quick
    sanity check in tests. Quality is intentionally lower than the
    LLM parser — the heuristics look for known verbs and sector
    keywords and pick a single primary sector.
    """
    text = (user_text or "").strip()
    is_status = any(p.search(text) for p in STATUS_QUERY_PATTERNS)
    is_cancel = any(p.search(text) for p in CANCELLATION_PATTERNS)
    if is_status or is_cancel or not text:
        # Status queries, cancellations, and empty input never spawn sectors.
        return StructuredObjective(
            objective_id=str(uuid.uuid4()),
            user_text=text,
            goal=(
                "Swarm status check" if is_status
                else "Cancel current workflow" if is_cancel
                else "(empty input)"
            ),
            verb="explain",
            primary_sector="planning",
            suggested_sectors=[],
            needs_approval=False,
            is_status_query=is_status or not text,
            is_cancellation=is_cancel,
            confidence=0.0 if not text else 0.4,
            notes=["heuristic parse — confidence is lower than LLM parse"],
        )
    verb = _infer_verb_from_text(text) or "create"
    primary = _infer_sector_from_text(text) or "coding"
    suggested: list[str] = [primary]
    # Cross-sector coupling: coding work almost always needs testing;
    # research work often needs analysis; deployments need review.
    for neighbour in _sector_couplings(primary):
        if neighbour not in suggested:
            suggested.append(neighbour)
    needs_approval = len(text.split()) > 6
    return StructuredObjective(
        objective_id=str(uuid.uuid4()),
        user_text=text,
        goal=_make_goal_statement(text, verb, primary),
        verb=verb,
        primary_sector=primary,
        suggested_sectors=suggested,
        needs_approval=needs_approval,
        is_status_query=False,
        is_cancellation=False,
        confidence=0.6,
        notes=["heuristic parse — confidence is lower than LLM parse"],
    )


def _empty_objective(user_text: str) -> StructuredObjective:
    """Return a no-op objective for empty input."""
    return StructuredObjective(
        objective_id=str(uuid.uuid4()),
        user_text=user_text,
        goal="(empty input)",
        verb="explain",
        primary_sector="planning",
        suggested_sectors=[],
        needs_approval=False,
        is_status_query=True,
        is_cancellation=False,
        confidence=0.0,
        notes=["empty user message"],
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _safe_str(value: Any, *, fallback: str) -> str:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return fallback


def _normalise_sector(value: Any) -> str | None:
    """Coerce a string to a canonical sector key, or return ``None``."""
    if not isinstance(value, str):
        return None
    key = value.strip().lower()
    if key in KNOWN_SECTORS:
        return key
    if key in SECTOR_ALIASES:
        return SECTOR_ALIASES[key]
    return None


def _infer_sector_from_text(text: str) -> str | None:
    """Return the most-likely sector key for ``text`` or ``None``."""
    lowered = text.lower()
    best_key: str | None = None
    best_len = 0
    for alias, sector in SECTOR_ALIASES.items():
        # Word-boundary match for single words, substring match for phrases.
        if " " in alias or "-" in alias:
            if alias in lowered:
                if len(alias) > best_len:
                    best_key = sector
                    best_len = len(alias)
        else:
            if re.search(rf"\b{re.escape(alias)}\b", lowered):
                if len(alias) > best_len:
                    best_key = sector
                    best_len = len(alias)
    return best_key


def _infer_verb_from_text(text: str) -> str | None:
    """Return the first matching intent verb in ``text`` or ``None``."""
    lowered = text.lower()
    for alias, verb in INTENT_VERBS.items():
        if " " in alias or "-" in alias:
            if alias in lowered:
                return verb
        else:
            if re.search(rf"\b{re.escape(alias)}\b", lowered):
                return verb
    return None


def _sector_couplings(sector: str) -> list[str]:
    """Return the sectors usually required alongside ``sector``."""
    return {
        "coding": ["testing", "review"],
        "research": ["analysis"],
        "deployment": ["review", "testing"],
        "planning": ["review"],
        "analysis": ["research"],
        "documentation": ["review"],
        "testing": ["review"],
        "review": [],
    }.get(sector, [])


def _make_goal_statement(text: str, verb: str, primary_sector: str) -> str:
    """Produce a clean one-sentence goal for the heuristic parser."""
    text = text.strip().rstrip(".?!")
    if not text:
        return f"{verb.capitalize()} something in {primary_sector}"
    return f"{verb.capitalize()} {text[:200]}"


# ---------------------------------------------------------------------------
# Convenience: produce the Conductor spawn intent envelope payload
# ---------------------------------------------------------------------------

def objective_to_spawn_payload(objective: StructuredObjective) -> dict[str, Any]:
    """Build the ``payload.data`` for the ``spawn_initial_swarm`` intent.

    The Conductor reads this shape to decide which sector managers to
    spawn and what each one should be working on.
    """
    return {
        "objective_id": objective.objective_id,
        "goal": objective.goal,
        "verb": objective.verb,
        "primary_sector": objective.primary_sector,
        "sectors": list(objective.suggested_sectors),
        "needs_approval": objective.needs_approval,
        "user_text": objective.user_text,
        "confidence": objective.confidence,
        "notes": list(objective.notes),
    }


__all__ = [
    "CANCELLATION_PATTERNS",
    "INTENT_VERBS",
    "KNOWN_SECTORS",
    "ObjectiveParseResult",
    "SECTOR_ALIASES",
    "STATUS_QUERY_PATTERNS",
    "StructuredObjective",
    "objective_to_spawn_payload",
    "parse_objective",
    "parse_objective_heuristic",
]
