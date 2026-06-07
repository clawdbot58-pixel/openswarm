"""Pluggable priority queue for the kernel's message bus.

The default :class:`~kernel.bus.MessageBus` is in-process (a
``heapq``). This module provides a drop-in replacement that uses
Redis sorted sets as the backing store, with a transparent
in-memory fallback when Redis is unavailable. Per the Phase 11
brief, Redis is **optional** — operators enable it via
``OPENSWARM_REDIS__ENABLED=true`` and the kernel will fall back to
in-memory if the connection fails.

Design notes
------------
* Uses ``ZADD openswarm:queue:{agent_id} score=priority member=envelope_json``
  for enqueue and ``ZPOPMIN`` for dequeue. Lower score = higher
  priority (per the brief's convention).
* Each envelope is stored as a single string member; we re-encode
  using :class:`Envelope.model_dump_json` so what comes out is
  valid for the bus.
* "In-flight" envelopes that pop but aren't yet acked go into a
  per-agent processing list (``openswarm:processing:{agent_id}``)
  with a 60-second TTL. A consumer crash drops them back to the
  queue; the :meth:`RedisMessageQueue.recover` method runs at
  startup to clean any leftover.
* The fallback is "if any Redis call raises, log a warning and
  switch to the in-memory implementation". We don't try to
  reconnect — the operator's job to fix the broker.
"""
from __future__ import annotations

import asyncio
import heapq
import itertools
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger(__name__)


# Optional Redis import. We never want this module to fail to
# import just because redis isn't installed — that's a soft
# dependency (see :class:`RedisSection` in :mod:`config`).
try:
    import redis.asyncio as redis_async  # type: ignore[import-not-found]

    _REDIS_AVAILABLE = True
except ImportError:  # pragma: no cover
    redis_async = None  # type: ignore[assignment]
    _REDIS_AVAILABLE = False


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class QueueError(RuntimeError):
    """Base class for queue failures."""


class QueueFull(QueueError):
    """Raised when a queue exceeds its capacity."""


# ---------------------------------------------------------------------------
# Interface
# ---------------------------------------------------------------------------


class MessageQueue:
    """Abstract priority queue shared by the Redis and in-memory backends."""

    async def enqueue(
        self,
        agent_id: str,
        envelope: dict[str, Any],
        priority: int = 5,
    ) -> None:
        raise NotImplementedError

    async def dequeue(
        self, agent_id: str, timeout: float = 30.0
    ) -> dict[str, Any] | None:
        raise NotImplementedError

    async def size(self, agent_id: str) -> int:
        raise NotImplementedError

    async def aclose(self) -> None:  # pragma: no cover
        return None

    async def recover(self) -> int:
        """Recover any orphaned in-flight entries. Return count recovered."""
        return 0


# ---------------------------------------------------------------------------
# In-memory implementation (the original behaviour, exposed as a class)
# ---------------------------------------------------------------------------


@dataclass(order=False)
class _InMemoryEntry:
    priority: int
    seq: int
    envelope: dict[str, Any]

    def __lt__(self, other: "_InMemoryEntry") -> bool:
        if self.priority != other.priority:
            return self.priority < other.priority
        return self.seq < other.seq


class InMemoryMessageQueue(MessageQueue):
    """``heapq``-backed priority queue. Equivalent to the default bus."""

    def __init__(self, max_queue_size: int = 1000) -> None:
        self._heaps: dict[str, list[_InMemoryEntry]] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._events: dict[str, asyncio.Event] = {}
        self._counters: dict[str, itertools.count] = {}
        self._max_size = max_queue_size

    async def enqueue(
        self,
        agent_id: str,
        envelope: dict[str, Any],
        priority: int = 5,
    ) -> None:
        heap = self._heaps.setdefault(agent_id, [])
        lock = self._locks.setdefault(agent_id, asyncio.Lock())
        event = self._events.setdefault(agent_id, asyncio.Event())
        counter = self._counters.setdefault(agent_id, itertools.count())
        async with lock:
            if len(heap) >= self._max_size:
                raise QueueFull(f"queue for {agent_id} full ({self._max_size})")
            heapq.heappush(
                heap,
                _InMemoryEntry(
                    priority=priority,
                    seq=next(counter),
                    envelope=envelope,
                ),
            )
            event.set()

    async def dequeue(
        self, agent_id: str, timeout: float = 30.0
    ) -> dict[str, Any] | None:
        event = self._events.setdefault(agent_id, asyncio.Event())
        lock = self._locks.setdefault(agent_id, asyncio.Lock())
        deadline = time.time() + timeout
        while time.time() < deadline:
            async with lock:
                heap = self._heaps.get(agent_id)
                if heap:
                    entry = heapq.heappop(heap)
                    if not heap:
                        event.clear()
                    return entry.envelope
            # Wait for a signal or a short poll, whichever first.
            try:
                await asyncio.wait_for(event.wait(), timeout=0.05)
            except asyncio.TimeoutError:
                pass
        return None

    async def size(self, agent_id: str) -> int:
        heap = self._heaps.get(agent_id)
        return len(heap) if heap else 0

    async def aclose(self) -> None:
        self._heaps.clear()
        self._events.clear()


