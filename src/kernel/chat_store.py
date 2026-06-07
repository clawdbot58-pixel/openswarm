"""In-process chat session store for synchronous Telegram/CLI conversations."""
from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


@dataclass(slots=True)
class ChatSession:
    chat_id: str
    session_id: str
    message: str
    steering: list[str] = field(default_factory=list)
    status: str = "pending"  # pending | running | complete | failed
    reply: str = ""
    error: str | None = None
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )
    updated_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )


class ChatStore:
    """Tracks chat turns and lets HTTP handlers block until the main agent replies."""

    def __init__(self) -> None:
        self._sessions: dict[str, ChatSession] = {}
        self._events: dict[str, asyncio.Event] = {}
        self._lock = asyncio.Lock()

    def create(self, message: str, session_id: str | None = None) -> ChatSession:
        chat_id = str(uuid.uuid4())
        sid = session_id or chat_id
        session = ChatSession(chat_id=chat_id, session_id=sid, message=message.strip())
        self._sessions[chat_id] = session
        self._events[chat_id] = asyncio.Event()
        return session

    def get(self, chat_id: str) -> ChatSession | None:
        return self._sessions.get(chat_id)

    def find_active_for_session(self, session_id: str) -> ChatSession | None:
        for session in reversed(list(self._sessions.values())):
            if session.session_id == session_id and session.status in {"pending", "running"}:
                return session
        return None

    def add_steering(self, chat_id: str, text: str) -> bool:
        session = self._sessions.get(chat_id)
        if session is None or session.status not in {"pending", "running"}:
            return False
        session.steering.append(text.strip())
        session.updated_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        return True

    def consume_steering(self, chat_id: str) -> list[str]:
        session = self._sessions.get(chat_id)
        if session is None:
            return []
        items = list(session.steering)
        session.steering.clear()
        return items

    def mark_running(self, chat_id: str) -> None:
        session = self._sessions.get(chat_id)
        if session is not None:
            session.status = "running"
            session.updated_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    def complete(self, chat_id: str, reply: str, *, error: str | None = None) -> None:
        session = self._sessions.get(chat_id)
        if session is None:
            return
        session.reply = reply
        session.error = error
        session.status = "failed" if error else "complete"
        session.updated_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        event = self._events.get(chat_id)
        if event is not None:
            event.set()

    async def wait(self, chat_id: str, timeout: float = 120.0) -> ChatSession:
        event = self._events.get(chat_id)
        session = self._sessions.get(chat_id)
        if event is None or session is None:
            raise KeyError(chat_id)
        try:
            await asyncio.wait_for(event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            session.status = "failed"
            session.error = "timeout waiting for agent reply"
            session.reply = (
                "I'm still working on that in the background. "
                "Check the dashboard or send another message in a moment."
            )
        return session


__all__ = ["ChatSession", "ChatStore"]
