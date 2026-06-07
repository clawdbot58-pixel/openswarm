"""Tests for the :class:`~kernel.failure_detector.FailureDetector`.

Covers every detection rule from ``vision/self-healing.md``:

* ``action_repeat``      — 3 consecutive same-action envelopes
* ``clarification_spin`` — 5 consecutive request_clarification
* ``tool_failure_repeat``— 3 errors of the same tool
* ``mutate_exhausted``   — 3 mutate attempts on a step

Plus the auxiliary :class:`TimeoutWatcher` and a couple of edge
cases (different actions do not trigger, similar parameters group
correctly).
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone
from typing import Any

import pytest

from kernel.failure_detector import (
    ACTION_REPEAT_THRESHOLD,
    CLARIFICATION_SPIN_THRESHOLD,
    EnvelopeSummary,
    FailureDetector,
    LoopReport,
    MUTATE_EXHAUSTED_THRESHOLD,
    TOOL_FAILURE_REPEAT_THRESHOLD,
    TimeoutWatcher,
)
from kernel.models import (
    AgentIdStr,
    Endpoint,
    Envelope,
    EnvelopeMetadata,
    Preamble,
    TextPayload,
    ToolPayload,
    DataPayload,
    UUID4Str,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _new_id() -> UUID4Str:
    return uuid.uuid4().__str__()


def _envelope(
    sender: str = "agent-1",
    receiver: str = "main-agent",
    *,
    payload: Any = None,
    envelope_type: str = "request",
) -> Envelope:
    if payload is None:
        payload = TextPayload(content="hi")
    return Envelope(
        envelope_id=_new_id(),
        created_at=datetime.now(timezone.utc),
        envelope_type=envelope_type,  # type: ignore[arg-type]
        sender=Endpoint(agent_id=sender, role="executor"),
        receiver=Endpoint(agent_id=receiver, role="orchestrator"),
        preamble=Preamble(intent={"goal": "test", "phase": "execution"}),
        payload=payload,
        metadata=EnvelopeMetadata(priority=5),
    )


def _tool_envelope(
    tool_name: str = "file.write",
    parameters: dict | None = None,
    *,
    envelope_type: str = "request",
    sender: str = "agent-1",
) -> Envelope:
    return _envelope(
        sender=sender,
        payload=ToolPayload(
            tool_name=tool_name,
            action="invoke",
            parameters=parameters or {"path": "/a/b.txt", "content": "x"},
        ),
        envelope_type=envelope_type,
    )


def _tool_error_envelope(
    tool_name: str = "file.write",
    code: str = "EACCES",
    message: str = "permission denied",
    parameters: dict | None = None,
) -> Envelope:
    """A tool error envelope — same tool, same parameters, same error."""
    return _envelope(
        payload=ToolPayload(
            tool_name=tool_name,
            action="invoke",
            parameters=parameters or {"path": "/a/b.txt", "content": "x"},
        ),
        envelope_type="error",
    )


def _clarification_envelope(*, sender: str = "agent-1") -> Envelope:
    return _envelope(
        sender=sender,
        payload=DataPayload(
            data={"action": "request_clarification", "question": "?"}
        ),
    )


class _StubBus:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    async def emit_event(self, event_type: str, details: dict | None = None) -> None:
        self.events.append((event_type, dict(details or {})))


# ---------------------------------------------------------------------------
# action_repeat
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_action_repeat_triggers_at_3_consecutive() -> None:
    bus = _StubBus()
    detector = FailureDetector(bus)
    # Two same-action envelopes — below the threshold.
    for _ in range(ACTION_REPEAT_THRESHOLD - 1):
        env = _tool_envelope()
        report = await detector.on_envelope(env)
        assert report is None
    assert bus.events == []
    # The third same-action envelope should fire.
    report = await detector.on_envelope(_tool_envelope())
    assert report is not None
    assert report.pattern == "action_repeat"
    assert report.consecutive_count == ACTION_REPEAT_THRESHOLD
    assert report.agent_id == "agent-1"
    assert bus.events and bus.events[0][0] == "loop_detected"


@pytest.mark.asyncio
async def test_action_repeat_does_not_fire_for_different_actions() -> None:
    detector = FailureDetector()
    for _ in range(5):
        await detector.on_envelope(_tool_envelope("file.write"))
        await detector.on_envelope(_tool_envelope("file.read"))
    # No pattern triggered.
    assert detector.get_loop_report("agent-1") is None


@pytest.mark.asyncio
async def test_action_repeat_uses_canonical_hash_for_params() -> None:
    """Non-deterministic fields stripped before grouping."""
    detector = FailureDetector()
    base = {"path": "/x", "content": "abc"}
    noise = {"request_id": "abc-1", "ts": 1}
    for _ in range(ACTION_REPEAT_THRESHOLD):
        env = _tool_envelope(parameters={**base, **noise})
        report = await detector.on_envelope(env)
        noise["request_id"] = f"abc-{noise['ts']}"
        noise["ts"] += 1
    report = detector.get_loop_report("agent-1")
    assert report is not None
    assert report.pattern == "action_repeat"


# ---------------------------------------------------------------------------
# clarification_spin
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_clarification_spin_triggers_at_5_consecutive() -> None:
    bus = _StubBus()
    detector = FailureDetector(bus)
    for _ in range(CLARIFICATION_SPIN_THRESHOLD - 1):
        report = await detector.on_envelope(_clarification_envelope())
        assert report is None
    report = await detector.on_envelope(_clarification_envelope())
    assert report is not None
    assert report.pattern == "clarification_spin"
    assert report.consecutive_count == CLARIFICATION_SPIN_THRESHOLD
    assert bus.events and bus.events[0][0] == "loop_detected"


@pytest.mark.asyncio
async def test_clarification_spin_breaks_on_non_clarification() -> None:
    detector = FailureDetector()
    for _ in range(3):
        await detector.on_envelope(_clarification_envelope())
    # Break the streak with a normal tool call.
    await detector.on_envelope(_tool_envelope("file.read"))
    for _ in range(3):
        await detector.on_envelope(_clarification_envelope())
    # 3+1+3 = 7 envelopes, but never 5 consecutive clarification.
    assert detector.get_loop_report("agent-1") is None


# ---------------------------------------------------------------------------
# tool_failure_repeat
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_tool_failure_repeat_triggers_at_3_failures() -> None:
    bus = _StubBus()
    detector = FailureDetector(bus)
    for _ in range(TOOL_FAILURE_REPEAT_THRESHOLD - 1):
        env = _tool_error_envelope("file.write", "EACCES", "perm denied")
        report = await detector.on_envelope(env)
        assert report is None
    report = await detector.on_envelope(
        _tool_error_envelope("file.write", "EACCES", "perm denied")
    )
    assert report is not None
    assert report.pattern == "tool_failure_repeat"
    assert report.consecutive_count == TOOL_FAILURE_REPEAT_THRESHOLD
    assert bus.events and bus.events[0][0] == "loop_detected"


@pytest.mark.asyncio
async def test_tool_failure_repeat_breaks_on_different_tool() -> None:
    """Same tool with different parameters is *not* a repeat.

    The detector's signature for a tool error includes the parameters
    (so the agent changing its arguments is not flagged as the same
    tool failing the same way).
    """
    detector = FailureDetector()
    # 3 errors of `file.write` but with different parameters — not
    # the same signature.
    for i in range(3):
        await detector.on_envelope(
            _tool_error_envelope(
                "file.write",
                parameters={"path": f"/file_{i}.txt", "content": "x"},
            )
        )
    assert detector.get_loop_report("agent-1") is None
    # 3 errors of a *different* tool — also not the same signature
    # (different tool name).
    for i in range(3):
        await detector.on_envelope(
            _tool_error_envelope(
                "file.read",
                parameters={"path": f"/file_{i}.txt"},
            )
        )
    # The last 3 are all file.read with different paths — different
    # signatures, so still no tool_failure_repeat.
    assert detector.get_loop_report("agent-1") is None
    # Now 3 errors of the same tool with the same parameters → fires.
    for _ in range(3):
        await detector.on_envelope(
            _tool_error_envelope("file.read", parameters={"path": "/x"})
        )
    report = detector.get_loop_report("agent-1")
    assert report is not None
    assert report.pattern == "tool_failure_repeat"


# ---------------------------------------------------------------------------
# mutate_exhausted
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_mutate_exhausted_triggers_at_3_mutate_attempts() -> None:
    bus = _StubBus()
    detector = FailureDetector(bus)
    # First two mutates: no event.
    assert detector.record_mutate("wf-1", "s1") == 1
    await asyncio.sleep(0.01)  # let any stray task run
    assert detector.get_mutate_count("wf-1", "s1") == 1
    assert detector.record_mutate("wf-1", "s1") == 2
    await asyncio.sleep(0.01)
    # Third mutate fires the event.
    assert detector.record_mutate("wf-1", "s1") == 3
    # Give the event loop a chance to schedule the emit task.
    for _ in range(20):
        if bus.events:
            break
        await asyncio.sleep(0.01)
    assert bus.events, "mutate_exhausted event not emitted"
    name, payload = bus.events[0]
    assert name == "loop_detected"
    assert payload["pattern"] == "mutate_exhausted"
    assert payload["workflow_id"] == "wf-1"
    assert payload["step_id"] == "s1"
    assert payload["mutate_count"] == MUTATE_EXHAUSTED_THRESHOLD


@pytest.mark.asyncio
async def test_mutate_exhausted_resets_between_steps() -> None:
    detector = FailureDetector()
    detector.record_mutate("wf-1", "s1")
    detector.record_mutate("wf-1", "s1")
    detector.record_mutate("wf-1", "s1")
    assert detector.get_mutate_count("wf-1", "s1") == 3
    assert detector.get_mutate_count("wf-1", "s2") == 0
    # Reset clears the count.
    detector.reset_mutate("wf-1", "s1")
    assert detector.get_mutate_count("wf-1", "s1") == 0


# ---------------------------------------------------------------------------
# TimeoutWatcher
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_timeout_watcher_fires_step_timeout() -> None:
    bus = _StubBus()
    watcher = TimeoutWatcher(bus)
    # 0.001 minutes ≈ 60 ms — keep the test fast.
    await watcher.start_step("wf-1", "s1", max_minutes=0.001)
    # Wait for the timer to fire.
    for _ in range(50):
        if bus.events:
            break
        await asyncio.sleep(0.02)
    assert bus.events, "step_timeout event not emitted"
    name, payload = bus.events[0]
    assert name == "step_timeout"
    assert payload["workflow_id"] == "wf-1"
    assert payload["step_id"] == "s1"


@pytest.mark.asyncio
async def test_timeout_watcher_complete_cancels_timer() -> None:
    bus = _StubBus()
    watcher = TimeoutWatcher(bus)
    await watcher.start_step("wf-1", "s1", max_minutes=10.0)
    await watcher.complete_step("wf-1", "s1")
    await asyncio.sleep(0.05)
    assert bus.events == []


@pytest.mark.asyncio
async def test_timeout_watcher_elapsed_minutes_reports_progress() -> None:
    watcher = TimeoutWatcher()
    await watcher.start_step("wf-1", "s1", max_minutes=10.0)
    await asyncio.sleep(0.05)
    elapsed = watcher.elapsed_minutes("wf-1", "s1")
    assert 0.0 < elapsed < 1.0


# ---------------------------------------------------------------------------
# Reporting helpers
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_history_returns_envelope_summaries() -> None:
    detector = FailureDetector()
    for _ in range(4):
        await detector.on_envelope(_tool_envelope("file.read"))
    history = detector.history("agent-1")
    assert len(history) == 4
    for h in history:
        assert isinstance(h, EnvelopeSummary)
        assert h.tool_name == "file.read"


@pytest.mark.asyncio
async def test_active_loop_does_not_duplicate_fire() -> None:
    bus = _StubBus()
    detector = FailureDetector(bus)
    # Trigger action_repeat once.
    for _ in range(ACTION_REPEAT_THRESHOLD + 2):
        await detector.on_envelope(_tool_envelope())
    # Only one loop_detected event was emitted.
    assert len([e for e in bus.events if e[0] == "loop_detected"]) == 1


@pytest.mark.asyncio
async def test_envelope_summary_from_envelope() -> None:
    env = _tool_envelope("shell.run", {"cmd": "ls"})
    summary = EnvelopeSummary.from_envelope(env)
    assert summary.tool_name == "shell.run"
    assert summary.action_type == "tool"
    assert summary.content_type == "tool"
    assert summary.args_hash is not None
    assert summary.error_signature is None


@pytest.mark.asyncio
async def test_loop_report_shape() -> None:
    """The LoopReport serialises cleanly to JSON."""
    detector = FailureDetector()
    for _ in range(ACTION_REPEAT_THRESHOLD):
        await detector.on_envelope(_tool_envelope())
    report = detector.get_loop_report("agent-1")
    assert isinstance(report, LoopReport)
    data = report.model_dump(mode="json")
    assert data["agent_id"] == "agent-1"
    assert data["pattern"] == "action_repeat"
    assert isinstance(data["recent_envelopes"], list)
    assert data["suggested_action"]


# ---------------------------------------------------------------------------
# Edge: kernel-originated envelopes are ignored
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_kernel_originated_envelopes_ignored() -> None:
    detector = FailureDetector()
    for _ in range(10):
        env = _tool_envelope(sender="kernel")
        report = await detector.on_envelope(env)
        assert report is None
    assert detector.history("kernel") == []


# ---------------------------------------------------------------------------
# Detached detector (no bus) is silent but functional
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_detached_detector_works_without_bus() -> None:
    detector = FailureDetector()
    for _ in range(ACTION_REPEAT_THRESHOLD):
        await detector.on_envelope(_tool_envelope())
    report = detector.get_loop_report("agent-1")
    assert report is not None
