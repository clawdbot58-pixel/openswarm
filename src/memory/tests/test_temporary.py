"""Tests for :class:`memory.temporary.TemporaryMemory`."""

from __future__ import annotations

import asyncio
import time

import pytest

from memory.temporary import MemoryItem, TemporaryMemory


# ---------------------------------------------------------------------------
# set / get / delete
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_set_and_get_round_trip():
    """A simple set / get returns the original value."""
    mem = TemporaryMemory("agent-1")
    await mem.set("greeting", "hello")
    assert await mem.get("greeting") == "hello"


@pytest.mark.asyncio
async def test_set_overwrites_existing_key():
    """A second set on the same key replaces the value."""
    mem = TemporaryMemory("agent-1")
    await mem.set("k", "v1")
    await mem.set("k", "v2")
    assert await mem.get("k") == "v2"


@pytest.mark.asyncio
async def test_get_missing_key_returns_none():
    mem = TemporaryMemory("agent-1")
    assert await mem.get("missing") is None


@pytest.mark.asyncio
async def test_delete_removes_key():
    mem = TemporaryMemory("agent-1")
    await mem.set("k", "v")
    assert await mem.delete("k") is True
    assert await mem.get("k") is None


@pytest.mark.asyncio
async def test_delete_missing_key_is_noop():
    mem = TemporaryMemory("agent-1")
    assert await mem.delete("nope") is False


@pytest.mark.asyncio
async def test_set_returns_memory_item_with_metadata():
    """The return value of set() is a usable MemoryItem."""
    mem = TemporaryMemory("agent-1")
    item = await mem.set("k", {"hello": "world"}, source="self")
    assert isinstance(item, MemoryItem)
    assert item.type == "context"
    assert item.content == {"hello": "world"}
    assert item.source == "self"


# ---------------------------------------------------------------------------
# TTL semantics
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ttl_expiration_returns_none(monkeypatch):
    """A custom TTL is honoured; the entry expires when the clock passes it."""
    mem = TemporaryMemory("agent-1", ttl_seconds=10)

    # Pin the monotonic clock so the test is deterministic.
    base = time.monotonic()
    counter = {"t": base}
    monkeypatch.setattr("memory.temporary.time.monotonic", lambda: counter["t"])

    await mem.set("k", "v", ttl=1)
    assert await mem.get("k") == "v"
    counter["t"] = base + 1.5
    assert await mem.get("k") is None


@pytest.mark.asyncio
async def test_default_ttl_is_30_minutes():
    """The default TTL is 1800 seconds per the spec."""
    mem = TemporaryMemory("agent-1")
    assert mem._default_ttl == 1800


@pytest.mark.asyncio
async def test_ttl_none_means_never_expire(monkeypatch):
    """If ttl is None, entries persist across time advancement."""
    mem = TemporaryMemory("agent-1", ttl_seconds=None)
    base = time.monotonic()
    counter = {"t": base}
    monkeypatch.setattr("memory.temporary.time.monotonic", lambda: counter["t"])

    await mem.set("k", "v", ttl=None)
    counter["t"] = base + 86_400  # 24 hours later
    assert await mem.get("k") == "v"


@pytest.mark.asyncio
async def test_expired_entry_is_evicted_on_access(monkeypatch):
    """Reading an expired entry evicts it from the store."""
    mem = TemporaryMemory("agent-1")
    base = time.monotonic()
    counter = {"t": base}
    monkeypatch.setattr("memory.temporary.time.monotonic", lambda: counter["t"])

    await mem.set("k", "v", ttl=1)
    counter["t"] = base + 2
    assert await mem.get("k") is None
    assert await mem.size() == 0


# ---------------------------------------------------------------------------
# get_recent
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_recent_returns_newest_first():
    mem = TemporaryMemory("agent-1")
    for i in range(5):
        await mem.set(f"k{i}", f"v{i}")
    recent = await mem.get_recent(n=3)
    assert [it.content for it in recent] == ["v4", "v3", "v2"]


