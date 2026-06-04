"""SQLite-backed agent registry and audit log.

The registry is the kernel's single source of truth for "who is in the
swarm right now". Two tables back it:

``agents``
    One row per registered agent. ``manifest_json`` stores the full
    validated :class:`~kernel.models.AgentManifest` blob so the kernel
    can re-validate permissions without re-asking the agent.

``audit_log``
    Append-only log written by :class:`~kernel.permissions.PermissionEnforcer`
    and other subsystems. One row per security-relevant event.

All write operations are serialized through a single :class:`asyncio.Lock`
because aiosqlite's default isolation can race when two coroutines try to
``INSERT`` simultaneously. The lock is cheap — it only guards write paths
and is released between awaits.
"""
from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import aiosqlite

from .exceptions import AgentAlreadyRegistered, AgentNotFound, ManifestRejected
from .models import AgentIdStr, AgentManifest, StatusLiteral

logger = logging.getLogger(__name__)


# SQL schema. Stored as a constant so tests can inspect it.
SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS agents (
    agent_id        TEXT PRIMARY KEY,
    manifest_json   TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'initializing',
    registered_at   TEXT NOT NULL,
    last_heartbeat  TEXT,
    instance_id     TEXT,
    connected_ws    INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_agents_status ON agents(status);

CREATE TABLE IF NOT EXISTS audit_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    envelope_id  TEXT,
    agent_id     TEXT,
    action       TEXT NOT NULL,
    result       TEXT NOT NULL,
    details      TEXT,
    timestamp    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_audit_agent ON audit_log(agent_id);
CREATE INDEX IF NOT EXISTS idx_audit_time  ON audit_log(timestamp);
"""


def _utcnow_iso() -> str:
    """Return the current UTC time as an ISO 8601 string with a Z suffix."""
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class AgentRegistry:
    """Async registry backed by a single SQLite file.

    The class is safe to share across coroutines. Use as an async context
    manager (``async with AgentRegistry(...) as registry:``) or call
    :meth:`initialize` and :meth:`close` manually.
    """

    def __init__(self, db_path: Path) -> None:
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db: aiosqlite.Connection | None = None
        self._write_lock = asyncio.Lock()
        self._initialized = False

    # -- lifecycle ---------------------------------------------------------

    async def initialize(self) -> None:
        """Open the DB and apply the schema. Idempotent."""
        if self._initialized:
            return
        self._db = await aiosqlite.connect(self._db_path)
        await self._db.execute("PRAGMA journal_mode=WAL;")
        await self._db.execute("PRAGMA foreign_keys=ON;")
        await self._db.executescript(SCHEMA_SQL)
        await self._db.commit()
        self._initialized = True
        logger.info("registry initialized db=%s", self._db_path)

    async def close(self) -> None:
        """Close the underlying connection. Idempotent."""
        if self._db is not None:
            await self._db.close()
            self._db = None
        self._initialized = False

    async def __aenter__(self) -> "AgentRegistry":
        await self.initialize()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close()

    def _require_db(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("AgentRegistry used before initialize()")
        return self._db

    # -- registration ------------------------------------------------------

    async def register(
        self,
        manifest: AgentManifest,
        *,
        instance_id: str | None = None,
        replace: bool = True,
    ) -> AgentManifest:
        """Insert or update an agent row.

        :param manifest: a manifest already validated against the contract.
        :param instance_id: optional per-process instance id (multi-instance
            agents). When ``None`` the agent_id is reused.
        :param replace: when ``True`` (default) an existing row is
            overwritten; when ``False`` a duplicate insert raises
            :class:`AgentAlreadyRegistered`.

        :raises ManifestRejected: if the manifest fails Pydantic validation.
        """
        if not isinstance(manifest, AgentManifest):
            # Defensive: callers should hand us a model. Convert lazily but
            # raise a structured error rather than a bare ValidationError.
            raise ManifestRejected(
                "register() requires an AgentManifest instance",
                manifest_type=type(manifest).__name__,
            )

        manifest_json = manifest.model_dump_json()
        now = _utcnow_iso()
        agent_id = manifest.agent_id
        status = manifest.status or "initializing"

        async with self._write_lock:
            db = self._require_db()
            if replace:
                await db.execute(
                    """
                    INSERT INTO agents
                        (agent_id, manifest_json, status, registered_at,
                         last_heartbeat, instance_id, connected_ws)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(agent_id) DO UPDATE SET
                        manifest_json = excluded.manifest_json,
                        status = excluded.status,
                        registered_at = excluded.registered_at,
                        instance_id = excluded.instance_id
                    """,
                    (
                        agent_id,
                        manifest_json,
                        status,
                        now,
                        None,
                        instance_id,
                        0,
                    ),
                )
            else:
                try:
                    await db.execute(
                        """
                        INSERT INTO agents
                            (agent_id, manifest_json, status, registered_at,
                             last_heartbeat, instance_id, connected_ws)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            agent_id,
                            manifest_json,
                            status,
                            now,
                            None,
                            instance_id,
                            0,
                        ),
                    )
                except sqlite3.IntegrityError as exc:
                    raise AgentAlreadyRegistered(
                        f"agent {agent_id!r} already registered",
                        agent_id=agent_id,
                    ) from exc
            await db.commit()
        logger.info("registered agent_id=%s role=%s", agent_id, manifest.role)
        return manifest

    async def unregister(self, agent_id: str) -> None:
        """Soft-delete: mark the row as ``offline`` and disconnect its WS.

        The manifest is kept on disk so the kernel can resurrect the agent
        if it reconnects. To remove the row entirely use
        :meth:`delete_hard`.
        """
        async with self._write_lock:
            db = self._require_db()
            cursor = await db.execute(
                "UPDATE agents SET status = 'offline', connected_ws = 0 "
                "WHERE agent_id = ?",
                (agent_id,),
            )
            await db.commit()
            if cursor.rowcount == 0:
                raise AgentNotFound(
                    f"agent {agent_id!r} not found", agent_id=agent_id
                )
        logger.info("unregistered agent_id=%s", agent_id)

    async def delete_hard(self, agent_id: str) -> None:
        """Permanently remove a row. Used by tests and admin tooling."""
        async with self._write_lock:
            db = self._require_db()
            cursor = await db.execute(
                "DELETE FROM agents WHERE agent_id = ?", (agent_id,)
            )
            await db.commit()
            if cursor.rowcount == 0:
                raise AgentNotFound(
                    f"agent {agent_id!r} not found", agent_id=agent_id
                )

    # -- lookups -----------------------------------------------------------

    async def get(self, agent_id: str) -> AgentManifest:
        """Return the manifest for ``agent_id``.

        :raises AgentNotFound: if no row exists.
        """
        db = self._require_db()
        async with db.execute(
            "SELECT manifest_json FROM agents WHERE agent_id = ?",
            (agent_id,),
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            raise AgentNotFound(
                f"agent {agent_id!r} not found", agent_id=agent_id
            )
        return AgentManifest.model_validate_json(row[0])

    async def get_status(self, agent_id: str) -> dict[str, Any]:
        """Return a small status dict for ``agent_id`` (cheap, no manifest parse)."""
        db = self._require_db()
        async with db.execute(
            "SELECT agent_id, status, last_heartbeat, connected_ws, "
            "registered_at, instance_id FROM agents WHERE agent_id = ?",
            (agent_id,),
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            raise AgentNotFound(
                f"agent {agent_id!r} not found", agent_id=agent_id
            )
        return {
            "agent_id": row[0],
            "status": row[1],
            "last_heartbeat": row[2],
            "connected_ws": bool(row[3]),
            "registered_at": row[4],
            "instance_id": row[5],
        }

    async def list(
        self,
        status_filter: StatusLiteral | None = None,
    ) -> list[AgentManifest]:
        """Return all registered agents, optionally filtered by status."""
        db = self._require_db()
        if status_filter is None:
            sql = "SELECT manifest_json FROM agents ORDER BY registered_at"
            params: tuple[Any, ...] = ()
        else:
            sql = (
                "SELECT manifest_json FROM agents WHERE status = ? "
                "ORDER BY registered_at"
            )
            params = (status_filter,)
        manifests: list[AgentManifest] = []
        async with db.execute(sql, params) as cur:
            async for row in cur:
                manifests.append(AgentManifest.model_validate_json(row[0]))
        return manifests

    async def list_status(
        self,
        status_filter: StatusLiteral | None = None,
    ) -> list[dict[str, Any]]:
        """Return status rows (cheap version of :meth:`list`)."""
        db = self._require_db()
        if status_filter is None:
            sql = (
                "SELECT agent_id, status, last_heartbeat, connected_ws, "
                "registered_at, instance_id FROM agents ORDER BY registered_at"
            )
            params: tuple[Any, ...] = ()
        else:
            sql = (
                "SELECT agent_id, status, last_heartbeat, connected_ws, "
                "registered_at, instance_id FROM agents WHERE status = ? "
                "ORDER BY registered_at"
            )
            params = (status_filter,)
        rows: list[dict[str, Any]] = []
        async with db.execute(sql, params) as cur:
            async for row in cur:
                rows.append(
                    {
                        "agent_id": row[0],
                        "status": row[1],
                        "last_heartbeat": row[2],
                        "connected_ws": bool(row[3]),
                        "registered_at": row[4],
                        "instance_id": row[5],
                    }
                )
        return rows

    # -- heartbeat + connection tracking ----------------------------------

    async def update_heartbeat(
        self,
        agent_id: str,
        timestamp: datetime | None = None,
    ) -> None:
        """Update ``last_heartbeat`` and refresh status to ``ready``/``busy``."""
        ts = (timestamp or datetime.now(timezone.utc)).isoformat().replace(
            "+00:00", "Z"
        )
        async with self._write_lock:
            db = self._require_db()
            cursor = await db.execute(
                "UPDATE agents SET last_heartbeat = ? "
                "WHERE agent_id = ?",
                (ts, agent_id),
            )
            await db.commit()
            if cursor.rowcount == 0:
                raise AgentNotFound(
                    f"agent {agent_id!r} not found", agent_id=agent_id
                )

    async def update_status(
        self,
        agent_id: str,
        status: StatusLiteral,
    ) -> None:
        """Update the ``status`` column for ``agent_id``."""
        async with self._write_lock:
            db = self._require_db()
            cursor = await db.execute(
                "UPDATE agents SET status = ? WHERE agent_id = ?",
                (status, agent_id),
            )
            await db.commit()
            if cursor.rowcount == 0:
                raise AgentNotFound(
                    f"agent {agent_id!r} not found", agent_id=agent_id
                )

    async def set_connected(self, agent_id: str, connected: bool) -> None:
        """Mark whether the agent currently has a live WebSocket."""
        async with self._write_lock:
            db = self._require_db()
            cursor = await db.execute(
                "UPDATE agents SET connected_ws = ? WHERE agent_id = ?",
                (1 if connected else 0, agent_id),
            )
            await db.commit()
            if cursor.rowcount == 0:
                raise AgentNotFound(
                    f"agent {agent_id!r} not found", agent_id=agent_id
                )

    # -- audit log ---------------------------------------------------------

    async def audit(
        self,
        *,
        action: str,
        result: str,
        agent_id: str | None = None,
        envelope_id: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        """Append a row to ``audit_log``. Never raises."""
        try:
            payload = json.dumps(details or {}, default=str)
        except (TypeError, ValueError):
            payload = "{}"
        async with self._write_lock:
            db = self._require_db()
            await db.execute(
                "INSERT INTO audit_log (envelope_id, agent_id, action, "
                "result, details, timestamp) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    envelope_id,
                    agent_id,
                    action,
                    result,
                    payload,
                    _utcnow_iso(),
                ),
            )
            await db.commit()

    async def audit_log(
        self,
        *,
        agent_id: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Return recent audit log entries, optionally filtered by agent."""
        db = self._require_db()
        if agent_id is None:
            sql = (
                "SELECT id, envelope_id, agent_id, action, result, "
                "details, timestamp FROM audit_log "
                "ORDER BY id DESC LIMIT ?"
            )
            params: tuple[Any, ...] = (limit,)
        else:
            sql = (
                "SELECT id, envelope_id, agent_id, action, result, "
                "details, timestamp FROM audit_log WHERE agent_id = ? "
                "ORDER BY id DESC LIMIT ?"
            )
            params = (agent_id, limit)
        out: list[dict[str, Any]] = []
        async with db.execute(sql, params) as cur:
            async for row in cur:
                out.append(
                    {
                        "id": row[0],
                        "envelope_id": row[1],
                        "agent_id": row[2],
                        "action": row[3],
                        "result": row[4],
                        "details": json.loads(row[5]) if row[5] else {},
                        "timestamp": row[6],
                    }
                )
        return out

    # -- misc --------------------------------------------------------------

    async def count(self, status: StatusLiteral | None = None) -> int:
        """Return the number of rows, optionally filtered by status."""
        db = self._require_db()
        if status is None:
            sql = "SELECT COUNT(*) FROM agents"
            params: tuple[Any, ...] = ()
        else:
            sql = "SELECT COUNT(*) FROM agents WHERE status = ?"
            params = (status,)
        async with db.execute(sql, params) as cur:
            row = await cur.fetchone()
        return int(row[0]) if row else 0

    async def all_ids(self) -> list[AgentIdStr]:
        """Return every agent_id. Used by the bus for broadcast expansion."""
        db = self._require_db()
        async with db.execute("SELECT agent_id FROM agents") as cur:
            return [r[0] for r in await cur.fetchall()]

    async def healthcheck(self) -> bool:
        """Return True if the DB is responsive. Used by ``GET /health``."""
        try:
            db = self._require_db()
            async with db.execute("SELECT 1") as cur:
                await cur.fetchone()
            return True
        except Exception:  # noqa: BLE001 — healthchecks must never raise
            return False


__all__ = ["AgentRegistry", "SCHEMA_SQL"]
