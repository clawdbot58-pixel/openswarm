"""Per-step USD budget tracking with hard-guardrail enforcement.

The model router reports ``cost_usd`` on every inference call. The
kernel records the cost against the running step in this in-memory
tracker and emits a ``budget_exhausted`` event the moment the running
total crosses the per-step budget. The Main Agent is the only entity
that may then issue a one-time ``budget_override`` to allow the step
to continue.

Why an in-memory store?
-----------------------

The tracker is **transient by design**. A workflow is short-lived; on
restart the cost starts at zero. Persistent cost history belongs in
the agent's memory (or a long-term analytics store), not in the
hot-path guardrail. The default per-step budget is the workflow's
``error_handling.budget_per_step_usd``, falling back to the manifest's
``model_tier.cost_budget_per_task``, falling back to ``0.50`` USD.

Hard guardrail
--------------

The tracker never lets a step continue past its budget *silently*. The
moment a step crosses its limit, the tracker emits the
``budget_exhausted`` event. The recovery executor pauses the step
until the Main Agent responds with one of: ``budget_override``,
``escalate_to_user``, or ``cancel_workflow``.

Mutate chain limits
-------------------

The tracker exposes :func:`can_mutate` to enforce the global mutate
cap (``MUTATE_MAX_PER_STEP = 3``) and the per-step USD ceiling. The
kernel calls this before applying any manifest delta; the Main Agent
is the *strategist* but the kernel is the *gatekeeper*.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, Awaitable

if TYPE_CHECKING:  # pragma: no cover
    from .bus import MessageBus

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Tunables — see vision/self-healing.md §"Cost Ceiling"
# ---------------------------------------------------------------------------

MUTATE_MAX_PER_STEP: int = 3
"""Hard cap on mutate attempts per step. After this, escalate."""

COST_BUDGET_PER_STEP_DEFAULT: float = 0.50
"""Default USD ceiling per step when no override is provided."""


# ---------------------------------------------------------------------------
# Snapshot types
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class StepCostSnapshot:
    """A point-in-time view of a step's spend."""

    workflow_id: str
    step_id: str
    cost_usd: float
    budget_usd: float
    remaining_usd: float
    exhausted: bool


# ---------------------------------------------------------------------------
# Tracker
# ---------------------------------------------------------------------------

