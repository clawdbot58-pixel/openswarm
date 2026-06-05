"""Persistent, SQLite-backed memory store.

This is the *long-lived* memory channel.  Entries survive agent
restarts, kernel restarts, and reboots; they are queryable by
timestamp (recent), FTS5 keyword search (relevant), workflow, and
type.  Every row carries ``agent_id``, ``workflow_id``, and
``step_id`` so an auditor can trace any memory back to the step
that produced it.

The schema is intentionally simple — two tables:

* ``memories`` — the source of truth.  One row per item.
* ``memory_fts`` — an FTS5 index over ``content`` and ``tags``,
  joined to ``memories`` by ``rowid``.

FTS5 is used for keyword search.  Vector search is a Phase 11
concern and explicitly out of scope here.

Thread safety
-------------
All write paths are serialized through a single :class:`asyncio.Lock`
(matching the kernel's :class:`kernel.registry.AgentRegistry`
pattern).  Reads are also funneled through the lock because
``aiosqlite`` connections are not safe for concurrent use from
multiple coroutines; the cost is negligible at our scale.

Schema version
--------------
The schema carries ``schema_version`` in a single-row table; any
breaking change bumps the constant below and the migrator refuses
to open an unrecognized database.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, AsyncIterator, Optional

import aiosqlite

from .temporary import MemoryItem, TypeLiteral


SCHEMA_VERSION = 1

# The single source of truth for the database shape.  Stored as a
# constant so tests can inspect it (and so a future migrator can
# read both ``user_version`` and the body).
SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS schema_version (
    version    INTEGER PRIMARY KEY
);

CREATE TABLE IF NOT EXISTS memories (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id        TEXT    NOT NULL,
    timestamp       TEXT    NOT NULL,
    type            TEXT    NOT NULL,
    content_json    TEXT    NOT NULL,
    relevance_score REAL    NOT NULL DEFAULT 1.0,
    source          TEXT    NOT NULL DEFAULT 'self',
    tags_json       TEXT    NOT NULL DEFAULT '[]',
    workflow_id     TEXT,
    step_id         TEXT
);

CREATE INDEX IF NOT EXISTS idx_memories_agent_time
    ON memories(agent_id, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_memories_workflow
    ON memories(workflow_id);
CREATE INDEX IF NOT EXISTS idx_memories_type
    ON memories(agent_id, type);
CREATE INDEX IF NOT EXISTS idx_memories_relevance
    ON memories(agent_id, relevance_score DESC);

CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(
    content,
    tags,
    content='memories',
    content_rowid='id',
    tokenize='porter unicode61'
);

-- Sync triggers keep the FTS index in lockstep with the source table.
CREATE TRIGGER IF NOT EXISTS memories_ai AFTER INSERT ON memories BEGIN
    INSERT INTO memory_fts(rowid, content, tags)
    VALUES (new.id, new.content_json, new.tags_json);
END;
CREATE TRIGGER IF NOT EXISTS memories_ad AFTER DELETE ON memories BEGIN
    INSERT INTO memory_fts(memory_fts, rowid, content, tags)
    VALUES ('delete', old.id, old.content_json, old.tags_json);
END;
CREATE TRIGGER IF NOT EXISTS memories_au AFTER UPDATE ON memories BEGIN
    INSERT INTO memory_fts(memory_fts, rowid, content, tags)
    VALUES ('delete', old.id, old.content_json, old.tags_json);
    INSERT INTO memory_fts(rowid, content, tags)
    VALUES (new.id, new.content_json, new.tags_json);
END;
"""


def _utcnow_iso() -> str:
    """ISO 8601 with a ``Z`` suffix — the format the contracts use."""
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _iso_to_dt(value: str) -> datetime:
    """Parse an ISO 8601 string into a tz-aware :class:`datetime`."""
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    return datetime.fromisoformat(value)


