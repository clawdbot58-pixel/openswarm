"""Tests for the :class:`~kernel.bus.MessageBus`.

Covers:

* routing 100 envelopes without loss
* priority ordering at delivery time
* TTL expiration
* broadcast expansion (with sender excluded)
* rejection of invalid envelopes
* queue overflow + ``queue_overflow`` event
"""
from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta, timezone

import pytest

from kernel.bus import MessageBus
from kernel.exceptions import EnvelopeRejected, PermissionDenied
from kernel.models import (
    AgentManifest,
    Endpoint,
    Envelope,
    Preamble,
    StatusLiteral,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _register(harness, *agents: AgentManifest) -> None:
    for a in agents:
        await harness.registry.register(a)


def _manifest(agent_id: str, role: str = "executor") -> AgentManifest:
    return AgentManifest.model_validate(
        {
            "agent_id": agent_id,
            "version": "1.0.0",
            "role": role,
            "intent": f"test {agent_id}",
            "capabilities": {"inference": {"provider": "anthropic"}},
            "lifecycle": {"persistence": "ephemeral"},
            "registration_time": datetime.now(timezone.utc)
            .isoformat()
            .replace("+00:00", "Z"),
        }
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_register_and_send_100_envelopes_routes_correctly(kernel_test):
    """100 envelopes to a single agent all reach the per-agent queue."""
    await _register(
        kernel_test,
        _manifest("main-agent", "orchestrator"),
        _manifest("coder", "executor"),
    )
    sent_ids = []
    for i in range(100):
        env = kernel_test.make_envelope(
            sender_id="main-agent",
            receiver_id="coder",
            content=f"msg {i}",
        )
        sent_ids.append(env.envelope_id)
        await kernel_test.bus.send(env)
    # Let the router drain.
    for _ in range(20):
        if kernel_test.bus.queue_size("coder") == 100:
            break
        await asyncio.sleep(0.02)
    assert kernel_test.bus.queue_size("coder") == 100
    # Drain in subscriber order and check no loss.
    received: list[str] = []
    async def sub(env: Envelope) -> None:
        received.append(env.envelope_id)

    await kernel_test.bus.register_subscriber("coder", sub)
    # All 100 should have been flushed.
    await asyncio.sleep(0.1)
    assert sorted(received) == sorted(sent_ids)
    # Metrics.
    assert kernel_test.bus.metrics.envelopes_received == 100
    assert kernel_test.bus.metrics.envelopes_routed == 100


@pytest.mark.asyncio
async def test_priority_ordering_at_drain(kernel_test):
    """Higher-priority envelopes are delivered before lower-priority ones."""
    await _register(
        kernel_test,
        _manifest("main-agent", "orchestrator"),
        _manifest("coder", "executor"),
    )
    # Send 10 envelopes with priorities 0..9 in ascending order.
    for prio in range(10):
        await kernel_test.bus.send(
            kernel_test.make_envelope(
                sender_id="main-agent",
                receiver_id="coder",
                content=f"prio {prio}",
                priority=prio,
            )
        )
    # Let the router push them into the queue (in heap-pop order).
    for _ in range(20):
        if kernel_test.bus.queue_size("coder") == 10:
            break
        await asyncio.sleep(0.02)
    received: list[int] = []
    async def sub(env: Envelope) -> None:
        received.append(env.priority)

    await kernel_test.bus.register_subscriber("coder", sub)
    await asyncio.sleep(0.1)
    # 9, 8, 7, ..., 0
    assert received == list(reversed(range(10)))


@pytest.mark.asyncio
async def test_ttl_expiration(kernel_test):
    """Expired envelopes are dropped before delivery."""
    await _register(
        kernel_test,
        _manifest("main-agent", "orchestrator"),
        _manifest("coder", "executor"),
    )
    # Build an envelope that expired 1 second ago.
    env = kernel_test.make_envelope(
        sender_id="main-agent", receiver_id="coder", content="stale"
    )
    env = env.model_copy(
        update={
            "expires_at": datetime.now(timezone.utc) - timedelta(seconds=1),
        }
    )
    await kernel_test.bus.send(env)
    await asyncio.sleep(0.2)
    assert kernel_test.bus.queue_size("coder") == 0
    assert kernel_test.bus.metrics.envelopes_dropped_expired == 1
    # A non-expired envelope still routes.
    env2 = kernel_test.make_envelope(
        sender_id="main-agent", receiver_id="coder", content="fresh"
    )
    env2 = env2.model_copy(
        update={
            "expires_at": datetime.now(timezone.utc) + timedelta(seconds=60),
        }
    )
    await kernel_test.bus.send(env2)
    for _ in range(20):
        if kernel_test.bus.queue_size("coder") == 1:
            break
        await asyncio.sleep(0.02)
    assert kernel_test.bus.queue_size("coder") == 1


@pytest.mark.asyncio
async def test_broadcast_delivery_excludes_sender(kernel_test):
    """Broadcast fan-out goes to every registered agent except the sender."""
    await _register(
        kernel_test,
        _manifest("main-agent", "orchestrator"),
        _manifest("coder", "executor"),
        _manifest("reviewer", "specialist"),
    )
    await kernel_test.bus.send(
        kernel_test.make_envelope(
            sender_id="main-agent",
            receiver_id="*",
            content="hi everyone",
        )
    )
    for _ in range(20):
        if (
            kernel_test.bus.queue_size("coder") == 1
            and kernel_test.bus.queue_size("reviewer") == 1
        ):
            break
        await asyncio.sleep(0.02)
    assert kernel_test.bus.queue_size("main-agent") == 0
    assert kernel_test.bus.queue_size("coder") == 1
    assert kernel_test.bus.queue_size("reviewer") == 1


@pytest.mark.asyncio
async def test_invalid_envelope_rejected_and_event_emitted(kernel_test):
    """A dict that fails validation is dropped and the kernel logs a system event."""
    await _register(
        kernel_test,
        _manifest("main-agent", "orchestrator"),
    )
    # Build a deliberately broken envelope (envelope_id is not a UUID).
    bad = {
        "envelope_id": "not-a-uuid",
        "created_at": "2026-06-04T10:00:00Z",
        "envelope_type": "request",
        "sender": {"agent_id": "main-agent", "role": "orchestrator"},
        "receiver": {"agent_id": "coder", "role": "executor"},
        "preamble": {"intent": {"goal": "x", "phase": "execution"}},
        "payload": {"content_type": "text", "content": "hi"},
    }
    with pytest.raises(EnvelopeRejected):
        await kernel_test.bus.send(bad)  # type: ignore[arg-type]
    assert kernel_test.bus.metrics.envelopes_dropped_invalid == 1


@pytest.mark.asyncio
async def test_queue_overflow_emits_event(kernel_test):
    """A queue over the cap drops the oldest and emits queue_overflow."""
    # Set a tiny cap for this test.
    kernel_test.bus._settings.bus_max_queue_size = 3
    await _register(
        kernel_test,
        _manifest("main-agent", "orchestrator"),
        _manifest("coder", "executor"),
    )
    # Subscribe so the bus goes through the queue-on-overflow path
    # (without a subscriber, overflow never fires because the subscriber
    # path is taken first).
    # Actually: with no subscriber, the bus enqueues. The overflow fires
    # only when the queue is full. So this test is correct as-is.
    # Send 5 envelopes — the first 3 enqueue, the 4th and 5th overflow.
    received_events: list[Envelope] = []
    kernel_test.bus.add_event_listener(received_events.append)
    for i in range(5):
        await kernel_test.bus.send(
            kernel_test.make_envelope(
                sender_id="main-agent",
                receiver_id="coder",
                content=f"msg {i}",
            )
        )
    # Wait for the router to process all 5.
    for _ in range(50):
        if kernel_test.bus.metrics.envelopes_dropped_overflow >= 2:
            break
        await asyncio.sleep(0.02)
    assert kernel_test.bus.metrics.envelopes_dropped_overflow >= 2
    # At least one queue_overflow event should have been emitted.
    overflow_events = [
        e
        for e in received_events
        if e.payload.data.get("event") == "queue_overflow"  # type: ignore[union-attr]
    ]
    assert overflow_events, "no queue_overflow event captured"


@pytest.mark.asyncio
async def test_reply_routing(kernel_test):
    """reply_to causes the envelope to route to the original sender."""
    await _register(
        kernel_test,
        _manifest("main-agent", "orchestrator"),
        _manifest("coder", "executor"),
    )
    original = kernel_test.make_envelope(
        sender_id="main-agent", receiver_id="coder", content="question"
    )
    await kernel_test.bus.send(original)
    # Wait for original to be routed (delivery remembers sender).
    for _ in range(20):
        if kernel_test.bus.queue_size("coder") == 1:
            break
        await asyncio.sleep(0.02)
    reply = kernel_test.make_envelope(
        sender_id="coder",
        receiver_id="ignored",  # ignored when reply_to is set
        content="answer",
        envelope_type="response",
        reply_to=original.envelope_id,
    )
    await kernel_test.bus.send(reply)
    for _ in range(20):
        if kernel_test.bus.queue_size("main-agent") == 1:
            break
        await asyncio.sleep(0.02)
    # Reply ended up in main-agent's queue, not coder's or "ignored".
    assert kernel_test.bus.queue_size("main-agent") == 1
    assert kernel_test.bus.queue_size("coder") == 1  # original stayed
    assert kernel_test.bus.queue_size("ignored") == 0


@pytest.mark.asyncio
async def test_subscriber_delivers_inline(kernel_test):
    """With a live subscriber, envelopes bypass the per-agent queue."""
    await _register(
        kernel_test,
        _manifest("main-agent", "orchestrator"),
        _manifest("coder", "executor"),
    )
    received: list[str] = []
    async def sub(env: Envelope) -> None:
        received.append(env.payload.content)  # type: ignore[union-attr]

    await kernel_test.bus.register_subscriber("coder", sub)
    await kernel_test.bus.send(
        kernel_test.make_envelope(
            sender_id="main-agent", receiver_id="coder", content="inline"
        )
    )
    for _ in range(20):
        if received:
            break
        await asyncio.sleep(0.02)
    assert received == ["inline"]
    # Queue should be empty because delivery was direct.
    assert kernel_test.bus.queue_size("coder") == 0


@pytest.mark.asyncio
async def test_unregister_subscriber_keeps_queue(kernel_test):
    """Detaching a subscriber preserves queued envelopes for the next attach."""
    await _register(
        kernel_test,
        _manifest("main-agent", "orchestrator"),
        _manifest("coder", "executor"),
    )
    # Send two envelopes (they enqueue; no subscriber yet).
    for c in ("a", "b"):
        await kernel_test.bus.send(
            kernel_test.make_envelope(
                sender_id="main-agent", receiver_id="coder", content=c
            )
        )
    for _ in range(20):
        if kernel_test.bus.queue_size("coder") == 2:
            break
        await asyncio.sleep(0.02)
    assert kernel_test.bus.queue_size("coder") == 2
    # Attach subscriber — should drain in arrival order.
    received: list[str] = []
    async def sub(env: Envelope) -> None:
        received.append(env.payload.content)  # type: ignore[union-attr]

    await kernel_test.bus.register_subscriber("coder", sub)
    await asyncio.sleep(0.1)
    assert received == ["a", "b"]
    assert kernel_test.bus.queue_size("coder") == 0
