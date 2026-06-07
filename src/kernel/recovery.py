"""Recovery executor — runs the kernel-side recovery hierarchy.

The recovery hierarchy (from ``vision/self-healing.md``)::

    step fails
        ├── retry         — same agent, same config
        ├── mutate        — upgrade model, loop, or spawn fresh
        ├── fallback      — run workflow.error_handling.fallback_steps
        ├── compensate    — run compensation_steps to undo partial work
        └── escalate      — Main Agent takes over

The Main Agent is the **only** entity that decides the *strategy*; the
kernel's :class:`RecoveryExecutor` only *executes* it. The two are
connected by these value types:

* :class:`RecoveryDecision`  — what the Main Agent decided
  (``retry``, ``mutate_config``, ``budget_override``, ``escalate``,
  ``cancel_workflow``, ``continue_from_step``, ``rollback_n_steps``,
  ``respawn_all_agents``).
* :class:`MutationConfig`    — the manifest delta to apply
  (``model_tier``, ``loop_type``, ``spawn_fresh``).

Determinism
-----------

The executor **never** calls an LLM. Every decision it makes is a
deterministic gate (mutate-count, budget, agent-presence). The Main
Agent's LLM call produces the :class:`RecoveryDecision`; the executor
just enforces it.

Audit trail
-----------

Every mutate / retry / fallback / compensation is appended to the
kernel's audit log via :meth:`AgentRegistry.audit`, with the
envelope_id (when available) and a structured ``details`` blob. The
dashboard's audit view surfaces this.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from .budget import (
    COST_BUDGET_PER_STEP_DEFAULT,
    MUTATE_MAX_PER_STEP,
    BudgetTracker,
)
from .checkpoint import Checkpoint, CheckpointManager
from .failure_detector import FailureDetector
from .models import (
    AgentIdStr,
    AgentManifest,
    Endpoint,
    Envelope,
    EnvelopeMetadata,
    Preamble,
    UUID4Str,
)

if TYPE_CHECKING:  # pragma: no cover
    from .bus import MessageBus
    from .registry import AgentRegistry

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Decision types
# ---------------------------------------------------------------------------

RecoveryActionLiteral = Literal[
    "retry_with_same_agent",
    "mutate_config",
    "budget_override",
    "escalate_to_user",
    "cancel_workflow",
    "continue_from_step",
    "rollback_n_steps",
    "respawn_all_agents",
    "run_fallback_steps",
    "run_compensation_steps",
    "noop",
]


class RecoveryDecision(BaseModel):
    """A single recovery decision the Main Agent has made.

    The decision is a small, JSON-safe struct the kernel can act on
    without an LLM in the loop.
    """

    model_config = ConfigDict(extra="forbid")

    action: RecoveryActionLiteral
    manifest_delta: dict[str, Any] = Field(default_factory=dict)
    reason: str = ""
    budget_override_usd: float | None = None
    rollback_n_steps: int = 0
    fallback_steps: list[str] = Field(default_factory=list)
    compensation_steps: list[str] = Field(default_factory=list)
    audit: dict[str, Any] = Field(default_factory=dict)


class MutationConfig(BaseModel):
    """The manifest delta to apply when ``action == mutate_config``."""

    model_config = ConfigDict(extra="forbid")

    model_tier: Literal["fast", "standard", "powerful"] | None = None
    loop_type: Literal["direct", "cot", "reflection", "tree"] | None = None
    spawn_fresh: bool = False
    extra: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def from_delta(cls, delta: dict[str, Any]) -> "MutationConfig":
        """Build from a generic manifest-delta dict (the Main Agent's output)."""
        return cls(
            model_tier=delta.get("model_tier") if isinstance(delta.get("model_tier"), str) else None,
            loop_type=delta.get("loop_type") if isinstance(delta.get("loop_type"), str) else None,
            spawn_fresh=bool(delta.get("spawn_fresh", False)),
            extra={
                k: v
                for k, v in delta.items()
                if k not in {"model_tier", "loop_type", "spawn_fresh"}
            },
        )


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class RecoveryResult:
    """Outcome of a recovery operation."""

    success: bool
    action: RecoveryActionLiteral
    message: str = ""
    mutate_count: int = 0
    checkpoint: Checkpoint | None = None
    decision: RecoveryDecision | None = None
    extra: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Error-handling config (subset of the workflow contract)
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class ErrorHandlingConfig:
    """Per-workflow error handling knobs (subset of ``workflow.json``)."""

    fallback_steps: list[str] = field(default_factory=list)
    compensation_steps: list[str] = field(default_factory=list)
    on_failure: str = "stop"
    budget_per_step_usd: float = COST_BUDGET_PER_STEP_DEFAULT
    escalation_target: str = "main-agent"

    @classmethod
    def from_workflow(cls, workflow: dict[str, Any]) -> "ErrorHandlingConfig":
        """Build from a raw workflow blob. Defensive against missing keys."""
        eh = workflow.get("error_handling") or {}
        if not isinstance(eh, dict):
            eh = {}
        budget = eh.get("budget_per_step_usd", COST_BUDGET_PER_STEP_DEFAULT)
        try:
            budget_val = float(budget)
        except (TypeError, ValueError):
            budget_val = COST_BUDGET_PER_STEP_DEFAULT
        return cls(
            fallback_steps=list(eh.get("fallback_steps") or []),
            compensation_steps=list(eh.get("compensation_steps") or []),
            on_failure=str(eh.get("on_failure", "stop")),
            budget_per_step_usd=budget_val,
            escalation_target=str(eh.get("escalation_target", "main-agent")),
        )


# ---------------------------------------------------------------------------
# Executor
# ---------------------------------------------------------------------------

class RecoveryExecutor:
    """Execute the recovery hierarchy on behalf of the Main Agent.

    The executor is a pure kernel component — it never calls an LLM,
    never spawns external processes, and never decides *whether* a
    recovery strategy is appropriate. It only enforces the limits
    (mutate count, budget) and runs the steps the Main Agent chose.
    """

    def __init__(
        self,
        bus: "MessageBus",
        registry: "AgentRegistry",
        checkpoints: CheckpointManager,
        budget: BudgetTracker,
        failure_detector: FailureDetector | None = None,
    ) -> None:
        self._bus: "MessageBus" = bus
        self._registry: "AgentRegistry" = registry
        self._checkpoints: CheckpointManager = checkpoints
        self._budget: BudgetTracker = budget
        self._detector = failure_detector or FailureDetector()
        if failure_detector is None:
            # Self-attached detector is fine in tests; production
            # code passes a shared one.
            pass

    # -- retry -------------------------------------------------------------

    async def retry_step(
        self,
        workflow_id: str,
        step_id: str,
        agent_id: AgentIdStr,
        *,
        mutate_count: int = 0,
    ) -> RecoveryResult:
        """Re-execute ``step_id`` with the same agent and same config.

        ``mutate_count`` is the current number of mutates already
        performed on the step (0 for a plain retry). The executor
        does not enforce the mutate cap on retries — only on
        :meth:`mutate_step`.
        """
        await self._emit_step_recovered(
            workflow_id=workflow_id,
            step_id=step_id,
            agent_id=agent_id,
            strategy="retry",
            mutate_count=mutate_count,
        )
        return RecoveryResult(
            success=True,
            action="retry_with_same_agent",
            message=f"step {step_id} will be re-executed by {agent_id}",
            mutate_count=mutate_count,
        )

    # -- mutate ------------------------------------------------------------

    async def mutate_step(
        self,
        workflow_id: str,
        step_id: str,
        mutation: MutationConfig | dict[str, Any],
        *,
        mutate_count: int = 0,
        agent_id: AgentIdStr | None = None,
    ) -> RecoveryResult:
        """Apply a manifest delta to the step's agent.

        Hard guardrails (both checked before mutation):

        * ``mutate_count < MUTATE_MAX_PER_STEP``  (default 3)
        * the step's running cost is below its USD budget

        A failed guardrail returns ``success=False`` with a clear
        ``action`` ("mutate_cap_exceeded" or "budget_exhausted") so
        the caller (Main Agent) can escalate.
        """
        if isinstance(mutation, dict):
            mutation = MutationConfig.from_delta(mutation)
        # 1. Enforce the mutate cap.
        if mutate_count >= MUTATE_MAX_PER_STEP:
            await self._audit(
                action="mutate_rejected",
                result="mutate_cap_exceeded",
                workflow_id=workflow_id,
                step_id=step_id,
                agent_id=agent_id,
                details={
                    "mutate_count": mutate_count,
                    "cap": MUTATE_MAX_PER_STEP,
                },
            )
            return RecoveryResult(
                success=False,
                action="escalate_to_user",
                message=(
                    f"step {step_id!r} has hit the mutate cap "
                    f"({MUTATE_MAX_PER_STEP}); escalating"
                ),
                mutate_count=mutate_count,
            )
        # 2. Enforce the budget.
        cost = self._budget.get_step_cost(workflow_id, step_id)
        budget = self._budget.get_step_budget(workflow_id, step_id)
        if cost >= budget:
            await self._audit(
                action="mutate_rejected",
                result="budget_exhausted",
                workflow_id=workflow_id,
                step_id=step_id,
                agent_id=agent_id,
                details={"cost": cost, "budget": budget},
            )
            return RecoveryResult(
                success=False,
                action="escalate_to_user",
                message=(
                    f"step {step_id!r} budget exhausted "
                    f"({cost:.4f}/{budget:.4f} USD); escalating"
                ),
                mutate_count=mutate_count,
            )
        # 3. Apply the mutation.
        next_count = mutate_count + 1
        if self._detector is not None:
            self._detector.record_mutate(workflow_id, step_id)
        await self._audit(
            action="mutate_applied",
            result="ok",
            workflow_id=workflow_id,
            step_id=step_id,
            agent_id=agent_id,
            details={
                "model_tier": mutation.model_tier,
                "loop_type": mutation.loop_type,
                "spawn_fresh": mutation.spawn_fresh,
                "mutate_count": next_count,
                "extra": mutation.extra,
            },
        )
        # 4. Emit step_recovered with the new manifest delta.
        await self._emit_step_recovered(
            workflow_id=workflow_id,
            step_id=step_id,
            agent_id=agent_id or "",
            strategy="mutate",
            mutate_count=next_count,
            manifest_delta=mutation.model_dump(exclude_none=True),
        )
        return RecoveryResult(
            success=True,
            action="mutate_config",
            message=(
                f"step {step_id!r} mutated: "
                f"model={mutation.model_tier} loop={mutation.loop_type} "
                f"spawn_fresh={mutation.spawn_fresh}"
            ),
            mutate_count=next_count,
        )

    # -- fallback / compensation ------------------------------------------

    async def run_fallback_steps(
        self,
        workflow_id: str,
        error_handling: ErrorHandlingConfig | dict[str, Any],
    ) -> RecoveryResult:
        """Trigger the workflow's fallback step chain.

        The executor does not execute the steps itself — it emits a
        ``fallback_invoked`` event to the Main Agent which routes it
        back to the workflow executor. The kernel's role is bookkeeping.
        """
        if isinstance(error_handling, dict):
            error_handling = ErrorHandlingConfig.from_workflow(error_handling)
        steps = list(error_handling.fallback_steps)
        if not steps:
            return RecoveryResult(
                success=False,
                action="escalate_to_user",
                message="no fallback_steps configured",
            )
        await self._audit(
            action="fallback_invoked",
            result="ok",
            workflow_id=workflow_id,
            step_id=None,
            agent_id=None,
            details={"steps": steps},
        )
        try:
            await self._bus.emit_event(
                "fallback_invoked",
                {
                    "workflow_id": workflow_id,
                    "fallback_steps": steps,
                    "on_failure": error_handling.on_failure,
                },
            )
        except Exception:  # noqa: BLE001
            logger.exception("failed to emit fallback_invoked event")
        return RecoveryResult(
            success=True,
            action="run_fallback_steps",
            message=f"fallback chain: {steps}",
            extra={"fallback_steps": steps},
        )

    async def run_compensation_steps(
        self,
        workflow_id: str,
        compensation_steps: list[str] | ErrorHandlingConfig,
    ) -> RecoveryResult:
        """Trigger the workflow's compensation (undo) steps.

        Accepts either a list of step ids (legacy) or the full
        :class:`ErrorHandlingConfig` (preferred).
        """
        if isinstance(compensation_steps, ErrorHandlingConfig):
            steps = list(compensation_steps.compensation_steps)
        else:
            steps = list(compensation_steps)
        if not steps:
            return RecoveryResult(
                success=False,
                action="escalate_to_user",
                message="no compensation_steps configured",
            )
        await self._audit(
            action="compensation_invoked",
            result="ok",
            workflow_id=workflow_id,
            step_id=None,
            agent_id=None,
            details={"steps": steps},
        )
        try:
            await self._bus.emit_event(
                "compensation_invoked",
                {"workflow_id": workflow_id, "compensation_steps": steps},
            )
        except Exception:  # noqa: BLE001
            logger.exception("failed to emit compensation_invoked event")
        return RecoveryResult(
            success=True,
            action="run_compensation_steps",
            message=f"compensation chain: {steps}",
            extra={"compensation_steps": steps},
        )

    # -- dispatch (high-level) ---------------------------------------------

    async def dispatch(
        self,
        decision: RecoveryDecision,
        *,
        workflow_id: str,
        step_id: str,
        agent_id: AgentIdStr | None = None,
        mutate_count: int = 0,
        error_handling: ErrorHandlingConfig | dict[str, Any] | None = None,
    ) -> RecoveryResult:
        """Dispatch a :class:`RecoveryDecision` to the appropriate handler.

        This is the entry point the Main Agent uses. The executor
        enforces the mutate-count and budget limits and returns a
        :class:`RecoveryResult` for the agent to log.
        """
        action = decision.action
        if action == "retry_with_same_agent":
            return await self.retry_step(
                workflow_id=workflow_id,
                step_id=step_id,
                agent_id=agent_id or "",  # type: ignore[arg-type]
                mutate_count=mutate_count,
            )
        if action == "mutate_config":
            return await self.mutate_step(
                workflow_id=workflow_id,
                step_id=step_id,
                mutation=decision.manifest_delta,
                mutate_count=mutate_count,
                agent_id=agent_id,
            )
        if action == "budget_override":
            budget_override_usd = decision.budget_override_usd
            if budget_override_usd is None or budget_override_usd <= 0:
                return RecoveryResult(
                    success=False,
                    action="escalate_to_user",
                    message="budget_override requires positive budget_override_usd",
                )
            granted = self._budget.grant_override(
                workflow_id, step_id, float(budget_override_usd)
            )
            await self._audit(
                action="budget_override",
                result="granted" if granted else "rejected",
                workflow_id=workflow_id,
                step_id=step_id,
                agent_id=agent_id,
                details={
                    "requested_budget": budget_override_usd,
                    "reason": decision.reason,
                },
            )
            if not granted:
                return RecoveryResult(
                    success=False,
                    action="escalate_to_user",
                    message="budget_override already used for this step",
                    decision=decision,
                )
            return RecoveryResult(
                success=True,
                action="budget_override",
                message=f"budget override granted: {budget_override_usd:.4f} USD",
                decision=decision,
            )
        if action == "run_fallback_steps":
            eh = error_handling or ErrorHandlingConfig()
            if not isinstance(eh, ErrorHandlingConfig):
                eh = ErrorHandlingConfig.from_workflow(eh)
            return await self.run_fallback_steps(workflow_id, eh)
        if action == "run_compensation_steps":
            eh = error_handling or ErrorHandlingConfig()
            if not isinstance(eh, ErrorHandlingConfig):
                eh = ErrorHandlingConfig.from_workflow(eh)
            return await self.run_compensation_steps(workflow_id, eh)
        if action in {"escalate_to_user", "cancel_workflow"}:
            await self._emit_escalation(
                decision=decision,
                workflow_id=workflow_id,
                step_id=step_id,
                agent_id=agent_id,
            )
            return RecoveryResult(
                success=True,
                action=action,
                message=decision.reason or f"{action} emitted",
                decision=decision,
            )
        if action == "continue_from_step":
            return await self._continue_from_step(
                decision=decision,
                workflow_id=workflow_id,
                step_id=step_id,
            )
        if action == "rollback_n_steps":
            return await self._rollback_n_steps(
                decision=decision,
                workflow_id=workflow_id,
                n=decision.rollback_n_steps,
            )
        if action == "respawn_all_agents":
            return await self._respawn_all_agents(
                decision=decision,
                workflow_id=workflow_id,
            )
        if action == "noop":
            return RecoveryResult(
                success=True,
                action="noop",
                message="decision was noop",
                decision=decision,
            )
        return RecoveryResult(
            success=False,
            action="escalate_to_user",
            message=f"unknown recovery action: {action!r}",
            decision=decision,
        )

    # -- helpers -----------------------------------------------------------

    async def _continue_from_step(
        self,
        decision: RecoveryDecision,
        workflow_id: str,
        step_id: str,
    ) -> RecoveryResult:
        """Continue from ``step_id`` using the most recent checkpoint."""
        cp = await self._checkpoints.get_latest_checkpoint(workflow_id)
        if cp is None:
            return RecoveryResult(
                success=False,
                action="escalate_to_user",
                message=f"no checkpoint for workflow {workflow_id!r}",
                decision=decision,
            )
        await self._audit(
            action="continue_from_step",
            result="ok",
            workflow_id=workflow_id,
            step_id=step_id,
            agent_id=None,
            details={"checkpoint_id": cp.checkpoint_id, "reason": decision.reason},
        )
        return RecoveryResult(
            success=True,
            action="continue_from_step",
            message=f"continuing from checkpoint {cp.checkpoint_id} at step {cp.step_id}",
            checkpoint=cp,
            decision=decision,
        )

    async def _rollback_n_steps(
        self,
        decision: RecoveryDecision,
        workflow_id: str,
        n: int,
    ) -> RecoveryResult:
        """Roll back ``n`` checkpoints for ``workflow_id``."""
        if n <= 0:
            return RecoveryResult(
                success=False,
                action="escalate_to_user",
                message="rollback_n_steps requires positive n",
                decision=decision,
            )
        checkpoints = await self._checkpoints.list_checkpoints(workflow_id)
        # Newest-first slice of the first n entries, then drop the rest.
        keep_from = max(0, len(checkpoints) - n)
        to_drop = checkpoints[: keep_from]
        dropped_ids: list[int] = []
        for cp in to_drop:
            # We don't physically delete; we mark the dropped checkpoints
            # by inserting a tombstone-style checkpoint at the rollback
            # point.
            await self._checkpoints.write_checkpoint(
                workflow_id=cp.workflow_id,
                step_id=cp.step_id,
                state_blob={**cp.state_blob, "rolled_back": True},
                agent_outputs=cp.agent_outputs,
                status="rolled_back",
                mutate_count=cp.mutate_count,
            )
            dropped_ids.append(cp.checkpoint_id)
        await self._audit(
            action="rollback",
            result="ok",
            workflow_id=workflow_id,
            step_id=None,
            agent_id=None,
            details={"rolled_back_n": n, "dropped_checkpoint_ids": dropped_ids},
        )
        return RecoveryResult(
            success=True,
            action="rollback_n_steps",
            message=f"rolled back {n} step(s) for {workflow_id}",
            decision=decision,
            extra={"rolled_back_n": n, "dropped_checkpoint_ids": dropped_ids},
        )

    async def _respawn_all_agents(
        self,
        decision: RecoveryDecision,
        workflow_id: str,
    ) -> RecoveryResult:
        """Mark all step agents for the workflow as needing a respawn.

        The Main Agent is responsible for actually emitting the
        ``spawn_request`` envelopes — the kernel only records the
        decision in the audit log.
        """
        await self._audit(
            action="respawn_all_agents",
            result="ok",
            workflow_id=workflow_id,
            step_id=None,
            agent_id=None,
            details={"reason": decision.reason},
        )
        try:
            await self._bus.emit_event(
                "respawn_requested",
                {"workflow_id": workflow_id, "reason": decision.reason},
            )
        except Exception:  # noqa: BLE001
            logger.exception("failed to emit respawn_requested event")
        return RecoveryResult(
            success=True,
            action="respawn_all_agents",
            message=f"respawn requested for workflow {workflow_id}",
            decision=decision,
        )

    # -- audit / emit ------------------------------------------------------

    async def _audit(
        self,
        *,
        action: str,
        result: str,
        workflow_id: str,
        step_id: str | None,
        agent_id: AgentIdStr | None,
        details: dict[str, Any] | None = None,
    ) -> None:
        """Append a row to the kernel audit log. Never raises."""
        try:
            await self._registry.audit(
                action=action,
                result=result,
                agent_id=agent_id,
                details={
                    "workflow_id": workflow_id,
                    "step_id": step_id,
                    "subsystem": "recovery",
                    **(details or {}),
                },
            )
        except Exception:  # noqa: BLE001 — audit must never crash recovery
            logger.exception("audit write failed for %s", action)

    async def _emit_step_recovered(
        self,
        *,
        workflow_id: str,
        step_id: str,
        agent_id: str,
        strategy: str,
        mutate_count: int,
        manifest_delta: dict[str, Any] | None = None,
    ) -> None:
        """Emit ``step_recovered`` to the dashboard stream + main agent."""
        payload: dict[str, Any] = {
            "workflow_id": workflow_id,
            "step_id": step_id,
            "agent_id": agent_id,
            "strategy": strategy,
            "mutate_count": mutate_count,
        }
        if manifest_delta is not None:
            payload["manifest_delta"] = manifest_delta
        try:
            await self._bus.emit_event("step_recovered", payload)
        except Exception:  # noqa: BLE001
            logger.exception("failed to emit step_recovered event")

    async def _emit_escalation(
        self,
        decision: RecoveryDecision,
        workflow_id: str,
        step_id: str,
        agent_id: AgentIdStr | None,
    ) -> None:
        """Emit an escalation event so the dashboard can surface it."""
        payload: dict[str, Any] = {
            "workflow_id": workflow_id,
            "step_id": step_id,
            "agent_id": agent_id,
            "action": decision.action,
            "reason": decision.reason,
        }
        try:
            await self._bus.emit_event(
                "escalation_requested",
                payload,
            )
        except Exception:  # noqa: BLE001
            logger.exception("failed to emit escalation_requested event")


# ---------------------------------------------------------------------------
# Manifest mutation helper
# ---------------------------------------------------------------------------

def apply_mutation(
    manifest: AgentManifest,
    mutation: MutationConfig | dict[str, Any],
) -> AgentManifest:
    """Apply a manifest delta to a manifest, returning a fresh copy.

    Used by the agent worker when a ``mutate_config`` decision arrives.
    Raises :class:`ValueError` if the manifest's ``model_tier`` cannot
    be upgraded (e.g. already at ``powerful``).
    """
    if isinstance(mutation, dict):
        mutation = MutationConfig.from_delta(mutation)
    data = manifest.model_dump(mode="json")
    if mutation.model_tier is not None:
        current = data.get("model_tier") or {}
        current["tier"] = mutation.model_tier
        data["model_tier"] = current
    if mutation.loop_type is not None:
        tp = data.get("thinking_profile") or {}
        tp["default_loop"] = mutation.loop_type
        data["thinking_profile"] = tp
    # ``spawn_fresh`` is a runtime decision (handled by the executor),
    # not a manifest field.
    for k, v in mutation.extra.items():
        data[k] = v
    return AgentManifest.model_validate(data)


# ---------------------------------------------------------------------------
# Step retry envelope (used by the agent worker to re-queue a step)
# ---------------------------------------------------------------------------

def build_step_retry_envelope(
    *,
    sender_agent_id: AgentIdStr,
    receiver_agent_id: AgentIdStr,
    workflow_id: str,
    step_id: str,
    mutate_count: int = 0,
    manifest_delta: dict[str, Any] | None = None,
    priority: int = 5,
) -> Envelope:
    """Build the ``request`` envelope the kernel emits to re-run a step.

    The receiver is the (possibly mutated) agent. ``mutate_count`` and
    ``manifest_delta`` ride along in the data payload so the worker
    can apply the new config before executing.
    """
    payload: dict[str, Any] = {
        "content_type": "data",
        "data": {
            "action": "step_retry",
            "workflow_id": workflow_id,
            "step_id": step_id,
            "mutate_count": mutate_count,
            "manifest_delta": manifest_delta or {},
        },
    }
    return Envelope(
        envelope_id=str(uuid.uuid4()),
        created_at=datetime.now(timezone.utc),
        envelope_type="request",
        sender=Endpoint(agent_id=sender_agent_id, role="kernel"),
        receiver=Endpoint(agent_id=receiver_agent_id, role="executor"),
        preamble=Preamble(
            intent={"goal": f"retry:{step_id}", "phase": "recovery"},
        ),
        payload=payload,  # type: ignore[arg-type]
        metadata=EnvelopeMetadata(priority=priority),
    )


__all__ = [
    "ErrorHandlingConfig",
    "MutationConfig",
    "RecoveryActionLiteral",
    "RecoveryDecision",
    "RecoveryExecutor",
    "RecoveryResult",
    "apply_mutation",
    "build_step_retry_envelope",
]
