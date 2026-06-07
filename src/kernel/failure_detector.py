"""Kernel-side loop, budget, and timeout detector.

The detector watches the envelope stream in real time and emits
``loop_detected`` / ``step_timeout`` events when an agent gets stuck.
The Main Agent is the only entity that decides what to do about it;
the kernel only signals the fact.

Detection patterns (from ``vision/self-healing.md``)
----------------------------------------------------

* ``action_repeat``        — same ``action_type`` + similar args for
                             3 consecutive envelopes from the agent.
* ``clarification_spin``   — 5 consecutive ``request_clarification``
                             envelopes with no new user input in
                             between.
* ``tool_failure_repeat``  — same tool, similar error, 3 failures.
* ``mutate_exhausted``     — 3 mutate attempts on the same step
                             (driven by the recovery executor bumping
                             the checkpoint's ``mutate_count``).

Additionally a separate :class:`TimeoutWatcher` checks each running
step's wall-clock and emits ``step_timeout`` when
``step.max_minutes`` is exceeded. This is decoupled from the loop
detector because it depends on per-step deadlines, not envelopes.

Hard guardrail
--------------

Detection is **deterministic** — no LLM calls, no fuzzy matching that
cannot be unit-tested. Two actions are considered "similar" when:

* the ``action_type`` (e.g. ``"tool"``, ``"text"``, ``"data"``) matches;
* AND for ``tool`` actions, the ``tool_name`` matches;
* AND the parameters hash to the same value after stripping
  non-deterministic fields (e.g. timestamps, request ids, the agent's
  own scratchpad). The detector uses a simple canonical JSON encoding
  and a stable SHA-1 hash for this purpose.

The Main Agent may be smarter about semantics later; the kernel's job
is to surface the fact that *something* is repeating so the agent has
a chance to break out.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Callable, Awaitable, Literal

from pydantic import BaseModel, ConfigDict, Field

from .models import Envelope

if TYPE_CHECKING:  # pragma: no cover
    from .bus import MessageBus

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

LoopPatternLiteral = Literal[
    "action_repeat",
    "clarification_spin",
    "tool_failure_repeat",
    "mutate_exhausted",
]


# Detection thresholds (kept here so the failure_detector tests can
# assert the kernel's wiring matches ``vision/self-healing.md``).
ACTION_REPEAT_THRESHOLD: int = 3
CLARIFICATION_SPIN_THRESHOLD: int = 5
TOOL_FAILURE_REPEAT_THRESHOLD: int = 3
MUTATE_EXHAUSTED_THRESHOLD: int = 3
ENVELOPE_HISTORY_LIMIT: int = 10

# Fields the canonical-hash function ignores when comparing parameters.
# These are expected to differ across attempts even when the agent is
# stuck on the same logical action.
NON_DETERMINISTIC_PARAM_FIELDS: frozenset[str] = frozenset(
    {
        "request_id",
        "idempotency_key",
        "ts",
        "timestamp",
        "trace_id",
        "span_id",
        "created_at",
        "_seq",
    }
)


# ---------------------------------------------------------------------------
# Envelope summary (kept small — the dashboard stream needs only
# enough context to render the recovery modal).
# ---------------------------------------------------------------------------

class EnvelopeSummary(BaseModel):
    """A short, JSON-safe summary of an envelope used in loop reports."""

    model_config = ConfigDict(extra="forbid")

    envelope_id: str
    sender_id: str
    receiver_id: str
    envelope_type: str
    action_type: str | None = None
    tool_name: str | None = None
    content_type: str | None = None
    args_hash: str | None = None
    error_signature: str | None = None
    created_at: str

    @classmethod
    def from_envelope(cls, env: Envelope) -> "EnvelopeSummary":
        """Build a summary from a full :class:`Envelope`."""
        action_type, tool_name, args_hash, content_type, error_sig = _inspect_envelope(env)
        return cls(
            envelope_id=env.envelope_id,
            sender_id=env.sender.agent_id,
            receiver_id=env.receiver.agent_id,
            envelope_type=env.envelope_type,
            action_type=action_type,
            tool_name=tool_name,
            content_type=content_type,
            args_hash=args_hash,
            error_signature=error_sig,
            created_at=env.created_at.isoformat()
            if isinstance(env.created_at, datetime)
            else str(env.created_at),
        )


class LoopReport(BaseModel):
    """Structured loop report the kernel hands to the Main Agent."""

    model_config = ConfigDict(extra="forbid")

    agent_id: str
    pattern: LoopPatternLiteral
    consecutive_count: int
    recent_envelopes: list[EnvelopeSummary] = Field(default_factory=list)
    suggested_action: str
    detected_at: str


# ---------------------------------------------------------------------------
# Suggested action mapping
# ---------------------------------------------------------------------------

SUGGESTED_ACTION: dict[LoopPatternLiteral, str] = {
    "action_repeat": "retry_with_different_approach",
    "clarification_spin": "escalate_to_user",
    "tool_failure_repeat": "mutate_config",
    "mutate_exhausted": "escalate_to_user",
}


# ---------------------------------------------------------------------------
# Envelope inspection
# ---------------------------------------------------------------------------

def _canonical_hash(value: Any) -> str:
    """Stable hash of an arbitrary JSON-able value.

    The result is short (first 16 hex chars of SHA-1) and good enough
    for grouping. Two actions with the same hash are "similar" for
    loop-detection purposes.
    """
    try:
        encoded = json.dumps(
            value, sort_keys=True, separators=(",", ":"), default=str
        )
    except (TypeError, ValueError):
        encoded = repr(value)
    return hashlib.sha1(encoded.encode("utf-8")).hexdigest()[:16]


def _strip_nondeterministic(params: Any) -> Any:
    """Recursively drop fields that are expected to differ across attempts."""
    if isinstance(params, dict):
        return {
            k: _strip_nondeterministic(v)
            for k, v in params.items()
            if k not in NON_DETERMINISTIC_PARAM_FIELDS
        }
    if isinstance(params, (list, tuple)):
        return [_strip_nondeterministic(x) for x in params]
    return params


def _inspect_envelope(
    env: Envelope,
) -> tuple[str | None, str | None, str | None, str | None, str | None]:
    """Return ``(action_type, tool_name, args_hash, content_type, error_sig)``.

    ``action_type`` is one of ``"tool"``, ``"text"``, ``"data"``,
    ``"workflow"``, ``"checkpoint"``, ``"spawn_request"``,
    ``"request_clarification"``, or ``None`` when the envelope is not
    an outbound action.
    """
    payload = env.payload
    content_type = getattr(payload, "content_type", None)
    if content_type is None and isinstance(payload, dict):
        content_type = payload.get("content_type")
    tool_name: str | None = None
    error_sig: str | None = None
    args_hash: str | None = None
    action_type: str | None = None
    if content_type == "tool":
        action_type = "tool"
        if isinstance(payload, dict):
            tool_name = payload.get("tool_name")
            params = payload.get("parameters") or {}
        else:
            tool_name = getattr(payload, "tool_name", None)
            params = getattr(payload, "parameters", None) or {}
        args_hash = _canonical_hash(
            {"tool": tool_name, "params": _strip_nondeterministic(params)}
        )
        # Detect tool errors: the kernel often returns error envelopes
        # with a "data" payload carrying {code, message}. When the
        # payload is a ToolPayload (rather than a DataPayload), we
        # derive the error signature from the *parameters*: if the
        # agent is changing its arguments between attempts the failure
        # mode is different, and we don't want to count that as the
        # "same tool failing the same way".
        if env.envelope_type == "error":
            data = getattr(payload, "data", None)
            if isinstance(data, dict):
                code = data.get("code")
                msg = data.get("message")
                error_sig = _canonical_hash(
                    {"tool": tool_name, "code": code, "message": msg}
                )
            else:
                error_sig = _canonical_hash(
                    {
                        "tool": tool_name,
                        "params": _strip_nondeterministic(params),
                    }
                )
    elif content_type == "text":
        action_type = "text"
        if isinstance(payload, dict):
            content = payload.get("content")
        else:
            content = getattr(payload, "content", None)
        args_hash = _canonical_hash({"content": content})
    elif content_type == "data":
        action_type = "data"
        if isinstance(payload, dict):
            data = payload.get("data")
        else:
            data = getattr(payload, "data", None)
        # request_clarification is a special case — agents send it
        # inside a data payload asking the user (or the main agent)
        # for more info.
        if isinstance(data, dict):
            action = data.get("action")
            if action == "request_clarification":
                action_type = "request_clarification"
            args_hash = _canonical_hash(_strip_nondeterministic(data))
    elif content_type == "workflow":
        action_type = "workflow"
    elif content_type == "checkpoint":
        action_type = "checkpoint"
    elif content_type == "spawn_request":
        action_type = "spawn_request"
    if error_sig is None and env.envelope_type == "error":
        # Generic error (not a tool call): signature is the code+message
        # from the data payload if present.
        data = getattr(payload, "data", None)
        if isinstance(data, dict):
            error_sig = _canonical_hash(
                {"code": data.get("code"), "message": data.get("message")}
            )
    return action_type, tool_name, args_hash, content_type, error_sig


# ---------------------------------------------------------------------------
# Detector
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class _StepMutateState:
    """Per-(workflow, step) mutate counter (drives ``mutate_exhausted``)."""

    count: int = 0


class FailureDetector:
    """Real-time loop and tool-failure detector.

    The detector is **stateless across instances** (each instance owns
    its own queues) and **deterministic**: it never calls an LLM, never
    reaches out over the network, and never decides recovery strategy.

    Usage::

        detector = FailureDetector(bus)
        detector.on_envelope(envelope)
        report = detector.get_loop_report(agent_id)
    """

    def __init__(self, bus: "MessageBus | None" = None) -> None:
        self._bus: "MessageBus | None" = bus
        # Per-agent history of (envelope, action_type, tool_name, args_hash,
        # error_sig, content_type). Bounded to ENVELOPE_HISTORY_LIMIT.
        self._history: dict[str, deque[tuple[Envelope, tuple[Any, ...]]]] = {}
        # Per-(workflow, step) mutate counter.
        self._mutate_state: dict[str, _StepMutateState] = {}
        # Per-agent last loop report (for the dashboard / get_loop_report).
        self._last_reports: dict[str, LoopReport] = {}
        # Per-agent in-flight loop pattern. Used to suppress duplicate
        # emissions of the same report.
        self._active_loop: dict[str, LoopPatternLiteral] = {}
        self._lock: asyncio.Lock = asyncio.Lock()
        # Optional hooks for tests.
        self.on_loop_detected: Callable[[LoopReport], Awaitable[None]] | None = None
        self.on_step_timeout: Callable[[str, str, float], Awaitable[None]] | None = None

    # -- wiring ------------------------------------------------------------

    def attach(self, bus: "MessageBus") -> None:
        """Attach the bus used to emit ``loop_detected`` events."""
        self._bus = bus

    # -- mutate accounting (called by recovery executor) -------------------

    def record_mutate(self, workflow_id: str, step_id: str) -> int:
        """Bump the mutate counter for the step and return the new total.

        Returns the new count. If the new count reaches the mutate
        cap, the detector emits ``loop_detected: mutate_exhausted``
        exactly once per step.
        """
        key = f"{workflow_id}:{step_id}"
        state = self._mutate_state.get(key)
        if state is None:
            state = _StepMutateState()
            self._mutate_state[key] = state
        state.count += 1
        if state.count >= MUTATE_EXHAUSTED_THRESHOLD:
            self._maybe_emit_mutate_exhausted(workflow_id, step_id, state.count)
        return state.count

    def reset_mutate(self, workflow_id: str, step_id: str) -> None:
        """Clear the mutate counter for a step (used on terminal completion)."""
        self._mutate_state.pop(f"{workflow_id}:{step_id}", None)

    def get_mutate_count(self, workflow_id: str, step_id: str) -> int:
        """Current mutate count for a step (0 if no attempts recorded)."""
        state = self._mutate_state.get(f"{workflow_id}:{step_id}")
        return state.count if state is not None else 0

    # -- envelope ingest ---------------------------------------------------

    async def on_envelope(self, envelope: Envelope) -> LoopReport | None:
        """Record ``envelope`` and emit a loop event if a pattern matches.

        The detector is *append-only* — it never drops an envelope
        from history unless it has aged out. The function is safe to
        call from the bus router's hot path.

        De-duplication: if the *same* pattern is still active for an
        agent (no intervening non-loop envelope), the report is
        returned but **not** re-emitted on the bus. The Main Agent
        already knows about the loop.
        """
        sender = envelope.sender.agent_id
        if not sender or sender == "kernel":
            # Kernel-originated envelopes do not represent agent actions.
            return None
        async with self._lock:
            history = self._history.setdefault(
                sender, deque(maxlen=ENVELOPE_HISTORY_LIMIT)
            )
            inspection = _inspect_envelope(envelope)
            history.append((envelope, inspection))
            # Capture the pre-detect active pattern so a brand-new
            # detection still fires. Without this, ``_detect`` would
            # update ``_active_loop`` first, and we'd see the new
            # pattern as "already active" and skip the emit.
            pre_active = self._active_loop.get(sender)
            report = self._detect(sender, history)
        if report is None:
            return None
        self._last_reports[sender] = report
        if pre_active != report.pattern:
            await self._emit_loop(report)
        return report

    def on_envelope_sync(
        self, envelope: Envelope
    ) -> LoopReport | None:
        """Synchronous variant of :meth:`on_envelope` for non-async callers.

        Used by the bus when the detector is wired into the heap-pop
        path. Returns the same :class:`LoopReport` (or ``None``); does
        not emit (caller is expected to do so asynchronously).
        """
        sender = envelope.sender.agent_id
        if not sender or sender == "kernel":
            return None
        history = self._history.setdefault(
            sender, deque(maxlen=ENVELOPE_HISTORY_LIMIT)
        )
        inspection = _inspect_envelope(envelope)
        history.append((envelope, inspection))
        report = self._detect(sender, history)
        if report is not None:
            self._last_reports[sender] = report
        return report

    # -- detection ---------------------------------------------------------

    def _detect(
        self,
        agent_id: str,
        history: deque[tuple[Envelope, tuple[Any, ...]]],
    ) -> LoopReport | None:
        """Apply every detection rule; return the highest-priority hit.

        Priority order (most specific first):

        1. ``tool_failure_repeat``  — 3 errors of the same tool.
        2. ``action_repeat``        — 3 envelopes with the same action.
        3. ``clarification_spin``   — 5 clarification asks in a row.
        """
        if not history:
            return None
        # Avoid double-reporting an active loop with the same pattern.
        existing = self._active_loop.get(agent_id)
        # Rule 1: tool failures.
        report = self._detect_tool_failures(agent_id, history)
        if report is not None:
            self._active_loop[agent_id] = report.pattern
            return report
        # Rule 2: action repeat.
        report = self._detect_action_repeat(agent_id, history)
        if report is not None:
            self._active_loop[agent_id] = report.pattern
            return report
        # Rule 3: clarification spin.
        report = self._detect_clarification_spin(agent_id, history)
        if report is not None:
            self._active_loop[agent_id] = report.pattern
            return report
        # No new pattern; clear the agent's active loop marker so a
        # future regression can fire again.
        if existing is not None:
            self._active_loop.pop(agent_id, None)
        return None

    def _detect_action_repeat(
        self,
        agent_id: str,
        history: deque[tuple[Envelope, tuple[Any, ...]]],
    ) -> LoopReport | None:
        """Same ``(action_type, args_hash)`` for N consecutive envelopes."""
        recent = list(history)[-ACTION_REPEAT_THRESHOLD:]
        if len(recent) < ACTION_REPEAT_THRESHOLD:
            return None
        first = recent[0][1]
        action_type = first[0]
        args_hash = first[2]
        if not action_type or action_type == "checkpoint":
            return None
        if action_type == "request_clarification":
            # Handled by a different rule.
            return None
        for env, insp in recent[1:]:
            if insp[0] != action_type or insp[2] != args_hash:
                return None
        # We have N consecutive matches. Build the report.
        summaries = [EnvelopeSummary.from_envelope(e) for e, _ in recent]
        return LoopReport(
            agent_id=agent_id,
            pattern="action_repeat",
            consecutive_count=len(recent),
            recent_envelopes=summaries,
            suggested_action=SUGGESTED_ACTION["action_repeat"],
            detected_at=_utcnow_iso(),
        )

    def _detect_tool_failures(
        self,
        agent_id: str,
        history: deque[tuple[Envelope, tuple[Any, ...]]],
    ) -> LoopReport | None:
        """Same tool, same error signature, N consecutive failures."""
        recent = list(history)[-TOOL_FAILURE_REPEAT_THRESHOLD:]
        if len(recent) < TOOL_FAILURE_REPEAT_THRESHOLD:
            return None
        first = recent[0][1]
        if first[0] != "tool" or not first[4]:
            # Not a tool envelope with an error signature.
            return None
        tool_name = first[1]
        error_sig = first[4]
        for env, insp in recent[1:]:
            if insp[0] != "tool":
                return None
            if insp[1] != tool_name:
                return None
            if insp[4] != error_sig:
                return None
        summaries = [EnvelopeSummary.from_envelope(e) for e, _ in recent]
        return LoopReport(
            agent_id=agent_id,
            pattern="tool_failure_repeat",
            consecutive_count=len(recent),
            recent_envelopes=summaries,
            suggested_action=SUGGESTED_ACTION["tool_failure_repeat"],
            detected_at=_utcnow_iso(),
        )

    def _detect_clarification_spin(
        self,
        agent_id: str,
        history: deque[tuple[Envelope, tuple[Any, ...]]],
    ) -> LoopReport | None:
        """5 consecutive ``request_clarification`` actions."""
        recent = list(history)[-CLARIFICATION_SPIN_THRESHOLD:]
        if len(recent) < CLARIFICATION_SPIN_THRESHOLD:
            return None
        for env, insp in recent:
            if insp[0] != "request_clarification":
                return None
        summaries = [EnvelopeSummary.from_envelope(e) for e, _ in recent]
        return LoopReport(
            agent_id=agent_id,
            pattern="clarification_spin",
            consecutive_count=len(recent),
            recent_envelopes=summaries,
            suggested_action=SUGGESTED_ACTION["clarification_spin"],
            detected_at=_utcnow_iso(),
        )

    def _maybe_emit_mutate_exhausted(
        self,
        workflow_id: str,
        step_id: str,
        count: int,
    ) -> None:
        """Schedule emission of ``mutate_exhausted`` for a step.

        Called from the synchronous :meth:`record_mutate` path; the
        event is fired asynchronously to keep that path lock-free.
        """
        report = LoopReport(
            agent_id="",  # mutate_exhausted is per-step, not per-agent
            pattern="mutate_exhausted",
            consecutive_count=count,
            recent_envelopes=[],
            suggested_action=SUGGESTED_ACTION["mutate_exhausted"],
            detected_at=_utcnow_iso(),
        )
        self._last_reports[f"step:{workflow_id}:{step_id}"] = report
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.create_task(
            self._emit_loop_with_extra(
                report,
                extra={
                    "workflow_id": workflow_id,
                    "step_id": step_id,
                    "mutate_count": count,
                },
            )
        )

    # -- reporting ---------------------------------------------------------

    def get_loop_report(self, agent_id: str) -> LoopReport | None:
        """Return the most recent loop report for ``agent_id``, or ``None``."""
        return self._last_reports.get(agent_id)

    def get_step_report(
        self, workflow_id: str, step_id: str
    ) -> LoopReport | None:
        """Return the most recent loop report for a step (mutate_exhausted)."""
        return self._last_reports.get(f"step:{workflow_id}:{step_id}")

    def history(self, agent_id: str) -> list[EnvelopeSummary]:
        """Return the recent envelope summaries for an agent (oldest first)."""
        history = self._history.get(agent_id)
        if not history:
            return []
        return [EnvelopeSummary.from_envelope(e) for e, _ in history]

    # -- emit --------------------------------------------------------------

    async def _emit_loop(self, report: LoopReport) -> None:
        """Default ``loop_detected`` emit (no extra fields)."""
        await self._emit_loop_with_extra(report, extra=None)

    async def _emit_loop_with_extra(
        self, report: LoopReport, extra: dict[str, Any] | None
    ) -> None:
        """Emit ``loop_detected`` with optional extra payload fields."""
        if self.on_loop_detected is not None:
            try:
                await self.on_loop_detected(report)
            except Exception:  # noqa: BLE001
                logger.exception("failure_detector on_loop_detected hook failed")
        if self._bus is None:
            logger.warning(
                "loop_detected (no bus attached) pattern=%s agent=%s",
                report.pattern, report.agent_id,
            )
            return
        payload: dict[str, Any] = {
            "agent_id": report.agent_id,
            "pattern": report.pattern,
            "consecutive_count": report.consecutive_count,
            "recent_envelopes": [s.model_dump(mode="json") for s in report.recent_envelopes],
            "suggested_action": report.suggested_action,
            "detected_at": report.detected_at,
        }
        if extra:
            payload.update(extra)
        try:
            await self._bus.emit_event("loop_detected", payload)
        except Exception:  # noqa: BLE001
            logger.exception("failed to emit loop_detected event")


# ---------------------------------------------------------------------------
# Timeout watcher
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class _StepTimer:
    """Per-(workflow, step) timer state."""

    step_id: str
    started_at: float
    max_minutes: float
    workflow_id: str
    timed_out: bool = False
    task: asyncio.Task[None] | None = None


class TimeoutWatcher:
    """Watchdog for steps that exceed their wall-clock budget.

    Each step starts a timer when :meth:`start_step` is called; the
    watcher sleeps for ``max_minutes`` and emits a ``step_timeout``
    event to the Main Agent if the step has not been completed by then.
    The watcher is a deterministic kernel concern — no LLM involvement.
    """

    def __init__(self, bus: "MessageBus | None" = None) -> None:
        self._bus: "MessageBus | None" = bus
        self._timers: dict[str, _StepTimer] = {}
        self._lock: asyncio.Lock = asyncio.Lock()
        self.on_step_timeout: Callable[[str, str, float], Awaitable[None]] | None = None

    def attach(self, bus: "MessageBus") -> None:
        """Attach the bus used to emit ``step_timeout`` events."""
        self._bus = bus

    async def start_step(
        self,
        workflow_id: str,
        step_id: str,
        max_minutes: float,
    ) -> None:
        """Begin watching ``(workflow, step)`` with a ``max_minutes`` ceiling.

        If the step is already being watched, the existing timer is
        replaced. ``max_minutes <= 0`` disables the timer for that step
        (used in tests).
        """
        if max_minutes <= 0:
            return
        key = self._key(workflow_id, step_id)
        async with self._lock:
            existing = self._timers.pop(key, None)
        if existing is not None and existing.task is not None:
            existing.task.cancel()
        timer = _StepTimer(
            step_id=step_id,
            started_at=time.monotonic(),
            max_minutes=float(max_minutes),
            workflow_id=workflow_id,
        )
        if max_minutes > 0:
            timer.task = asyncio.create_task(
                self._wait(timer),
                name=f"timeout-watcher-{workflow_id}-{step_id}",
            )
        async with self._lock:
            self._timers[key] = timer

    async def complete_step(self, workflow_id: str, step_id: str) -> None:
        """Cancel the timer for ``(workflow, step)`` (step finished)."""
        key = self._key(workflow_id, step_id)
        async with self._lock:
            timer = self._timers.pop(key, None)
        if timer is not None and timer.task is not None:
            timer.task.cancel()
            try:
                await timer.task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

    def is_timed_out(self, workflow_id: str, step_id: str) -> bool:
        """True if the step's timer has already fired."""
        key = self._key(workflow_id, step_id)
        timer = self._timers.get(key)
        return bool(timer and timer.timed_out)

    def elapsed_minutes(self, workflow_id: str, step_id: str) -> float:
        """Wall-clock minutes the step has been running so far."""
        key = self._key(workflow_id, step_id)
        timer = self._timers.get(key)
        if timer is None:
            return 0.0
        return (time.monotonic() - timer.started_at) / 60.0

    async def _wait(self, timer: _StepTimer) -> None:
        """Sleep ``max_minutes`` then fire the timeout event."""
        try:
            await asyncio.sleep(timer.max_minutes * 60.0)
        except asyncio.CancelledError:
            return
        timer.timed_out = True
        await self._emit(timer.workflow_id, timer.step_id, timer.max_minutes)

    async def _emit(
        self, workflow_id: str, step_id: str, max_minutes: float
    ) -> None:
        """Emit ``step_timeout`` to the Main Agent."""
        if self.on_step_timeout is not None:
            try:
                await self.on_step_timeout(workflow_id, step_id, max_minutes)
            except Exception:  # noqa: BLE001
                logger.exception("timeout_watcher on_step_timeout hook failed")
        if self._bus is None:
            logger.warning(
                "step_timeout (no bus attached) workflow=%s step=%s",
                workflow_id, step_id,
            )
            return
        try:
            await self._bus.emit_event(
                "step_timeout",
                {
                    "workflow_id": workflow_id,
                    "step_id": step_id,
                    "max_minutes": float(max_minutes),
                    "elapsed_minutes": round(
                        self.elapsed_minutes(workflow_id, step_id), 4
                    ),
                },
            )
        except Exception:  # noqa: BLE001
            logger.exception("failed to emit step_timeout event")

    @staticmethod
    def _key(workflow_id: str, step_id: str) -> str:
        return f"{workflow_id}:{step_id}"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


__all__ = [
    "ACTION_REPEAT_THRESHOLD",
    "CLARIFICATION_SPIN_THRESHOLD",
    "EnvelopeSummary",
    "FailureDetector",
    "LoopPatternLiteral",
    "LoopReport",
    "MUTATE_EXHAUSTED_THRESHOLD",
    "SUGGESTED_ACTION",
    "TOOL_FAILURE_REPEAT_THRESHOLD",
    "TimeoutWatcher",
]
