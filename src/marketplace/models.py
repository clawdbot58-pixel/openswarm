"""Marketplace data models for Phase 11.

The marketplace is a discoverable catalog of agent configurations
that users can browse, install, and (optionally) publish to. It
mirrors the OpenClaw "agent template" pattern: each marketplace
entry bundles a manifest with enough metadata (category, tags,
rating, downloads) to be useful in a search UI.

Design choices
--------------
* Models are Pydantic v2 with strict validation, mirroring the
  :mod:`kernel.models` style.
* The :class:`MarketplaceAgent` ``manifest`` field is a dict (not
  a typed :class:`AgentManifest`) because marketplace entries
  may include fields the local kernel doesn't accept — we
  validate only on install, not on catalog.
* :class:`RatingSummary` is a snapshot computed by the registry
  at install/publish time; it is not a real-time aggregate.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


CategoryLiteral = Literal[
    "coding",
    "research",
    "review",
    "planning",
    "ops",
    "data",
    "writing",
    "general",
    "experimental",
]


class MarketplaceAgent(BaseModel):
    """One agent in the marketplace catalog."""

    model_config = ConfigDict(extra="ignore")

    agent_id: str = Field(..., min_length=1, max_length=128)
    name: str = Field(..., min_length=1, max_length=200)
    description: str = Field(default="", max_length=2000)
    category: CategoryLiteral = "general"
    tags: list[str] = Field(default_factory=list)
    author: str = Field(default="anonymous", max_length=128)
    version: str = Field(default="1.0.0")
    manifest: dict[str, Any] = Field(default_factory=dict)
    downloads: int = Field(default=0, ge=0)
    rating: float = Field(default=0.0, ge=0.0, le=5.0)
    rating_count: int = Field(default=0, ge=0)
    published_at: datetime | None = None
    updated_at: datetime | None = None
    source_url: str | None = None
    install_count_local: int = Field(default=0, ge=0)
    """How many times this agent has been installed on the local swarm."""

    @field_validator("tags")
    @classmethod
    def _normalize_tags(cls, value: list[str]) -> list[str]:
        # Dedupe, lowercase, drop empties.
        seen: set[str] = set()
        out: list[str] = []
        for tag in value:
            t = tag.strip().lower()
            if t and t not in seen:
                seen.add(t)
                out.append(t)
        return out


class MarketplaceSearchQuery(BaseModel):
    """Query parameters for ``GET /api/marketplace/agents``."""

    q: str | None = None
    category: CategoryLiteral | None = None
    tag: str | None = None
    min_rating: float = Field(default=0.0, ge=0.0, le=5.0)
    sort: Literal["downloads", "rating", "recent", "name"] = "downloads"
    limit: int = Field(default=50, ge=1, le=500)
    offset: int = Field(default=0, ge=0)


class InstallRequest(BaseModel):
    """Body of ``POST /api/marketplace/install``."""

    agent_id: str = Field(..., min_length=1)
    version: str | None = None
    target_dir: str | None = None


class InstallResponse(BaseModel):
    """Body returned by ``POST /api/marketplace/install``."""

    agent_id: str
    installed_path: str
    version: str


class PublishRequest(BaseModel):
    """Body of ``POST /api/marketplace/publish``."""

    agent: MarketplaceAgent
    dry_run: bool = False


class PublishResponse(BaseModel):
    """Body returned by ``POST /api/marketplace/publish``."""

    accepted: bool
    agent_id: str
    version: str
    catalog_url: str | None = None
    message: str = ""


class RatingSummary(BaseModel):
    """Aggregate rating for a marketplace agent."""

    agent_id: str
    average: float
    count: int
    distribution: dict[str, int] = Field(default_factory=dict)
    """Histogram of ``1..5`` star counts."""


__all__ = [
    "CategoryLiteral",
    "InstallRequest",
    "InstallResponse",
    "MarketplaceAgent",
    "MarketplaceSearchQuery",
    "PublishRequest",
    "PublishResponse",
    "RatingSummary",
]
