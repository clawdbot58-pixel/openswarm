"""Trial store — persistent record of every loop trial/error run.

The :class:`TrialStore` is the Phase 10 equivalent of the Phase 4
:class:`loops.registry.LoopRegistry` "loop_templates" table.  The two
coexist on purpose:

* ``loop_templates`` stores *templates* (premade and ad-hoc graphs)
  and their *aggregate* performance stats (rolling averages).
* ``trials`` stores *individual trial records* (one row per execution,
  immutable).  Aggregations are derived on demand via
  :meth:`TrialStore.get_leaderboard`.

Why two stores?  Templates are mutable (the meta-agent can update
them as it learns), trials are not.  A trial record is the audit log
for one specific run of one specific graph against one specific task;
the leaderboard is a view over those rows.

Storage
-------
* SQLite, one file.  ``db_path=None`` selects an in-memory DB, which
  is what tests use.
* All methods are async wrappers over the sync ``sqlite3`` driver —
  :func:`asyncio.to_thread` keeps the event loop responsive.
* Trial rows are immutable.  :meth:`record_trial` only ever inserts;
  there is no ``update_trial``.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Literal

from pydantic import BaseModel, ConfigDict, Field

from .base_loop import LoopResult
from .critic import CriticScore


# ---------------------------------------------------------------------------
# Default path — matches the dashboard's ``data/loops.db`` convention.
# ---------------------------------------------------------------------------

DEFAULT_DB_PATH: str | None = None  # in-memory by default

#: Default minimum number of trials before a loop appears in the
#: leaderboard.  Single-trial results are noise (one LLM sample
#: tells you almost nothing about a loop's true performance).
DEFAULT_MIN_TRIALS: int = 3


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class Trial(BaseModel):
    """A single recorded execution of a loop on a single task.

    The trial record is immutable.  Once stored, it never changes;
    corrections happen by inserting new trials.

    Attributes:
        trial_id: UUID4 id, generated at insertion time.
        loop_id: The graph's id at the time of execution.
        task_type: Optional tag (e.g. ``"code_review"``).
        loop_graph: The full serialised :class:`LoopGraph` that was
            executed (JSON-safe dict).
        score: The :class:`CriticScore` returned by the critic.
        result: The :class:`LoopResult` returned by the assembler.
        timestamp: UTC timestamp of when the trial finished.
        task_preview: First ~200 chars of the input task, for display.
        output_preview: First ~200 chars of the output, for display.
    """

    model_config = ConfigDict(extra="forbid")

    trial_id: str
    loop_id: str
    task_type: str | None = None
    loop_graph: dict[str, Any]
    score: CriticScore
    result: LoopResult
    timestamp: datetime
    task_preview: str = ""
    output_preview: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a JSON-safe dict (timestamps → ISO 8601)."""
        return {
            "trial_id": self.trial_id,
            "loop_id": self.loop_id,
            "task_type": self.task_type,
            "loop_graph": self.loop_graph,
            "score": self.score.to_dict(),
            "result": {
                "output": self.result.output,
                "confidence": self.result.confidence,
                "tokens_used": self.result.tokens_used,
                "cost_usd": self.result.cost_usd,
                "latency_ms": self.result.latency_ms,
                "iterations": self.result.iterations,
                "intermediate_outputs": self.result.intermediate_outputs,
            },
            "timestamp": self.timestamp.isoformat().replace("+00:00", "Z"),
            "task_preview": self.task_preview,
            "output_preview": self.output_preview,
        }


class LeaderboardEntry(BaseModel):
    """A single row of the leaderboard.

    Averages are over the trials that contributed to the entry; the
    ``trial_count`` and ``last_trial`` fields let callers decide
    whether the entry has enough data to be trusted.

    Attributes:
        loop_id: Loop graph id.
        task_type: ``None`` for the global leaderboard, otherwise the
            task-type tag used to filter.
        avg_score: Average composite score (the spec's formula).
        avg_quality: Average quality score (0-10).
        avg_cost_usd: Average cost per trial.
        avg_latency_ms: Average latency per trial.
        trial_count: Number of trials contributing to the entry.
        last_trial: UTC timestamp of the most recent trial.
        best_variant: Serialised :class:`LoopGraph` of the highest
            composite-scoring trial in the group.
    """

    model_config = ConfigDict(extra="forbid")

    loop_id: str
    task_type: str | None = None
    avg_score: float
    avg_quality: float
    avg_cost_usd: float
    avg_latency_ms: float
    trial_count: int
    last_trial: datetime
    best_variant: dict[str, Any] = Field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        data = self.model_dump()
        data["last_trial"] = self.last_trial.isoformat().replace("+00:00", "Z")
        return data


# ---------------------------------------------------------------------------
# The store
# ---------------------------------------------------------------------------