@pytest.mark.asyncio
async def test_get_recent_respects_context_window():
    mem = TemporaryMemory("agent-1")
    for i in range(20):
        await mem.set(f"k{i}", i)
    recent = await mem.get_recent(n=10)
    assert len(recent) == 10


@pytest.mark.asyncio
async def test_get_recent_zero_returns_empty():
    mem = TemporaryMemory("agent-1")
    await mem.set("k", "v")
    assert await mem.get_recent(n=0) == []


@pytest.mark.asyncio
async def test_get_recent_type_filter():
    mem = TemporaryMemory("agent-1")
    await mem.set("a", "1", type="action")
    await mem.set("r", "2", type="result")
    await mem.set("d", "3", type="decision")
    recent = await mem.get_recent(n=10, type="result")
    assert [it.content for it in recent] == ["2"]


# ---------------------------------------------------------------------------
# clear / size
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_clear_empties_store():
    mem = TemporaryMemory("agent-1")
    await mem.set("a", 1)
    await mem.set("b", 2)
    await mem.clear()
    assert await mem.size() == 0
    assert await mem.get("a") is None


@pytest.mark.asyncio
async def test_size_reflects_live_entries(monkeypatch):
    mem = TemporaryMemory("agent-1")
    base = time.monotonic()
    counter = {"t": base}
    monkeypatch.setattr("memory.temporary.time.monotonic", lambda: counter["t"])

    await mem.set("live", "v", ttl=10)
    await mem.set("dead", "v", ttl=1)
    counter["t"] = base + 2
    assert await mem.size() == 1


@pytest.mark.asyncio
async def test_max_entries_cap_evicts_oldest(monkeypatch):
    """The cap drops the oldest entries when exceeded."""
    mem = TemporaryMemory("agent-1", max_entries=2)
    await mem.set("a", "1")
    await mem.set("b", "2")
    await mem.set("c", "3")
    assert await mem.size() == 2
    assert await mem.get("a") is None
    assert await mem.get("c") == "3"


# ---------------------------------------------------------------------------
# session_state
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_state_returns_context_typed_items():
    """``get_state`` only returns items with type=='context'."""
    mem = TemporaryMemory("agent-1")
    await mem.set("a", 1, type="context")
    await mem.set("b", 2, type="action")
    await mem.set("c", 3, type="context")
    state = await mem.get_state()
    assert state == {"a": 1, "c": 3}


# ---------------------------------------------------------------------------
# MemoryItem round-trip
# ---------------------------------------------------------------------------


def test_memory_item_to_kernel_strips_internal_fields():
    """The kernel wire format doesn't carry source/workflow_id/step_id/ttl."""
    item = MemoryItem(
        type="action",
        content={"x": 1},
        source="self",
        ttl=42,
        workflow_id="wf-1",
        step_id="step-1",
    )
    kernel_item = item.to_kernel()
    assert kernel_item.type == "action"
    assert kernel_item.content == {"x": 1}
    assert kernel_item.relevance_score == 1.0
    # The kernel item has no source/ttl/workflow_id/step_id fields.
    with pytest.raises(AttributeError):
        _ = kernel_item.source  # type: ignore[attr-defined]


def test_memory_item_from_kernel_fills_defaults():
    """A kernel item with no relevance score becomes 1.0 here."""
    from datetime import datetime, timezone
    from kernel.models import MemoryItem as KernelMemoryItem

    k = KernelMemoryItem(
        timestamp=datetime.now(timezone.utc),
        type="result",
        content="ok",
    )
    item = MemoryItem.from_kernel(k, source="kernel", workflow_id="wf")
    assert item.source == "kernel"
    assert item.workflow_id == "wf"
    assert item.relevance_score == 1.0


# ---------------------------------------------------------------------------
# Concurrency
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_writes_do_not_corrupt():
    """Many concurrent set()s should not lose updates."""
    mem = TemporaryMemory("agent-1")
    await asyncio.gather(*[mem.set(f"k{i}", i) for i in range(100)])
    assert await mem.size() == 100
    for i in range(100):
        assert await mem.get(f"k{i}") == i
