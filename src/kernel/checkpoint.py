"""SQLite-backed checkpoint manager for workflow recovery.

The kernel writes a checkpoint **after every step completes** so that, on
crash or restart, a workflow can resume from its last successful step.

The on-disk schema is intentionally tiny — we store the opaque state
blob and the per-step agent outputs as JSON, plus a wall-clock timestamp
for the replay UI.

Public surface
--------------

* :class:`Checkpoint`             — Pydantic model of a single row.
* :class:`CheckpointManager`      — async SQLite wrapper. Same lifecycle
                                   idiom as :class:`~kernel.registry.AgentRegistry`:
                                   ``async with CheckpointManager(...) as mgr:``.

Mutate chain and recovery hierarchy
-----------------------------------

The :class:`~kernel.recovery.RecoveryExecutor` writes checkpoints
*after* every step. A resume-on-boot pass (see
:mod:`kernel.resume`) reads the most recent checkpoint per workflow
and re-emits a ``workflow_resume`` event to the Main Agent. The Main
Agent is then the only entity that decides what to do next.

Failure mode coverage
---------------------

If the kernel dies mid-step, no checkpoint for that step exists, so on
resume the workflow re-runs the unfinished step. If a checkpoint write
itself fails, the manager logs and raises; the kernel stops the
workflow rather than silently losing recovery data.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiosqlite
from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

CHECKPOINTS_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS checkpoints (
    checkpoint_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    workflow_id     TEXT NOT NULL,
    step_id         TEXT NOT NULL,
    state_blob      TEXT NOT NULL,
    agent_outputs   TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'completed',
    mutate_count    INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_checkpoints_workflow
    ON checkpoints(workflow_id, checkpoint_id DESC);
"""


def _utcnow_iso() -> str:
    """Return the current UTC time as an ISO 8601 string with a Z suffix."""
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# Pydantic model
# ---------------------------------------------------------------------------

WorkflowStatusLiteral = str
"""Workflow status (free-form; matches the workflow contract enum)."""


class Checkpoint(BaseModel):
    """A single persisted workflow checkpoint.

    Mirrors one row in the ``checkpoints`` table. ``checkpoint_id`` is
    assigned by SQLite on insert; ``last_step_id`` and ``next_step_id``
    are convenience accessors for the resume UI.
    """

    model_config = ConfigDict(extra="forbid")

    checkpoint_id: int
    workflow_id: str
    step_id: str
    state_blob: dict[str, Any] = Field(default_factory=dict)
    agent_outputs: dict[str, Any] = Field(default_factory=dict)
    status: str = "completed"
    mutate_count: int = 0
    created_at: str

    @property
    def last_step_id(self) -> str:
        """The step this checkpoint is *for* (alias for ``step_id``)."""
        return self.step_id

    @property
    def next_step_id(self) -> str | None:
        """Resume-from hint stored inside ``state_blob`` if present.

        Workflow executors can set ``state_blob["next_step_id"]`` when
        writing the checkpoint to tell the resume code which step to
        continue from. Returns ``None`` if not present.
        """
        return self.state_blob.get("next_step_id") if isinstance(self.state_blob, dict) else None


# ---------------------------------------------------------------------------
# Manager
# ---------------------------------------------------------------------------