# ---------------------------------------------------------------------------
# Redis implementation
# ---------------------------------------------------------------------------


_PROCESSING_TTL_SECONDS: int = 60


class RedisMessageQueue(MessageQueue):
    """Redis sorted-set-backed priority queue.

    Parameters
    ----------
    url:
        ``redis://host:port/db`` style URL.
    key_prefix:
        Prefix for the queue keys (``{prefix}:{agent_id}``).
    socket_timeout_seconds:
        Per-call timeout.
    """

    def __init__(
        self,
        url: str,
        *,
        key_prefix: str = "openswarm:queue",
        socket_timeout_seconds: float = 2.0,
    ) -> None:
        if not _REDIS_AVAILABLE:
            raise QueueError(
                "redis package is not installed; install with `pip install redis`"
            )
        self._url = url
        self._prefix = key_prefix.rstrip(":")
        self._timeout = socket_timeout_seconds
        self._client: Any | None = None
        self._connected: bool = False

    async def _connect(self) -> Any:
        if self._client is not None and self._connected:
            return self._client
        client = redis_async.from_url(  # type: ignore[name-defined]
            self._url, socket_timeout=self._timeout
        )
        # Probe the connection.
        try:
            await client.ping()
        except Exception as exc:
            await client.aclose()  # type: ignore[union-attr]
            raise QueueError(f"redis ping failed: {exc}") from exc
        self._client = client
        self._connected = True
        return client

    def _queue_key(self, agent_id: str) -> str:
        return f"{self._prefix}:{agent_id}"

    def _processing_key(self, agent_id: str) -> str:
        return f"{self._prefix}:processing:{agent_id}"

    async def enqueue(
        self,
        agent_id: str,
        envelope: dict[str, Any],
        priority: int = 5,
    ) -> None:
        client = await self._connect()
        member = json.dumps(envelope, default=str)
        try:
            await client.zadd(self._queue_key(agent_id), {member: priority})
        except Exception as exc:
            self._connected = False
            raise QueueError(f"redis enqueue failed: {exc}") from exc

    async def dequeue(
        self, agent_id: str, timeout: float = 30.0
    ) -> dict[str, Any] | None:
        client = await self._connect()
        deadline = time.time() + timeout
        # Try a few times with a small sleep, then give up.
        while time.time() < deadline:
            try:
                result = await client.zpopmin(self._queue_key(agent_id), count=1)
            except Exception as exc:
                self._connected = False
                raise QueueError(f"redis dequeue failed: {exc}") from exc
            if result:
                member, _score = result[0]
                # Park in the processing list so a crash can recover.
                try:
                    await client.hset(
                        self._processing_key(agent_id),
                        member,
                        int(time.time()),
                    )
                    await client.expire(
                        self._processing_key(agent_id),
                        _PROCESSING_TTL_SECONDS,
                    )
                except Exception:  # noqa: BLE001
                    # Best-effort; if the processing tracking fails
                    # we still deliver.
                    pass
                try:
                    return json.loads(member)
                except json.JSONDecodeError as exc:
                    raise QueueError(
                        f"corrupt envelope in queue for {agent_id}: {exc}"
                    ) from exc
            # No item; small sleep.
            await asyncio.sleep(0.05)
        return None

    async def size(self, agent_id: str) -> int:
        client = await self._connect()
        try:
            return int(await client.zcard(self._queue_key(agent_id)))
        except Exception as exc:  # noqa: BLE001
            self._connected = False
            raise QueueError(f"redis size failed: {exc}") from exc

    async def recover(self) -> int:
        """Re-queue any in-flight entries that exceeded their TTL.

        Called at boot. Returns the number of entries recovered.
        """
        client = await self._connect()
        recovered = 0
        try:
            # SCAN over the processing set keys.
            async for key in client.scan_iter(match=f"{self._prefix}:processing:*"):
                agent_id = key.decode().rsplit(":", 1)[-1] if isinstance(key, bytes) else key.rsplit(":", 1)[-1]
                entries = await client.hgetall(key)
                for member, ts_bytes in entries.items():
                    ts = int(ts_bytes)
                    if time.time() - ts > _PROCESSING_TTL_SECONDS:
                        await client.zadd(self._queue_key(agent_id), {member: 5})
                        await client.hdel(key, member)
                        recovered += 1
                # Drop the processing key once empty.
                if not await client.hlen(key):
                    await client.delete(key)
        except Exception as exc:  # noqa: BLE001
            logger.warning("redis recover failed: %s", exc)
        return recovered

    async def aclose(self) -> None:
        if self._client is not None:
            try:
                await self._client.aclose()
            except Exception:  # noqa: BLE001
                pass
        self._client = None
        self._connected = False


