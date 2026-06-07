"""Tests for the :class:`~kernel.budget.BudgetTracker`.

Covers:

* recording cost increases the running total
* crossing the threshold emits ``budget_exhausted`` exactly once
* per-step cost is isolated (per workflow, per step)
* one-time budget override is enforced
* :func:`can_mutate` enforces both the mutate cap and the budget
"""
from __future__ import annotations

import pytest

from kernel.budget import (
    COST_BUDGET_PER_STEP_DEFAULT,
    MUTATE_MAX_PER_STEP,
    BudgetTracker,
)


class _StubBus:
    """Minimal stand-in for :class:`MessageBus` that records emissions."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    async def emit_event(self, event_type: str, details: dict | None = None) -> None:
        self.events.append((event_type, dict(details or {})))


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_record_cost_increments_running_total() -> None:
    bus = _StubBus()
    tracker = BudgetTracker(bus)
    tracker.set_step_budget("wf-1", "s1", 1.0)
    snap = await tracker.record_cost("wf-1", "s1", 0.25)
    assert snap.cost_usd == 0.25
    assert snap.budget_usd == 1.0
    assert not snap.exhausted
    assert tracker.get_step_cost("wf-1", "s1") == 0.25
    # No event yet.
    assert bus.events == []


@pytest.mark.asyncio
async def test_crossing_budget_emits_event_exactly_once() -> None:
    bus = _StubBus()
    tracker = BudgetTracker(bus)
    tracker.set_step_budget("wf-1", "s1", 0.50)
    await tracker.record_cost("wf-1", "s1", 0.20)
    await tracker.record_cost("wf-1", "s1", 0.20)
    # Still under.
    assert bus.events == []
    snap = await tracker.record_cost("wf-1", "s1", 0.20)
    assert snap.exhausted
    assert len(bus.events) == 1
    name, payload = bus.events[0]
    assert name == "budget_exhausted"
    assert payload["workflow_id"] == "wf-1"
    assert payload["step_id"] == "s1"
    assert payload["cost_so_far"] == pytest.approx(0.60, abs=1e-6)
    assert payload["budget"] == pytest.approx(0.50, abs=1e-6)
    assert payload["currency"] == "USD"
    # A second call past the threshold does NOT re-emit.
    await tracker.record_cost("wf-1", "s1", 0.10)
    assert len(bus.events) == 1


@pytest.mark.asyncio
async def test_cost_is_per_step_per_workflow() -> None:
    bus = _StubBus()
    tracker = BudgetTracker(bus)
    tracker.set_step_budget("wf-1", "s1", 0.50)
    tracker.set_step_budget("wf-1", "s2", 0.50)
    tracker.set_step_budget("wf-2", "s1", 0.50)
    await tracker.record_cost("wf-1", "s1", 0.30)
    await tracker.record_cost("wf-1", "s2", 0.10)
    await tracker.record_cost("wf-2", "s1", 0.40)
    assert tracker.get_step_cost("wf-1", "s1") == 0.30
    assert tracker.get_step_cost("wf-1", "s2") == 0.10
    assert tracker.get_step_cost("wf-2", "s1") == 0.40
    # None of the three steps has crossed its 0.50 budget.
    assert bus.events == []
    # Push wf-1/s2 to exhaustion.
    await tracker.record_cost("wf-1", "s2", 0.45)
    assert len(bus.events) == 1
    assert bus.events[0][1]["step_id"] == "s2"


@pytest.mark.asyncio
async def test_default_budget_when_unset() -> None:
    bus = _StubBus()
    tracker = BudgetTracker(bus)
    assert tracker.get_step_budget("wf", "s") == COST_BUDGET_PER_STEP_DEFAULT
    snap = tracker.snapshot("wf", "s")
    assert snap.budget_usd == COST_BUDGET_PER_STEP_DEFAULT
    assert snap.cost_usd == 0.0
    assert not snap.exhausted


@pytest.mark.asyncio
async def test_is_budget_exhausted() -> None:
    tracker = BudgetTracker()
    tracker.set_step_budget("wf", "s", 0.10)
    assert not tracker.is_budget_exhausted("wf", "s")
    await tracker.record_cost("wf", "s", 0.10)
    assert tracker.is_budget_exhausted("wf", "s")
    # Custom budget passed in.
    assert tracker.is_budget_exhausted("wf", "s", budget=0.05)


@pytest.mark.asyncio
async def test_one_time_budget_override() -> None:
    bus = _StubBus()
    tracker = BudgetTracker(bus)
    tracker.set_step_budget("wf", "s", 0.50)
    await tracker.record_cost("wf", "s", 0.50)
    assert tracker.is_budget_exhausted("wf", "s")
    # First override granted.
    assert tracker.grant_override("wf", "s", 1.00)
    # Second override rejected.
    assert not tracker.grant_override("wf", "s", 2.00)
    assert tracker.has_override("wf", "s")
    # The override raised the budget; further cost does not re-fire
    # the event for the original threshold.
    await tracker.record_cost("wf", "s", 0.10)
    # Original emission was 1; nothing new yet.
    assert len(bus.events) == 1
    # Push past the new override budget.
    await tracker.record_cost("wf", "s", 0.50)
    assert len(bus.events) == 2
    name, payload = bus.events[-1]
    assert name == "budget_exhausted"
    assert payload["budget"] == pytest.approx(1.00, abs=1e-6)


@pytest.mark.asyncio
async def test_can_mutate_respects_cap_and_budget() -> None:
    tracker = BudgetTracker()
    tracker.set_step_budget("wf", "s", 1.0)
    # Under the cap and budget.
    allowed, reason = tracker.can_mutate("wf", "s", mutate_count=0)
    assert allowed and reason == "ok"
    allowed, reason = tracker.can_mutate("wf", "s", mutate_count=MUTATE_MAX_PER_STEP)
    assert not allowed and reason == "mutate_cap_exceeded"
    # Burn the budget.
    await tracker.record_cost("wf", "s", 1.0)
    allowed, reason = tracker.can_mutate("wf", "s", mutate_count=0)
    assert not allowed and reason == "budget_exhausted"


@pytest.mark.asyncio
async def test_reset_step_clears_bookkeeping() -> None:
    bus = _StubBus()
    tracker = BudgetTracker(bus)
    tracker.set_step_budget("wf", "s", 0.50)
    await tracker.record_cost("wf", "s", 0.50)
    assert tracker.is_budget_exhausted("wf", "s")
    tracker.reset_step("wf", "s")
    assert tracker.get_step_cost("wf", "s") == 0.0
    # The set budget is also cleared.
    assert tracker.get_step_budget("wf", "s") == COST_BUDGET_PER_STEP_DEFAULT
    # A new record after reset does not produce a duplicate event for
    # the previous threshold (we count bus emissions, not tracker state).
    await tracker.record_cost("wf", "s", 0.10)
    assert bus.events == [(  # type: ignore[comparison-overlap]
        "budget_exhausted",
        {
            "workflow_id": "wf",
            "step_id": "s",
            "cost_so_far": pytest.approx(0.50, abs=1e-6),
            "budget": pytest.approx(0.50, abs=1e-6),
            "currency": "USD",
            "remaining_usd": pytest.approx(0.0, abs=1e-6),
        },
    )]


@pytest.mark.asyncio
async def test_reset_workflow_clears_all_steps() -> None:
    tracker = BudgetTracker()
    tracker.set_step_budget("wf", "s1", 0.10)
    tracker.set_step_budget("wf", "s2", 0.10)
    await tracker.record_cost("wf", "s1", 0.05)
    await tracker.record_cost("wf", "s2", 0.05)
    cleared = tracker.reset_workflow("wf")
    assert cleared == 2
    assert tracker.get_step_cost("wf", "s1") == 0.0
    assert tracker.get_step_cost("wf", "s2") == 0.0


@pytest.mark.asyncio
async def test_negative_and_nan_costs_are_clamped() -> None:
    bus = _StubBus()
    tracker = BudgetTracker(bus)
    tracker.set_step_budget("wf", "s", 0.10)
    await tracker.record_cost("wf", "s", -1.0)
    assert tracker.get_step_cost("wf", "s") == 0.0
    nan = float("nan")
    await tracker.record_cost("wf", "s", nan)
    assert tracker.get_step_cost("wf", "s") == 0.0


@pytest.mark.asyncio
async def test_attach_lets_existing_tracker_emit() -> None:
    bus = _StubBus()
    tracker = BudgetTracker()
    tracker.attach(bus)
    tracker.set_step_budget("wf", "s", 0.10)
    await tracker.record_cost("wf", "s", 0.10)
    assert len(bus.events) == 1