@dataclass
class _Filter:
    """Internal: the parameter bundle that gates every read query."""

    loop_id: str | None
    task_type: str | None
    limit: int


class TrialStore:
    """SQLite-backed persistent store for trial/error cycle results.

    Args:
        db_path: Path to the SQLite file.  ``None`` selects an
            in-memory database (used by tests).  An empty string is
            treated the same as ``None``.
    """

    SCHEMA: str = """
        CREATE TABLE IF NOT EXISTS trials (
            trial_id        TEXT PRIMARY KEY,
            loop_id         TEXT NOT NULL,
            task_type       TEXT,
            loop_graph_json TEXT NOT NULL,
            score_json      TEXT NOT NULL,
            result_json     TEXT NOT NULL,
            task_preview    TEXT,
            output_preview  TEXT,
            timestamp       TEXT NOT NULL
        )
    """

    def __init__(self, db_path: str | None = DEFAULT_DB_PATH) -> None:
        if db_path == "":
            db_path = None
        self._db_path: str = db_path or ":memory:"
        # ``sqlite3`` connections are not safe to share across threads,
        # so each thread gets its own.  ``threading.local`` holds the
        # per-thread connection and the initialisation lock guards the
        # first CREATE TABLE on each new thread.
        self._tls = threading.local()
        self._init_lock = threading.Lock()
        # Apply the schema on the current thread so single-threaded
        # callers (and tests) see a working DB right away.
        self._init_db()

    # ------------------------------------------------------------------
    # Connection management
    # ------------------------------------------------------------------

    def _init_db(self) -> None:
        with self._get_conn():
            pass

    @contextmanager
    def _get_conn(self) -> Iterator[sqlite3.Connection]:
        conn = getattr(self._tls, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self._db_path)
            conn.row_factory = sqlite3.Row
            self._tls.conn = conn
            with self._init_lock:
                conn.executescript(self.SCHEMA)
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_trials_loop_id "
                    "ON trials(loop_id)"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_trials_task_type "
                    "ON trials(task_type)"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_trials_timestamp "
                    "ON trials(timestamp DESC)"
                )
                conn.commit()
        try:
            yield conn
        finally:
            conn.commit()

    # ------------------------------------------------------------------
    # Writes (immutable — INSERT only)
    # ------------------------------------------------------------------

    def record_trial(
        self,
        loop_id: str,
        task_type: str | None,
        loop_graph: dict[str, Any],
        score: CriticScore,
        result: LoopResult,
        *,
        task_preview: str = "",
    ) -> str:
        """Insert an immutable trial record.

        Args:
            loop_id: Loop graph id.
            task_type: Optional tag (e.g. ``"code_review"``).
            loop_graph: Serialised :class:`LoopGraph` (a plain dict).
            score: The :class:`CriticScore` for this trial.
            result: The :class:`LoopResult` from the assembler.
            task_preview: Optional short task snippet for display.

        Returns:
            The new trial's UUID4 id.
        """
        trial_id = str(uuid.uuid4())
        timestamp = datetime.now(timezone.utc)
        # Backfill the loop_id / task_type on the score so the
        # critic's score can be stored without losing context.
        enriched_score = score.model_copy(
            update={"loop_id": loop_id, "task_type": task_type}
        )
        output_preview = (result.output or "")[:200]
        with self._get_conn() as conn:
            conn.execute(
                """
                INSERT INTO trials (
                    trial_id, loop_id, task_type, loop_graph_json,
                    score_json, result_json, task_preview,
                    output_preview, timestamp
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    trial_id,
                    loop_id,
                    task_type,
                    json.dumps(loop_graph),
                    json.dumps(enriched_score.to_dict()),
                    json.dumps(_result_to_dict(result)),
                    task_preview,
                    output_preview,
                    timestamp.isoformat().replace("+00:00", "Z"),
                ),
            )
        return trial_id

    async def arecord_trial(
        self,
        loop_id: str,
        task_type: str | None,
        loop_graph: dict[str, Any],
        score: CriticScore,
        result: LoopResult,
        *,
        task_preview: str = "",
    ) -> str:
        """Async wrapper around :meth:`record_trial`."""
        return await asyncio.to_thread(
            self.record_trial,
            loop_id,
            task_type,
            loop_graph,
            score,
            result,
            task_preview=task_preview,
        )

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    def get_trials(
        self,
        loop_id: str | None = None,
        task_type: str | None = None,
        limit: int = 100,
    ) -> list[Trial]:
        """Return trials matching the optional filters, newest first."""
        flt = _Filter(loop_id=loop_id, task_type=task_type, limit=limit)
        return self._query_trials(flt)

    async def aget_trials(
        self,
        loop_id: str | None = None,
        task_type: str | None = None,
        limit: int = 100,
    ) -> list[Trial]:
        """Async wrapper around :meth:`get_trials`."""
        return await asyncio.to_thread(
            self.get_trials, loop_id, task_type, limit
        )

    def get_leaderboard(
        self,
        task_type: str | None = None,
        sort_by: Literal["score", "cost", "speed", "trials"] = "score",
        min_trials: int = DEFAULT_MIN_TRIALS,
    ) -> list[LeaderboardEntry]:
        """Aggregate trials into a ranked leaderboard.

        Args:
            task_type: Filter to a single task type.  ``None`` returns
                the global leaderboard.
            sort_by: Which column to rank by:

                * ``"score"`` (default) — highest composite first;
                * ``"cost"`` — cheapest first;
                * ``"speed"`` — fastest first;
                * ``"trials"`` — most-evidence first.
            min_trials: Drop entries with fewer trials.  Defaults to
                :data:`DEFAULT_MIN_TRIALS` (3) — single-trial results
                are noise.

        Returns:
            A list of :class:`LeaderboardEntry`, sorted as requested.
        """
        rows = self._aggregate_by_loop(task_type=task_type)
        entries = [
            LeaderboardEntry(
                loop_id=row["loop_id"],
                task_type=row["task_type"],
                avg_score=row["avg_score"],
                avg_quality=row["avg_quality"],
                avg_cost_usd=row["avg_cost_usd"],
                avg_latency_ms=row["avg_latency_ms"],
                trial_count=row["trial_count"],
                last_trial=row["last_trial"],
                best_variant=row["best_variant"],
            )
            for row in rows
            if row["trial_count"] >= min_trials
        ]
        if sort_by == "score":
            entries.sort(key=lambda e: e.avg_score, reverse=True)
        elif sort_by == "cost":
            entries.sort(key=lambda e: e.avg_cost_usd)
        elif sort_by == "speed":
            entries.sort(key=lambda e: e.avg_latency_ms)
        elif sort_by == "trials":
            entries.sort(key=lambda e: e.trial_count, reverse=True)
        else:
            raise ValueError(
                f"unknown sort_by: {sort_by!r}; "
                "valid: score, cost, speed, trials"
            )
        return entries

    async def aget_leaderboard(
        self,
        task_type: str | None = None,
        sort_by: Literal["score", "cost", "speed", "trials"] = "score",
        min_trials: int = DEFAULT_MIN_TRIALS,
    ) -> list[LeaderboardEntry]:
        """Async wrapper around :meth:`get_leaderboard`."""
        return await asyncio.to_thread(
            self.get_leaderboard, task_type, sort_by, min_trials
        )

    def count(self, loop_id: str | None = None, task_type: str | None = None) -> int:
        """Count trials matching the optional filters."""
        with self._get_conn() as conn:
            sql = "SELECT COUNT(*) AS n FROM trials WHERE 1=1"
            params: list[Any] = []
            if loop_id:
                sql += " AND loop_id = ?"
                params.append(loop_id)
            if task_type:
                sql += " AND task_type = ?"
                params.append(task_type)
            row = conn.execute(sql, params).fetchone()
            return int(row["n"]) if row else 0

    async def acount(
        self, loop_id: str | None = None, task_type: str | None = None
    ) -> int:
        """Async wrapper around :meth:`count`."""
        return await asyncio.to_thread(self.count, loop_id, task_type)

    def delete_trials(self, loop_id: str) -> int:
        """Delete every trial for ``loop_id`` (used by tests).

        Trial rows are immutable in production; this method exists so
        tests can reset state without dropping the whole table.
        """
        with self._get_conn() as conn:
            cur = conn.execute("DELETE FROM trials WHERE loop_id = ?", (loop_id,))
            return cur.rowcount

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _query_trials(self, flt: _Filter) -> list[Trial]:
        sql = [
            "SELECT trial_id, loop_id, task_type, loop_graph_json, score_json,",
            "       result_json, task_preview, output_preview, timestamp",
            "FROM trials WHERE 1=1",
        ]
        params: list[Any] = []
        if flt.loop_id is not None:
            sql.append("AND loop_id = ?")
            params.append(flt.loop_id)
        if flt.task_type is not None:
            sql.append("AND task_type = ?")
            params.append(flt.task_type)
        sql.append("ORDER BY timestamp DESC LIMIT ?")
        params.append(max(1, int(flt.limit)))
        query = "\n".join(sql)
        with self._get_conn() as conn:
            rows = conn.execute(query, params).fetchall()
        return [self._row_to_trial(row) for row in rows]

    def _aggregate_by_loop(
        self, task_type: str | None
    ) -> list[dict[str, Any]]:
        """Group trials by (loop_id, task_type) and compute averages."""
        sql = [
            "SELECT loop_id, task_type,",
            "       AVG(json_extract(score_json, '$.composite_score')) AS avg_score,",
            "       AVG(json_extract(score_json, '$.quality_score')) AS avg_quality,",
            "       AVG(json_extract(result_json, '$.cost_usd')) AS avg_cost_usd,",
            "       AVG(json_extract(result_json, '$.latency_ms')) AS avg_latency_ms,",
            "       COUNT(*) AS trial_count,",
            "       MAX(timestamp) AS last_trial,",
            "       (SELECT loop_graph_json FROM trials t2",
            "          WHERE t2.loop_id = trials.loop_id",
            "            AND (t2.task_type IS trials.task_type",
            "                 OR (t2.task_type IS NULL AND trials.task_type IS NULL))",
            "          ORDER BY json_extract(t2.score_json, '$.composite_score') DESC",
            "          LIMIT 1) AS best_variant_json",
            "FROM trials",
        ]
        params: list[Any] = []
        if task_type is not None:
            sql.append("WHERE task_type = ?")
            params.append(task_type)
        sql.append("GROUP BY loop_id, task_type")
        query = "\n".join(sql)
        with self._get_conn() as conn:
            rows = conn.execute(query, params).fetchall()
        out: list[dict[str, Any]] = []
        for row in rows:
            best_variant_raw = row["best_variant_json"] or "{}"
            try:
                best_variant = json.loads(best_variant_raw)
            except json.JSONDecodeError:
                best_variant = {}
            out.append(
                {
                    "loop_id": str(row["loop_id"]),
                    "task_type": row["task_type"],
                    "avg_score": float(row["avg_score"] or 0.0),
                    "avg_quality": float(row["avg_quality"] or 0.0),
                    "avg_cost_usd": float(row["avg_cost_usd"] or 0.0),
                    "avg_latency_ms": float(row["avg_latency_ms"] or 0.0),
                    "trial_count": int(row["trial_count"] or 0),
                    "last_trial": _parse_iso(str(row["last_trial"]))
                    or datetime.now(timezone.utc),
                    "best_variant": best_variant,
                }
            )
        return out

    @staticmethod
    def _row_to_trial(row: sqlite3.Row) -> Trial:
        loop_graph = json.loads(row["loop_graph_json"] or "{}")
        score_dict = json.loads(row["score_json"] or "{}")
        # ``score_dict`` may contain the stored ``composite_score``
        # (we wrote it via :meth:`CriticScore.to_dict`).  Strip it
        # before re-validating — the field is a derived property, not
        # a real Pydantic field, and ``extra="forbid"`` would reject
        # it otherwise.
        score_dict.pop("composite_score", None)
        score = CriticScore.model_validate(score_dict)
        result_dict = json.loads(row["result_json"] or "{}")
        result = LoopResult(
            output=str(result_dict.get("output", "")),
            confidence=float(result_dict.get("confidence", 0.0)),
            tokens_used=int(result_dict.get("tokens_used", 0)),
            cost_usd=float(result_dict.get("cost_usd", 0.0)),
            latency_ms=float(result_dict.get("latency_ms", 0.0)),
            iterations=int(result_dict.get("iterations", 0)),
            intermediate_outputs=list(result_dict.get("intermediate_outputs", [])),
        )
        return Trial(
            trial_id=str(row["trial_id"]),
            loop_id=str(row["loop_id"]),
            task_type=row["task_type"],
            loop_graph=loop_graph,
            score=score,
            result=result,
            timestamp=_parse_iso(str(row["timestamp"])) or datetime.now(timezone.utc),
            task_preview=str(row["task_preview"] or ""),
            output_preview=str(row["output_preview"] or ""),
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _result_to_dict(result: LoopResult) -> dict[str, Any]:
    """Serialise a :class:`LoopResult` to a JSON-safe dict."""
    return {
        "output": result.output,
        "confidence": result.confidence,
        "tokens_used": result.tokens_used,
        "cost_usd": result.cost_usd,
        "latency_ms": result.latency_ms,
        "iterations": result.iterations,
        "intermediate_outputs": result.intermediate_outputs,
    }


def _parse_iso(value: str | None) -> datetime | None:
    """Parse an ISO 8601 timestamp, returning ``None`` on failure."""
    if not value:
        return None
    try:
        # Tolerate ``Z`` suffix and naive timestamps.
        cleaned = value.replace("Z", "+00:00")
        dt = datetime.fromisoformat(cleaned)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def default_trial_db_path() -> str:
    """Return the canonical default on-disk path (``data/trials.db``)."""
    return str(Path("data") / "trials.db")


__all__ = [
    "DEFAULT_MIN_TRIALS",
    "DEFAULT_DB_PATH",
    "LeaderboardEntry",
    "Trial",
    "TrialStore",
    "default_trial_db_path",
]
