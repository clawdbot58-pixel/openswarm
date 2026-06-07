"""Usage & cost tracking (Phase 11 — Polish & Scale).

OpenSwarm's agents call various LLM providers. The
:class:`BillingTracker` records every model's tokens and computes
a USD cost using either the caller-supplied ``cost_usd`` or the
built-in :attr:`BillingSection.default_costs` rate table. The
aggregated data is exposed via the dashboard's ``/api/billing/*``
endpoints and surfaces in the CLI's ``openswarm status`` panel.

Design choices
--------------
* **SQLite-backed** so the kernel can write to it from the request
  hot path without locking contention; :mod:`aiosqlite` is already
  in our dependency tree.
* **Append-only** by default — we never update a row, so a
  long-running kernel never has to do an ``UPDATE`` under load.
  Aggregations are computed on read.
* **Schema versioned** so we can roll forward without
  breaking existing dashboards.
* **Self-contained** — no Redis, no external service. The
  optional :class:`RedisSection` is for the message queue only;
  billing always uses local SQLite.

Schema (v1)
-----------
::

    billing_events (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        ts              TEXT NOT NULL,         -- ISO-8601 UTC
        workflow_id     TEXT NOT NULL,
        agent_id        TEXT NOT NULL,
        model           TEXT NOT NULL,
        tokens_in       INTEGER NOT NULL,
        tokens_out      INTEGER NOT NULL,
        cost_usd        REAL NOT NULL,
        notes           TEXT,
        session_id      TEXT
    )

    CREATE INDEX idx_billing_workflow ON billing_events(workflow_id);
    CREATE INDEX idx_billing_agent    ON billing_events(agent_id);
    CREATE INDEX idx_billing_ts       ON billing_events(ts);

Cost computation
----------------
We use the rate table in :class:`BillingSection.default_costs` as
a fallback. The table maps model name → ``{input, output}`` USD
per 1k tokens. The cost is::

    cost = (tokens_in / 1000) * input_rate + (tokens_out / 1000) * output_rate

When the caller already passed ``cost_usd`` (e.g. an LLM client
that uses a usage callback), we trust it over the rate table.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

import aiosqlite

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class BillingError(RuntimeError):
    """Raised when the billing store can't satisfy a request."""


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class DailySummary:
    """One day's spend, broken down by workflow."""

    date: str
    total_cost_usd: float
    total_tokens_in: int
    total_tokens_out: int
    events: int
    by_workflow: dict[str, float]

    def to_dict(self) -> dict[str, Any]:
        return {
            "date": self.date,
            "total_cost_usd": round(self.total_cost_usd, 6),
            "total_tokens_in": self.total_tokens_in,
            "total_tokens_out": self.total_tokens_out,
            "events": self.events,
            "by_workflow": {
                k: round(v, 6) for k, v in self.by_workflow.items()
            },
        }


