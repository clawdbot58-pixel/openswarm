"""Dashboard view & layout configuration storage.

The :class:`ConfigAPI` persists user-defined dashboard layouts in a
dedicated SQLite file (``data/dashboard.db``).  The backend does
**not** interpret the contents of a view or layout — the Phase 8
frontend (and the Main Agent) own that contract.  Storage is opaque
JSON.

The view and layout tables look like::

    views (
        view_id   TEXT PRIMARY KEY,
        name      TEXT NOT NULL,
        config    TEXT NOT NULL,   -- JSON blob
        created_at TEXT,
        updated_at TEXT
    );

    layouts (
        layout_id TEXT PRIMARY KEY,
        name      TEXT NOT NULL,
        config    TEXT NOT NULL,
        created_at TEXT,
        updated_at TEXT
    );

CRITICAL: this is the only mutable state owned by the dashboard.  The
introspection API is strictly read-only with respect to the rest of
the swarm.
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path

import aiosqlite

from .models import (
    LayoutConfig,
    LayoutConfigInput,
    ViewConfig,
    ViewConfigInput,
)

logger = logging.getLogger(__name__)


SCHEMA_VERSION = 1

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS views (
    view_id    TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    config     TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS layouts (
    layout_id  TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    config     TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY
);
"""


def _utcnow_iso() -> str:
    """Return the current UTC time as an ISO 8601 string with a Z suffix."""
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# ConfigAPI
# ---------------------------------------------------------------------------


