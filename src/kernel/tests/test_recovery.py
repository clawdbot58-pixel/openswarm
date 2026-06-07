"""Tests for the :class:`~kernel.recovery.RecoveryExecutor`.

Covers the deterministic kernel side of the recovery hierarchy:

* ``retry_step`` re-runs the step with the same agent
* ``mutate_step`` upgrades model tier / loop type / spawns fresh
* the mutate cap (3 attempts) and the budget are enforced
* ``run_fallback_steps`` and ``run_compensation_steps`` emit
  orchestration events
* ``dispatch`` is the high-level entry point the Main Agent uses
* budget_override is granted exactly once per step
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from kernel.budget import COST_BUDGET_PER_STEP_DEFAULT, BudgetTracker
from kernel.checkpoint import CheckpointManager
from kernel.failure_detector import FailureDetector
from kernel.models import AgentManifest
from kernel.recovery import (
    ErrorHandlingConfig,
    MutationConfig,
    RecoveryDecision,
    RecoveryExecutor,
    apply_mutation,
    build_step_retry_envelope,
)


# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------

class _StubBus:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    async def emit_event(self, event_type: str, details: dict | None = None) -> None:
        self.events.append((event_type, dict(details or {})))


class _StubRegistry:
    """Minimal stand-in for :class:`AgentRegistry` for the executor."""

    def __init__(self) -> None:
        self.audit_rows: list[dict[str, Any]] = []

    async def audit(
        self,
        *,
        action: str,
        result: str,
        agent_id: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        self.audit_rows.append(
            {
                "action": action,
                "result": result,
                "agent_id": agent_id,
                "details": details or {},
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
        )


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------

@pytest.fixture
async def stack(tmp_path: Path):
    """Return a bundle of (bus, registry, checkpoints, budget, detector, executor)."""
    bus = _StubBus()
    registry = _StubRegistry()
    cm = CheckpointManager(tmp_path / "cp.db")
    await cm.initialize()
    budget = BudgetTracker(bus)
    detector = FailureDetector(bus)
    executor = RecoveryExecutor(bus, registry, cm, budget, detector)
    return bus, registry, cm, budget, detector, executor


# ---------------------------------------------------------------------------
# retry_step
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_retry_step_emits_step_recovered(stack) -> None:
    bus, registry, cm, budget, detector, executor = stack
    result = await executor.retry_step("wf-1", "step_a", "coder")
    assert result.success
    assert result.action == "retry_with_same_agent"
    # A step_recovered event was emitted on the bus.
    assert any(ev[0] == "step_recovered" for ev in bus.events)
    name, payload = next(ev for ev in bus.events if ev[0] == "step_recovered")
    assert payload["strategy"] == "retry"
    assert payload["workflow_id"] == "wf-1"
    assert payload["step_id"] == "step_a"
    assert payload["agent_id"] == "coder"
    # An audit row was written.
    assert any(r["action"] == "mutate_rejected" or r["action"] == "mutate_applied" for r in registry.audit_rows) is False
    # The mutate count is unchanged (retry is not a mutate).
    assert detector.get_mutate_count("wf-1", "step_a") == 0


# ---------------------------------------------------------------------------
# mutate_step — happy paths
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_mutate_step_upgrades_model_tier(stack) -> None:
    bus, registry, cm, budget, detector, executor = stack
    result = await executor.mutate_step(
        "wf-1", "step_a",
        mutation=MutationConfig(model_tier="powerful"),
        mutate_count=0,
        agent_id="coder",
    )
    assert result.success
    assert result.action == "mutate_config"
    assert result.mutate_count == 1
    # The detector saw the mutate.
    assert detector.get_mutate_count("wf-1", "step_a") == 1
    # An audit row was written.
    rows = [r for r in registry.audit_rows if r["action"] == "mutate_applied"]
    assert len(rows) == 1
    assert rows[0]["details"]["model_tier"] == "powerful"
    # The step_recovered event carries the new manifest delta.
    name, payload = next(ev for ev in bus.events if ev[0] == "step_recovered")
    assert payload["strategy"] == "mutate"
    assert payload["manifest_delta"]["model_tier"] == "powerful"


@pytest.mark.asyncio
async def test_mutate_step_upgrades_loop_type(stack) -> None:
    bus, registry, cm, budget, detector, executor = stack
    result = await executor.mutate_step(
        "wf-1", "step_a",
        mutation=MutationConfig(loop_type="reflection"),
        mutate_count=1,
        agent_id="coder",
    )
    assert result.success
    assert result.mutate_count == 2
    rows = [r for r in registry.audit_rows if r["action"] == "mutate_applied"]
    assert rows[0]["details"]["loop_type"] == "reflection"


@pytest.mark.asyncio
async def test_mutate_step_spawns_fresh(stack) -> None:
    bus, registry, cm, budget, detector, executor = stack
    result = await executor.mutate_step(
        "wf-1", "step_a",
        mutation=MutationConfig(spawn_fresh=True, model_tier="standard"),
        mutate_count=2,
        agent_id="coder",
    )
    assert result.success
    assert result.mutate_count == 3
    rows = [r for r in registry.audit_rows if r["action"] == "mutate_applied"]
    assert rows[0]["details"]["spawn_fresh"] is True


# ---------------------------------------------------------------------------
# mutate_step — hard guardrails
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_mutate_step_rejects_when_cap_reached(stack) -> None:
    bus, registry, cm, budget, detector, executor = stack
    # mutate_count == 3 means we already used all 3 attempts.
    result = await executor.mutate_step(
        "wf-1", "step_a",
        mutation=MutationConfig(model_tier="powerful"),
        mutate_count=3,
    )
    assert not result.success
    assert result.action == "escalate_to_user"
    rows = [r for r in registry.audit_rows if r["action"] == "mutate_rejected"]
    assert rows[0]["result"] == "mutate_cap_exceeded"


@pytest.mark.asyncio
async def test_mutate_step_rejects_when_budget_exhausted(stack) -> None:
    bus, registry, cm, budget, detector, executor = stack
    # Use up the budget.
    budget.set_step_budget("wf-1", "step_a", 0.10)
    await budget.record_cost("wf-1", "step_a", 0.10)
    result = await executor.mutate_step(
        "wf-1", "step_a",
        mutation=MutationConfig(model_tier="powerful"),
        mutate_count=0,
    )
    assert not result.success
    assert result.action == "escalate_to_user"
    rows = [r for r in registry.audit_rows if r["action"] == "mutate_rejected"]
    assert rows[0]["result"] == "budget_exhausted"


# ---------------------------------------------------------------------------
# mutate_step — mutate-exhausted loop event after 3 attempts
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_three_mutates_emit_loop_detected(stack) -> None:
    bus, registry, cm, budget, detector, executor = stack
    budget.set_step_budget("wf-1", "step_a", 10.0)  # plenty of headroom
    for i in range(3):
        r = await executor.mutate_step(
            "wf-1", "step_a",
            mutation=MutationConfig(model_tier="powerful"),
            mutate_count=i,
        )
        assert r.success
    # Give the asyncio.create_task inside the detector a chance to run.
    for _ in range(20):
        if any(ev[0] == "loop_detected" for ev in bus.events):
            break
        await asyncio.sleep(0.01)
    assert any(ev[0] == "loop_detected" for ev in bus.events)
    name, payload = next(ev for ev in bus.events if ev[0] == "loop_detected")
    assert payload["pattern"] == "mutate_exhausted"
    assert payload["workflow_id"] == "wf-1"
    assert payload["step_id"] == "step_a"


# ---------------------------------------------------------------------------
# run_fallback_steps / run_compensation_steps
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_run_fallback_steps_with_config(stack) -> None:
    bus, registry, cm, budget, detector, executor = stack
    eh = ErrorHandlingConfig(
        fallback_steps=["step_fallback_a", "step_fallback_b"],
        compensation_steps=[],
    )
    result = await executor.run_fallback_steps("wf-1", eh)
    assert result.success
    assert result.action == "run_fallback_steps"
    assert result.extra["fallback_steps"] == ["step_fallback_a", "step_fallback_b"]
    assert any(ev[0] == "fallback_invoked" for ev in bus.events)
    assert any(r["action"] == "fallback_invoked" for r in registry.audit_rows)


@pytest.mark.asyncio
async def test_run_fallback_steps_empty_returns_escalation(stack) -> None:
    bus, registry, cm, budget, detector, executor = stack
    result = await executor.run_fallback_steps("wf-1", ErrorHandlingConfig())
    assert not result.success
    assert result.action == "escalate_to_user"


@pytest.mark.asyncio
async def test_run_compensation_steps(stack) -> None:
    bus, registry, cm, budget, detector, executor = stack
    eh = ErrorHandlingConfig(compensation_steps=["undo_a", "undo_b"])
    result = await executor.run_compensation_steps("wf-1", eh)
    assert result.success
    assert result.action == "run_compensation_steps"
    assert any(ev[0] == "compensation_invoked" for ev in bus.events)


@pytest.mark.asyncio
async def test_run_compensation_steps_empty_returns_escalation(stack) -> None:
    bus, registry, cm, budget, detector, executor = stack
    result = await executor.run_compensation_steps("wf-1", [])
    assert not result.success
    assert result.action == "escalate_to_user"


# ---------------------------------------------------------------------------
# dispatch (high-level)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_dispatch_retry(stack) -> None:
    bus, registry, cm, budget, detector, executor = stack
    decision = RecoveryDecision(
        action="retry_with_same_agent", reason="transient error"
    )
    result = await executor.dispatch(
        decision, workflow_id="wf-1", step_id="s1", agent_id="coder"
    )
    assert result.success
    assert result.action == "retry_with_same_agent"


@pytest.mark.asyncio
async def test_dispatch_mutate(stack) -> None:
    bus, registry, cm, budget, detector, executor = stack
    decision = RecoveryDecision(
        action="mutate_config",
        manifest_delta={"model_tier": "powerful"},
        reason="upgrade model",
    )
    result = await executor.dispatch(
        decision,
        workflow_id="wf-1",
        step_id="s1",
        agent_id="coder",
        mutate_count=0,
    )
    assert result.success
    assert result.mutate_count == 1


@pytest.mark.asyncio
async def test_dispatch_budget_override_grants_once(stack) -> None:
    bus, registry, cm, budget, detector, executor = stack
    # First override: granted.
    decision1 = RecoveryDecision(
        action="budget_override", budget_override_usd=1.0, reason="user ok"
    )
    r1 = await executor.dispatch(decision1, workflow_id="wf-1", step_id="s1")
    assert r1.success
    assert budget.has_override("wf-1", "s1")
    # Second override: rejected.
    decision2 = RecoveryDecision(
        action="budget_override", budget_override_usd=2.0, reason="again"
    )
    r2 = await executor.dispatch(decision2, workflow_id="wf-1", step_id="s1")
    assert not r2.success
    assert r2.action == "escalate_to_user"


@pytest.mark.asyncio
async def test_dispatch_budget_override_rejects_non_positive(stack) -> None:
    bus, registry, cm, budget, detector, executor = stack
    decision = RecoveryDecision(
        action="budget_override", budget_override_usd=-1.0
    )
    result = await executor.dispatch(decision, workflow_id="wf-1", step_id="s1")
    assert not result.success
    assert result.action == "escalate_to_user"


@pytest.mark.asyncio
async def test_dispatch_escalate(stack) -> None:
    bus, registry, cm, budget, detector, executor = stack
    decision = RecoveryDecision(
        action="escalate_to_user", reason="stuck"
    )
    result = await executor.dispatch(decision, workflow_id="wf-1", step_id="s1")
    assert result.success
    assert result.action == "escalate_to_user"
    assert any(ev[0] == "escalation_requested" for ev in bus.events)


@pytest.mark.asyncio
async def test_dispatch_cancel_workflow(stack) -> None:
    bus, registry, cm, budget, detector, executor = stack
    decision = RecoveryDecision(action="cancel_workflow", reason="user quit")
    result = await executor.dispatch(decision, workflow_id="wf-1", step_id="s1")
    assert result.success
    assert result.action == "cancel_workflow"


@pytest.mark.asyncio
async def test_dispatch_continue_from_step_uses_checkpoint(stack) -> None:
    bus, registry, cm, budget, detector, executor = stack
    # Write a checkpoint first.
    cp = await cm.write_checkpoint(
        workflow_id="wf-1",
        step_id="step_done",
        state_blob={"counter": 5, "next_step_id": "step_next"},
        agent_outputs={"step_done": {"value": "ok"}},
    )
    decision = RecoveryDecision(
        action="continue_from_step", reason="resume"
    )
    result = await executor.dispatch(decision, workflow_id="wf-1", step_id="step_next")
    assert result.success
    assert result.action == "continue_from_step"
    assert result.checkpoint is not None
    assert result.checkpoint.checkpoint_id == cp.checkpoint_id


@pytest.mark.asyncio
async def test_dispatch_continue_from_step_without_checkpoint_escalates(
    stack,
) -> None:
    bus, registry, cm, budget, detector, executor = stack
    decision = RecoveryDecision(action="continue_from_step", reason="resume")
    result = await executor.dispatch(decision, workflow_id="wf-no-cp", step_id="s1")
    assert not result.success
    assert result.action == "escalate_to_user"


@pytest.mark.asyncio
async def test_dispatch_rollback_n_steps(stack) -> None:
    bus, registry, cm, budget, detector, executor = stack
    for i in range(4):
        await cm.write_checkpoint("wf-1", f"s{i}", {"i": i}, {})
    decision = RecoveryDecision(
        action="rollback_n_steps", rollback_n_steps=2, reason="bad"
    )
    result = await executor.dispatch(decision, workflow_id="wf-1", step_id="s3")
    assert result.success
    assert result.extra["rolled_back_n"] == 2


@pytest.mark.asyncio
async def test_dispatch_rollback_n_steps_must_be_positive(stack) -> None:
    bus, registry, cm, budget, detector, executor = stack
    decision = RecoveryDecision(
        action="rollback_n_steps", rollback_n_steps=0
    )
    result = await executor.dispatch(decision, workflow_id="wf-1", step_id="s1")
    assert not result.success
    assert result.action == "escalate_to_user"


@pytest.mark.asyncio
async def test_dispatch_respawn_all_agents(stack) -> None:
    bus, registry, cm, budget, detector, executor = stack
    decision = RecoveryDecision(
        action="respawn_all_agents", reason="everything is on fire"
    )
    result = await executor.dispatch(decision, workflow_id="wf-1", step_id="s1")
    assert result.success
    assert result.action == "respawn_all_agents"
    assert any(ev[0] == "respawn_requested" for ev in bus.events)


@pytest.mark.asyncio
async def test_dispatch_noop(stack) -> None:
    bus, registry, cm, budget, detector, executor = stack
    decision = RecoveryDecision(action="noop", reason="nothing to do")
    result = await executor.dispatch(decision, workflow_id="wf-1", step_id="s1")
    assert result.success
    assert result.action == "noop"


@pytest.mark.asyncio
async def test_dispatch_unknown_action_escalates(stack) -> None:
    bus, registry, cm, budget, detector, executor = stack
    # Bypass validation by constructing a dict directly.
    decision = RecoveryDecision(action="retry_with_same_agent", reason="x")
    # Pydantic constrains ``action`` to the literal set. Simulate an
    # unknown action by mutating after construction.
    object.__setattr__(decision, "action", "frobnicate")
    result = await executor.dispatch(decision, workflow_id="wf-1", step_id="s1")
    assert not result.success
    assert result.action == "escalate_to_user"


# ---------------------------------------------------------------------------
# apply_mutation helper
# ---------------------------------------------------------------------------

def _manifest() -> AgentManifest:
    from datetime import datetime, timezone
    return AgentManifest.model_validate(
        {
            "agent_id": "coder",
            "version": "1.0.0",
            "role": "executor",
            "intent": "code",
            "model_tier": {"tier": "fast"},
            "thinking_profile": {"default_loop": "direct"},
            "capabilities": {"inference": {"provider": "anthropic"}},
            "lifecycle": {"persistence": "ephemeral"},
            "registration_time": datetime.now(timezone.utc)
            .isoformat()
            .replace("+00:00", "Z"),
        }
    )


def test_apply_mutation_model_tier() -> None:
    m = _manifest()
    out = apply_mutation(m, MutationConfig(model_tier="powerful"))
    assert out.model_tier is not None
    assert out.model_tier.tier == "powerful"
    # Original is unchanged.
    assert m.model_tier is not None
    assert m.model_tier.tier == "fast"


def test_apply_mutation_loop_type() -> None:
    m = _manifest()
    out = apply_mutation(m, MutationConfig(loop_type="reflection"))
    assert out.thinking_profile is not None
    assert out.thinking_profile.default_loop == "reflection"


def test_apply_mutation_with_dict() -> None:
    m = _manifest()
    out = apply_mutation(m, {"model_tier": "standard"})
    assert out.model_tier is not None
    assert out.model_tier.tier == "standard"


# ---------------------------------------------------------------------------
# build_step_retry_envelope
# ---------------------------------------------------------------------------

def test_build_step_retry_envelope_shape() -> None:
    env = build_step_retry_envelope(
        sender_agent_id="main-agent",
        receiver_agent_id="coder",
        workflow_id="wf-1",
        step_id="s1",
        mutate_count=2,
        manifest_delta={"model_tier": "powerful"},
    )
    assert env.envelope_type == "request"
    assert env.sender.agent_id == "main-agent"
    assert env.receiver.agent_id == "coder"
    assert env.payload.content_type == "data"
    assert env.payload.data["action"] == "step_retry"
    assert env.payload.data["workflow_id"] == "wf-1"
    assert env.payload.data["step_id"] == "s1"
    assert env.payload.data["mutate_count"] == 2
    assert env.payload.data["manifest_delta"] == {"model_tier": "powerful"}
