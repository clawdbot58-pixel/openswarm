"""The kernel's central message bus.

The bus is the **only** router in the swarm. It accepts envelopes, validates
them, runs the permission enforcer, and delivers them to the right agent
queue or live WebSocket subscriber.

Routing model
-------------
* Incoming :class:`Envelope` objects land on a **priority heap** (``heapq``).
* A background **router task** pops envelopes one at a time, checks TTL,
  resolves recipients, and either delivers to a connected subscriber
  callback or pushes onto a per-agent ``asyncio.Queue``.
* A subscriber is a coroutine the WebSocket layer registers when an agent
  connects. While a subscriber is present, envelopes go straight through;
  when it disconnects, envelopes accumulate in the per-agent queue so the
  agent can drain on reconnect.
* If the per-agent queue exceeds :attr:`KernelSettings.bus_max_queue_size`,
  the oldest envelope is dropped and a ``queue_overflow`` event is emitted
  to the main agent.

Delivery modes
--------------
* **direct**   — ``receiver.agent_id`` matches a registered agent
* **broadcast**— ``receiver.agent_id == "*"`` expands to every registered
                 agent
* **response** — ``reply_to`` carries the original envelope id; the bus
                 re-resolves the recipient from the original envelope's
                 sender (not implemented as a separate path: the response
                 is just a regular direct envelope whose
                 ``receiver.agent_id`` is the original sender).

The bus never inspects or generates natural-language output. It is pure
infrastructure.
"""
from __future__ import annotations

import asyncio
import heapq
import itertools
import logging
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Iterable

from pydantic import ValidationError as PydanticValidationError

from .config import KernelSettings
from .exceptions import (
    EnvelopeRejected,
    ExpiredEnvelope,
    PermissionDenied,
    QueueOverflow,
    RoutingError,
)
from .models import (
    AgentIdStr,
    Endpoint,
    Envelope,
    EnvelopeMetadata,
    Preamble,
    UUID4Str,
)
from .registry import AgentRegistry

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------

