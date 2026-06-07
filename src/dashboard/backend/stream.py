"""
EventStream: WebSocket server for the dashboard live-view.

Receives kernel events from the bus and fans them out to all connected
browser tabs. Each tab gets a full snapshot on connect and then receives
delta events as they fire.

Phase 9 added a set of self-healing event types (``loop_detected``,
``budget_exhausted``, ``step_timeout``, ``workflow_resume``,
``step_recovered``). They flow through the same broadcast path as
every other kernel event; the constants below make the set discoverable
for the frontend and for tests.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING, Any, Final

from fastapi import WebSocket
from starlette.websockets import WebSocketDisconnect

from kernel.models import Envelope

if TYPE_CHECKING:
    from .bus import MessageBus
    from .introspection import IntrospectionAPI

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Event-name constants — Phase 9 self-healing + Phase 7 base set
# ---------------------------------------------------------------------------

# Phase 7 baseline.
LOOP_DETECTED_EVENT: Final[str] = "loop_detected"
BUDGET_EXHAUSTED_EVENT: Final[str] = "budget_exhausted"
STEP_TIMEOUT_EVENT: Final[str] = "step_timeout"
WORKFLOW_RESUME_EVENT: Final[str] = "workflow_resume"
STEP_RECOVERED_EVENT: Final[str] = "step_recovered"

# Recovery orchestration events (kernel-emitted by the recovery executor).
FALLBACK_INVOKED_EVENT: Final[str] = "fallback_invoked"
COMPENSATION_INVOKED_EVENT: Final[str] = "compensation_invoked"
RESPAWN_REQUESTED_EVENT: Final[str] = "respawn_requested"
ESCALATION_REQUESTED_EVENT: Final[str] = "escalation_requested"

# Aggregate set of every event the dashboard stream may carry. Used by
# the introspection helpers and by tests.
PHASE9_EVENT_NAMES: Final[frozenset[str]] = frozenset(
    {
        LOOP_DETECTED_EVENT,
        BUDGET_EXHAUSTED_EVENT,
        STEP_TIMEOUT_EVENT,
        WORKFLOW_RESUME_EVENT,
        STEP_RECOVERED_EVENT,
        FALLBACK_INVOKED_EVENT,
        COMPENSATION_INVOKED_EVENT,
        RESPAWN_REQUESTED_EVENT,
        ESCALATION_REQUESTED_EVENT,
    }
)


# ---------------------------------------------------------------------------
# Payload shapes (Pydantic-style dicts; we don't bring in pydantic here
# so the constants double as the contract surface for the frontend).
# ---------------------------------------------------------------------------

#: Shape of a ``loop_detected`` payload. See ``vision/self-healing.md``
#: §"Kernel-Side Loop Detection" for the heuristic rules that produce it.
LoopDetectedPayload: Final[dict[str, str]] = {
    "agent_id": "str",
    "pattern": (
        "Literal['action_repeat', 'clarification_spin', "
        "'tool_failure_repeat', 'mutate_exhausted']"
    ),
    "consecutive_count": "int",
    "recent_envelopes": "list[EnvelopeSummary]",
    "suggested_action": "str",
    "detected_at": "str (ISO 8601)",
}

#: Shape of a ``budget_exhausted`` payload.
BudgetExhaustedPayload: Final[dict[str, str]] = {
    "workflow_id": "str",
    "step_id": "str",
    "cost_so_far": "float",
    "budget": "float",
    "currency": "Literal['USD']",
    "remaining_usd": "float",
}

#: Shape of a ``step_timeout`` payload.
StepTimeoutPayload: Final[dict[str, str]] = {
    "workflow_id": "str",
    "step_id": "str",
    "max_minutes": "float",
    "elapsed_minutes": "float",
}

#: Shape of a ``workflow_resume`` payload.
WorkflowResumePayload: Final[dict[str, str]] = {
    "workflow_id": "str",
    "checkpoint": "Optional[Checkpoint]",
    "last_step": "Optional[str]",
    "resumed_state": "dict",
    "resumed_at": "str (ISO 8601)",
    "previous_status": "str",
}

#: Shape of a ``step_recovered`` payload.
StepRecoveredPayload: Final[dict[str, str]] = {
    "workflow_id": "str",
    "step_id": "str",
    "agent_id": "str",
    "strategy": "Literal['retry', 'mutate']",
    "mutate_count": "int",
    "manifest_delta": "Optional[dict]",
}


class EventStream:
    """Manage connected WebSocket clients and broadcast kernel events.

    The stream subscribes to the bus's ``add_event_listener`` hook on
    startup, so every kernel event flows through here. Clients are
    stored in an in-memory set guarded by an :class:`asyncio.Lock`.

    Public surface:

    * :meth:`attach` — wire up the bus listener.
    * :meth:`add_client` — handle a new WebSocket connection (called
    from the FastAPI endpoint).
    * :meth:`remove_client` — explicit removal (used by tests and
    graceful shutdowns).
    * :meth:`broadcast` — push a stream event to all clients.
    * :meth:`filter_and_broadcast` — broadcast with per-client
    filtering based on the client's ``subscribe`` query string.
    """

    def __init__(
        self,
        introspection: IntrospectionAPI,
        *,
        heartbeat_interval_seconds: float = 5.0,
    ) -> None:
        self._introspection = introspection
        self._heartbeat = heartbeat_interval_seconds
        self._clients: set[WebSocket] = set()
        self._client_filters: dict[WebSocket, set[str] | None] = {}
        self._lock = asyncio.Lock()
        self._listener_registered = False
        self._bus: MessageBus | None = None
        self._metrics_task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()
        self._started = False
        self._send_queue: dict[WebSocket, asyncio.Queue[str]] = {}

    # -- lifecycle --------------------------------------------------------

    async def attach(self, bus: MessageBus) -> None:
        """Subscribe to the bus's event listener hook.

        Idempotent; safe to call multiple times. After this call, any
        kernel-emitted event envelope flows through
        :meth:`filter_and_broadcast`.
        """
        if self._listener_registered:
            return
        self._bus = bus
        bus.add_event_listener(self._on_bus_event)
        self._listener_registered = True
        logger.info("EventStream attached to bus")

    async def start(self) -> None:
        """Spawn the periodic metrics pusher. Idempotent."""
        if self._started:
            return
        self._stop_event.clear()
        self._metrics_task = asyncio.create_task(
            self._metrics_loop(), name="dashboard-metrics-pusher"
        )
        self._started = True

    async def stop(self) -> None:
        """Cancel the metrics task. Idempotent."""
        if not self._started:
            return
        self._stop_event.set()
        if self._metrics_task is not None:
            self._metrics_task.cancel()
            try:
                await self._metrics_task
            except (asyncio.CancelledError, Exception):
                pass
            self._metrics_task = None
        async with self._lock:
            clients = list(self._clients)
            self._clients.clear()
            self._client_filters.clear()
        for ws in clients:
            try:
                await ws.close()
            except Exception:
                pass
        self._started = False

    # -- client management -----------------------------------------------

    async def add_client(
        self,
        websocket: WebSocket,
        *,
        subscribe: list[str] | None = None,
        _accept: bool = True,
    ) -> None:
        """Accept ``websocket`` and start serving it.

        Sends the initial snapshot, then enters a read loop that
        consumes (and discards) client messages. The client's only
        outgoing path is server-pushed events.

        Args:
        websocket: The accepted FastAPI WebSocket.
        subscribe: Optional list of envelope_type names to filter
        on. ``None`` means "no filter — give me everything".
        _accept: Whether to call ``websocket.accept()``. Set to
        ``False`` when the caller has already accepted the socket
        (e.g. the FastAPI endpoint handler).
        """
        if _accept:
            await websocket.accept()
        filter_set = {s.strip() for s in subscribe} if subscribe else None
        async with self._lock:
            self._clients.add(websocket)
            self._client_filters[websocket] = filter_set

        try:
            snapshot = await self._build_snapshot()
            await websocket.send_text(
                json.dumps({"type": "snapshot", "data": snapshot}, default=str)
            )
        except Exception as exc:
            logger.warning("failed to send initial snapshot: %s", exc)
            await self.remove_client(websocket)
            return

        try:
            while True:
                try:
                    raw = await asyncio.wait_for(
                        websocket.receive_text(), timeout=30.0
                    )
                    data = json.loads(raw)
                    cmd = data.get("type", "")
                    if cmd == "ping":
                        await websocket.send_text(
                            json.dumps({"type": "pong"})
                        )
                except asyncio.TimeoutError:
                    try:
                        await websocket.send_text(
                            json.dumps({"type": "heartbeat"})
                        )
                    except Exception:
                        break
        except WebSocketDisconnect:
            pass
        except Exception:
            pass
        finally:
            await self.remove_client(websocket)

    @property
    def client_count(self) -> int:
        """Number of currently connected WebSocket clients."""
        return len(self._clients)

    async def remove_client(self, websocket: WebSocket) -> None:
        """Remove a WebSocket from the client set.

        Safe to call even if the websocket is not in the set.
        """
        async with self._lock:
            self._clients.discard(websocket)
            self._client_filters.pop(websocket, None)
        self._send_queue.pop(websocket, None)

    # -- event handling ---------------------------------------------------

    async def _on_bus_event(self, envelope: Envelope) -> None:
        """Called by the bus for every kernel event."""
        await self.filter_and_broadcast(envelope)

    async def filter_and_broadcast(
        self, envelope: Envelope
    ) -> None:
        """Fan out ``envelope`` to every client whose filter matches.

        The event name used for filtering is extracted from
        ``envelope.payload.data.event`` (e.g. ``"agent_zombie"``); the
        bare ``envelope_type`` is always ``"event"`` and is not useful
        for per-event subscription.
        """
        ev_type = self._extract_event_name(envelope)
        async with self._lock:
            targets = [
                ws
                for ws, flt in self._client_filters.items()
                if flt is None or (ev_type is not None and ev_type in flt)
            ]

        payload = json.dumps(
            {"type": "envelope", "event_name": ev_type, "data": envelope.model_dump(mode="json")},
            default=str,
        )
        for ws in targets:
            try:
                if ws in self._send_queue:
                    await self._send_queue[ws].put(payload)
                else:
                    await ws.send_text(payload)
            except Exception:
                pass

    async def broadcast(self, payload: str | dict[str, Any]) -> None:
        """Send ``payload`` to every connected client.

        ``payload`` may be a pre-serialised string or a dict that the
        stream will JSON-encode for the caller.
        """
        if not isinstance(payload, str):
            payload = json.dumps(payload, default=str)
        async with self._lock:
            clients = list(self._clients)

        for ws in clients:
            try:
                if ws in self._send_queue:
                    await self._send_queue[ws].put(payload)
                else:
                    await ws.send_text(payload)
            except Exception:
                pass

    @staticmethod
    def _extract_event_name(envelope: Envelope) -> str | None:
        """Best-effort: pull the kernel event name from an envelope."""
        try:
            payload = envelope.payload
        except AttributeError:
            return None
        # ``payload`` may be a Pydantic model or a plain dict depending
        # on whether the envelope came from a tool that round-tripped
        # through ``model_dump``.  Accept both.
        if payload is None:
            return None
        data: Any
        if isinstance(payload, dict):
            data = payload.get("data")
        else:
            data = getattr(payload, "data", None)
        if isinstance(data, dict):
            ev = data.get("event")
            if isinstance(ev, str):
                return ev
        return getattr(envelope, "envelope_type", None)

    # -- Phase 9 helpers --------------------------------------------------

    @staticmethod
    def is_phase9_event(name: str | None) -> bool:
        """True if ``name`` is one of the self-healing event types."""
        if not name:
            return False
        return name in PHASE9_EVENT_NAMES

    @staticmethod
    def build_loop_detected_payload(
        *,
        agent_id: str,
        pattern: str,
        consecutive_count: int,
        recent_envelopes: list[dict[str, Any]] | None = None,
        suggested_action: str = "",
        detected_at: str = "",
    ) -> dict[str, Any]:
        """Build a ``loop_detected`` payload dict for test fixtures."""
        from datetime import datetime, timezone
        return {
            "event": LOOP_DETECTED_EVENT,
            "agent_id": agent_id,
            "pattern": pattern,
            "consecutive_count": int(consecutive_count),
            "recent_envelopes": list(recent_envelopes or []),
            "suggested_action": suggested_action,
            "detected_at": detected_at or datetime.now(timezone.utc)
            .isoformat()
            .replace("+00:00", "Z"),
        }

    @staticmethod
    def build_budget_exhausted_payload(
        *,
        workflow_id: str,
        step_id: str,
        cost_so_far: float,
        budget: float,
        currency: str = "USD",
        remaining_usd: float | None = None,
    ) -> dict[str, Any]:
        """Build a ``budget_exhausted`` payload dict for test fixtures."""
        if remaining_usd is None:
            remaining_usd = max(0.0, float(budget) - float(cost_so_far))
        return {
            "event": BUDGET_EXHAUSTED_EVENT,
            "workflow_id": workflow_id,
            "step_id": step_id,
            "cost_so_far": round(float(cost_so_far), 6),
            "budget": round(float(budget), 6),
            "currency": currency,
            "remaining_usd": round(float(remaining_usd), 6),
        }

    @staticmethod
    def build_workflow_resume_payload(
        *,
        workflow_id: str,
        checkpoint: dict[str, Any] | None = None,
        last_step: str | None = None,
        resumed_state: dict[str, Any] | None = None,
        previous_status: str = "running",
    ) -> dict[str, Any]:
        """Build a ``workflow_resume`` payload dict for test fixtures."""
        from datetime import datetime, timezone
        return {
            "event": WORKFLOW_RESUME_EVENT,
            "workflow_id": workflow_id,
            "checkpoint": checkpoint,
            "last_step": last_step,
            "resumed_state": dict(resumed_state or {}),
            "resumed_at": datetime.now(timezone.utc)
            .isoformat()
            .replace("+00:00", "Z"),
            "previous_status": previous_status,
        }

    # -- internal ---------------------------------------------------------

    async def _build_snapshot(self) -> dict:
        """Return a serializable snapshot of current system state."""
        try:
            metrics = await self._introspection.get_system_metrics()
            metrics = metrics.model_dump(mode="json")
            agents = await self._introspection.get_agents()
            agents = [a.model_dump(mode="json") for a in agents]
        except Exception as exc:
            logger.warning("snapshot: introspection failed: %s", exc)
            metrics = {}
            agents = []

        return {
            "metrics": metrics,
            "agents": agents,
        }

    async def _metrics_loop(self) -> None:
        """Every 30 s push a fresh snapshot to every client."""
        while True:
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(), timeout=30.0
                )
                return
            except asyncio.TimeoutError:
                pass

            snapshot = await self._build_snapshot()
            payload = json.dumps(
                {"type": "snapshot", "data": snapshot}, default=str
            )
            await self.broadcast(payload)
