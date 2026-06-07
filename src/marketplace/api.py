"""Marketplace API and local registry (Phase 11).

This module exposes the marketplace surface that the dashboard
and CLI both consume. It does three things:

1. **Local registry** — a SQLite-backed index of every agent
   manifest under :attr:`MarketplaceSection.local_manifests_dir`.
   On boot we scan that directory and index any valid
   ``*.json`` file as a marketplace entry.
2. **Search & filter** — the HTTP surface that powers
   ``GET /api/marketplace/agents`` and friends.
3. **Install / publish** — copy a manifest from the local
   directory to a target dir (install) or write a marketplace
   entry to the catalog (publish).

The remote catalog (``MarketplaceSection.index_url``) is a stub
for Phase 11: we always return the local registry. A future
Phase 12 can add a real client that talks to a public catalog
service.

Security note
-------------
Install copies JSON files into the operator's filesystem.
We **deliberately do not execute** any code in the manifest.
The manifest is validated against
:class:`kernel.models.AgentManifest` on install so a corrupt
or malicious file is rejected before the operator can spawn
the agent. See :doc:`vision/security`.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

import aiosqlite

from .models import (
    CategoryLiteral,
    InstallRequest,
    InstallResponse,
    MarketplaceAgent,
    MarketplaceSearchQuery,
    PublishRequest,
    PublishResponse,
    RatingSummary,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class MarketplaceError(RuntimeError):
    """Base class for marketplace failures."""


class AgentNotFound(MarketplaceError):
    pass


class ManifestInvalid(MarketplaceError):
    pass


# ---------------------------------------------------------------------------
# Local registry
# ---------------------------------------------------------------------------


_SCHEMA = """
CREATE TABLE IF NOT EXISTS marketplace_agents (
    agent_id      TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    description   TEXT NOT NULL DEFAULT '',
    category      TEXT NOT NULL DEFAULT 'general',
    tags          TEXT NOT NULL DEFAULT '[]',
    author        TEXT NOT NULL DEFAULT 'anonymous',
    version       TEXT NOT NULL DEFAULT '1.0.0',
    manifest_json TEXT NOT NULL,
    downloads     INTEGER NOT NULL DEFAULT 0,
    rating        REAL    NOT NULL DEFAULT 0.0,
    rating_count  INTEGER NOT NULL DEFAULT 0,
    published_at  TEXT,
    updated_at    TEXT,
    source_url    TEXT,
    install_count INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_marketplace_category ON marketplace_agents(category);
CREATE INDEX IF NOT EXISTS idx_marketplace_author   ON marketplace_agents(author);
"""


@dataclass(slots=True)
class Registry:
    """SQLite-backed marketplace index.

    The registry is a thin layer over SQLite that supports the
    operations the HTTP surface needs: list, search, install,
    publish, rate. Everything is append-only on writes; reads
    are paged via the offset/limit parameters.
    """

    db_path: Path
    local_manifests_dir: Path

    def __post_init__(self) -> None:
        self._initialized = False

    # -- lifecycle ---------------------------------------------------------

    async def initialize(self) -> None:
        if self._initialized:
            return
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.db_path) as db:
            await db.executescript(_SCHEMA)
            await db.commit()
        self._initialized = True
        # Best-effort: index the local manifests directory.
        try:
            await self._scan_local_manifests()
        except Exception as exc:  # noqa: BLE001
            logger.warning("local manifest scan failed: %s", exc)

    async def close(self) -> None:
        self._initialized = False

    # -- indexing ----------------------------------------------------------

    async def _scan_local_manifests(self) -> int:
        """Read every ``*.json`` under :attr:`local_manifests_dir`.

        Returns the number of new entries added. Existing entries
        are not overwritten — re-indexing is opt-in via
        :meth:`reindex`.
        """
        if not self.local_manifests_dir.is_dir():
            return 0
        added = 0
        for path in sorted(self.local_manifests_dir.glob("*.json")):
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            agent_id = raw.get("agent_id") or path.stem
            try:
                existing = await self._get(agent_id)
            except AgentNotFound:
                existing = None
            if existing is None:
                await self._insert_from_manifest(raw, path)
                added += 1
        return added

    async def reindex(self) -> int:
        """Force a full re-index of :attr:`local_manifests_dir`.

        Overwrites entries whose ``agent_id`` matches a local
        manifest. Used by ``openswarm marketplace refresh``.
        """
        if not self.local_manifests_dir.is_dir():
            return 0
        added = 0
        for path in sorted(self.local_manifests_dir.glob("*.json")):
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            agent_id = raw.get("agent_id") or path.stem
            try:
                await self._get(agent_id)
                await self._update_from_manifest(raw)
            except AgentNotFound:
                await self._insert_from_manifest(raw, path)
            added += 1
        return added

    async def _insert_from_manifest(self, raw: dict, path: Path) -> None:
        agent = _manifest_to_agent(raw, source_path=path)
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT OR REPLACE INTO marketplace_agents ("
                "  agent_id, name, description, category, tags,"
                "  author, version, manifest_json,"
                "  downloads, rating, rating_count, published_at,"
                "  updated_at, source_url, install_count"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    agent.agent_id,
                    agent.name,
                    agent.description,
                    agent.category,
                    json.dumps(agent.tags),
                    agent.author,
                    agent.version,
                    json.dumps(agent.manifest),
                    agent.downloads,
                    agent.rating,
                    agent.rating_count,
                    _iso(agent.published_at),
                    _iso(agent.updated_at),
                    agent.source_url or str(path),
                    agent.install_count_local,
                ),
            )
            await db.commit()

    async def _update_from_manifest(self, raw: dict) -> None:
        agent = _manifest_to_agent(raw)
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "UPDATE marketplace_agents SET"
                "  name = ?, description = ?, category = ?, tags = ?,"
                "  version = ?, manifest_json = ?, updated_at = ?"
                " WHERE agent_id = ?",
                (
                    agent.name,
                    agent.description,
                    agent.category,
                    json.dumps(agent.tags),
                    agent.version,
                    json.dumps(agent.manifest),
                    _iso(datetime.now(timezone.utc)),
                    agent.agent_id,
                ),
            )
            await db.commit()

    # -- read API ----------------------------------------------------------

    async def list_agents(
        self, query: MarketplaceSearchQuery
    ) -> list[MarketplaceAgent]:
        if not self._initialized:
            await self.initialize()
        clauses: list[str] = []
        args: list[Any] = []
        if query.q:
            clauses.append("(LOWER(name) LIKE ? OR LOWER(description) LIKE ?)")
            like = f"%{query.q.lower()}%"
            args.extend([like, like])
        if query.category:
            clauses.append("category = ?")
            args.append(query.category)
        if query.min_rating > 0:
            clauses.append("rating >= ?")
            args.append(float(query.min_rating))
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        order_by = {
            "downloads": "downloads DESC",
            "rating": "rating DESC, rating_count DESC",
            "recent": "COALESCE(updated_at, published_at) DESC",
            "name": "name ASC",
        }.get(query.sort, "downloads DESC")
        sql = (
            "SELECT agent_id, name, description, category, tags,"
            "       author, version, manifest_json,"
            "       downloads, rating, rating_count, published_at,"
            "       updated_at, source_url, install_count"
            f"  FROM marketplace_agents{where}"
            f" ORDER BY {order_by}"
            f" LIMIT ? OFFSET ?"
        )
        args.extend([query.limit, query.offset])
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            rows = await (await db.execute(sql, tuple(args))).fetchall()
        out: list[MarketplaceAgent] = []
        for r in rows:
            out.append(_row_to_agent(r))
        if query.tag:
            tag = query.tag.lower()
            out = [a for a in out if tag in a.tags]
        return out

    async def get(self, agent_id: str) -> MarketplaceAgent:
        record = await self._get(agent_id)
        return record

    async def _get(self, agent_id: str) -> MarketplaceAgent:
        if not self._initialized:
            await self.initialize()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            row = await (
                await db.execute(
                    "SELECT * FROM marketplace_agents WHERE agent_id = ?",
                    (agent_id,),
                )
            ).fetchone()
        if row is None:
            raise AgentNotFound(f"no marketplace agent with id {agent_id!r}")
        return _row_to_agent(row)

    async def count(self) -> int:
        if not self._initialized:
            await self.initialize()
        async with aiosqlite.connect(self.db_path) as db:
            row = await (
                await db.execute("SELECT COUNT(*) AS c FROM marketplace_agents")
            ).fetchone()
        return int(row[0]) if row else 0

    # -- write API ---------------------------------------------------------

    async def install(self, request: InstallRequest) -> InstallResponse:
        if not self._initialized:
            await self.initialize()
        agent = await self._get(request.agent_id)
        target_dir = Path(request.target_dir) if request.target_dir else Path("manifests")
        target_dir.mkdir(parents=True, exist_ok=True)
        safe_name = _safe_filename(agent.agent_id)
        path = target_dir / f"{safe_name}.json"
        path.write_text(
            json.dumps(agent.manifest, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "UPDATE marketplace_agents SET install_count = install_count + 1"
                " WHERE agent_id = ?",
                (agent.agent_id,),
            )
            await db.commit()
        return InstallResponse(
            agent_id=agent.agent_id,
            installed_path=str(path),
            version=agent.version,
        )

    async def publish(self, request: PublishRequest) -> PublishResponse:
        if not self._initialized:
            await self.initialize()
        agent = request.agent
        # Local-registry always accepts the publish; the remote
        # catalog would call a real HTTP endpoint.
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT OR REPLACE INTO marketplace_agents ("
                "  agent_id, name, description, category, tags,"
                "  author, version, manifest_json,"
                "  downloads, rating, rating_count, published_at,"
                "  updated_at, source_url, install_count"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    agent.agent_id,
                    agent.name,
                    agent.description,
                    agent.category,
                    json.dumps(agent.tags),
                    agent.author,
                    agent.version,
                    json.dumps(agent.manifest),
                    agent.downloads,
                    agent.rating,
                    agent.rating_count,
                    _iso(agent.published_at or datetime.now(timezone.utc)),
                    _iso(agent.updated_at or datetime.now(timezone.utc)),
                    agent.source_url,
                    agent.install_count_local,
                ),
            )
            await db.commit()
        if request.dry_run:
            return PublishResponse(
                accepted=True,
                agent_id=agent.agent_id,
                version=agent.version,
                catalog_url=None,
                message="dry-run: not written to remote catalog",
            )
        return PublishResponse(
            accepted=True,
            agent_id=agent.agent_id,
            version=agent.version,
            catalog_url=None,
            message="published to local catalog (remote sync is a Phase 12 concern)",
        )

    async def rate(self, agent_id: str, stars: int) -> None:
        if not 1 <= stars <= 5:
            raise MarketplaceError("stars must be in 1..5")
        agent = await self._get(agent_id)
        total = agent.rating * agent.rating_count + stars
        count = agent.rating_count + 1
        new_avg = total / count
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "UPDATE marketplace_agents SET rating = ?, rating_count = ?"
                " WHERE agent_id = ?",
                (new_avg, count, agent_id),
            )
            await db.commit()

    async def rating_summary(self, agent_id: str) -> RatingSummary:
        agent = await self._get(agent_id)
        return RatingSummary(
            agent_id=agent.agent_id,
            average=agent.rating,
            count=agent.rating_count,
            distribution={},
        )


# ---------------------------------------------------------------------------
# FastAPI router
# ---------------------------------------------------------------------------


def create_router(registry: Registry):
    """Build the FastAPI router.

    The router is a small wrapper that knows how to translate
    HTTP requests into registry calls. Tests build the registry
    and pass it in; production wires the registry into
    ``app.state.marketplace`` and includes the router.
    """
    try:
        from fastapi import APIRouter, HTTPException, status
    except ImportError:  # pragma: no cover
        return None

    router = APIRouter(prefix="/api/marketplace")

    @router.get("/agents")
    async def list_agents(
        q: str | None = None,
        category: str | None = None,
        tag: str | None = None,
        min_rating: float = 0.0,
        sort: str = "downloads",
        limit: int = 50,
        offset: int = 0,
    ) -> dict:
        if category and category not in _CATEGORIES:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    "code": "invalid_category",
                    "message": f"category must be one of {_CATEGORIES}",
                },
            )
        query = MarketplaceSearchQuery(
            q=q,
            category=category,  # type: ignore[arg-type]
            tag=tag,
            min_rating=min_rating,
            sort=sort,  # type: ignore[arg-type]
            limit=limit,
            offset=offset,
        )
        agents = await registry.list_agents(query)
        return {
            "total": len(agents),
            "limit": query.limit,
            "offset": query.offset,
            "agents": [a.model_dump(mode="json") for a in agents],
        }

    @router.get("/agents/{agent_id}")
    async def get_agent(agent_id: str) -> dict:
        try:
            agent = await registry.get(agent_id)
        except AgentNotFound as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"code": "agent_not_found", "message": str(exc)},
            ) from exc
        return agent.model_dump(mode="json")

    @router.post("/install")
    async def install_agent(request: InstallRequest) -> dict:
        try:
            result = await registry.install(request)
        except AgentNotFound as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"code": "agent_not_found", "message": str(exc)},
            ) from exc
        return result.model_dump()

    @router.post("/publish")
    async def publish_agent(request: PublishRequest) -> dict:
        result = await registry.publish(request)
        return result.model_dump()

    @router.post("/agents/{agent_id}/rate")
    async def rate_agent(agent_id: str, stars: int) -> dict:
        if not 1 <= stars <= 5:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"code": "invalid_rating", "message": "stars must be 1..5"},
            )
        try:
            await registry.rate(agent_id, stars)
        except AgentNotFound as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"code": "agent_not_found", "message": str(exc)},
            ) from exc
        except MarketplaceError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"code": "invalid_rating", "message": str(exc)},
            ) from exc
        return {"agent_id": agent_id, "stars": stars, "ok": True}

    @router.get("/agents/{agent_id}/rating")
    async def rating(agent_id: str) -> dict:
        try:
            summary = await registry.rating_summary(agent_id)
        except AgentNotFound as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"code": "agent_not_found", "message": str(exc)},
            ) from exc
        return summary.model_dump()

    return router


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_CATEGORIES: set[str] = {
    "coding",
    "research",
    "review",
    "planning",
    "ops",
    "data",
    "writing",
    "general",
    "experimental",
}


_SAFE_NAME = re.compile(r"[^a-zA-Z0-9._-]+")


def _safe_filename(agent_id: str) -> str:
    return _SAFE_NAME.sub("_", agent_id).strip("_") or "agent"


def _iso(ts: datetime | None) -> str | None:
    if ts is None:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.isoformat().replace("+00:00", "Z")


def _manifest_to_agent(
    raw: dict, *, source_path: Path | None = None
) -> MarketplaceAgent:
    """Convert a manifest dict into a :class:`MarketplaceAgent`.

    Best-effort: a malformed field becomes the default value
    rather than raising — the marketplace must be tolerant so a
    single broken manifest doesn't kill the boot.
    """
    try:
        return MarketplaceAgent(
            agent_id=raw.get("agent_id") or (source_path.stem if source_path else "agent"),
            name=raw.get("human_readable_name")
            or raw.get("name")
            or raw.get("agent_id", "Agent"),
            description=raw.get("description", ""),
            category=_coerce_category(raw.get("category")),
            tags=list(raw.get("tags") or []),
            author=raw.get("author", "anonymous"),
            version=str(raw.get("version", "1.0.0")),
            manifest=raw,
            source_url=str(source_path) if source_path else None,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("manifest_to_agent failed for %s: %s", source_path, exc)
        return MarketplaceAgent(
            agent_id=(source_path.stem if source_path else "agent"),
            name=raw.get("agent_id") or (source_path.stem if source_path else "agent"),
            manifest=raw,
            source_url=str(source_path) if source_path else None,
        )


def _coerce_category(value: Any) -> CategoryLiteral:
    if isinstance(value, str) and value in _CATEGORIES:
        return value  # type: ignore[return-value]
    return "general"


def _row_to_agent(row: Any) -> MarketplaceAgent:
    manifest = json.loads(row["manifest_json"])
    tags = json.loads(row["tags"]) if row["tags"] else []
    return MarketplaceAgent(
        agent_id=row["agent_id"],
        name=row["name"],
        description=row["description"],
        category=_coerce_category(row["category"]),
        tags=tags,
        author=row["author"],
        version=row["version"],
        manifest=manifest,
        downloads=int(row["downloads"]),
        rating=float(row["rating"]),
        rating_count=int(row["rating_count"]),
        published_at=_parse_iso(row["published_at"]),
        updated_at=_parse_iso(row["updated_at"]),
        source_url=row["source_url"],
        install_count_local=int(row["install_count"]),
    )


def _parse_iso(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    try:
        # Tolerate trailing 'Z'.
        s = str(value).replace("Z", "+00:00")
        return datetime.fromisoformat(s)
    except ValueError:
        return None


__all__ = [
    "AgentNotFound",
    "ManifestInvalid",
    "MarketplaceError",
    "Registry",
    "create_router",
]
