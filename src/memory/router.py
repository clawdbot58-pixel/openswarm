"""Memory router — kernel-side dispatcher for ``memory_write`` envelopes.

Agents emit ``memory_write`` envelopes to record events, results,
decisions, and errors.  The router's job is to:

1. Validate the payload.
2. Route to the correct store (temporary or persistent) based on the
   ``persistence`` field.
3. Return a response envelope acknowledging the write.

The router is **not** the public API for writing memory.  Agent code
that already has a :class:`TemporaryMemory` or
:class:`PersistentMemory` reference can write directly.  The router
exists for cross-process writes — i.e. the conductor writing on
behalf of a worker, or the dashboard pushing a user annotation.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional
from uuid import uuid4

from kernel.models import Endpoint, Envelope, MemoryItem as KernelMemoryItem

from .persistent import PersistentMemory
from .temporary import MemoryItem, TemporaryMemory, TypeLiteral

logger = logging.getLogger(__name__)


class MemoryRouterError(ValueError):
    """Raised when an envelope is malformed or cannot be routed."""


class MemoryRouter:
    """Routes ``memory_write`` envelopes to temporary or persistent stores.

    Args:
        temp_memory: The temporary store.  Must be the one
            associated with the receiving agent (the kernel picks).
        persistent_memory: The persistent store.  Shared across
            agents.
        agent_id: The agent that owns the temporary store.  If
            ``None``, the router infers it from the envelope's
            ``sender.agent_id``.
    """

    def __init__(
        self,
        temp_memory: TemporaryMemory,
        persistent_memory: PersistentMemory,
        agent_id: Optional[str] = None,
    ) -> None:
        self._temp = temp_memory
        self._persistent = persistent_memory
        self._agent_id = agent_id

    # -- public API -------------------------------------------------------

    async def handle_envelope(
        self,
        envelope: Envelope,
    ) -> Optional[Envelope]:
        """Inspect ``envelope`` and, if it's a memory write, route it.

        Returns an acknowledgement :class:`Envelope` on success,
        ``None`` if the envelope is not a memory write (so the
        caller can keep processing it as something else).
        """
        action, payload = self._extract_action(envelope)
        if action is None:
            return None
        if action != "memory_write":
            # Some other ``data`` action; not for us.
            return None

        persistence = self._coerce_persistence(payload.get("persistence"))
        item_dict = payload.get("item") or {}
        if not isinstance(item_dict, dict):
            raise MemoryRouterError(
                f"memory_write 'item' must be a dict, got {type(item_dict).__name__}"
            )

        item = self._item_from_dict(envelope, item_dict)
        tags = payload.get("tags") or []
        workflow_id = payload.get("workflow_id")
        step_id = payload.get("step_id")

        if persistence == "temporary":
            await self._temp.set(
                key=item_dict.get("key", f"{item.type}:{uuid4().hex[:8]}"),
                value=item.content,
                type=item.type,
                source=item.source,
                relevance_score=item.relevance_score,
                workflow_id=workflow_id,
                step_id=step_id,
            )
            stored_id: Optional[int] = None
        else:
            stored_id = await self._persistent.store(
                agent_id=self._resolve_agent_id(envelope),
                item=item,
                workflow_id=workflow_id,
                step_id=step_id,
                tags=list(tags) if isinstance(tags, list) else None,
            )

        return self._build_ack(
            envelope=envelope,
            item=item,
            persistence=persistence,
            stored_id=stored_id,
        )

    async def write(
        self,
        agent_id: str,
        *,
        type: TypeLiteral,  # noqa: A002
        content: Any,
        persistence: str = "persistent",
        relevance_score: float = 1.0,
        source: str = "self",
        workflow_id: Optional[str] = None,
        step_id: Optional[str] = None,
        tags: Optional[list[str]] = None,
        key: Optional[str] = None,
        ttl: Optional[int] = None,
    ) -> tuple[Optional[int], MemoryItem]:
        """Convenience write used by kernel-side helpers (no envelope).

        Returns ``(stored_id_or_None, item)``.  ``stored_id`` is
        ``None`` for temporary writes.
        """
        item = MemoryItem(
            type=type,
            content=content,
            relevance_score=relevance_score,
            source=source,  # type: ignore[arg-type]
            workflow_id=workflow_id,
            step_id=step_id,
            ttl=ttl,
        )
        if persistence == "temporary":
            await self._temp.set(
                key=key or f"{type}:{uuid4().hex[:8]}",
                value=content,
                type=type,
                source=source,  # type: ignore[arg-type]
                relevance_score=relevance_score,
                workflow_id=workflow_id,
                step_id=step_id,
            )
            return None, item
        stored_id = await self._persistent.store(
            agent_id=agent_id,
            item=item,
            workflow_id=workflow_id,
            step_id=step_id,
            tags=tags,
        )
        return stored_id, item

    # -- helpers ----------------------------------------------------------

    @staticmethod
    def _extract_action(envelope: Envelope) -> tuple[Optional[str], dict[str, Any]]:
        """Pull ``action`` and ``payload.data`` out of an envelope.

        Returns ``(None, {})`` if the envelope isn't a ``data``
        request; otherwise ``(action_string, data_dict)``.
        """
        payload = envelope.payload
        if payload.content_type != "data":
            return None, {}
        data = payload.data  # type: ignore[attr-defined]
        if not isinstance(data, dict):
            return None, {}
        action = data.get("action")
        if not isinstance(action, str):
            return None, {}
        return action, data

    @staticmethod
    def _coerce_persistence(value: Any) -> str:
        if value in {"temporary", "persistent"}:
            return value
        if value is None:
            # Default to persistent: "if you didn't say, we remember it".
            return "persistent"
        raise MemoryRouterError(
            f"persistence must be 'temporary' or 'persistent', got {value!r}"
        )

    def _resolve_agent_id(self, envelope: Envelope) -> str:
        if self._agent_id is not None:
            return self._agent_id
        return envelope.sender.agent_id

    @staticmethod
    def _item_from_dict(envelope: Envelope, item_dict: dict[str, Any]) -> MemoryItem:
        type_ = item_dict.get("type")
        if type_ not in {"action", "result", "decision", "error", "context"}:
            raise MemoryRouterError(
                f"memory item 'type' must be one of "
                f"action/result/decision/error/context, got {type_!r}"
            )
        try:
            relevance = float(item_dict.get("relevance_score", 1.0))
        except (TypeError, ValueError) as exc:
            raise MemoryRouterError(
                f"relevance_score must be a number, got {item_dict.get('relevance_score')!r}"
            ) from exc
        relevance = max(0.0, min(1.0, relevance))

        ts_raw = item_dict.get("timestamp")
        if ts_raw is None:
            timestamp = datetime.now(timezone.utc)
        else:
            if isinstance(ts_raw, str) and ts_raw.endswith("Z"):
                ts_raw = ts_raw[:-1] + "+00:00"
            timestamp = datetime.fromisoformat(ts_raw)
            if timestamp.tzinfo is None:
                timestamp = timestamp.replace(tzinfo=timezone.utc)

        source = item_dict.get("source") or envelope.sender.agent_id or "self"
        if source not in {"user", "kernel", "self", "other_agent"}:
            source = "self"

        return MemoryItem(
            timestamp=timestamp,
            type=type_,  # type: ignore[arg-type]
            content=item_dict.get("content"),
            relevance_score=relevance,
            source=source,  # type: ignore[arg-type]
            workflow_id=item_dict.get("workflow_id"),
            step_id=item_dict.get("step_id"),
        )

    @staticmethod
    def _build_ack(
        envelope: Envelope,
        item: MemoryItem,
        persistence: str,
        stored_id: Optional[int],
    ) -> Envelope:
        return Envelope(
            envelope_id=str(uuid4()),
            created_at=datetime.now(timezone.utc),
            envelope_type="response",
            reply_to=envelope.envelope_id,
            sender=Endpoint(agent_id="memory-router", role="kernel"),
            receiver=envelope.sender,
            preamble=envelope.preamble,
            payload={
                "content_type": "data",
                "data": {
                    "event": "memory_stored",
                    "persistence": persistence,
                    "memory_id": stored_id,
                    "type": item.type,
                    "source": item.source,
                },
            },
        )


__all__ = ["MemoryRouter", "MemoryRouterError"]