@dataclass
class BusMetrics:
    """Counters exposed via ``GET /metrics``."""

    envelopes_received: int = 0
    envelopes_routed: int = 0
    envelopes_dropped_invalid: int = 0
    envelopes_dropped_expired: int = 0
    envelopes_dropped_permission: int = 0
    envelopes_dropped_overflow: int = 0
    permission_denials: int = 0
    queue_overflows: int = 0
    zombies_detected: int = 0
    agents_registered: int = 0
    agents_unregistered: int = 0
    events_emitted: int = 0
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable snapshot of all counters."""
        return {
            "envelopes_received": self.envelopes_received,
            "envelopes_routed": self.envelopes_routed,
            "envelopes_dropped_invalid": self.envelopes_dropped_invalid,
            "envelopes_dropped_expired": self.envelopes_dropped_expired,
            "envelopes_dropped_permission": self.envelopes_dropped_permission,
            "envelopes_dropped_overflow": self.envelopes_dropped_overflow,
            "permission_denials": self.permission_denials,
            "queue_overflows": self.queue_overflows,
            "zombies_detected": self.zombies_detected,
            "agents_registered": self.agents_registered,
            "agents_unregistered": self.agents_unregistered,
            "events_emitted": self.events_emitted,
            "started_at": self.started_at.isoformat().replace("+00:00", "Z"),
        }


# ---------------------------------------------------------------------------
# Subscriber protocol
# ---------------------------------------------------------------------------

SubscriberCallback = Callable[[Envelope], Awaitable[None]]
"""Coroutine the WebSocket layer registers to receive envelopes."""


# ---------------------------------------------------------------------------
# Message bus
# ---------------------------------------------------------------------------

# A small dataclass to keep the heap entries self-documenting.
@dataclass(order=False)
class _HeapEntry:
    priority: int
    seq: int
    envelope: Envelope

    def __lt__(self, other: "_HeapEntry") -> bool:
        # Higher priority first; ties broken by FIFO.
        if self.priority != other.priority:
            return self.priority > other.priority
        return self.seq < other.seq


class MessageBus:
    """Priority-queue message bus with per-agent delivery queues.

    Parameters
    ----------
    registry
        The :class:`AgentRegistry` used to expand broadcast recipients and
        look up sender manifests for permission checks.
    permissions
        The :class:`~kernel.permissions.PermissionEnforcer` invoked before
        every delivery. May be ``None`` only in unit tests that opt out.
    settings
        Kernel configuration.
    """

    def __init__(
        self,
        registry: AgentRegistry,
        permissions: Any | None,  # PermissionEnforcer (avoid circular import)
        settings: KernelSettings,
    ) -> None:
        self._registry = registry
        self._permissions = permissions
        self._settings = settings

        # Heap of pending envelopes (priority, FIFO, payload).
        self._heap: list[_HeapEntry] = []
        self._heap_lock = asyncio.Lock()
        self._heap_event = asyncio.Event()
        self._counter = itertools.count()

        # Per-agent state.
        self._agent_queues: dict[AgentIdStr, asyncio.Queue[Envelope]] = {}
        self._subscribers: dict[AgentIdStr, SubscriberCallback] = {}
        self._agent_locks: dict[AgentIdStr, asyncio.Lock] = defaultdict(asyncio.Lock)

        # Router lifecycle.
        self._router_task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()
        self._started = False

        # Metrics and event hooks.
        self.metrics = BusMetrics()
        self._event_listeners: list[SubscriberCallback] = []

    # -- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        """Spawn the background router task. Idempotent."""
        if self._started:
            return
        self._stop_event.clear()
        self._router_task = asyncio.create_task(
            self._router_loop(), name="kernel-bus-router"
        )
        self._started = True
        logger.info("bus started poll_interval=%s", self._settings.bus_router_poll_interval_seconds)

    async def stop(self) -> None:
        """Cancel the router and wait for it to exit. Idempotent."""
        if not self._started:
            return
        self._stop_event.set()
        self._heap_event.set()  # wake the router
        if self._router_task is not None:
            self._router_task.cancel()
            try:
                await self._router_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._router_task = None
        self._started = False
        logger.info("bus stopped")

    # -- subscribers -------------------------------------------------------

    def add_event_listener(self, cb: SubscriberCallback) -> None:
        """Register a callback to receive every kernel-emitted event envelope.

        The WebSocket layer uses this to stream system events to the
        dashboard or to the main agent.
        """
        self._event_listeners.append(cb)

    async def register_subscriber(
        self,
        agent_id: AgentIdStr,
        callback: SubscriberCallback,
    ) -> None:
        """Attach a live subscriber for ``agent_id`` and drain its queue.

        Existing queued envelopes are flushed in priority order before the
        subscriber starts receiving new ones. The flush is best-effort:
        if the callback raises, the offending envelope is dropped and the
        loop continues.
        """
        async with self._agent_locks[agent_id]:
            self._subscribers[agent_id] = callback
            await self._registry.set_connected(agent_id, True)
            await self._flush_queue(agent_id)

    async def unregister_subscriber(self, agent_id: AgentIdStr) -> None:
        """Detach the subscriber. Queue contents are retained."""
        async with self._agent_locks[agent_id]:
            self._subscribers.pop(agent_id, None)
            try:
                await self._registry.set_connected(agent_id, False)
            except Exception:  # noqa: BLE001 — agent may be gone
                logger.debug("set_connected(False) failed for %s", agent_id)

    def has_subscriber(self, agent_id: AgentIdStr) -> bool:
        """True if a live subscriber is attached for ``agent_id``."""
        return agent_id in self._subscribers

    # -- send API ----------------------------------------------------------

    async def send(self, envelope: Envelope | dict[str, Any]) -> Envelope:
        """Inject an envelope onto the bus.

        Accepts either a :class:`Envelope` (preferred) or a raw dict (which
        is validated against :class:`Envelope`).

        :raises EnvelopeRejected: when the dict fails validation. The
            rejection is also emitted to the main agent as a system event.
        """
        if not isinstance(envelope, Envelope):
            try:
                envelope = Envelope.model_validate(envelope)
            except PydanticValidationError as exc:
                self.metrics.envelopes_dropped_invalid += 1
                await self._emit_to_main(
                    "envelope_rejected",
                    {
                        "reason": "schema_validation_failed",
                        "errors": exc.errors(include_url=False),
                    },
                )
                raise EnvelopeRejected(
                    "envelope failed schema validation",
                    errors=exc.errors(include_url=False),
                ) from exc

        self.metrics.envelopes_received += 1

        # Permission check (only for tool payloads from registered senders).
        if (
            self._permissions is not None
            and envelope.payload.content_type == "tool"
            and envelope.sender.agent_id != "kernel"
            and envelope.sender.role != "kernel"
        ):
            allowed = await self._permissions.check(envelope, self._registry)
            if not allowed:
                self.metrics.envelopes_dropped_permission += 1
                self.metrics.permission_denials += 1
                # The enforcer already emits permission_denied to sender + main.
                raise PermissionDenied(
                    "envelope blocked by permission enforcer",
                    agent_id=envelope.sender.agent_id,
                    envelope_id=envelope.envelope_id,
                )

        async with self._heap_lock:
            entry = _HeapEntry(
                priority=envelope.metadata.priority,
                seq=next(self._counter),
                envelope=envelope,
            )
            heapq.heappush(self._heap, entry)
            self._heap_event.set()
        return envelope

    async def emit_event(
        self,
        event_type: str,
        details: dict[str, Any] | None = None,
        *,
        recipient: AgentIdStr = "main-agent",
        sender: Endpoint | None = None,
    ) -> Envelope:
        """Build and send a system event envelope from the kernel.

        Used by the bus itself, the heartbeat monitor, and the permission
        enforcer to push notifications to the main agent. The bus does
        **not** permission-check its own outbound events.

        The envelope's ``envelope_type`` is always ``"event"`` (the only
        valid literal in :class:`Envelope`); the specific kernel event name
        (``"agent_zombie"``, ``"permission_denied"``, …) is placed in
        ``payload.data.event`` so consumers can dispatch on it without
        violating the contract schema.
        """
        from_kernel = sender or Endpoint(agent_id="kernel", role="kernel")
        data = dict(details or {})
        data.setdefault("event", event_type)
        env = Envelope(
            envelope_id=str(uuid.uuid4()),
            created_at=datetime.now(timezone.utc),
            envelope_type="event",
            sender=from_kernel,
            receiver=Endpoint(agent_id=recipient, role="orchestrator"),
            preamble=Preamble(
                intent={"goal": "system_event", "phase": "execution"},
            ),
            payload={"content_type": "data", "data": data},
            metadata=EnvelopeMetadata(priority=8),
        )
        self.metrics.events_emitted += 1
        await self._emit_to_main(event_type, details)
        await self.send(env)
        return env

    # -- router loop -------------------------------------------------------

    async def _router_loop(self) -> None:
        """Background task: pop heap entries, resolve, deliver."""
        try:
            while not self._stop_event.is_set():
                entry = await self._take_next()
                if entry is None:
                    # Woken spuriously or shutting down.
                    if self._stop_event.is_set():
                        return
                    continue
                envelope = entry.envelope
                # TTL check at pop time.
                if envelope.is_expired:
                    self.metrics.envelopes_dropped_expired += 1
                    logger.debug(
                        "dropping expired envelope_id=%s", envelope.envelope_id
                    )
                    continue
                try:
                    recipients = await self._resolve_recipients(envelope)
                except RoutingError as exc:
                    logger.warning(
                        "routing failed envelope_id=%s: %s",
                        envelope.envelope_id,
                        exc,
                    )
                    self.metrics.envelopes_dropped_invalid += 1
                    continue
                for agent_id in recipients:
                    await self._deliver(agent_id, envelope)
                self.metrics.envelopes_routed += 1
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("bus router crashed; restarting in 1s")
            await asyncio.sleep(1.0)
            if not self._stop_event.is_set():
                self._router_task = asyncio.create_task(
                    self._router_loop(), name="kernel-bus-router"
                )

    async def _take_next(self) -> _HeapEntry | None:
        """Block until an envelope is available, then return it."""
        while not self._stop_event.is_set():
            async with self._heap_lock:
                if self._heap:
                    return heapq.heappop(self._heap)
            # Wait for a signal or a poll interval, whichever comes first.
            try:
                await asyncio.wait_for(
                    self._heap_event.wait(),
                    timeout=self._settings.bus_router_poll_interval_seconds,
                )
            except asyncio.TimeoutError:
                pass
            self._heap_event.clear()
        return None

    # -- recipient resolution ---------------------------------------------

    async def _resolve_recipients(self, envelope: Envelope) -> list[AgentIdStr]:
        """Compute the set of agent_ids that should receive ``envelope``."""
        # Response routing: route to the original sender via reply_to.
        # We resolve reply_to against the per-agent history cache.
        if envelope.reply_to is not None:
            target = self._lookup_reply_target(envelope.reply_to)
            if target is None:
                raise RoutingError(
                    f"reply_to target {envelope.reply_to!r} not found",
                    envelope_id=envelope.envelope_id,
                )
            return [target]

        # Broadcast expansion.
        if envelope.is_broadcast():
            logger.debug("broadcast resolve sender=%s", envelope.sender.agent_id)
            ids = await self._registry.all_ids()
            logger.debug("broadcast ids=%s", ids)
            # Don't echo a broadcast back to the sender.
            return [i for i in ids if i != envelope.sender.agent_id]

        # Direct delivery.
        agent_id = envelope.receiver.agent_id
        try:
            await self._registry.get_status(agent_id)
        except Exception as exc:  # AgentNotFound or similar
            raise RoutingError(
                f"recipient {agent_id!r} not registered",
                envelope_id=envelope.envelope_id,
            ) from exc
        return [agent_id]

    # -- reply history -----------------------------------------------------

    # Maximum number of (original_envelope_id, recipient) pairs we remember
    # for reply routing. Bound the memory footprint of long-running swarms.
    _REPLY_HISTORY_MAX: int = 4096

    def _ensure_reply_history(self) -> None:
        """Idempotently install the reply-history cache.

        Lives in its own method so the cache can be initialised after
        ``__init__`` has run, but still early enough that every code path
        that touches the cache (delivery, recipient resolution) sees it.
        """
        if not hasattr(self, "_reply_history"):
            self._reply_history: dict[UUID4Str, AgentIdStr] = {}
            self._reply_order: deque[UUID4Str] = deque(maxlen=self._REPLY_HISTORY_MAX)

    def _remember_reply(
        self, envelope_id: UUID4Str, recipient: AgentIdStr
    ) -> None:
        self._ensure_reply_history()
        self._reply_history[envelope_id] = recipient
        self._reply_order.append(envelope_id)
        while len(self._reply_history) > self._REPLY_HISTORY_MAX:
            old = self._reply_order.popleft()
            self._reply_history.pop(old, None)

    def _lookup_reply_target(self, envelope_id: UUID4Str) -> AgentIdStr | None:
        self._ensure_reply_history()
        return self._reply_history.get(envelope_id)

    # -- delivery ----------------------------------------------------------

    async def _deliver(self, agent_id: AgentIdStr, envelope: Envelope) -> None:
        """Deliver ``envelope`` to ``agent_id`` (subscriber or queue)."""
        # Record the original sender for reply routing BEFORE delivery so
        # a synchronous reply (or one that races the delivery) finds it.
        self._remember_reply(envelope.envelope_id, envelope.sender.agent_id)
        subscriber = self._subscribers.get(agent_id)
        if subscriber is not None:
            try:
                await subscriber(envelope)
                return
            except Exception:  # noqa: BLE001 — never let a bad subscriber kill the bus
                logger.exception(
                    "subscriber raised delivering to %s; falling back to queue",
                    agent_id,
                )
        await self._enqueue(agent_id, envelope)

    async def _enqueue(self, agent_id: AgentIdStr, envelope: Envelope) -> None:
        """Push onto the per-agent queue, dropping oldest on overflow."""
        queue = self._agent_queues.setdefault(agent_id, asyncio.Queue())
        if queue.qsize() >= self._settings.bus_max_queue_size:
            # Drop the oldest (FIFO) — we do not have random access into the
            # underlying queue, so we drain one and re-queue everything.
            await self._drop_oldest_and_notify(agent_id, envelope)
            return
        await queue.put(envelope)

    async def _drop_oldest_and_notify(
        self, agent_id: AgentIdStr, incoming: Envelope
    ) -> None:
        """Pop the oldest envelope for ``agent_id`` and emit a queue_overflow event.

        The ``incoming`` envelope is then enqueued in its place.
        """
        queue = self._agent_queues.get(agent_id)
        if queue is None:
            return
        dropped: Envelope | None = None
        try:
            dropped = queue.get_nowait()
        except asyncio.QueueEmpty:
            pass
        await queue.put(incoming)
        self.metrics.envelopes_dropped_overflow += 1
        self.metrics.queue_overflows += 1
        logger.warning(
            "queue overflow for agent_id=%s size=%d dropped=%s",
            agent_id,
            queue.qsize(),
            getattr(dropped, "envelope_id", None),
        )
        await self._emit_to_main(
            "queue_overflow",
            {
                "agent_id": agent_id,
                "queue_size": queue.qsize(),
                "dropped_envelope_id": getattr(dropped, "envelope_id", None),
            },
        )

    async def _flush_queue(self, agent_id: AgentIdStr) -> None:
        """Drain the per-agent queue through the live subscriber.

        Stops on the first delivery failure (subscriber exception) so the
        caller can re-attach and retry. TTL is checked at drain time.
        """
        queue = self._agent_queues.get(agent_id)
        if queue is None:
            return
        subscriber = self._subscribers.get(agent_id)
        if subscriber is None:
            return
        flushed = 0
        while not queue.empty():
            env = queue.get_nowait()
            if env.is_expired:
                self.metrics.envelopes_dropped_expired += 1
                continue
            try:
                await subscriber(env)
            except Exception:  # noqa: BLE001
                # Put the envelope back at the head and bail.
                # asyncio.Queue lacks put_front; we re-insert by
                # constructing a new queue. This is fine for small backlogs.
                new_q: asyncio.Queue[Envelope] = asyncio.Queue()
                await new_q.put(env)
                while not queue.empty():
                    await new_q.put(queue.get_nowait())
                self._agent_queues[agent_id] = new_q
                logger.exception("flush failed mid-drain for %s", agent_id)
                return
            flushed += 1
        if flushed:
            logger.debug("flushed %d envelopes to %s", flushed, agent_id)

    # -- helpers -----------------------------------------------------------

    def queue_size(self, agent_id: AgentIdStr) -> int:
        """Return the current size of ``agent_id``'s queue (0 if unknown)."""
        q = self._agent_queues.get(agent_id)
        return 0 if q is None else q.qsize()

    def total_queued(self) -> int:
        """Sum of all per-agent queue sizes."""
        return sum(q.qsize() for q in self._agent_queues.values())

    async def _emit_to_main(
        self, event_type: str, details: dict[str, Any]
    ) -> None:
        """Send a system event to the main agent and any event listeners.

        The event name lives in ``payload.data.event``; ``envelope_type``
        is always ``"event"`` to comply with the envelope contract.
        """
        # Notify in-process listeners first (used by the WS layer for the
        # dashboard stream).
        data = dict(details or {})
        data.setdefault("event", event_type)
        env = Envelope(
            envelope_id=str(uuid.uuid4()),
            created_at=datetime.now(timezone.utc),
            envelope_type="event",
            sender=Endpoint(agent_id="kernel", role="kernel"),
            receiver=Endpoint(
                agent_id=self._settings.main_agent_id, role="orchestrator"
            ),
            preamble=Preamble(
                intent={"goal": "system_event", "phase": "execution"},
            ),
            payload={"content_type": "data", "data": data},
            metadata=EnvelopeMetadata(priority=9),
        )
        self.metrics.events_emitted += 1
        for listener in list(self._event_listeners):
            try:
                await listener(env)
            except Exception:  # noqa: BLE001
                logger.exception("event listener failed")
        # Also try to push it onto the main agent's queue (or to its
        # subscriber if connected).
        await self._deliver(self._settings.main_agent_id, env)


# ---------------------------------------------------------------------------
# Reply-history init shim
# ---------------------------------------------------------------------------

# The double-underscore name-mangling trick above is correct but a little
# obscure. We expose a public method to install the cache for tests that
# construct a MessageBus via __new__.
def _ensure_reply_history(bus: MessageBus) -> None:
    bus.__init_reply_history()  # type: ignore[attr-defined]


__all__ = [
    "BusMetrics",
    "MessageBus",
    "SubscriberCallback",
]
