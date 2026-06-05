"""Loop registry - SQLite-backed template store."""

import asyncio
import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Generator

from .graph import LoopGraph


@dataclass
class LoopStats:
    """Statistics for a loop template.

    Attributes:
        success_rate: Fraction of successful executions (0.0-1.0).
        avg_score: Average critic score (1-10).
        avg_cost_usd: Average cost per execution.
        avg_latency_ms: Average latency in milliseconds.
        usage_count: Number of times this template was used.
    """
    success_rate: float = 0.0
    avg_score: float = 0.0
    avg_cost_usd: float = 0.0
    avg_latency_ms: float = 0.0
    usage_count: int = 0


class LoopRegistry:
    """SQLite-backed loop template registry.

    Stores loop graph templates and tracks their performance statistics.
    """

    def __init__(self, db_path: str | None = None):
        """Initialize the loop registry.

        Args:
            db_path: Path to SQLite database. If None, uses in-memory database.
        """
        if db_path is None:
            self._db_path = ":memory:"
        else:
            self._db_path = db_path

        # Each thread gets its own connection (sqlite3 connections are
        # not safe to share across threads).  ``threading.local`` holds
        # a per-thread ``sqlite3.Connection`` so sync and
        # ``asyncio.to_thread`` callers can coexist.
        self._tls = threading.local()
        # Schema init is per-process, guarded by this lock; the first
        # connection on each thread re-runs the (idempotent) ``CREATE
        # TABLE IF NOT EXISTS`` statements.
        self._init_lock = threading.Lock()
        # Apply the schema on the current thread so single-threaded
        # callers (and tests) see a working DB right away.
        self._init_db()

    def _init_db(self) -> None:
        """Apply schema to the current thread's connection.

        Delegates to :meth:`_get_conn`, which is thread-safe.
        """
        with self._get_conn():
            pass

    @contextmanager
    def _get_conn(self) -> Generator[sqlite3.Connection, None, None]:
        """Get a database connection, opening one per thread if needed.

        Yields:
            SQLite connection.
        """
        conn = getattr(self._tls, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self._db_path)
            conn.row_factory = sqlite3.Row
            self._tls.conn = conn
            # First connection on this thread — apply the schema.
            with self._init_lock:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS loop_templates (
                        id TEXT PRIMARY KEY,
                        name TEXT NOT NULL,
                        description TEXT,
                        graph_json TEXT NOT NULL,
                        task_type TEXT,
                        success_rate REAL DEFAULT 0.0,
                        avg_score REAL DEFAULT 0.0,
                        avg_cost_usd REAL DEFAULT 0.0,
                        avg_latency_ms REAL DEFAULT 0.0,
                        usage_count INTEGER DEFAULT 0,
                        created_at TEXT,
                        updated_at TEXT,
                        is_premade BOOLEAN DEFAULT FALSE
                    )
                """)
                conn.execute("""
                    CREATE INDEX IF NOT EXISTS idx_task_type
                    ON loop_templates(task_type)
                """)
                conn.execute("""
                    CREATE INDEX IF NOT EXISTS idx_success_rate
                    ON loop_templates(success_rate DESC)
                """)
                conn.commit()

        try:
            yield conn
        finally:
            conn.commit()

    def register_template(
        self,
        graph: LoopGraph,
        task_type: str | None = None,
        is_premade: bool = False,
    ) -> None:
        """Register a new loop template.

        Args:
            graph: The LoopGraph to register.
            task_type: Optional task type (coding, review, research, etc.).
            is_premade: Whether this is a premade template.
        """
        with self._get_conn() as conn:
            now = datetime.now(timezone.utc).isoformat()

            existing = conn.execute(
                "SELECT id FROM loop_templates WHERE id = ?",
                (graph.id,),
            ).fetchone()

            if existing:
                conn.execute("""
                    UPDATE loop_templates
                    SET name = ?, description = ?, graph_json = ?, task_type = ?,
                        updated_at = ?
                    WHERE id = ?
                """, (
                    graph.name,
                    graph.description,
                    json.dumps(graph.to_dict()),
                    task_type,
                    now,
                    graph.id,
                ))
            else:
                conn.execute("""
                    INSERT INTO loop_templates
                    (id, name, description, graph_json, task_type, created_at,
                     updated_at, is_premade)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    graph.id,
                    graph.name,
                    graph.description,
                    json.dumps(graph.to_dict()),
                    task_type,
                    now,
                    now,
                    is_premade,
                ))

    def get_template(self, template_id: str) -> LoopGraph | None:
        """Get a template by ID.

        Args:
            template_id: The template ID.

        Returns:
            LoopGraph or None if not found.
        """
        with self._get_conn() as conn:
            row = conn.execute(
                "SELECT graph_json FROM loop_templates WHERE id = ?",
                (template_id,),
            ).fetchone()

            if row is None:
                return None

            graph_dict = json.loads(row["graph_json"])
            return LoopGraph.from_dict(graph_dict)

    def list_templates(
        self,
        task_type: str | None = None,
        min_success_rate: float = 0.0,
    ) -> list[dict[str, Any]]:
        """List templates, optionally filtered.

        Args:
            task_type: Filter by task type.
            min_success_rate: Minimum success rate filter.

        Returns:
            List of template dicts with stats.
        """
        with self._get_conn() as conn:
            query = """
                SELECT id, name, description, task_type, success_rate, avg_score,
                       avg_cost_usd, avg_latency_ms, usage_count, is_premade
                FROM loop_templates
                WHERE success_rate >= ?
            """
            params: list[Any] = [min_success_rate]

            if task_type:
                query += " AND task_type = ?"
                params.append(task_type)

            query += " ORDER BY success_rate DESC, avg_score DESC"

            rows = conn.execute(query, params).fetchall()

            return [dict(row) for row in rows]

    def update_stats(
        self,
        template_id: str,
        score: float,
        cost: float,
        latency: float,
        success: bool = True,
    ) -> None:
        """Update running statistics for a template.

        Uses incremental average formula to update running stats.

        Args:
            template_id: Template ID.
            score: Critic score (1-10).
            cost: Cost in USD.
            latency: Latency in milliseconds.
            success: Whether execution was successful.
        """
        with self._get_conn() as conn:
            row = conn.execute(
                """
                SELECT usage_count, success_rate, avg_score, avg_cost_usd,
                       avg_latency_ms
                FROM loop_templates WHERE id = ?
                """,
                (template_id,),
            ).fetchone()

            if row is None:
                return

            n = row["usage_count"]
            old_success_rate = row["success_rate"]
            old_avg_score = row["avg_score"]
            old_avg_cost = row["avg_cost_usd"]
            old_avg_latency = row["avg_latency_ms"]

            new_n = n + 1

            new_success_rate = old_success_rate + ((1.0 if success else 0.0) - old_success_rate) / new_n

            new_avg_score = old_avg_score + (score - old_avg_score) / new_n

            new_avg_cost = old_avg_cost + (cost - old_avg_cost) / new_n

            new_avg_latency = old_avg_latency + (latency - old_avg_latency) / new_n

            now = datetime.now(timezone.utc).isoformat()

            conn.execute("""
                UPDATE loop_templates
                SET usage_count = ?, success_rate = ?, avg_score = ?,
                    avg_cost_usd = ?, avg_latency_ms = ?, updated_at = ?
                WHERE id = ?
            """, (
                new_n,
                new_success_rate,
                new_avg_score,
                new_avg_cost,
                new_avg_latency,
                now,
                template_id,
            ))

    def get_stats(self, template_id: str) -> LoopStats | None:
        """Get statistics for a template.

        Args:
            template_id: Template ID.

        Returns:
            LoopStats or None if not found.
        """
        with self._get_conn() as conn:
            row = conn.execute(
                """
                SELECT success_rate, avg_score, avg_cost_usd, avg_latency_ms,
                       usage_count
                FROM loop_templates WHERE id = ?
                """,
                (template_id,),
            ).fetchone()

            if row is None:
                return None

            return LoopStats(
                success_rate=row["success_rate"],
                avg_score=row["avg_score"],
                avg_cost_usd=row["avg_cost_usd"],
                avg_latency_ms=row["avg_latency_ms"],
                usage_count=row["usage_count"],
            )

    def get_recommendation(
        self,
        task_type: str,
        budget_usd: float | None = None,
        limit: int = 3,
    ) -> list[dict[str, Any]]:
        """Get recommended templates for a task type.

        Score = (avg_score * 0.6) + (1/avg_cost * 0.3) + (1/avg_latency * 0.1)

        Args:
            task_type: Task type to get recommendations for.
            budget_usd: Optional maximum cost budget.
            limit: Maximum number of recommendations.

        Returns:
            List of recommended templates with scores.
        """
        templates = self.list_templates(task_type=task_type, min_success_rate=0.0)

        if not templates:
            templates = self.list_templates(min_success_rate=0.0)

        recommendations = []

        for t in templates:
            if budget_usd and t.get("avg_cost_usd", 0) > budget_usd:
                continue

            avg_score = t.get("avg_score", 5.0)
            avg_cost = t.get("avg_cost_usd", 0.001)
            avg_latency = t.get("avg_latency_ms", 1.0)

            score = (avg_score / 10.0 * 0.6) + (1.0 / (avg_cost + 0.001) * 0.3) + (1.0 / (avg_latency + 1.0) * 0.1)

            recommendations.append({
                "id": t["id"],
                "name": t["name"],
                "description": t.get("description"),
                "recommendation_score": score,
                "success_rate": t.get("success_rate", 0.0),
                "avg_score": avg_score,
                "avg_cost_usd": avg_cost,
                "avg_latency_ms": avg_latency,
                "usage_count": t.get("usage_count", 0),
            })

        recommendations.sort(key=lambda x: x["recommendation_score"], reverse=True)

        return recommendations[:limit]

    def delete_template(self, template_id: str) -> bool:
        """Delete a template.

        Args:
            template_id: Template ID to delete.

        Returns:
            True if deleted, False if not found.
        """
        with self._get_conn() as conn:
            cursor = conn.execute(
                "DELETE FROM loop_templates WHERE id = ?",
                (template_id,),
            )
            return cursor.rowcount > 0

    def insert_premade_templates(self) -> None:
        """Insert premade loop templates at boot.

        Creates the standard set of premade loops:
        - direct: single generate node
        - cot: generate with CoT prefix
        - reflection: generate -> critique -> revise
        - tree: branch(3) -> vote -> merge
        - debate: generate(FOR) -> generate(AGAINST) -> vote
        - ensemble: generate(model1) -> generate(model2) -> vote
        """
        templates = [
            LoopGraph.direct_graph("direct"),
            LoopGraph.cot_graph("cot"),
            LoopGraph.reflection_graph("reflection"),
            LoopGraph.tree_graph("tree", branch_count=3),
            LoopGraph.debate_graph("debate"),
            LoopGraph.ensemble_graph("ensemble"),
        ]

        task_types = {
            "direct": "general",
            "cot": "reasoning",
            "reflection": "review",
            "tree": "design",
            "debate": "analysis",
            "ensemble": "general",
        }

        for graph in templates:
            self.register_template(graph, task_type=task_types.get(graph.id), is_premade=True)

    def count(self) -> int:
        """Get total number of templates.

        Returns:
            Number of templates in registry.
        """
        with self._get_conn() as conn:
            row = conn.execute("SELECT COUNT(*) as count FROM loop_templates").fetchone()
            return row["count"] if row else 0

    # -- async wrappers ----------------------------------------------------
    #
    # Phase 6 callers (ContextAssembler, MemoryRouter) need an async
    # surface so they can ``await`` without blocking the event loop.  The
    # underlying ``sqlite3`` connection is sync, so each async method
    # just delegates to the sync implementation via ``asyncio.to_thread``.
    # The wrappers are intentionally thin — no extra logic, no caching.

    async def aget_template(self, template_id: str) -> LoopGraph | None:
        """Async wrapper around :meth:`get_template`."""
        return await asyncio.to_thread(self.get_template, template_id)

    async def alist_templates(
        self,
        task_type: str | None = None,
        min_success_rate: float = 0.0,
    ) -> list[dict[str, Any]]:
        """Async wrapper around :meth:`list_templates`."""
        return await asyncio.to_thread(
            self.list_templates,
            task_type,
            min_success_rate,
        )

    async def aupdate_stats(
        self,
        template_id: str,
        score: float,
        cost: float,
        latency: float,
        success: bool = True,
    ) -> None:
        """Async wrapper around :meth:`update_stats`."""
        await asyncio.to_thread(
            self.update_stats,
            template_id,
            score,
            cost,
            latency,
            success,
        )

    async def aget_recommendation(
        self,
        task_type: str,
        budget_usd: float | None = None,
        limit: int = 3,
    ) -> list[dict[str, Any]]:
        """Async wrapper around :meth:`get_recommendation`."""
        return await asyncio.to_thread(
            self.get_recommendation,
            task_type,
            budget_usd,
            limit,
        )

    async def acount(self) -> int:
        """Async wrapper around :meth:`count`."""
        return await asyncio.to_thread(self.count)


def create_registry(db_path: str | None = None) -> LoopRegistry:
    """Create and initialize a loop registry.

    Args:
        db_path: Optional path to SQLite database.

    Returns:
        Initialized LoopRegistry with premade templates.
    """
    registry = LoopRegistry(db_path)
    registry.insert_premade_templates()
    return registry