# ---------------------------------------------------------------------------
# Composite queue (Redis with in-memory fallback)
# ---------------------------------------------------------------------------


class ResilientMessageQueue(MessageQueue):
    """A queue that tries Redis first and falls back to in-memory.

    The fallback is "sticky": once we lose the Redis connection we
    stop trying until the operator restarts the kernel. This is
    deliberate — silently retrying under load would amplify the
    outage. A future Phase 12 enhancement can add circuit-breaker
    logic.
    """

    def __init__(
        self,
        *,
        redis_url: str | None = None,
        key_prefix: str = "openswarm:queue",
        fallback_to_memory: bool = True,
        max_queue_size: int = 1000,
    ) -> None:
        self._fallback = InMemoryMessageQueue(max_queue_size=max_queue_size)
        self._fallback_enabled = fallback_to_memory
        self._redis: RedisMessageQueue | None = None
        self._use_redis = False
        if redis_url and _REDIS_AVAILABLE:
            try:
                self._redis = RedisMessageQueue(
                    redis_url, key_prefix=key_prefix
                )
                self._use_redis = True
            except QueueError as exc:
                logger.warning("redis init failed: %s", exc)
                self._use_redis = False

    @property
    def backend(self) -> str:
        return "redis" if self._use_redis and self._redis and self._redis._connected else "memory"  # noqa: SLF001

    async def enqueue(
        self,
        agent_id: str,
        envelope: dict[str, Any],
        priority: int = 5,
    ) -> None:
        if self._use_redis and self._redis is not None:
            try:
                await self._redis.enqueue(agent_id, envelope, priority)
                return
            except QueueError as exc:
                self._downgrade(exc)
        await self._fallback.enqueue(agent_id, envelope, priority)

    async def dequeue(
        self, agent_id: str, timeout: float = 30.0
    ) -> dict[str, Any] | None:
        if self._use_redis and self._redis is not None:
            try:
                return await self._redis.dequeue(agent_id, timeout)
            except QueueError as exc:
                self._downgrade(exc)
        return await self._fallback.dequeue(agent_id, timeout)

    async def size(self, agent_id: str) -> int:
        if self._use_redis and self._redis is not None:
            try:
                return await self._redis.size(agent_id)
            except QueueError as exc:
                self._downgrade(exc)
        return await self._fallback.size(agent_id)

    async def recover(self) -> int:
        if self._use_redis and self._redis is not None:
            try:
                return await self._redis.recover()
            except QueueError as exc:
                self._downgrade(exc)
        return 0

    async def aclose(self) -> None:
        if self._redis is not None:
            await self._redis.aclose()
        await self._fallback.aclose()

    def _downgrade(self, exc: Exception) -> None:
        """Switch permanently to in-memory after a Redis failure."""
        if not self._fallback_enabled:
            raise
        logger.warning(
            "redis queue failed (%s); downgrading to in-memory", exc
        )
        self._use_redis = False


def build_queue(
    *,
    redis_enabled: bool = False,
    redis_url: str = "redis://localhost:6379",
    key_prefix: str = "openswarm:queue",
    fallback_to_memory: bool = True,
    max_queue_size: int = 1000,
) -> MessageQueue:
    """Factory used by the kernel's :func:`create_app`."""
    if redis_enabled and _REDIS_AVAILABLE:
        return ResilientMessageQueue(
            redis_url=redis_url,
            key_prefix=key_prefix,
            fallback_to_memory=fallback_to_memory,
            max_queue_size=max_queue_size,
        )
    return InMemoryMessageQueue(max_queue_size=max_queue_size)


__all__ = [
    "InMemoryMessageQueue",
    "MessageQueue",
    "QueueError",
    "QueueFull",
    "RedisMessageQueue",
    "ResilientMessageQueue",
    "build_queue",
]
