"""Tests for the objective parser."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Make ``src`` importable.
_SRC = Path(__file__).resolve().parents[3] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from agents.llm_client import LLMClient  # noqa: E402
from agents.objective_parser import (  # noqa: E402
    KNOWN_SECTORS,
    SECTOR_ALIASES,
    StructuredObjective,
    objective_to_spawn_payload,
    parse_objective,
    parse_objective_heuristic,
)


# ---------------------------------------------------------------------------
# Heuristic parser
# ---------------------------------------------------------------------------

def test_heuristic_parses_status_query() -> None:
    """Status queries are tagged and need no sectors."""
    obj = parse_objective_heuristic("how is the swarm doing?")
    assert obj.is_status_query is True
    assert obj.is_cancellation is False
    assert obj.suggested_sectors == []
    assert obj.needs_approval is False


def test_heuristic_parses_cancellation() -> None:
    obj = parse_objective_heuristic("stop everything")
    assert obj.is_cancellation is True
    assert obj.is_status_query is False


def test_heuristic_infers_coding_sector() -> None:
    obj = parse_objective_heuristic("please implement a /login endpoint")
    assert obj.primary_sector == "coding"
    assert "coding" in obj.suggested_sectors
    assert "testing" in obj.suggested_sectors  # auto-coupling


def test_heuristic_infers_research_sector() -> None:
    obj = parse_objective_heuristic("investigate the failure rate of the billing API")
    assert obj.primary_sector == "research"
    assert "research" in obj.suggested_sectors
    assert "analysis" in obj.suggested_sectors


def test_heuristic_infers_testing_sector() -> None:
    obj = parse_objective_heuristic("write a test for the new endpoint")
    assert obj.primary_sector == "testing"


def test_heuristic_infers_deployment_sector() -> None:
    obj = parse_objective_heuristic("deploy the new release to staging")
    assert obj.primary_sector == "deployment"


def test_heuristic_infers_review_sector() -> None:
    obj = parse_objective_heuristic("audit the security of the auth module")
    assert obj.primary_sector == "review"


def test_heuristic_infers_documentation_sector() -> None:
    obj = parse_objective_heuristic("update the README with the new install steps")
    assert obj.primary_sector == "documentation"


def test_heuristic_infers_planning_sector() -> None:
    obj = parse_objective_heuristic("design the architecture for the new service")
    assert obj.primary_sector == "planning"


def test_heuristic_defaults_to_coding_when_no_keyword_matches() -> None:
    obj = parse_objective_heuristic("add unicorn mode to the dashboard")
    assert obj.primary_sector == "coding"
    # Sectors list is non-empty (primary + at least one coupling).
    assert obj.suggested_sectors


def test_heuristic_handles_empty_input() -> None:
    obj = parse_objective_heuristic("")
    assert obj.is_status_query is True
    assert obj.suggested_sectors == []
    assert obj.confidence == 0.0


def test_heuristic_infers_verb() -> None:
    assert parse_objective_heuristic("add a button").verb == "create"
    assert parse_objective_heuristic("fix the bug").verb == "repair"
    assert parse_objective_heuristic("refactor the parser").verb == "restructure"
    assert parse_objective_heuristic("investigate the failure").verb == "research"
    assert parse_objective_heuristic("deploy to staging").verb == "deploy"
    assert parse_objective_heuristic("test the endpoint").verb == "verify"
    assert parse_objective_heuristic("review the PR").verb == "review"


def test_heuristic_deduplicates_sectors_preserving_order() -> None:
    obj = parse_objective_heuristic(
        "test the new code with thorough tests"
    )
    # Sectors should be unique; order reflects insertion.
    assert len(obj.suggested_sectors) == len(set(obj.suggested_sectors))


def test_heuristic_assigns_objective_id() -> None:
    obj1 = parse_objective_heuristic("test something")
    obj2 = parse_objective_heuristic("test something else")
    assert obj1.objective_id != obj2.objective_id


def test_heuristic_long_goals_need_approval() -> None:
    short = parse_objective_heuristic("status?")
    long = parse_objective_heuristic(
        "implement a new login flow with OAuth2 and PKCE and add tests"
    )
    assert short.needs_approval is False
    assert long.needs_approval is True


def test_heuristic_preserves_user_text() -> None:
    """The user text is stored after a leading/trailing whitespace strip."""
    text = "  build a todo app  "
    obj = parse_objective_heuristic(text)
    assert obj.user_text == text.strip()


# ---------------------------------------------------------------------------
# Sector alias coverage
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("alias,expected", list(SECTOR_ALIASES.items()))
def test_every_alias_maps_to_a_known_sector(alias: str, expected: str) -> None:
    """Every alias must point to a sector in :data:`KNOWN_SECTORS`."""
    assert expected in KNOWN_SECTORS


# ---------------------------------------------------------------------------
# LLM-driven parser
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_llm_parser_uses_llm_response(mock_llm: LLMClient) -> None:
    """The LLM's JSON is honoured when it is well-formed."""
    payload = {
        "goal": "Investigate the API error rate spike",
        "verb": "research",
        "primary_sector": "research",
        "suggested_sectors": ["research", "analysis"],
        "needs_approval": True,
        "is_status_query": False,
        "is_cancellation": False,
        "confidence": 0.92,
        "notes": ["error rate spike since 14:00"],
    }
    # Reconfigure the mock to return this dict.
    mock_llm = LLMClient.from_config(
        {"routes": [{"provider": "mock", "model": "m"}]},
        providers={
            "mock": _StubProvider(payload),
        },
    )
    result = await parse_objective("investigate the API error spike", mock_llm)
    assert result.source == "llm"
    assert result.objective.goal == payload["goal"]
    assert result.objective.verb == "research"
    assert result.objective.primary_sector == "research"
    assert result.objective.confidence == pytest.approx(0.92)
    assert "analysis" in result.objective.suggested_sectors


