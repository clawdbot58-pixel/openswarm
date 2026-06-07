"""In-memory cache for pre-aggregated dashboard data.

The :class:`DataAggregator` (see :mod:`dashboard.backend.aggregator`)
recomputes expensive queries every few seconds. The cache stores the
result of the last computation so the FastAPI request handlers can
return a value in O(1) without hitting SQLite.

Design constraints
------------------
* **Single writer, many readers.** The aggregator is the only writer.
  Read paths must not mutate state.
* **Lock-free reads.** A single ``asyncio.Event`` signals "cache
  refreshed" so readers can avoid polling.  Writes hold a short lock
  so the swap is atomic.
* **Ttl-bounded.** Every entry carries a ``computed_at`` timestamp.
  Read paths can verify freshness without blocking the writer.

The cache is process-local. Multi-process deployments would need a
shared store (Redis), but that's a Phase 11 concern.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Generic, TypeVar

T = TypeVar("T")


# ---------------------------------------------------------------------------
# Cached entry
# ---------------------------------------------------------------------------


@dataclass
class CacheEntry(Generic[T]):
    """A single cache row.

    Attributes:
        value: The cached payload.  Type is generic; the caller
            decides.
        computed_at: Monotonic timestamp of the last write.  Used to
            compute staleness; the aggregator refreshes on its own
            cadence.
        wall_time: The wall-clock time of the last write.  Surfaced
            in API responses so the frontend can show "last updated
            3s ago".
    """

    value: T
    computed_at: float = field(default_factory=time.monotonic)
    wall_time: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def age_seconds(self) -> float:
        """Monotonic age since the last refresh."""
        return time.monotonic() - self.computed_at

    def fresh(self, max_age_seconds: float) -> bool:
        """True if this entry is at most ``max_age_seconds`` old."""
        return self.age_seconds() <= max_age_seconds


# ---------------------------------------------------------------------------
# AggregateCache
# ---------------------------------------------------------------------------


class AggregateCache:
    """A typed key-value cache for aggregate snapshots.

    Keys are dotted strings (``"system_metrics"``, ``"agent_counts"``,
    ``"loop_performance:direct"``) so callers can keep namespace
    conventions without inventing a class hierarchy.
    """

    def __init__(self) -> None:
        self._entries: dict[str, CacheEntry[Any]] = {}
        self._lock = asyncio.Lock()
        self._refresh_event = asyncio.Event()
        # Track how often each key was refreshed — surfaced via
        # :meth:`stats` for observability.
        self._refresh_counts: dict[str, int] = {}
        self._hits: dict[str, int] = {}
        self._misses: dict[str, int] = {}

    # -- write -----------------------------------------------------------

    async def set(self, key: str, value: Any) -> None:
        """Store ``value`` under ``key`` and signal readers."""
        async with self._lock:
            self._entries[key] = CacheEntry(value=value)
            self._refresh_counts[key] = self._refresh_counts.get(key, 0) + 1
        self._refresh_event.set()

    async def set_many(self, items: dict[str, Any]) -> None:
        """Atomically write a batch of values under a single lock."""
        async with self._lock:
            for key, value in items.items():
                self._entries[key] = CacheEntry(value=value)
                self._refresh_counts[key] = self._refresh_counts.get(key, 0) + 1
        self._refresh_event.set()

    # -- read ------------------------------------------------------------

    async def get(self, key: str, default: Any = None) -> Any:
        """Return the cached value, or ``default`` on miss."""
        async with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                self._misses[key] = self._misses.get(key, 0) + 1
                return default
            self._hits[key] = self._hits.get(key, 0) + 1
            return entry.value

    def get_sync(self, key: str, default: Any = None) -> Any:
        """Synchronous read.  Use only when the writer is the same task."""
        entry = self._entries.get(key)
        if entry is None:
            self._misses[key] = self._misses.get(key, 0) + 1
            return default
        self._hits[key] = self._hits.get(key, 0) + 1
        return entry.value

    def get_entry(self, key: str) -> CacheEntry[Any] | None:
        """Return the full :class:`CacheEntry` (with age metadata)."""
        return self._entries.get(key)

    async def wait_for_refresh(self, timeout: float = 5.0) -> bool:
        """Block until a refresh occurs, or ``timeout`` elapses.

        Returns ``True`` if a refresh was observed, ``False`` on
        timeout.  Used by the WebSocket layer to throttle
        ``system_metrics`` pushes.
        """
        try:
            await asyncio.wait_for(self._refresh_event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            return False
        return True

    def ack_refresh(self) -> None:
        """Reset the refresh event after consumers have handled it.

        Callers should invoke this after consuming a refresh signal,
        otherwise :meth:`wait_for_refresh` will keep returning
        immediately.
        """
        self._refresh_event.clear()

    # -- introspection ---------------------------------------------------

    def stats(self) -> dict[str, Any]:
        """Return a small stats dict for diagnostics."""
        return {
            "keys": sorted(self._entries.keys()),
            "refresh_counts": dict(self._refresh_counts),
            "hits": dict(self._hits),
            "misses": dict(self._misses),
        }

    def clear(self) -> None:
        """Remove every entry.  Used by tests."""
        self._entries.clear()
        self._refresh_counts.clear()
        self._hits.clear()
        self._misses.clear()
        self._refresh_event.clear()


__all__ = ["AggregateCache", "CacheEntry"]
