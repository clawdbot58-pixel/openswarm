"""Base WebSocket agent that every user-facing agent inherits from.

This is the single foundation for :class:`MainAgent`, :class:`Conductor`,
and :class:`SectorManager`. It owns the WebSocket connection to the
kernel, the registration handshake, the heartbeat loop, the inbound
envelope dispatch, and the envelope construction helpers.

Subclasses are expected to override :meth:`BaseAgent.on_envelope` and
optionally :meth:`BaseAgent.on_event`. The base class takes care of
every transport concern, including:

* connecting to ``ws://{host}:{port}/ws`` (defaults to localhost:8765);
* sending the registration envelope (manifest wrapped in a data
  payload) as the first message;
* starting a heartbeat task that emits a heartbeat envelope every
  :attr:`BaseAgent.heartbeat_interval` seconds;
* routing inbound envelopes to ``on_envelope``;
* routing kernel-emitted events to ``on_event``;
* serialising outbound envelopes as JSON;
* exposing :meth:`BaseAgent.send`, :meth:`BaseAgent.send_event`,
  :meth:`BaseAgent.build_request`, etc. for subclasses to use;
* reconnecting with exponential backoff on transient drops;
* shutting down cleanly on :meth:`BaseAgent.close`.

The class is async-first but is also usable as an :term:`async context
manager`::

    async with BaseAgent(manifest, ws_url=...) as agent:
        await agent.send(...)

This is a pure client: it never modifies the kernel. It validates
locally-built envelopes against :class:`kernel.models.Envelope` only
as a guard against the agent's own bugs; the kernel re-validates
everything on receipt.
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable, Optional

try:  # websockets is in requirements.txt; degrade gracefully if missing.
    import websockets
    from websockets.exceptions import ConnectionClosed
except ImportError:  # pragma: no cover — only happens without deps
    websockets = None  # type: ignore[assignment]
    ConnectionClosed = Exception  # type: ignore[assignment, misc]

# The kernel's Pydantic models are the source of truth for envelope
# and manifest shapes. We import them lazily so that tests that mock
# the transport can construct a BaseAgent without a working ``kernel``
# import path (some CI setups put ``src`` on the path after the test
# is collected).
def _import_kernel_models() -> tuple[type, type, type]:
    from kernel.models import (
        AgentManifest as _AgentManifest,
        Endpoint as _Endpoint,
        Envelope as _Envelope,
    )
    return _AgentManifest, _Endpoint, _Envelope


AgentManifest, Endpoint, Envelope = _import_kernel_models()


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DEFAULT_WS_URL: str = "ws://127.0.0.1:8765/ws"
DEFAULT_HEARTBEAT_INTERVAL: float = 10.0
DEFAULT_RECONNECT_INITIAL_BACKOFF: float = 0.5
DEFAULT_RECONNECT_MAX_BACKOFF: float = 30.0
DEFAULT_REGISTRATION_TIMEOUT: float = 15.0
DEFAULT_ENVELOPE_QUEUE_SIZE: int = 256
DEFAULT_SHUTDOWN_TIMEOUT: float = 5.0


# ---------------------------------------------------------------------------
# Envelope helpers (also exposed to subclasses)
# ---------------------------------------------------------------------------

def new_envelope_id() -> str:
    """Generate a fresh UUID4 envelope id (string)."""
    return str(uuid.uuid4())


def utc_now() -> datetime:
    """Return a timezone-aware UTC ``datetime``."""
    return datetime.now(timezone.utc)


def _utc_now_iso() -> str:
    """Return the current UTC time as an ISO 8601 string with a ``Z`` suffix."""
    return utc_now().isoformat().replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# Exception types
# ---------------------------------------------------------------------------

class AgentError(RuntimeError):
    """Base class for :class:`BaseAgent` errors."""


class AgentNotRegistered(AgentError):
    """Raised when the agent tries to send before the registration handshake completes."""


class AgentConnectionLost(AgentError):
    """Raised when the WebSocket disconnects mid-send."""


# ---------------------------------------------------------------------------
# Base agent
# ---------------------------------------------------------------------------

class BaseAgent:
    """Async WebSocket agent. Subclass to add domain behaviour.

    Parameters
    ----------
    manifest
        A :class:`kernel.models.AgentManifest` (or a dict matching its
        schema). The manifest is what the kernel validates during
        registration.
    ws_url
        The kernel's WebSocket endpoint. Defaults to localhost.
    heartbeat_interval
        Seconds between heartbeat envelopes. Pass ``0`` to disable
        heartbeats (not recommended in production).
    auto_reconnect
        If ``True`` (default) the agent will reconnect with
        exponential backoff when the socket drops.
    on_envelope
        Optional default handler for inbound envelopes. Subclasses
        usually override :meth:`on_envelope` instead of passing this.
    on_event
        Optional default handler for kernel-emitted events.
    """

    def __init__(
        self,
        manifest: AgentManifest | dict[str, Any],
        *,
        ws_url: str = DEFAULT_WS_URL,
        heartbeat_interval: float = DEFAULT_HEARTBEAT_INTERVAL,
        auto_reconnect: bool = True,
        reconnect_initial_backoff: float = DEFAULT_RECONNECT_INITIAL_BACKOFF,
        reconnect_max_backoff: float = DEFAULT_RECONNECT_MAX_BACKOFF,
        registration_timeout: float = DEFAULT_REGISTRATION_TIMEOUT,
        send_queue_size: int = DEFAULT_ENVELOPE_QUEUE_SIZE,
        system_prompt_path: str | Path | None = None,
    ) -> None:
        if isinstance(manifest, dict):
            manifest = AgentManifest.model_validate(manifest)
        elif not isinstance(manifest, AgentManifest):
            raise TypeError(
                "manifest must be an AgentManifest instance or a dict"
            )
        self._manifest: AgentManifest = manifest
        self.agent_id: str = manifest.agent_id
        self.role: str = manifest.role
        self._ws_url: str = ws_url
        self._heartbeat_interval: float = float(heartbeat_interval)
        self._auto_reconnect: bool = bool(auto_reconnect)
        self._reconnect_initial: float = float(reconnect_initial_backoff)
        self._reconnect_max: float = float(reconnect_max_backoff)
        self._registration_timeout: float = float(registration_timeout)
        self._send_queue_size: int = int(send_queue_size)

        # Runtime state.
        self._ws: Any = None
        self._reader_task: asyncio.Task[None] | None = None
        self._heartbeat_task: asyncio.Task[None] | None = None
        self._send_loop_task: asyncio.Task[None] | None = None
        self._send_queue: asyncio.Queue[Envelope] = asyncio.Queue(
            maxsize=self._send_queue_size
        )
        self._outbox: list[Envelope] = []  # envelopes sent but not yet acked
        self._connected_event: asyncio.Event = asyncio.Event()
        self._registered_event: asyncio.Event = asyncio.Event()
        self._draining: bool = False
        self._closed: bool = False
        self._send_lock: asyncio.Lock = asyncio.Lock()
        self._last_ack_id: str | None = None
        self._last_error: dict[str, Any] | None = None
        self._reconnect_attempts: int = 0
        # The kernel tells the agent it has accepted the manifest via
        # the "registered" message on the WebSocket; we capture the
        # ack so subclasses can read it.
        self.registration_ack: dict[str, Any] | None = None

        # System prompt loader. Used by MainAgent / Conductor / SectorManager
        # to build their preamble. Path is resolved relative to the project
        # root by the subclasses; this base class just stores it.
        self._system_prompt_path: Path | None = (
            Path(system_prompt_path) if system_prompt_path else None
        )

    # -- properties --------------------------------------------------------

    @property
    def manifest(self) -> AgentManifest:
        """The agent's manifest (read-only)."""
        return self._manifest

    @property
    def is_connected(self) -> bool:
        """``True`` when the WebSocket is open and the agent is registered."""
        return (
            self._ws is not None
            and not self._ws.close_code
            and self._registered_event.is_set()
        )

    @property
    def is_draining(self) -> bool:
        """``True`` after :meth:`drain` has been called."""
        return self._draining

    @property
    def is_closed(self) -> bool:
        """``True`` after :meth:`close` has completed."""
        return self._closed

    @property
    def ws_url(self) -> str:
        """The WebSocket URL this agent connects to."""
        return self._ws_url

    @property
    def system_prompt_path(self) -> Path | None:
        """The path to the system prompt (set by subclasses)."""
        return self._system_prompt_path

    @property
    def system_prompt(self) -> str:
        """The contents of the system prompt file, or an empty string."""
        if self._system_prompt_path is None:
            return ""
        try:
            return self._system_prompt_path.read_text(encoding="utf-8")
        except OSError:
            return ""

    # -- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        """Open the WebSocket, register, and start the background tasks.

        Idempotent — calling :meth:`start` on an already-started agent
        is a no-op.
        """
        if self._closed:
            raise AgentError("cannot start a closed agent")
        if self._reader_task is not None and not self._reader_task.done():
            return
        await self._connect_and_register()
        self._reader_task = asyncio.create_task(
            self._reader_loop(), name=f"agent-reader-{self.agent_id}"
        )
        self._send_loop_task = asyncio.create_task(
            self._send_loop(), name=f"agent-send-{self.agent_id}"
        )
        if self._heartbeat_interval > 0:
            self._heartbeat_task = asyncio.create_task(
                self._heartbeat_loop(), name=f"agent-hb-{self.agent_id}"
            )

    async def __aenter__(self) -> "BaseAgent":
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close()

    async def drain(self) -> None:
        """Mark the agent as draining.

        The agent stops accepting new outbound work but does not
        disconnect. Kernel-side this maps to ``status=draining``.
        """
        self._draining = True

    async def close(self) -> None:
        """Stop background tasks, close the WebSocket, mark closed.

        Safe to call multiple times. Waits up to
        :attr:`DEFAULT_SHUTDOWN_TIMEOUT` seconds for the reader to
        exit cleanly.
        """
        if self._closed:
            return
        self._draining = True
        self._closed = True
        # Cancel background tasks.
        for task in (
            self._heartbeat_task,
            self._send_loop_task,
            self._reader_task,
        ):
            if task is not None and not task.done():
                task.cancel()
        # Drain the send queue best-effort.
        await self._drain_send_queue()
        # Close the websocket.
        if self._ws is not None:
            try:
                await asyncio.wait_for(self._ws.close(), timeout=2.0)
            except (asyncio.TimeoutError, Exception):  # noqa: BLE001
                pass
            self._ws = None
        # Await background task terminations.
        for task in (
            self._heartbeat_task,
            self._send_loop_task,
            self._reader_task,
        ):
            if task is not None:
                try:
                    await asyncio.wait_for(task, timeout=DEFAULT_SHUTDOWN_TIMEOUT)
                except (asyncio.TimeoutError, asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
        self._heartbeat_task = None
        self._send_loop_task = None
        self._reader_task = None
        self._connected_event.clear()
        self._registered_event.clear()

    # -- WebSocket connection ---------------------------------------------

    async def _connect_and_register(self) -> None:
        """Open the WebSocket and complete the registration handshake."""
        if websockets is None:
            raise AgentError(
                "websockets library is required; install it via "
                "'pip install websockets'"
            )
        try:
            self._ws = await asyncio.wait_for(
                websockets.connect(self._ws_url, max_size=2**20),
                timeout=self._registration_timeout,
            )
        except (asyncio.TimeoutError, OSError) as exc:
            raise AgentConnectionLost(
                f"could not connect to {self._ws_url}: {exc}"
            ) from exc
        # Build the registration envelope and send it.
        registration = self.build_registration_envelope()
        try:
            await asyncio.wait_for(
                self._ws.send(registration.model_dump_json()),
                timeout=self._registration_timeout,
            )
        except (asyncio.TimeoutError, Exception) as exc:  # noqa: BLE001
            raise AgentError(f"failed to send registration envelope: {exc}") from exc
        # Wait for the "registered" ack. Kernel sends it as a small JSON
        # control message (not an Envelope).
        try:
            raw = await asyncio.wait_for(
                self._ws.recv(), timeout=self._registration_timeout
            )
        except (asyncio.TimeoutError, Exception) as exc:  # noqa: BLE001
            raise AgentError(f"no registration ack in time: {exc}") from exc
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise AgentError(
                f"registration ack was not valid JSON: {exc}"
            ) from exc
        if msg.get("type") == "registered":
            self.registration_ack = msg
            self._registered_event.set()
            self._connected_event.set()
            self._reconnect_attempts = 0
        elif msg.get("type") == "error":
            self._last_error = msg
            raise AgentError(
                f"registration rejected: {msg.get('code')!r} — "
                f"{msg.get('message')!r}"
            )
        else:
            raise AgentError(
                f"unexpected first message from kernel: {msg.get('type')!r}"
            )

    async def _reader_loop(self) -> None:
        """Receive envelopes from the kernel and dispatch them."""
        if self._ws is None:
            return
        try:
            async for raw in self._ws:
                if self._draining:
                    # After drain() we still process pending inbound
                    # messages, but we may choose to ignore them in
                    # subclasses. The base class dispatches as usual.
                    pass
                await self._dispatch_raw(raw)
        except ConnectionClosed:
            logger.info("ws closed for %s", self.agent_id)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("reader crashed for %s", self.agent_id)
        finally:
            self._connected_event.clear()
            self._registered_event.clear()
            self._ws = None
            if self._auto_reconnect and not self._closed:
                asyncio.create_task(self._reconnect())

    async def _reconnect(self) -> None:
        """Reconnect with exponential backoff + jitter."""
        delay = self._reconnect_initial
        while not self._closed:
            self._reconnect_attempts += 1
            wait = delay + random.uniform(0, delay * 0.25)
            logger.info(
                "reconnecting %s in %.2fs (attempt %d)",
                self.agent_id, wait, self._reconnect_attempts,
            )
            await asyncio.sleep(wait)
            try:
                await self._connect_and_register()
            except Exception as exc:  # noqa: BLE001
                logger.warning("reconnect failed for %s: %s", self.agent_id, exc)
                delay = min(self._reconnect_max, delay * 2)
                continue
            # Successful reconnect: restart the reader.
            self._reader_task = asyncio.create_task(
                self._reader_loop(), name=f"agent-reader-{self.agent_id}"
            )
            return

    async def _dispatch_raw(self, raw: str | bytes) -> None:
        """Parse a single raw WS frame and dispatch it."""
        if isinstance(raw, (bytes, bytearray)):
            raw = raw.decode("utf-8", errors="replace")
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("dropped non-JSON frame on %s", self.agent_id)
            return
        mtype = msg.get("type")
        if mtype == "ack":
            self._last_ack_id = msg.get("envelope_id")
            # Remove from the outbox.
            async with self._send_lock:
                self._outbox = [
                    e for e in self._outbox
                    if str(e.envelope_id) != str(self._last_ack_id)
                ]
            return
        if mtype == "envelope":
            env_data = msg.get("envelope")
            if not isinstance(env_data, dict):
                return
            try:
                env = Envelope.model_validate(env_data)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "kernel sent unparseable envelope: %s", exc
                )
                return
            await self._dispatch_envelope(env)
            return
        if mtype in {"error", "envelope_rejected"}:
            self._last_error = msg
            # Some errors are recoverable, some are fatal; for now we
            # just surface them via on_event so subclasses can decide.
            await self.on_event(mtype, msg)
            return
        if mtype == "registered":
            # Rare: kernel may re-ack after a reconnect. Treat as
            # already-registered and continue.
            self._registered_event.set()
            return
        # Unknown control frame — log and continue.
        logger.debug("unknown control frame on %s: %s", self.agent_id, mtype)

    async def _dispatch_envelope(self, envelope: Envelope) -> None:
        """Route an inbound envelope to the right handler."""
        # Kernel-emitted events use envelope_type=event and carry the
        # event name in payload.data.event. We special-case them.
        if envelope.envelope_type == "event":
            data = envelope.payload
            event_name: str | None = None
            try:
                # payload is a discriminated union; only DataPayload has .data
                if hasattr(data, "data") and isinstance(data.data, dict):  # type: ignore[attr-defined]
                    event_name = str(data.data.get("event", ""))  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001
                event_name = None
            if event_name:
                details = data.data if isinstance(data.data, dict) else {}  # type: ignore[attr-defined]
                await self.on_event(event_name, details)
            return
        await self.on_envelope(envelope)

    # -- heartbeat ---------------------------------------------------------

    async def _heartbeat_loop(self) -> None:
        """Emit heartbeat envelopes at the configured interval."""
        try:
            while not self._closed:
                await asyncio.sleep(self._heartbeat_interval)
                if not self.is_connected:
                    continue
                try:
                    envelope = self.build_heartbeat()
                    await self._send_raw(envelope.model_dump_json())
                except Exception:  # noqa: BLE001
                    logger.debug(
                        "heartbeat send failed for %s", self.agent_id
                    )
        except asyncio.CancelledError:
            raise

    # -- send API ----------------------------------------------------------

    async def send(self, envelope: Envelope) -> Envelope:
        """Queue an envelope for delivery. Returns it for chaining."""
        if self._draining:
            raise AgentError("agent is draining; refusing new send")
        if not self._registered_event.is_set() and not self._closed:
            # Wait briefly for registration; if it never comes, raise.
            try:
                await asyncio.wait_for(
                    self._registered_event.wait(), timeout=2.0
                )
            except asyncio.TimeoutError as exc:
                raise AgentNotRegistered(
                    "cannot send before registration handshake completes"
                ) from exc
        await self._send_queue.put(envelope)
        async with self._send_lock:
            self._outbox.append(envelope)
        return envelope

    async def _drain_send_queue(self) -> None:
        """Best-effort flush of pending outbound envelopes on shutdown."""
        if self._ws is None or self._ws.close_code is not None:
            return
        while not self._send_queue.empty():
            try:
                env = self._send_queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            try:
                await asyncio.wait_for(
                    self._ws.send(env.model_dump_json()),
                    timeout=2.0,
                )
            except Exception:  # noqa: BLE001
                break

    async def _send_loop(self) -> None:
        """Background task that pulls envelopes from the queue and writes them."""
        try:
            while not self._closed:
                try:
                    env = await asyncio.wait_for(
                        self._send_queue.get(), timeout=0.5
                    )
                except asyncio.TimeoutError:
                    continue
                if self._ws is None or self._ws.close_code is not None:
                    # Reconnect is the reader's job. Re-queue and back off.
                    try:
                        self._send_queue.put_nowait(env)
                    except asyncio.QueueFull:
                        logger.warning(
                            "send queue full; dropping %s", env.envelope_id
                        )
                    await asyncio.sleep(0.5)
                    continue
                try:
                    await self._send_raw(env.model_dump_json())
                except Exception as exc:  # noqa: BLE001
                    logger.warning("send failed for %s: %s", self.agent_id, exc)
        except asyncio.CancelledError:
            raise

    async def _send_raw(self, payload: str) -> None:
        """Write a single frame to the WebSocket."""
        if self._ws is None:
            raise AgentConnectionLost("websocket is not connected")
        await self._ws.send(payload)

    # -- envelope construction helpers -------------------------------------

    def build_registration_envelope(self) -> Envelope:
        """Build the registration envelope (first message on connect)."""
        manifest_dict = self._manifest.model_dump(mode="json")
        # Force registration_time to a fresh value: the manifest's
        # own value may be stale across restarts.
        manifest_dict["registration_time"] = _utc_now_iso()
        return Envelope(
            envelope_id=new_envelope_id(),
            created_at=utc_now(),
            envelope_type="request",
            sender=Endpoint(agent_id=self.agent_id, role=self.role),
            receiver=Endpoint(agent_id=self.agent_id, role=self.role),
            preamble={
                "intent": {"goal": "Register with kernel", "phase": "discovery"},
                "permissions": {
                    "can_read": [],
                    "can_write": [],
                    "can_execute": [],
                    "can_delegate": False,
                },
                "thinking_loop_config": {
                    "mode": "fast",
                    "max_iterations": 1,
                },
            },
            payload={
                "content_type": "data",
                "data": {"manifest": manifest_dict},
            },
            metadata={"priority": 5},
        )

    def build_heartbeat(self) -> Envelope:
        """Build a heartbeat envelope (envelope_type='heartbeat')."""
        return Envelope(
            envelope_id=new_envelope_id(),
            created_at=utc_now(),
            envelope_type="heartbeat",
            sender=Endpoint(agent_id=self.agent_id, role=self.role),
            receiver=Endpoint(agent_id="kernel", role="kernel"),
            preamble={
                "intent": {"goal": "heartbeat", "phase": "execution"},
                "permissions": {"can_delegate": False},
                "thinking_loop_config": {"mode": "fast", "max_iterations": 1},
            },
            payload={"content_type": "data", "data": {"status": "alive"}},
            metadata={"priority": 1},
        )

    def build_request(
        self,
        receiver_id: str,
        payload: dict[str, Any],
        *,
        receiver_role: str = "executor",
        goal: str = "agent-task",
        phase: str = "execution",
        reply_to: str | None = None,
    ) -> Envelope:
        """Build a request envelope from a payload dict.

        The payload dict must be a valid :class:`Envelope.payload`
        sub-dict (``content_type`` plus the matching shape).
        """
        return Envelope(
            envelope_id=new_envelope_id(),
            created_at=utc_now(),
            envelope_type="request",
            sender=Endpoint(agent_id=self.agent_id, role=self.role),
            receiver=Endpoint(agent_id=receiver_id, role=receiver_role),
            reply_to=reply_to,
            preamble={
                "intent": {"goal": goal, "phase": phase},
                "permissions": {"can_delegate": False},
                "thinking_loop_config": {"mode": "thorough", "max_iterations": 10},
            },
            payload=payload,  # type: ignore[arg-type]
            metadata={"priority": 5},
        )

    def build_event(
        self,
        receiver_id: str,
        event_name: str,
        details: dict[str, Any] | None = None,
        *,
        receiver_role: str = "executor",
    ) -> Envelope:
        """Build a fire-and-forget event envelope."""
        data: dict[str, Any] = {"event": event_name}
        if details:
            data.update(details)
        return Envelope(
            envelope_id=new_envelope_id(),
            created_at=utc_now(),
            envelope_type="event",
            sender=Endpoint(agent_id=self.agent_id, role=self.role),
            receiver=Endpoint(agent_id=receiver_id, role=receiver_role),
            preamble={
                "intent": {"goal": f"event:{event_name}", "phase": "execution"},
                "permissions": {"can_delegate": False},
                "thinking_loop_config": {"mode": "fast", "max_iterations": 1},
            },
            payload={"content_type": "data", "data": data},
            metadata={"priority": 8},
        )

    def build_response(
        self,
        receiver_id: str,
        reply_to: str,
        payload: dict[str, Any],
        *,
        receiver_role: str = "executor",
    ) -> Envelope:
        """Build a response envelope linked to an earlier request."""
        return Envelope(
            envelope_id=new_envelope_id(),
            created_at=utc_now(),
            envelope_type="response",
            sender=Endpoint(agent_id=self.agent_id, role=self.role),
            receiver=Endpoint(agent_id=receiver_id, role=receiver_role),
            reply_to=reply_to,
            preamble={
                "intent": {"goal": "response", "phase": "execution"},
                "permissions": {"can_delegate": False},
                "thinking_loop_config": {"mode": "fast", "max_iterations": 1},
            },
            payload=payload,  # type: ignore[arg-type]
            metadata={"priority": 5},
        )

    def build_error(
        self,
        receiver_id: str,
        code: str,
        message: str,
        details: dict[str, Any] | None = None,
        *,
        receiver_role: str = "executor",
        reply_to: str | None = None,
    ) -> Envelope:
        """Build an error envelope."""
        return Envelope(
            envelope_id=new_envelope_id(),
            created_at=utc_now(),
            envelope_type="error",
            sender=Endpoint(agent_id=self.agent_id, role=self.role),
            receiver=Endpoint(agent_id=receiver_id, role=receiver_role),
            reply_to=reply_to,
            preamble={
                "intent": {"goal": "error", "phase": "execution"},
                "permissions": {"can_delegate": False},
                "thinking_loop_config": {"mode": "fast", "max_iterations": 1},
            },
            payload={
                "content_type": "data",
                "data": {"code": code, "message": message, "details": details or {}},
            },
            metadata={"priority": 9},
        )

    # -- subclass hooks (must/can override) --------------------------------

    async def on_envelope(self, envelope: Envelope) -> None:
        """Handle an inbound envelope from the kernel.

        Subclasses override this. The default behaviour is to log
        and drop. **Never raise** from this method unless you want
        to crash the agent — wrap risky work in try/except and call
        :meth:`on_event` for kernel-emitted events.
        """
        logger.debug(
            "agent %s received envelope type=%s from %s",
            self.agent_id,
            envelope.envelope_type,
            envelope.sender.agent_id,
        )

    async def on_event(self, event_name: str, details: dict[str, Any]) -> None:
        """Handle a kernel-emitted event.

        Subclasses override this for events they care about. The
        default is to log.
        """
        logger.debug(
            "agent %s received event=%s details=%s",
            self.agent_id, event_name, details,
        )

    # -- introspection -----------------------------------------------------

    def outbox_snapshot(self) -> list[dict[str, Any]]:
        """Return a JSON-safe snapshot of pending outbound envelopes."""
        return [e.model_dump(mode="json") for e in self._outbox]

    def last_error(self) -> dict[str, Any] | None:
        """Return the most recent error the kernel reported, or ``None``."""
        return dict(self._last_error) if self._last_error else None


__all__ = [
    "AgentConnectionLost",
    "AgentError",
    "AgentNotRegistered",
    "BaseAgent",
    "DEFAULT_HEARTBEAT_INTERVAL",
    "DEFAULT_RECONNECT_INITIAL_BACKOFF",
    "DEFAULT_RECONNECT_MAX_BACKOFF",
    "DEFAULT_REGISTRATION_TIMEOUT",
    "DEFAULT_SHUTDOWN_TIMEOUT",
    "DEFAULT_WS_URL",
    "new_envelope_id",
    "utc_now",
]