class CheckpointManager:
    """Async SQLite store for workflow checkpoints.

    The class is safe to share across coroutines. Use as an async
    context manager (``async with CheckpointManager(...) as mgr:``) or
    call :meth:`initialize` and :meth:`close` manually.
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
        await self._db.executescript(CHECKPOINTS_SCHEMA_SQL)
        await self._db.commit()
        self._initialized = True
        logger.info("checkpoint manager initialized db=%s", self._db_path)

    async def close(self) -> None:
        """Close the underlying connection. Idempotent."""
        if self._db is not None:
            await self._db.close()
            self._db = None
        self._initialized = False

    async def __aenter__(self) -> "CheckpointManager":
        await self.initialize()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close()

    def _require_db(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("CheckpointManager used before initialize()")
        return self._db

    # -- write -------------------------------------------------------------

    async def write_checkpoint(
        self,
        workflow_id: str,
        step_id: str,
        state_blob: dict[str, Any],
        agent_outputs: dict[str, Any],
        *,
        status: str = "completed",
        mutate_count: int = 0,
    ) -> Checkpoint:
        """Insert a new checkpoint row and return the resulting model.

        :raises RuntimeError: when called before :meth:`initialize`.
        """
        db = self._require_db()
        now = _utcnow_iso()
        try:
            # Use a strict encoder: we want the call to fail loudly if
            # the caller shoved a ``set`` or other non-JSON value into
            # the blob. ``default=str`` would silently coerce to a
            # string, which is exactly the bug we want to surface.
            state_json = json.dumps(state_blob)
            outputs_json = json.dumps(agent_outputs)
        except (TypeError, ValueError) as exc:
            raise RuntimeError(
                f"checkpoint for {workflow_id!r}/{step_id!r} contains "
                f"non-serializable data: {exc}"
            ) from exc
        async with self._write_lock:
            cur = await db.execute(
                """
                INSERT INTO checkpoints
                    (workflow_id, step_id, state_blob, agent_outputs,
                     status, mutate_count, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    workflow_id,
                    step_id,
                    state_json,
                    outputs_json,
                    status,
                    int(mutate_count),
                    now,
                ),
            )
            checkpoint_id = cur.lastrowid or 0
            await db.commit()
        logger.debug(
            "checkpoint written workflow_id=%s step_id=%s id=%d",
            workflow_id, step_id, checkpoint_id,
        )
        return Checkpoint(
            checkpoint_id=checkpoint_id,
            workflow_id=workflow_id,
            step_id=step_id,
            state_blob=state_blob,
            agent_outputs=agent_outputs,
            status=status,
            mutate_count=int(mutate_count),
            created_at=now,
        )

    # -- read --------------------------------------------------------------

    async def get_checkpoint(self, checkpoint_id: int) -> Checkpoint | None:
        """Fetch a specific checkpoint by id, or ``None`` if not found."""
        db = self._require_db()
        async with db.execute(
            """
            SELECT checkpoint_id, workflow_id, step_id, state_blob,
                   agent_outputs, status, mutate_count, created_at
            FROM checkpoints
            WHERE checkpoint_id = ?
            """,
            (checkpoint_id,),
        ) as cur:
            row = await cur.fetchone()
        return _row_to_checkpoint(row) if row is not None else None

    async def get_latest_checkpoint(
        self, workflow_id: str
    ) -> Checkpoint | None:
        """Return the most recent checkpoint for ``workflow_id``.

        ``None`` when the workflow has no checkpoints (never executed
        a step successfully).
        """
        db = self._require_db()
        async with db.execute(
            """
            SELECT checkpoint_id, workflow_id, step_id, state_blob,
                   agent_outputs, status, mutate_count, created_at
            FROM checkpoints
            WHERE workflow_id = ?
            ORDER BY checkpoint_id DESC
            LIMIT 1
            """,
            (workflow_id,),
        ) as cur:
            row = await cur.fetchone()
        return _row_to_checkpoint(row) if row is not None else None

    async def list_checkpoints(
        self, workflow_id: str
    ) -> list[Checkpoint]:
        """All checkpoints for ``workflow_id`` (oldest first).

        Used by the replay UI to show a workflow's full step history.
        """
        db = self._require_db()
        out: list[Checkpoint] = []
        async with db.execute(
            """
            SELECT checkpoint_id, workflow_id, step_id, state_blob,
                   agent_outputs, status, mutate_count, created_at
            FROM checkpoints
            WHERE workflow_id = ?
            ORDER BY checkpoint_id ASC
            """,
            (workflow_id,),
        ) as cur:
            async for row in cur:
                cp = _row_to_checkpoint(row)
                if cp is not None:
                    out.append(cp)
        return out

    async def count(self, workflow_id: str | None = None) -> int:
        """Return the number of checkpoints, optionally filtered by workflow."""
        db = self._require_db()
        if workflow_id is None:
            sql = "SELECT COUNT(*) FROM checkpoints"
            params: tuple[Any, ...] = ()
        else:
            sql = "SELECT COUNT(*) FROM checkpoints WHERE workflow_id = ?"
            params = (workflow_id,)
        async with db.execute(sql, params) as cur:
            row = await cur.fetchone()
        return int(row[0]) if row else 0

    # -- mutate-count helpers ---------------------------------------------

    async def get_mutate_count(
        self, workflow_id: str, step_id: str
    ) -> int:
        """Return the largest ``mutate_count`` recorded for the given step.

        The recovery executor writes a fresh checkpoint every time it
        bumps the mutate counter, so the latest row for the step is
        the authoritative value.
        """
        db = self._require_db()
        async with db.execute(
            """
            SELECT mutate_count
            FROM checkpoints
            WHERE workflow_id = ? AND step_id = ?
            ORDER BY checkpoint_id DESC
            LIMIT 1
            """,
            (workflow_id, step_id),
        ) as cur:
            row = await cur.fetchone()
        return int(row[0]) if row else 0

    # -- resume ------------------------------------------------------------

    async def resume_from_checkpoint(
        self,
        workflow_id: str,
        checkpoint: Checkpoint,
    ) -> dict[str, Any]:
        """Rebuild a workflow's execution state from a checkpoint.

        The returned dict is the minimal state the kernel needs to
        continue executing the workflow:

        * ``workflow_id``              — passthrough.
        * ``last_step_id``             — step the checkpoint is for.
        * ``next_step_id``             — step to resume from (the
                                        ``state_blob["next_step_id"]``
                                        hint if present, else the
                                        checkpoint's own ``step_id``,
                                        i.e. re-run it).
        * ``state_blob``               — opaque executor state.
        * ``agent_outputs``            — outputs from prior steps.
        * ``mutate_count``             — number of mutates already used.
        * ``resume_strategy``          — always ``"continue_from_step"``
                                        on the kernel side; the Main
                                        Agent may override.

        The kernel never decides recovery strategy — it only rebuilds
        the state and hands it to the Main Agent.
        """
        return {
            "workflow_id": workflow_id,
            "last_step_id": checkpoint.step_id,
            "next_step_id": checkpoint.next_step_id or checkpoint.step_id,
            "state_blob": dict(checkpoint.state_blob),
            "agent_outputs": dict(checkpoint.agent_outputs),
            "mutate_count": int(checkpoint.mutate_count),
            "resume_strategy": "continue_from_step",
            "checkpoint_id": checkpoint.checkpoint_id,
            "checkpoint_created_at": checkpoint.created_at,
        }

    # -- delete ------------------------------------------------------------

    async def delete_for_workflow(self, workflow_id: str) -> int:
        """Remove every checkpoint for ``workflow_id``. Returns the row count.

        Used by the workflow executor on terminal completion (success
        or cancel) to free disk space. The dashboard keeps a small
        summary; the kernel is the only one that owns the raw rows.
        """
        db = self._require_db()
        async with self._write_lock:
            cur = await db.execute(
                "DELETE FROM checkpoints WHERE workflow_id = ?",
                (workflow_id,),
            )
            await db.commit()
            return int(cur.rowcount or 0)


