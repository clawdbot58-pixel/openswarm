"""Tests for :class:`memory.router.MemoryRouter`."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from kernel.models import Endpoint, Envelope, Preamble

from memory.router import MemoryRouter, MemoryRouterError
from memory.temporary import TemporaryMemory, MemoryItem
from memory.persistent import PersistentMemory


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def temp():
    m = TemporaryMemory("test-agent")
    yield m


@pytest.fixture
async def persistent(tmp_path: Path):
    p = PersistentMemory(tmp_path / "memory.db")
    await p.initialize()
    yield p
    await p.close()


@pytest.fixture
def router(temp, persistent) -> MemoryRouter:
    return MemoryRouter(temp, persistent, agent_id="test-agent")


def _envelope(
    *,
    action: str = "memory_write",
    sender_id: str = "agent-1",
    **data: Any,
) -> Envelope:
    """Build a request envelope carrying ``data`` as ``payload.data``.

    The kwargs become top-level keys in the data dict.  This mirrors
    how an agent worker would build the envelope in production.
    """
    data_with_action = {"action": action, **data}
    return Envelope(
        envelope_id=str(uuid4()),
        created_at=datetime.now(timezone.utc),
        envelope_type="request",
        sender=Endpoint(agent_id=sender_id, role="executor"),
        receiver=Endpoint(agent_id="memory-router", role="kernel"),
        preamble=Preamble(
            intent={"goal": "store memory", "phase": "execution"},
        ),
        payload={"content_type": "data", "data": data_with_action},
    )


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_temporary_write_routes_to_temp_store(router, temp):
    env = _envelope(
        persistence="temporary",
        item={"type": "action", "content": "wrote file foo.py"},
    )
    ack = await router.handle_envelope(env)
    assert ack is not None
    assert ack.envelope_type == "response"
    payload_data = ack.payload.data  # type: ignore[attr-defined]
    assert payload_data["event"] == "memory_stored"
    assert payload_data["persistence"] == "temporary"
    assert payload_data["memory_id"] is None
    # The temp store should have the entry.
    assert await temp.size() >= 1


@pytest.mark.asyncio
async def test_persistent_write_routes_to_persistent_store(router, persistent):
    env = _envelope(
        persistence="persistent",
        item={"type": "result", "content": {"x": 1}, "relevance_score": 0.8},
    )
    ack = await router.handle_envelope(env)
    assert ack is not None
    payload_data = ack.payload.data  # type: ignore[attr-defined]
    assert payload_data["persistence"] == "persistent"
    assert payload_data["memory_id"] is not None
    assert payload_data["type"] == "result"
    # The persistent store has the entry.
    items = await persistent.retrieve_recent("test-agent", n=10)
    assert len(items) == 1


@pytest.mark.asyncio
async def test_default_persistence_is_persistent(router, persistent):
    """A missing persistence field defaults to 'persistent'."""
    env = _envelope(item={"type": "context", "content": "hi"})
    ack = await router.handle_envelope(env)
    payload_data = ack.payload.data  # type: ignore[attr-defined]
    assert payload_data["persistence"] == "persistent"


@pytest.mark.asyncio
async def test_non_memory_envelope_returns_none(router):
    """An envelope that isn't a memory_write is passed through."""
    env = _envelope(action="something_else")
    assert await router.handle_envelope(env) is None


@pytest.mark.asyncio
async def test_non_data_envelope_returns_none(router):
    """An envelope with a non-data content_type is ignored."""
    env = Envelope(
        envelope_id=str(uuid4()),
        created_at=datetime.now(timezone.utc),
        envelope_type="request",
        sender=Endpoint(agent_id="agent-1", role="executor"),
        receiver=Endpoint(agent_id="memory-router", role="kernel"),
        preamble=Preamble(
            intent={"goal": "x", "phase": "execution"},
        ),
        payload={"content_type": "text", "content": "hello"},
    )
    assert await router.handle_envelope(env) is None