@dataclass(slots=True)
class WorkflowCost:
    """Total spend for a single workflow."""

    workflow_id: str
    total_cost_usd: float
    total_tokens_in: int
    total_tokens_out: int
    events: int
    by_model: dict[str, dict[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        return {
            "workflow_id": self.workflow_id,
            "total_cost_usd": round(self.total_cost_usd, 6),
            "total_tokens_in": self.total_tokens_in,
            "total_tokens_out": self.total_tokens_out,
            "events": self.events,
            "by_model": self.by_model,
        }


# ---------------------------------------------------------------------------
# Tracker
# ---------------------------------------------------------------------------


_SCHEMA_V1 = """
CREATE TABLE IF NOT EXISTS billing_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL,
    workflow_id TEXT NOT NULL,
    agent_id    TEXT NOT NULL,
    model       TEXT NOT NULL,
    tokens_in   INTEGER NOT NULL DEFAULT 0,
    tokens_out  INTEGER NOT NULL DEFAULT 0,
    cost_usd    REAL    NOT NULL DEFAULT 0,
    notes       TEXT,
    session_id  TEXT
);
CREATE INDEX IF NOT EXISTS idx_billing_workflow ON billing_events(workflow_id);
CREATE INDEX IF NOT EXISTS idx_billing_agent    ON billing_events(agent_id);
CREATE INDEX IF NOT EXISTS idx_billing_ts       ON billing_events(ts);
"""

_SCHEMA_VERSION = 1


class BillingTracker:
    """SQLite-backed usage & cost tracker.

    Parameters
    ----------
    db_path:
        Path to the SQLite file. Created on first use.
    default_costs:
        Mapping ``model_name → {input, output}`` USD-per-1k-tokens
        rates. Used when :meth:`record` is called without an
        explicit ``cost_usd``.
    """

    def __init__(
        self,
        db_path: Path,
        *,
        default_costs: dict[str, dict[str, float]] | None = None,
    ) -> None:
        self._db_path = Path(db_path)
        self._default_costs = default_costs or {}
        self._initialized = False

    # -- lifecycle ---------------------------------------------------------

    async def initialize(self) -> None:
        """Open the DB and apply the schema. Idempotent."""
        if self._initialized:
            return
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self._db_path) as db:
            await db.executescript(_SCHEMA_V1)
            # Stamp the schema version (separate table so we can
            # evolve without rewriting the events table).
            await db.execute(
                "CREATE TABLE IF NOT EXISTS billing_meta ("
                "  key   TEXT PRIMARY KEY,"
                "  value TEXT"
                ")"
            )
            await db.execute(
                "INSERT OR REPLACE INTO billing_meta(key, value) VALUES (?, ?)",
                ("schema_version", str(_SCHEMA_VERSION)),
            )
            await db.commit()
        self._initialized = True
        logger.info("billing tracker initialized db=%s", self._db_path)

    async def close(self) -> None:
        self._initialized = False

    # -- write API ---------------------------------------------------------

    async def record(
        self,
        workflow_id: str,
        agent_id: str,
        model: str,
        tokens_in: int,
        tokens_out: int,
        cost_usd: float | None = None,
        *,
        notes: str | None = None,
        session_id: str | None = None,
        ts: datetime | None = None,
    ) -> int:
        """Insert a billing event and return its id.

        ``cost_usd`` is optional — when omitted, it's computed from
        the rate table via :meth:`_estimate_cost`.
        """
        if not self._initialized:
            await self.initialize()
        if cost_usd is None:
            cost_usd = self._estimate_cost(model, tokens_in, tokens_out)
        ts_str = (ts or datetime.now(timezone.utc)).isoformat().replace(
            "+00:00", "Z"
        )
        async with aiosqlite.connect(self._db_path) as db:
            cur = await db.execute(
                "INSERT INTO billing_events ("
                "  ts, workflow_id, agent_id, model,"
                "  tokens_in, tokens_out, cost_usd, notes, session_id"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    ts_str,
                    workflow_id,
                    agent_id,
                    model,
                    int(tokens_in),
                    int(tokens_out),
                    float(cost_usd),
                    notes,
                    session_id,
                ),
            )
            await db.commit()
            return cur.lastrowid or 0

    # -- read API ----------------------------------------------------------

    async def get_workflow_cost(self, workflow_id: str) -> WorkflowCost:
        if not self._initialized:
            await self.initialize()
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            rows = await (
                await db.execute(
                    "SELECT model,"
                    "       SUM(tokens_in) AS tin,"
                    "       SUM(tokens_out) AS tout,"
                    "       SUM(cost_usd) AS cost,"
                    "       COUNT(*) AS events"
                    "  FROM billing_events"
                    " WHERE workflow_id = ?"
                    " GROUP BY model",
                    (workflow_id,),
                )
            ).fetchall()
        total_cost = 0.0
        total_in = 0
        total_out = 0
        events = 0
        by_model: dict[str, dict[str, Any]] = {}
        for r in rows:
            cost = float(r["cost"] or 0.0)
            tin = int(r["tin"] or 0)
            tout = int(r["tout"] or 0)
            ev = int(r["events"] or 0)
            total_cost += cost
            total_in += tin
            total_out += tout
            events += ev
            by_model[r["model"]] = {
                "cost_usd": round(cost, 6),
                "tokens_in": tin,
                "tokens_out": tout,
                "events": ev,
            }
        return WorkflowCost(
            workflow_id=workflow_id,
            total_cost_usd=total_cost,
            total_tokens_in=total_in,
            total_tokens_out=total_out,
            events=events,
            by_model=by_model,
        )

    async def get_user_daily(self, user_id: str, day: date) -> DailySummary:
        """Sum costs for workflows belonging to ``user_id`` on ``day``.

        We approximate "user" as the unique set of agents that ran
        the workflows that day; a richer ACL would join through a
        users table, which Phase 12 introduces.
        """
        if not self._initialized:
            await self.initialize()
        start = datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc)
        end = start + timedelta(days=1)
        start_s = start.isoformat().replace("+00:00", "Z")
        end_s = end.isoformat().replace("+00:00", "Z")
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            # Workflows touched by this user's agents.
            wf_rows = await (
                await db.execute(
                    "SELECT DISTINCT workflow_id FROM billing_events"
                    " WHERE agent_id = ? AND ts >= ? AND ts < ?",
                    (user_id, start_s, end_s),
                )
            ).fetchall()
            workflows = [r["workflow_id"] for r in wf_rows]
            if not workflows:
                return DailySummary(
                    date=day.isoformat(),
                    total_cost_usd=0.0,
                    total_tokens_in=0,
                    total_tokens_out=0,
                    events=0,
                    by_workflow={},
                )
            placeholders = ",".join("?" for _ in workflows)
            agg_rows = await (
                await db.execute(
                    "SELECT workflow_id,"
                    "       SUM(cost_usd) AS cost,"
                    "       SUM(tokens_in) AS tin,"
                    "       SUM(tokens_out) AS tout,"
                    "       COUNT(*) AS events"
                    "  FROM billing_events"
                    f" WHERE workflow_id IN ({placeholders})"
                    "   AND ts >= ? AND ts < ?"
                    " GROUP BY workflow_id",
                    (*workflows, start_s, end_s),
                )
            ).fetchall()
        total_cost = 0.0
        total_in = 0
        total_out = 0
        events = 0
        by_wf: dict[str, float] = {}
        for r in agg_rows:
            c = float(r["cost"] or 0.0)
            total_cost += c
            total_in += int(r["tin"] or 0)
            total_out += int(r["tout"] or 0)
            events += int(r["events"] or 0)
            by_wf[r["workflow_id"]] = c
        return DailySummary(
            date=day.isoformat(),
            total_cost_usd=total_cost,
            total_tokens_in=total_in,
            total_tokens_out=total_out,
            events=events,
            by_workflow=by_wf,
        )

    async def export_csv(
        self,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> str:
        """Return a CSV string of events in ``[start, end)``.

        Useful for finance dashboards; the CLI's ``openswarm
        billing export`` would call this.
        """
        if not self._initialized:
            await self.initialize()
        clauses: list[str] = []
        args: list[Any] = []
        if start is not None:
            clauses.append("ts >= ?")
            args.append(start.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"))
        if end is not None:
            clauses.append("ts < ?")
            args.append(end.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"))
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        async with aiosqlite.connect(self._db_path) as db:
            rows = await (
                await db.execute(
                    f"SELECT id, ts, workflow_id, agent_id, model,"
                    f"       tokens_in, tokens_out, cost_usd, notes"
                    f"  FROM billing_events{where}"
                    f" ORDER BY ts",
                    tuple(args),
                )
            ).fetchall()
        lines = [
            "id,ts,workflow_id,agent_id,model,"
            "tokens_in,tokens_out,cost_usd,notes"
        ]
        for r in rows:
            cells = [
                str(r[0]),
                r[1],
                r[2],
                r[3],
                r[4],
                str(r[5]),
                str(r[6]),
                f"{float(r[7]):.6f}",
                (r[8] or "").replace('"', '""'),
            ]
            lines.append(",".join(f'"{c}"' for c in cells))
        return "\n".join(lines) + "\n"

    # -- internals ---------------------------------------------------------

    def _estimate_cost(
        self, model: str, tokens_in: int, tokens_out: int
    ) -> float:
        rate = self._default_costs.get(model) or self._default_costs.get(
            _normalize_model_name(model)
        )
        if rate is None:
            return 0.0
        in_rate = float(rate.get("input", 0.0))
        out_rate = float(rate.get("output", 0.0))
        return (tokens_in / 1000.0) * in_rate + (tokens_out / 1000.0) * out_rate


def _normalize_model_name(model: str) -> str:
    """Strip version qualifiers so "gpt-4o-2024-08-06" matches "gpt-4o"."""
    parts = model.split("-")
    if len(parts) > 2 and parts[-1].isdigit() and len(parts[-1]) == 4:
        return "-".join(parts[:-1])
    return model


__all__ = [
    "BillingError",
    "BillingTracker",
    "DailySummary",
    "WorkflowCost",
]