# ---------------------------------------------------------------------------
# Row → model helper
# ---------------------------------------------------------------------------

def _row_to_checkpoint(row: tuple[Any, ...]) -> Checkpoint | None:
    """Convert a ``SELECT`` row into a :class:`Checkpoint`.

    Returns ``None`` on a malformed row instead of raising — callers
    treat that as "not found".
    """
    if row is None:
        return None
    try:
        (
            checkpoint_id,
            workflow_id,
            step_id,
            state_blob,
            agent_outputs,
            status,
            mutate_count,
            created_at,
        ) = row
    except ValueError:
        logger.warning("malformed checkpoint row: %r", row)
        return None
    try:
        state = json.loads(state_blob) if state_blob else {}
    except (TypeError, ValueError):
        state = {}
    try:
        outputs = json.loads(agent_outputs) if agent_outputs else {}
    except (TypeError, ValueError):
        outputs = {}
    return Checkpoint(
        checkpoint_id=int(checkpoint_id),
        workflow_id=str(workflow_id),
        step_id=str(step_id),
        state_blob=state,
        agent_outputs=outputs,
        status=str(status),
        mutate_count=int(mutate_count or 0),
        created_at=str(created_at),
    )


__all__ = [
    "CHECKPOINTS_SCHEMA_SQL",
    "Checkpoint",
    "CheckpointManager",
]
