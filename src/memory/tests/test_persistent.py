"""Tests for :class:`memory.persistent.PersistentMemory`."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import aiosqlite
import pytest

from memory.persistent import PersistentMemory, SCHEMA_VERSION
from memory.temporary import MemoryItem


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def mem(tmp_path: Path) -> PersistentMemory:
    """A fresh :class:`PersistentMemory` per test, in a temp file."""
    db = PersistentMemory(tmp_path / "memory.db")
    await db.initialize()
    yield db
    await db.close()


def _make_item(
    type: str = "result",
    content: object = "ok",
    relevance: float = 0.8,
    source: str = "self",
    workflow_id: str | None = None,
    step_id: str | None = None,
) -> MemoryItem:
    return MemoryItem(
        type=type,  # type: ignore[arg-type]
        content=content,
        relevance_score=relevance,
        source=source,  # type: ignore[arg-type]
        workflow_id=workflow_id,
        step_id=step_id,
    )


# ---------------------------------------------------------------------------
# store + retrieve_recent
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_store_returns_increasing_ids(mem: PersistentMemory):
    """Each store() returns a new autoincrement id."""
    a = await mem.store("agent-1", _make_item(content="a"))
    b = await mem.store("agent-1", _make_item(content="b"))
    assert a < b
    assert a >= 1


@pytest.mark.asyncio
async def test_retrieve_recent_newest_first(mem: PersistentMemory):
    """Newest items come first."""
    for i in range(5):
        await mem.store("a1", _make_item(content=i))
    items = await mem.retrieve_recent("a1", n=3)
    assert [it.content for it in items] == [4, 3, 2]


@pytest.mark.asyncio
async def test_retrieve_recent_filters_by_agent(mem: PersistentMemory):
    """Items from other agents are not returned."""
    await mem.store("a1", _make_item(content="for-a1"))
    await mem.store("a2", _make_item(content="for-a2"))
    items = await mem.retrieve_recent("a1", n=10)
    assert all(it.content == "for-a1" for it in items)


@pytest.mark.asyncio
async def test_retrieve_recent_filters_by_type(mem: PersistentMemory):
    await mem.store("a1", _make_item(type="action", content="act"))
    await mem.store("a1", _make_item(type="result", content="res"))
    actions = await mem.retrieve_recent("a1", n=10, type="action")
    assert [it.content for it in actions] == ["act"]


@pytest.mark.asyncio
async def test_retrieve_recent_zero_returns_empty(mem: PersistentMemory):
    await mem.store("a1", _make_item())
    assert await mem.retrieve_recent("a1", n=0) == []


# ---------------------------------------------------------------------------
# FTS5 search
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_retrieve_relevant_finds_matching(mem: PersistentMemory):
    """FTS5 keyword search returns the matching item."""
    await mem.store(
        "a1",
        _make_item(content="deployed the python service to staging"),
    )
    await mem.store(
        "a1",
        _make_item(content="investigated a database deadlock"),
    )
    hits = await mem.retrieve_relevant("a1", "python", threshold=0.0, n=5)
    assert len(hits) >= 1
    assert any("python" in (it.content or "") for it in hits)


@pytest.mark.asyncio
async def test_retrieve_relevant_threshold_drops_low_scores(mem: PersistentMemory):
    """Items below the threshold are filtered out."""
    await mem.store(
        "a1",
        _make_item(
            content="completely unrelated content about cooking recipes",
            relevance=0.0,
        ),
    )
    hits = await mem.retrieve_relevant("a1", "kubernetes", threshold=0.99, n=5)
    assert hits == []


@pytest.mark.asyncio
async def test_retrieve_relevant_empty_query_returns_empty(mem: PersistentMemory):
    """An empty query never returns anything (avoids an FTS5 error)."""
    await mem.store("a1", _make_item(content="anything"))
    assert await mem.retrieve_relevant("a1", "", n=5) == []
    assert await mem.retrieve_relevant("a1", "   ", n=5) == []


# ---------------------------------------------------------------------------
# workflow / type filters
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_retrieve_by_workflow(mem: PersistentMemory):
    await mem.store("a1", _make_item(content="1", workflow_id="wf-1"))
    await mem.store("a1", _make_item(content="2", workflow_id="wf-1"))
    await mem.store("a1", _make_item(content="3", workflow_id="wf-2"))
    items = await mem.retrieve_by_workflow("wf-1")
    assert {it.content for it in items} == {1, 2} or {it.content for it in items} == {"1", "2"}


@pytest.mark.asyncio
async def test_retrieve_by_type(mem: PersistentMemory):
    await mem.store("a1", _make_item(type="error", content="e1"))
    await mem.store("a1", _make_item(type="error", content="e2"))
    await mem.store("a1", _make_item(type="action", content="a1"))
    errs = await mem.retrieve_by_type("a1", "error", n=10)
    assert {it.content for it in errs} == {"e1", "e2"}


@pytest.mark.asyncio
async def test_retrieve_by_tags_finds_intersection(mem: PersistentMemory):
    """Tags act as an additional index for retrieval."""
    await mem.store("a1", _make_item(content="x"), tags=["urgent", "deploy"])
    await mem.store("a1", _make_item(content="y"), tags=["low"])
    items = await mem.retrieve_by_tags("a1", ["urgent"], n=10)
    assert {it.content for it in items} == {"x"}


# ---------------------------------------------------------------------------
# update_relevance / delete
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_relevance_persists(mem: PersistentMemory):
    mid = await mem.store("a1", _make_item(relevance=0.1))
    assert await mem.update_relevance(mid, 0.9) is True
    items = await mem.retrieve_recent("a1", n=1)
    assert items[0].relevance_score == 0.9


@pytest.mark.asyncio
async def test_update_relevance_validates_range(mem: PersistentMemory):
    mid = await mem.store("a1", _make_item())
    with pytest.raises(ValueError):
        await mem.update_relevance(mid, 1.5)
    with pytest.raises(ValueError):
        await mem.update_relevance(mid, -0.1)


@pytest.mark.asyncio
async def test_delete_removes(mem: PersistentMemory):
    mid = await mem.store("a1", _make_item())
    assert await mem.delete(mid) is True
    items = await mem.retrieve_recent("a1", n=10)
    assert items == []


# ---------------------------------------------------------------------------
# prune
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_prune_removes_old_low_relevance(mem: PersistentMemory):
    """Prune drops rows older than max_age_days with low relevance."""
    # Insert one old, low-relevance row.
    old_id = await mem.store("a1", _make_item(content="old", relevance=0.1))
    # Backdate it via direct SQL.
    async with aiosqlite.connect(mem._db_path) as db:
        await db.execute(
            "UPDATE memories SET timestamp = ? WHERE id = ?",
            (
                "2000-01-01T00:00:00Z",
                old_id,
            ),
        )
        await db.commit()

    # And a recent, low-relevance row that should be KEPT (young).
    await mem.store("a1", _make_item(content="young", relevance=0.1))

    removed = await mem.prune(max_age_days=30, min_relevance=0.3)
    assert removed == 1
    remaining = await mem.retrieve_recent("a1", n=10)
    assert {it.content for it in remaining} == {"young"}


# ---------------------------------------------------------------------------
# count + persistence across restart
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_count(mem: PersistentMemory):
    assert await mem.count() == 0
    await mem.store("a1", _make_item())
    await mem.store("a1", _make_item())
    await mem.store("a2", _make_item())
    assert await mem.count() == 3
    assert await mem.count("a1") == 2


@pytest.mark.asyncio
async def test_persistence_across_restart(tmp_path: Path):
    """Reopening the same DB file yields the same data."""
    db_path = tmp_path / "persist.db"
    m1 = PersistentMemory(db_path)
    await m1.initialize()
    await m1.store("a1", _make_item(content="kept"))
    await m1.close()

    m2 = PersistentMemory(db_path)
    await m2.initialize()
    items = await m2.retrieve_recent("a1", n=10)
    assert {it.content for it in items} == {"kept"}
    await m2.close()


# ---------------------------------------------------------------------------
# schema
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_schema_version_recorded(tmp_path: Path):
    """Opening a fresh DB records the schema version."""
    db = PersistentMemory(tmp_path / "v.db")
    await db.initialize()
    async with aiosqlite.connect(db._db_path) as conn:
        cursor = await conn.execute("SELECT version FROM schema_version")
        row = await cursor.fetchone()
        assert row is not None
        assert row[0] == SCHEMA_VERSION
    await db.close()


@pytest.mark.asyncio
async def test_fts_triggers_sync_index(tmp_path: Path):
    """Inserting, updating, and deleting keeps the FTS index in sync."""
    db = PersistentMemory(tmp_path / "fts.db")
    await db.initialize()
    mid = await db.store("a1", _make_item(content="findable token"))

    async with aiosqlite.connect(db._db_path) as conn:
        cursor = await conn.execute(
            "SELECT count(*) FROM memory_fts WHERE memory_fts MATCH 'findable'"
        )
        (count,) = await cursor.fetchone()
        assert count == 1

    await db.delete(mid)
    async with aiosqlite.connect(db._db_path) as conn:
        cursor = await conn.execute(
            "SELECT count(*) FROM memory_fts WHERE memory_fts MATCH 'findable'"
        )
        (count,) = await cursor.fetchone()
        assert count == 0
    await db.close()