@pytest.mark.asyncio
async def test_payload_data_must_be_dict(router):
    """If payload.data isn't a dict, the router ignores it."""
    env = Envelope(
        envelope_id=str(uuid4()),
        created_at=datetime.now(timezone.utc),
        envelope_type="request",
        sender=Endpoint(agent_id="agent-1", role="executor"),
        receiver=Endpoint(agent_id="memory-router", role="kernel"),
        preamble=Preamble(
            intent={"goal": "x", "phase": "execution"},
        ),
        payload={"content_type": "data", "data": "not a dict"},
    )
    assert await router.handle_envelope(env) is None


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_invalid_persistence_rejected(router):
    env = _envelope(persistence="flying", item={"type": "action", "content": "x"})
    with pytest.raises(MemoryRouterError):
        await router.handle_envelope(env)


@pytest.mark.asyncio
async def test_invalid_item_type_rejected(router):
    env = _envelope(item={"type": "weird", "content": "x"})
    with pytest.raises(MemoryRouterError):
        await router.handle_envelope(env)


@pytest.mark.asyncio
async def test_invalid_relevance_rejected(router):
    env = _envelope(
        item={"type": "action", "content": "x", "relevance_score": "abc"}
    )
    with pytest.raises(MemoryRouterError):
        await router.handle_envelope(env)


@pytest.mark.asyncio
async def test_non_dict_item_rejected(router):
    env = _envelope(item="not a dict")
    with pytest.raises(MemoryRouterError):
        await router.handle_envelope(env)


# ---------------------------------------------------------------------------
# Tags / workflow / step
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_workflow_and_step_attached(router, persistent):
    env = _envelope(
        persistence="persistent",
        item={"type": "action", "content": "x"},
        workflow_id="wf-1",
        step_id="step-2",
    )
    # The router reads workflow_id and step_id from the envelope's
    # data dict — verify by re-fetching via the persistent store.
    await router.handle_envelope(env)
    items = await persistent.retrieve_by_workflow("wf-1")
    assert len(items) == 1
    assert items[0].step_id == "step-2"


@pytest.mark.asyncio
async def test_tags_attached(router, persistent):
    env = _envelope(
        persistence="persistent",
        item={"type": "action", "content": "x"},
        tags=["alpha", "beta"],
    )
    await router.handle_envelope(env)
    items = await persistent.retrieve_by_tags("test-agent", ["alpha"])
    assert len(items) == 1
    items2 = await persistent.retrieve_by_tags("test-agent", ["gamma"])
    assert items2 == []


# ---------------------------------------------------------------------------
# Direct write() convenience
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_write_convenience_temporary(router, temp):
    stored_id, item = await router.write(
        "test-agent",
        type="decision",
        content={"choice": "A"},
        persistence="temporary",
    )
    assert stored_id is None
    assert isinstance(item, MemoryItem)
    assert item.type == "decision"


@pytest.mark.asyncio
async def test_write_convenience_persistent(router, persistent):
    stored_id, _ = await router.write(
        "test-agent",
        type="result",
        content="done",
        persistence="persistent",
    )
    assert stored_id is not None
    items = await persistent.retrieve_recent("test-agent", n=10)
    assert len(items) == 1


# ---------------------------------------------------------------------------
# Agent id resolution
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_agent_id_falls_back_to_sender():
    """Without an explicit agent_id, the router uses the sender's id."""
    temp = TemporaryMemory("agent-fallback")
    p = PersistentMemory(":memory:")
    await p.initialize()
    try:
        router = MemoryRouter(temp, p)  # no agent_id
        env = _envelope(
            sender_id="agent-fallback",
            persistence="temporary",
            item={"type": "action", "content": "x"},
        )
        ack = await router.handle_envelope(env)
        assert ack is not None
        assert await temp.size() == 1
    finally:
        await p.close()