class BudgetTracker:
    """In-memory per-step USD cost accumulator with exhaustion emission.

    The tracker holds:

    * ``self.step_costs``        — ``"workflow:step"`` → cumulative USD
    * ``self.step_budgets``      — ``"workflow:step"`` → per-step budget
    * ``self.override_used``     — set of keys that have already been
                                   granted a one-time override. The
                                   Main Agent can override **at most
                                   once per step**.

    On every :meth:`record_cost` call the tracker updates the running
    total, and — if the new total is at or above the budget — fires a
    ``budget_exhausted`` event exactly once per step. Subsequent calls
    that keep the step in the "exhausted" state do **not** refire the
    event (idempotent).
    """

    def __init__(self, bus: "MessageBus | None" = None) -> None:
        self._bus: "MessageBus | None" = bus
        self._step_costs: dict[str, float] = {}
        self._step_budgets: dict[str, float] = {}
        self._override_used: set[str] = set()
        self._exhausted_emitted: set[str] = set()
        # Cost-rate bookkeeping. Per-step in-memory only.
        self._lock: asyncio.Lock = asyncio.Lock()
        # Optional hook for tests: called every time a step is recorded.
        self.on_record: Callable[[str, str, float, float], Awaitable[None]] | None = None
        self.on_exhausted: Callable[[str, str, float, float], Awaitable[None]] | None = None

    # -- wiring ------------------------------------------------------------

    def attach(self, bus: "MessageBus") -> None:
        """Attach the bus used to emit ``budget_exhausted`` events.

        Idempotent. Call after construction once the bus exists; tests
        may construct the tracker without a bus and inject one later.
        """
        self._bus = bus

    # -- configuration -----------------------------------------------------

    def set_step_budget(
        self,
        workflow_id: str,
        step_id: str,
        budget_usd: float,
    ) -> None:
        """Set (or overwrite) the per-step USD budget for ``(workflow, step)``.

        Called by the workflow executor when it begins a step, using
        the workflow's ``error_handling.budget_per_step_usd`` (or the
        fallback chain). Setting a new budget after cost has been
        recorded does **not** reset the running total.
        """
        key = self._key(workflow_id, step_id)
        self._step_budgets[key] = float(budget_usd)

    def get_step_budget(
        self, workflow_id: str, step_id: str
    ) -> float:
        """Return the per-step budget, falling back to the default."""
        key = self._key(workflow_id, step_id)
        return self._step_budgets.get(key, COST_BUDGET_PER_STEP_DEFAULT)

    def get_step_cost(
        self, workflow_id: str, step_id: str
    ) -> float:
        """Return the cumulative USD spent on the step so far."""
        key = self._key(workflow_id, step_id)
        return self._step_costs.get(key, 0.0)

    def is_budget_exhausted(
        self,
        workflow_id: str,
        step_id: str,
        budget: float | None = None,
    ) -> bool:
        """True if the step has already met or exceeded its USD ceiling."""
        cost = self.get_step_cost(workflow_id, step_id)
        cap = budget if budget is not None else self.get_step_budget(workflow_id, step_id)
        return cost >= cap

    def snapshot(
        self, workflow_id: str, step_id: str
    ) -> StepCostSnapshot:
        """Return a :class:`StepCostSnapshot` for the step."""
        cost = self.get_step_cost(workflow_id, step_id)
        budget = self.get_step_budget(workflow_id, step_id)
        return StepCostSnapshot(
            workflow_id=workflow_id,
            step_id=step_id,
            cost_usd=cost,
            budget_usd=budget,
            remaining_usd=max(0.0, budget - cost),
            exhausted=cost >= budget,
        )

    # -- recording ---------------------------------------------------------

    async def record_cost(
        self,
        workflow_id: str,
        step_id: str,
        cost_usd: float,
    ) -> StepCostSnapshot:
        """Add ``cost_usd`` to the step's running total.

        Emits a ``budget_exhausted`` event exactly once per step the
        moment the total crosses the budget. Returns the post-update
        snapshot for callers that want to inspect remaining headroom.

        Negative or NaN values are clamped to ``0.0`` — cost cannot be
        "un-spent".
        """
        key = self._key(workflow_id, step_id)
        delta = float(cost_usd)
        if delta < 0.0 or delta != delta:  # NaN check
            delta = 0.0
        async with self._lock:
            self._step_costs[key] = self._step_costs.get(key, 0.0) + delta
            snap = self.snapshot(workflow_id, step_id)
            exhausted = snap.exhausted
            already_emitted = key in self._exhausted_emitted
        # Fire hooks / events outside the lock.
        if self.on_record is not None:
            try:
                await self.on_record(workflow_id, step_id, snap.cost_usd, snap.budget_usd)
            except Exception:  # noqa: BLE001
                logger.exception("budget on_record hook failed")
        if exhausted and not already_emitted:
            async with self._lock:
                self._exhausted_emitted.add(key)
            await self._emit_exhausted(workflow_id, step_id, snap)
            if self.on_exhausted is not None:
                try:
                    await self.on_exhausted(workflow_id, step_id, snap.cost_usd, snap.budget_usd)
                except Exception:  # noqa: BLE001
                    logger.exception("budget on_exhausted hook failed")
        return snap

    # -- overrides ---------------------------------------------------------

    def grant_override(
        self,
        workflow_id: str,
        step_id: str,
        new_budget_usd: float,
    ) -> bool:
        """Grant a one-time budget override for the step.

        Returns ``True`` if the override was applied, ``False`` if the
        step has already used its one allowed override. The Main Agent
        is the only entity that may call this; the kernel merely
        enforces the rule.

        The override raises the per-step budget to ``new_budget_usd``
        and clears the ``budget_exhausted`` flag so subsequent cost
        recordings do not refire the event. An audit log entry is
        written to the kernel registry when one is attached.
        """
        key = self._key(workflow_id, step_id)
        if key in self._override_used:
            return False
        self._override_used.add(key)
        self._step_budgets[key] = float(new_budget_usd)
        # Allow the step to record more cost before re-emitting.
        self._exhausted_emitted.discard(key)
        logger.info(
            "budget override granted workflow_id=%s step_id=%s new=%.4f",
            workflow_id, step_id, new_budget_usd,
        )
        return True

    def has_override(self, workflow_id: str, step_id: str) -> bool:
        """True if a one-time override was already granted for the step."""
        key = self._key(workflow_id, step_id)
        return key in self._override_used

    # -- reset / cleanup ---------------------------------------------------

    def reset_step(self, workflow_id: str, step_id: str) -> None:
        """Clear all bookkeeping for a single step."""
        key = self._key(workflow_id, step_id)
        self._step_costs.pop(key, None)
        self._step_budgets.pop(key, None)
        self._override_used.discard(key)
        self._exhausted_emitted.discard(key)

    def reset_workflow(self, workflow_id: str) -> int:
        """Reset bookkeeping for every step that belongs to ``workflow_id``.

        Returns the number of steps cleared. Used by the workflow
        executor on terminal completion.
        """
        prefix = f"{workflow_id}:"
        cleared = 0
        for key in list(self._step_costs.keys()):
            if key.startswith(prefix):
                self._step_costs.pop(key, None)
                self._step_budgets.pop(key, None)
                self._override_used.discard(key)
                self._exhausted_emitted.discard(key)
                cleared += 1
        return cleared

    # -- mutate-chain policy ----------------------------------------------

    def can_mutate(
        self,
        workflow_id: str,
        step_id: str,
        mutate_count: int,
        budget: float | None = None,
    ) -> tuple[bool, str]:
        """Decide whether another mutate attempt is allowed for the step.

        Returns ``(True, "ok")`` when the step is under both the mutate
        count cap and the USD budget. Returns ``(False, reason)``
        otherwise; ``reason`` is one of:

        * ``"mutate_cap_exceeded"``   — already at the cap
        * ``"budget_exhausted"``      — USD ceiling reached
        """
        if mutate_count >= MUTATE_MAX_PER_STEP:
            return False, "mutate_cap_exceeded"
        cost = self.get_step_cost(workflow_id, step_id)
        cap = budget if budget is not None else self.get_step_budget(workflow_id, step_id)
        if cost >= cap:
            return False, "budget_exhausted"
        return True, "ok"

    # -- helpers -----------------------------------------------------------

    @staticmethod
    def _key(workflow_id: str, step_id: str) -> str:
        return f"{workflow_id}:{step_id}"

    async def _emit_exhausted(
        self,
        workflow_id: str,
        step_id: str,
        snap: StepCostSnapshot,
    ) -> None:
        """Emit a ``budget_exhausted`` event on the bus (if attached)."""
        if self._bus is None:
            logger.warning(
                "budget exhausted (no bus attached) workflow=%s step=%s "
                "cost=%.4f budget=%.4f",
                workflow_id, step_id, snap.cost_usd, snap.budget_usd,
            )
            return
        try:
            await self._bus.emit_event(
                "budget_exhausted",
                {
                    "workflow_id": workflow_id,
                    "step_id": step_id,
                    "cost_so_far": round(snap.cost_usd, 6),
                    "budget": round(snap.budget_usd, 6),
                    "currency": "USD",
                    "remaining_usd": round(snap.remaining_usd, 6),
                },
            )
        except Exception:  # noqa: BLE001 — never let emission failure crash the tracker
            logger.exception("failed to emit budget_exhausted event")


# ---------------------------------------------------------------------------
# Module-level guard helpers
# ---------------------------------------------------------------------------

def can_mutate(
    tracker: BudgetTracker,
    workflow_id: str,
    step_id: str,
    mutate_count: int,
    budget: float | None = None,
) -> bool:
    """Convenience wrapper around :meth:`BudgetTracker.can_mutate`."""
    allowed, _reason = tracker.can_mutate(
        workflow_id, step_id, mutate_count, budget
    )
    return allowed


__all__ = [
    "COST_BUDGET_PER_STEP_DEFAULT",
    "MUTATE_MAX_PER_STEP",
    "BudgetTracker",
    "StepCostSnapshot",
    "can_mutate",
]