@pytest.mark.asyncio
async def test_llm_parser_falls_back_to_heuristic_on_error() -> None:
    """When the LLM raises, the heuristic parser is used."""
    from agents.llm_client import LLMError

    class _BoomProvider:
        name = "mock"

        async def complete(self, request):  # noqa: D401
            raise LLMError("simulated outage")

    client = LLMClient(
        router=__import__("agents.llm_client", fromlist=["ModelRouter"]).ModelRouter(
            [__import__("agents.llm_client", fromlist=["ModelRoute"]).ModelRoute("mock", "m")],
            providers={"mock": _BoomProvider()},
        ),
        max_retries=0,
    )
    result = await parse_objective("deploy the fix", client)
    assert result.source == "heuristic"
    assert result.objective.primary_sector == "deployment"


@pytest.mark.asyncio
async def test_llm_parser_handles_empty_input_without_calling_llm() -> None:
    """An empty message never hits the LLM."""
    calls: list[object] = []

    class _RecorderProvider:
        name = "mock"

        async def complete(self, request):  # noqa: D401
            calls.append(request)
            return _FakeResult({})

    from agents.llm_client import (
        CompletionResult,
        ModelRoute,
        ModelRouter,
    )

    client = LLMClient(
        ModelRouter(
            [ModelRoute("mock", "m")],
            providers={"mock": _RecorderProvider()},
        )
    )
    result = await parse_objective("", client)
    assert result.source == "heuristic"
    assert calls == []


# ---------------------------------------------------------------------------
# Spawn payload
# ---------------------------------------------------------------------------

def test_objective_to_spawn_payload_round_trip() -> None:
    obj = StructuredObjective(
        objective_id="abc-123",
        user_text="do the thing",
        goal="do the thing",
        verb="create",
        primary_sector="coding",
        suggested_sectors=["coding", "testing"],
        needs_approval=True,
        is_status_query=False,
        is_cancellation=False,
        confidence=0.8,
        notes=[],
    )
    payload = objective_to_spawn_payload(obj)
    assert payload["objective_id"] == "abc-123"
    assert payload["primary_sector"] == "coding"
    assert payload["sectors"] == ["coding", "testing"]
    assert payload["needs_approval"] is True


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------

class _StubProvider:
    """Provider that returns a fixed dict (as JSON) every call."""

    name = "mock"

    def __init__(self, payload: dict) -> None:
        import json as _json
        self._payload = _json.dumps(payload)
        self.calls = []

    async def complete(self, request):  # noqa: D401
        from agents.llm_client import CompletionResult

        self.calls.append(request)
        return CompletionResult(
            text=self._payload,
            model=request.model,
            provider=self.name,
        )


class _FakeResult:
    """Minimal CompletionResult-like duck type for tests."""

    def __init__(self, raw: dict) -> None:
        self.raw = raw
        self.text = "{}"
        self.model = "stub"
        self.provider = "stub"
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.latency_ms = 0.0