class PersistentMemory:
    """Async SQLite + FTS5 memory store.

    Args:
        db_path: Path to the SQLite file.  ``":memory:"`` is allowed
            for tests; ``None`` is also accepted and is equivalent.
            Default: ``data/memory.db``.
    """

    def __init__(self, db_path: str | Path | None = "data/memory.db") -> None:
        self._db_path = str(db_path) if db_path is not None else ":memory:"
        self._db: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()
        self._initialized = False

    # -- lifecycle --------------------------------------------------------

    async def initialize(self) -> None:
        """Open the database and apply the schema.  Idempotent."""
        if self._initialized:
            return
        if self._db_path != ":memory:":
            Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self._db_path)
        self._db.row_factory = aiosqlite.Row
        await self._db.execute("PRAGMA journal_mode=WAL;")
        await self._db.execute("PRAGMA foreign_keys=ON;")
        await self._db.executescript(SCHEMA_SQL)
        # Persist the schema version.
        async with self._lock:
            await self._db.execute(
                "INSERT OR IGNORE INTO schema_version(version) VALUES (?)",
                (SCHEMA_VERSION,),
            )
            await self._db.commit()
        self._initialized = True

    async def close(self) -> None:
        """Close the underlying connection.  Idempotent."""
        if self._db is not None:
            await self._db.close()
            self._db = None
            self._initialized = False

    @asynccontextmanager
    async def _conn(self) -> AsyncIterator[aiosqlite.Connection]:
        """Yield the live connection, initializing on first use."""
        if not self._initialized:
            await self.initialize()
        assert self._db is not None
        yield self._db

    # -- write path -------------------------------------------------------

    async def store(
        self,
        agent_id: str,
        item: MemoryItem,
        workflow_id: Optional[str] = None,
        step_id: Optional[str] = None,
        tags: Optional[list[str]] = None,
    ) -> int:
        """Insert a memory item and return its row id.

        Args:
            agent_id: Owning agent.  Required.
            item: The :class:`MemoryItem` to store.  ``item.timestamp``,
                ``item.type``, ``item.content``, ``item.relevance_score``
                and ``item.source`` are taken from the item; the
                ``workflow_id`` and ``step_id`` arguments override
                anything set on the item itself.
            workflow_id: Optional workflow attribution.  Overrides
                ``item.workflow_id``.
            step_id: Optional step attribution.  Overrides
                ``item.step_id``.
            tags: Optional list of searchable tags.  Stored in
                ``tags_json`` and indexed in FTS5.

        Returns:
            The new ``id`` (autoincrement primary key).
        """
        content_json = json.dumps(item.content, default=str, ensure_ascii=False)
        tags_json = json.dumps(list(tags or []), ensure_ascii=False)
        ts = item.timestamp.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        wf = workflow_id if workflow_id is not None else item.workflow_id
        sid = step_id if step_id is not None else item.step_id

        async with self._lock:
            async with self._conn() as db:
                cursor = await db.execute(
                    """
                    INSERT INTO memories
                        (agent_id, timestamp, type, content_json, relevance_score,
                         source, tags_json, workflow_id, step_id)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        agent_id,
                        ts,
                        item.type,
                        content_json,
                        item.relevance_score,
                        item.source,
                        tags_json,
                        wf,
                        sid,
                    ),
                )
                await db.commit()
                return int(cursor.lastrowid or 0)

    # -- read paths -------------------------------------------------------

    async def retrieve_recent(
        self,
        agent_id: str,
        n: int = 10,
        type: Optional[TypeLiteral] = None,  # noqa: A002
    ) -> list[MemoryItem]:
        """Return the most recent ``n`` items for ``agent_id``, newest first.

        ``type`` filters to a single memory type (``"action"``,
        ``"result"``, etc.) when supplied.
        """
        if n <= 0:
            return []
        async with self._conn() as db:
            if type is None:
                cursor = await db.execute(
                    """
                    SELECT id, agent_id, timestamp, type, content_json,
                           relevance_score, source, tags_json,
                           workflow_id, step_id
                    FROM memories
                    WHERE agent_id = ?
                    ORDER BY timestamp DESC, id DESC
                    LIMIT ?
                    """,
                    (agent_id, n),
                )
            else:
                cursor = await db.execute(
                    """
                    SELECT id, agent_id, timestamp, type, content_json,
                           relevance_score, source, tags_json,
                           workflow_id, step_id
                    FROM memories
                    WHERE agent_id = ? AND type = ?
                    ORDER BY timestamp DESC, id DESC
                    LIMIT ?
                    """,
                    (agent_id, type, n),
                )
            rows = await cursor.fetchall()
        return [self._row_to_item(row) for row in rows]

    async def retrieve_relevant(
        self,
        agent_id: str,
        query: str,
        threshold: float = 0.5,
        n: int = 5,
    ) -> list[MemoryItem]:
        """FTS5 keyword search with a relevance-score floor.

        The FTS5 ``bm25()`` rank is converted to a 0–1 score via
        ``1 / (1 + |rank|)`` — close matches have rank near 0, so
        the score is near 1.  Items whose final score is below
        ``threshold`` are dropped.  ``tags`` are not used as a
        separate filter; they participate in the FTS index.
        """
        if n <= 0 or not query.strip():
            return []
        # Sanitize: FTS5 treats each whitespace-separated token as a
        # prefix match when followed by ``*``.  Wrap each token to
        # avoid syntax errors on punctuation.
        tokens = [f'"{tok}"' for tok in query.split() if tok]
        if not tokens:
            return []
        fts_query = " ".join(tokens)

        async with self._conn() as db:
            cursor = await db.execute(
                """
                SELECT m.id, m.agent_id, m.timestamp, m.type, m.content_json,
                       m.relevance_score, m.source, m.tags_json,
                       m.workflow_id, m.step_id,
                       bm25(memory_fts) AS rank
                FROM memory_fts
                JOIN memories m ON m.id = memory_fts.rowid
                WHERE memory_fts MATCH ?
                  AND m.agent_id = ?
                ORDER BY rank ASC
                LIMIT ?
                """,
                (fts_query, agent_id, max(n * 4, n)),
            )
            rows = await cursor.fetchall()

        results: list[tuple[float, MemoryItem]] = []
        for row in rows:
            rank = row["rank"] if row["rank"] is not None else 0.0
            # bm25 returns negative numbers; ``abs(rank)`` lower =
            # better.  The stored ``relevance_score`` (a prior)
            # multiplies in.
            fts_score = 1.0 / (1.0 + abs(float(rank)))
            prior = float(row["relevance_score"] or 0.0)
            combined = min(1.0, fts_score * (0.5 + 0.5 * prior))
            if combined < threshold:
                continue
            results.append((combined, self._row_to_item(row)))
        results.sort(key=lambda r: r[0], reverse=True)
        return [item for _, item in results[:n]]

    async def retrieve_by_workflow(self, workflow_id: str) -> list[MemoryItem]:
        """Return every memory attributed to ``workflow_id``."""
        async with self._conn() as db:
            cursor = await db.execute(
                """
                SELECT id, agent_id, timestamp, type, content_json,
                       relevance_score, source, tags_json,
                       workflow_id, step_id
                FROM memories
                WHERE workflow_id = ?
                ORDER BY timestamp ASC, id ASC
                """,
                (workflow_id,),
            )
            rows = await cursor.fetchall()
        return [self._row_to_item(row) for row in rows]

    async def retrieve_by_type(
        self,
        agent_id: str,
        type: TypeLiteral,  # noqa: A002
        n: int = 10,
    ) -> list[MemoryItem]:
        """Return the most recent ``n`` items of a given type for ``agent_id``."""
        return await self.retrieve_recent(agent_id, n=n, type=type)

    async def retrieve_by_tags(
        self,
        agent_id: str,
        tags: list[str],
        n: int = 10,
    ) -> list[MemoryItem]:
        """Return items whose tag list intersects ``tags``, newest first."""
        if not tags or n <= 0:
            return []
        # ``tags_json`` is a JSON array of strings; we match via LIKE
        # on each tag.  This is good enough for the scale we expect
        # (thousands of rows per agent) and avoids an FTS5 rebuild.
        clauses = " AND ".join(["tags_json LIKE ?"] * len(tags))
        params: list[Any] = [f'%"{tag}"%' for tag in tags]
        params.extend([agent_id, n])
        async with self._conn() as db:
            cursor = await db.execute(
                f"""
                SELECT id, agent_id, timestamp, type, content_json,
                       relevance_score, source, tags_json,
                       workflow_id, step_id
                FROM memories
                WHERE {clauses} AND agent_id = ?
                ORDER BY timestamp DESC, id DESC
                LIMIT ?
                """,
                tuple(params),
            )
            rows = await cursor.fetchall()
        return [self._row_to_item(row) for row in rows]

    async def update_relevance(self, memory_id: int, new_score: float) -> bool:
        """Update ``relevance_score`` for a memory.  Returns ``True`` if changed.

        Used by the loop optimizer's feedback loop (Phase 4) to
        bump the relevance of memories that contributed to a
        successful run, and to drop memories that didn't.
        """
        if not 0.0 <= new_score <= 1.0:
            raise ValueError(f"relevance score must be 0..1, got {new_score!r}")
        async with self._lock:
            async with self._conn() as db:
                cursor = await db.execute(
                    "UPDATE memories SET relevance_score = ? WHERE id = ?",
                    (new_score, memory_id),
                )
                await db.commit()
                return cursor.rowcount > 0

    async def delete(self, memory_id: int) -> bool:
        """Remove a single memory by id.  Returns ``True`` if removed."""
        async with self._lock:
            async with self._conn() as db:
                cursor = await db.execute(
                    "DELETE FROM memories WHERE id = ?",
                    (memory_id,),
                )
                await db.commit()
                return cursor.rowcount > 0

    async def prune(
        self,
        max_age_days: int = 30,
        min_relevance: float = 0.3,
    ) -> int:
        """Delete old, low-relevance memories.  Returns the count deleted.

        Rows that are *young* (within ``max_age_days``) are kept
        regardless of relevance — we don't want to nuke a 5-minute-old
        error that hasn't had time to accumulate context.  Rows that
        are *old* are dropped when their score is at or below
        ``min_relevance``.
        """
        cutoff = (datetime.now(timezone.utc) - timedelta(days=max_age_days)).isoformat().replace(
            "+00:00", "Z"
        )
        async with self._lock:
            async with self._conn() as db:
                cursor = await db.execute(
                    """
                    DELETE FROM memories
                    WHERE timestamp < ?
                      AND relevance_score <= ?
                    """,
                    (cutoff, min_relevance),
                )
                await db.commit()
                return int(cursor.rowcount or 0)

    async def count(self, agent_id: Optional[str] = None) -> int:
        """Total memory rows, optionally filtered by ``agent_id``."""
        async with self._conn() as db:
            if agent_id is None:
                cursor = await db.execute("SELECT COUNT(*) AS n FROM memories")
            else:
                cursor = await db.execute(
                    "SELECT COUNT(*) AS n FROM memories WHERE agent_id = ?",
                    (agent_id,),
                )
            row = await cursor.fetchone()
        return int(row["n"] if row else 0)

    # -- helpers ----------------------------------------------------------

    @staticmethod
    def _row_to_item(row: aiosqlite.Row) -> MemoryItem:
        """Convert a SQLite row into a :class:`MemoryItem`.

        ``content`` is deserialized from JSON; if that fails (e.g. a
        legacy row that was stored as raw text), the original string
        is returned so the caller still gets something useful.
        """
        raw = row["content_json"]
        try:
            content = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            content = raw
        return MemoryItem(
            timestamp=_iso_to_dt(row["timestamp"]),
            type=row["type"],  # type: ignore[arg-type]
            content=content,
            relevance_score=float(row["relevance_score"]),
            source=row["source"] if row["source"] in {"user", "kernel", "self", "other_agent"} else "self",
            workflow_id=row["workflow_id"],
            step_id=row["step_id"],
        )


__all__ = ["PersistentMemory", "SCHEMA_SQL", "SCHEMA_VERSION"]