class ConfigAPI:
    """Persistent store for dashboard views and layouts.

    The class is async-safe: a single :class:`asyncio.Lock` serializes
    writes (matching the kernel's :class:`AgentRegistry` pattern).
    Reads are also funneled through the lock because ``aiosqlite``
    connections are not safe for concurrent use from multiple
    coroutines.
    """

    def __init__(self, db_path: Path | str = "data/dashboard.db") -> None:
        """Store the DB path.  Call :meth:`initialize` before use."""
        self._db_path = str(db_path)
        self._db: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()
        self._initialized = False

    # -- lifecycle --------------------------------------------------------

    async def initialize(self) -> None:
        """Open the DB and apply the schema.  Idempotent."""
        if self._initialized:
            return
        if self._db_path != ":memory:":
            Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self._db_path)
        self._db.row_factory = aiosqlite.Row
        await self._db.execute("PRAGMA journal_mode=WAL;")
        await self._db.execute("PRAGMA foreign_keys=ON;")
        await self._db.executescript(SCHEMA_SQL)
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

    def _require_db(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("ConfigAPI used before initialize()")
        return self._db

    # -- views ------------------------------------------------------------

    async def get_views(self) -> list[ViewConfig]:
        """Return every saved view, newest first."""
        async with self._lock:
            db = self._require_db()
            async with db.execute(
                "SELECT view_id, name, config, created_at, updated_at "
                "FROM views ORDER BY created_at DESC"
            ) as cur:
                rows = await cur.fetchall()
        return [self._row_to_view(row) for row in rows]

    async def get_view(self, view_id: str) -> ViewConfig:
        """Return one view, or raise :class:`KeyError`."""
        async with self._lock:
            db = self._require_db()
            async with db.execute(
                "SELECT view_id, name, config, created_at, updated_at "
                "FROM views WHERE view_id = ?",
                (view_id,),
            ) as cur:
                row = await cur.fetchone()
        if row is None:
            raise KeyError(f"view {view_id!r} not found")
        return self._row_to_view(row)

    async def save_view(self, view: ViewConfigInput | ViewConfig) -> str:
        """Persist ``view`` and return its ``view_id``.

        If ``view`` is a :class:`ViewConfig` (i.e. it carries an
        explicit ``view_id``), that id is honored.  Otherwise a fresh
        UUID4 is generated.
        """
        if isinstance(view, ViewConfig):
            view_id = view.view_id
            created_at = view.created_at
        else:
            view_id = f"view-{uuid.uuid4().hex[:12]}"
            now = datetime.now(timezone.utc)
            created_at = now
        blob = self._view_to_blob(view)
        now_iso = _utcnow_iso()
        async with self._lock:
            db = self._require_db()
            await db.execute(
                """
                INSERT INTO views (view_id, name, config, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(view_id) DO UPDATE SET
                    name = excluded.name,
                    config = excluded.config,
                    updated_at = excluded.updated_at
                """,
                (view_id, view.name, blob, _iso(created_at), now_iso),
            )
            await db.commit()
        return view_id

    async def delete_view(self, view_id: str) -> None:
        """Remove ``view_id`` from the store.  Idempotent.

        :raises KeyError: when the view does not exist (so callers can
            distinguish "deleted successfully" from "no-op").
        """
        async with self._lock:
            db = self._require_db()
            cursor = await db.execute(
                "DELETE FROM views WHERE view_id = ?", (view_id,)
            )
            await db.commit()
            if cursor.rowcount == 0:
                raise KeyError(f"view {view_id!r} not found")

    # -- layouts ----------------------------------------------------------

    async def get_layout(self, layout_id: str) -> LayoutConfig:
        """Return one layout, or raise :class:`KeyError`."""
        async with self._lock:
            db = self._require_db()
            async with db.execute(
                "SELECT layout_id, name, config, created_at, updated_at "
                "FROM layouts WHERE layout_id = ?",
                (layout_id,),
            ) as cur:
                row = await cur.fetchone()
        if row is None:
            raise KeyError(f"layout {layout_id!r} not found")
        return self._row_to_layout(row)

    async def list_layouts(self) -> list[LayoutConfig]:
        """Return every saved layout, newest first."""
        async with self._lock:
            db = self._require_db()
            async with db.execute(
                "SELECT layout_id, name, config, created_at, updated_at "
                "FROM layouts ORDER BY created_at DESC"
            ) as cur:
                rows = await cur.fetchall()
        return [self._row_to_layout(row) for row in rows]

    async def save_layout(
        self, layout: LayoutConfigInput | LayoutConfig
    ) -> str:
        """Persist ``layout`` and return its ``layout_id``."""
        if isinstance(layout, LayoutConfig):
            layout_id = layout.layout_id
            created_at = layout.created_at
        else:
            layout_id = f"layout-{uuid.uuid4().hex[:12]}"
            now = datetime.now(timezone.utc)
            created_at = now
        blob = self._layout_to_blob(layout)
        now_iso = _utcnow_iso()
        async with self._lock:
            db = self._require_db()
            await db.execute(
                """
                INSERT INTO layouts (layout_id, name, config, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(layout_id) DO UPDATE SET
                    name = excluded.name,
                    config = excluded.config,
                    updated_at = excluded.updated_at
                """,
                (layout_id, layout.name, blob, _iso(created_at), now_iso),
            )
            await db.commit()
        return layout_id

    async def delete_layout(self, layout_id: str) -> None:
        """Remove ``layout_id``.  Idempotent.

        :raises KeyError: when the layout does not exist.
        """
        async with self._lock:
            db = self._require_db()
            cursor = await db.execute(
                "DELETE FROM layouts WHERE layout_id = ?", (layout_id,)
            )
            await db.commit()
            if cursor.rowcount == 0:
                raise KeyError(f"layout {layout_id!r} not found")

    # -- helpers ----------------------------------------------------------

    @staticmethod
    def _view_to_blob(view: ViewConfigInput | ViewConfig) -> str:
        """Serialise a view to its JSON blob form.

        We store the full Pydantic dict so future schema changes
        don't drop fields silently.
        """
        if isinstance(view, ViewConfig):
            payload = view.model_dump(mode="json")
        else:
            payload = view.model_dump(mode="json")
        return json.dumps(payload, default=str, ensure_ascii=False)

    @staticmethod
    def _row_to_view(row: aiosqlite.Row) -> ViewConfig:
        """Convert a SQLite row into a :class:`ViewConfig`."""
        raw = row["config"]
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            data = {}
        data.setdefault("view_id", row["view_id"])
        data.setdefault("name", row["name"])
        data.setdefault("description", "")
        data.setdefault("view_type", "custom")
        data.setdefault("data_sources", [])
        data.setdefault("filters", {})
        data.setdefault("refresh_interval_ms", 5000)
        data.setdefault("created_by", "system")
        # Pydantic's validator parses ISO 8601 strings into datetimes
        # for ``created_at``/``updated_at``; the JSON blob may carry
        # them as strings, so coerce.
        if "created_at" not in data:
            data["created_at"] = row["created_at"]
        if "updated_at" not in data:
            data["updated_at"] = row["updated_at"]
        return ViewConfig.model_validate(data)

    @staticmethod
    def _layout_to_blob(layout: LayoutConfigInput | LayoutConfig) -> str:
        """Serialise a layout to JSON."""
        return json.dumps(layout.model_dump(mode="json"), default=str, ensure_ascii=False)

    @staticmethod
    def _row_to_layout(row: aiosqlite.Row) -> LayoutConfig:
        """Convert a SQLite row into a :class:`LayoutConfig`."""
        raw = row["config"]
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            data = {}
        data.setdefault("layout_id", row["layout_id"])
        data.setdefault("name", row["name"])
        data.setdefault("description", "")
        data.setdefault("panes", {})
        data.setdefault("created_by", "system")
        if "created_at" not in data:
            data["created_at"] = row["created_at"]
        if "updated_at" not in data:
            data["updated_at"] = row["updated_at"]
        return LayoutConfig.model_validate(data)


def _iso(value: datetime) -> str:
    """Format a :class:`datetime` for storage."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


__all__ = ["SCHEMA_SQL", "SCHEMA_VERSION", "ConfigAPI"]
