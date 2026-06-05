"""Temporary, in-process memory store.

This is the *fast, ephemeral* memory channel: a per-agent key-value
store that lives for one session and is wiped when the agent goes
offline.  No SQLite, no disk, no cross-process state — just an
``asyncio.Lock``-guarded dict with a TTL.

Per :mod:`memory`, the four hard rules for this module are:

1. In-memory only.  No SQLite, no FTS, no network.
2. Per-agent scope.  A :class:`TemporaryMemory` instance is bound to
   exactly one ``agent_id``; if you want shared state, use
   :class:`memory.persistent.PersistentMemory`.
3. TTL-based.  Every entry expires; the default is 30 minutes of
   inactivity.  Reads do **not** extend TTL — only writes do (the
   common "touched recently" semantics).
4. The on-disk contract is **not** this module's problem.  We
   surface dicts and Pydantic :class:`MemoryItem` instances; what
   the caller does with them is their business.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


# Sources are used for traceability and for the kernel-side audit log.
# They are free-form strings, but the canonical values are:
#   "user"          — produced by a human (rare; usually arrives as a
#                     permanent persistent record instead).
#   "kernel"        — produced by the kernel (zombie events,
#                     budget updates, etc.).
#   "self"          — produced by the agent itself (a result, a
#                     decision, a tool call).
#   "other_agent"   — produced by another agent in the swarm.
SourceLiteral = Literal["user", "kernel", "self", "other_agent"]
TypeLiteral = Literal["action", "result", "decision", "error", "context"]


class MemoryItem(BaseModel):
    """A single memory entry surfaced into a preamble.

    This is the *richer* cousin of :class:`kernel.models.MemoryItem`.
    It carries everything we need for storage and retrieval
    (``source``, ``ttl``, ``workflow_id``, ``step_id``) and can be
    converted to/from the kernel's wire-format via :meth:`to_kernel`
    and :meth:`from_kernel`.

    Attributes:
        timestamp: When the memory was created.  UTC.
        type: One of ``"action"``, ``"result"``, ``"decision"``,
            ``"error"``, ``"context"``.
        content: Arbitrary payload.  Will be JSON-serialized when
            crossing the envelope boundary.
        relevance_score: 0.0–1.0.  Used by the
            :class:`memory.context_assembler.ContextAssembler` to
            filter ``relevant_history`` against the manifest's
            ``relevance_threshold``.
        source: Who produced this memory.  Default ``"self"``.
        ttl: Optional per-item TTL override in seconds.  ``None``
            means "use the store default".
        workflow_id: Optional workflow attribution.
        step_id: Optional step attribution.
    """

    model_config = ConfigDict(extra="forbid")

    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    type: TypeLiteral
    content: Any
    relevance_score: float = Field(default=1.0, ge=0.0, le=1.0)
    source: SourceLiteral = "self"
    ttl: Optional[int] = Field(default=None, ge=1)
    workflow_id: Optional[str] = None
    step_id: Optional[str] = None

    def to_kernel(self) -> "kernel.models.MemoryItem":  # type: ignore[name-defined]  # noqa: F821
        """Convert to the kernel wire-format ``MemoryItem``.

        Strips fields the kernel doesn't need (``source``, ``ttl``,
        ``workflow_id``, ``step_id``) and re-validates the relevance
        score.  The import is local to keep this module importable
        in environments where ``kernel`` is not yet available.
        """
        from kernel.models import MemoryItem as KernelMemoryItem

        return KernelMemoryItem(
            timestamp=self.timestamp,
            type=self.type,
            content=self.content,
            relevance_score=self.relevance_score,
        )

    @classmethod
    def from_kernel(
        cls,
        item: "kernel.models.MemoryItem",  # type: ignore[name-defined]  # noqa: F821
        *,
        source: SourceLiteral = "kernel",
        workflow_id: Optional[str] = None,
        step_id: Optional[str] = None,
    ) -> "MemoryItem":
        """Build a :class:`MemoryItem` from a kernel wire-format entry."""
        return cls(
            timestamp=item.timestamp,
            type=item.type,
            content=item.content,
            relevance_score=item.relevance_score if item.relevance_score is not None else 1.0,
            source=source,
            workflow_id=workflow_id,
            step_id=step_id,
        )


@dataclass
class _Entry:
    """Internal storage record.  Not part of the public API."""

    value: Any
    expires_at: float  # monotonic seconds; ``float("inf")`` means no expiry
    created_at: float
    item: MemoryItem


@dataclass
class _Key:
    """Key = (memory_type, key).  We allow the same key to be used
    with different types so callers can partition their state
    cheaply without inventing unique prefixes."""

    type: str
    key: str


class TemporaryMemory:
    """Per-agent, in-memory, TTL-based key-value store.

    A single instance is bound to one ``agent_id``.  Stores are
    partitioned by ``type`` (``"action"``, ``"result"``, etc.) so the
    same key under two types is two distinct entries — this matches
    the manifest's :class:`MemoryConfig` and makes
    :meth:`get_recent` cheap.

    Args:
        agent_id: The owning agent.  Surfaced in :meth:`__repr__` for
            debug logging; not used as a key.
        ttl_seconds: Default TTL for entries that don't supply their
            own.  ``None`` disables expiry entirely.  Default 1800 s
            (30 minutes), per the spec.
        max_entries: Soft cap on the number of stored entries.
            When exceeded, the oldest-expired entry is evicted; if
            nothing is expired, the oldest by ``created_at`` is
            evicted.  ``None`` disables the cap.
    """

    def __init__(
        self,
        agent_id: str,
        ttl_seconds: int = 1800,
        max_entries: Optional[int] = 10_000,
    ) -> None:
        self._agent_id = agent_id
        self._default_ttl = ttl_seconds
        self._max_entries = max_entries
        self._entries: dict[tuple[str, str], _Entry] = {}
        self._lock = asyncio.Lock()
        # Parallel index for O(1) "is this entry alive?" checks.
        # ``_insertion_order`` preserves FIFO so :meth:`get_recent` is
        # deterministic even when timestamps collide.
        self._insertion_order: list[tuple[str, str]] = []

    # -- properties --------------------------------------------------------

    @property
    def agent_id(self) -> str:
        """The owning agent id."""
        return self._agent_id

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"TemporaryMemory(agent_id={self._agent_id!r}, size={len(self._entries)})"

    # -- internal eviction -------------------------------------------------

    def _evict_expired(self, now: float) -> None:
        """Remove everything whose ``expires_at`` is in the past.

        Sync; must be called under :attr:`_lock`.  Expensive entries
        (large payloads) are dropped from the dict; their keys are
        also removed from the order index.
        """
        expired = [k for k, e in self._entries.items() if e.expires_at <= now]
        for k in expired:
            del self._entries[k]
        if expired:
            self._insertion_order = [
                k for k in self._insertion_order if k not in set(expired)
            ]

    def _evict_oldest(self) -> None:
        """Drop the oldest entry by insertion order.  Must hold the lock."""
        if not self._insertion_order:
            return
        oldest = self._insertion_order.pop(0)
        self._entries.pop(oldest, None)

    # -- core API ----------------------------------------------------------

    async def set(
        self,
        key: str,
        value: Any,
        ttl: Optional[int] = None,
        type: str = "context",  # noqa: A002 -- matches MemoryItem.type literal
        source: SourceLiteral = "self",
        relevance_score: float = 1.0,
        workflow_id: Optional[str] = None,
        step_id: Optional[str] = None,
    ) -> MemoryItem:
        """Store ``value`` under ``key``.

        Args:
            key: Storage key.  Unqualified (will be paired with
                ``type``).
            value: Anything JSON-serializable.  Stored as-is.
            ttl: Optional override for this entry's TTL in seconds.
            type: Memory type for the entry.  Default ``"context"``.
            source: Traceability source.  Default ``"self"``.
            relevance_score: 0.0–1.0.  Default 1.0.
            workflow_id: Optional workflow attribution.
            step_id: Optional step attribution.

        Returns:
            The :class:`MemoryItem` that was stored, including its
            generated ``timestamp``.
        """
        async with self._lock:
            now = time.monotonic()
            effective_ttl = ttl if ttl is not None else self._default_ttl
            expires_at = now + effective_ttl if effective_ttl is not None else float("inf")
            item = MemoryItem(
                type=type,  # type: ignore[arg-type]
                content=value,
                relevance_score=relevance_score,
                source=source,
                ttl=ttl,
                workflow_id=workflow_id,
                step_id=step_id,
            )
            k = (type, key)
            if k in self._entries:
                self._insertion_order.remove(k)
            self._entries[k] = _Entry(
                value=value,
                expires_at=expires_at,
                created_at=now,
                item=item,
            )
            self._insertion_order.append(k)

            # Cap enforcement: drop expired first, then the oldest.
            if self._max_entries is not None and len(self._entries) > self._max_entries:
                self._evict_expired(now)
                while self._max_entries is not None and len(self._entries) > self._max_entries:
                    self._evict_oldest()
            return item

    async def get(
        self,
        key: str,
        type: str = "context",  # noqa: A002
    ) -> Optional[Any]:
        """Return the stored value for ``key`` or ``None`` if absent / expired.

        Reads do **not** extend TTL.  This matches the spec's
        "touched recently" semantics where only writes reset the
        clock.  Expired entries are evicted lazily on access.
        """
        async with self._lock:
            k = (type, key)
            entry = self._entries.get(k)
            if entry is None:
                return None
            if entry.expires_at <= time.monotonic():
                del self._entries[k]
                try:
                    self._insertion_order.remove(k)
                except ValueError:
                    pass
                return None
            return entry.value

    async def get_item(
        self,
        key: str,
        type: str = "context",  # noqa: A002
    ) -> Optional[MemoryItem]:
        """Return the full :class:`MemoryItem` (with metadata) for ``key``.

        Same expiry semantics as :meth:`get`.
        """
        async with self._lock:
            k = (type, key)
            entry = self._entries.get(k)
            if entry is None:
                return None
            if entry.expires_at <= time.monotonic():
                del self._entries[k]
                try:
                    self._insertion_order.remove(k)
                except ValueError:
                    pass
                return None
            return entry.item

    async def delete(
        self,
        key: str,
        type: str = "context",  # noqa: A002
    ) -> bool:
        """Remove ``key`` (no error if absent).  Returns ``True`` if removed."""
        async with self._lock:
            k = (type, key)
            if k in self._entries:
                del self._entries[k]
                try:
                    self._insertion_order.remove(k)
                except ValueError:
                    pass
                return True
            return False

    async def get_recent(
        self,
        n: int = 10,
        type: Optional[str] = None,  # noqa: A002
    ) -> list[MemoryItem]:
        """Return up to ``n`` most-recently-inserted items, newest first.

        Args:
            n: Maximum number of items to return.  ``0`` is valid (and
                always returns ``[]``).
            type: Optional filter; if given, only items of this type
                are returned.

        Returns:
            A list of :class:`MemoryItem`, ordered newest-first by
            ``timestamp`` (ties broken by insertion order).
        """
        if n <= 0:
            return []
        async with self._lock:
            now = time.monotonic()
            self._evict_expired(now)
            if type is None:
                items = [self._entries[k].item for k in reversed(self._insertion_order)]
            else:
                items = [
                    self._entries[k].item
                    for k in reversed(self._insertion_order)
                    if k[0] == type
                ]
            return items[:n]

    async def get_state(self) -> dict[str, Any]:
        """Return all non-expired items as a flat ``{key: value}`` dict.

        Only items with ``type == "context"`` are returned; other
        types are partitioned by their type field, not their key,
        so collapsing them into a single dict would lose
        information.  Use :meth:`get_recent` for those.
        """
        async with self._lock:
            now = time.monotonic()
            self._evict_expired(now)
            return {
                key: entry.value
                for (type_, key), entry in self._entries.items()
                if type_ == "context"
            }

    async def clear(self) -> None:
        """Remove every entry for this agent.  Idempotent."""
        async with self._lock:
            self._entries.clear()
            self._insertion_order.clear()

    async def size(self) -> int:
        """Current number of non-expired entries.  O(1) plus eviction."""
        async with self._lock:
            now = time.monotonic()
            self._evict_expired(now)
            return len(self._entries)


__all__ = ["MemoryItem", "SourceLiteral", "TemporaryMemory", "TypeLiteral"]
