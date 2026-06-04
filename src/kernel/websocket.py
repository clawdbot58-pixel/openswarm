"""WebSocket transport for agents.

Agents connect to ``/ws`` and exchange :class:`~kernel.models.Envelope`
objects as JSON. The expected lifecycle is:

1. Agent opens a WebSocket.
2. Agent sends a ``register`` envelope whose ``payload.content_type`` is
   ``"data"`` and whose ``payload.data`` contains the agent's full
   ``manifest`` blob (matches :class:`~kernel.models.AgentManifest`).
   The bus's :class:`~kernel.bus.MessageBus` registers the manifest and
   attaches the WebSocket as a live subscriber.
3. Agent may now send any envelope. Outgoing envelopes enter the bus and
   are routed to their recipients. Envelopes addressed back to this
   agent are pushed down the WebSocket.
4. The agent may send an envelope of type ``heartbeat`` as an
   alternative to the file-based heartbeat protocol.
5. On disconnect the bus marks ``connected_ws = False`` and queues any
   future envelopes for this agent until it reconnects.

This module is a FastAPI router — it does not own the bus or the
registry. Both are pulled from ``app.state`` at call time.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, status
from pydantic import ValidationError as PydanticValidationError

from .exceptions import EnvelopeRejected
from .models import (
    AgentIdStr,
    AgentManifest,
    BROADCAST_AGENT_ID,
    Envelope,
    HeartbeatFile,
)
from .registry import AgentRegistry

if TYPE_CHECKING:  # pragma: no cover — only used for type hints
    from .bus import MessageBus
    from .heartbeat import HeartbeatMonitor

logger = logging.getLogger(__name__)


router = APIRouter()


# Names of the kernel state attributes we expect on ``app.state``.
STATE_BUS = "bus"
STATE_REGISTRY = "registry"
STATE_HEARTBEAT = "heartbeat"


def _state_attr(websocket: WebSocket, name: str) -> Any:
    """Return ``websocket.app.state.<name>`` or raise :class:`RuntimeError`."""
    state = getattr(websocket.app, "state", None)
    if state is None:
        raise RuntimeError("FastAPI app has no state; lifespan not initialised")
    value = getattr(state, name, None)
    if value is None:
        raise RuntimeError(f"app.state.{name} is not set")
    return value


def _get_bus(websocket: WebSocket) -> "MessageBus":
    return _state_attr(websocket, STATE_BUS)


def _get_registry(websocket: WebSocket) -> AgentRegistry:
    return _state_attr(websocket, STATE_REGISTRY)


def _get_heartbeat(websocket: WebSocket) -> "HeartbeatMonitor | None":
    return _state_attr(websocket, STATE_HEARTBEAT)


# ---------------------------------------------------------------------------
# Registration handshake
# ---------------------------------------------------------------------------

def _extract_manifest(envelope: Envelope) -> AgentManifest:
    """Pull a manifest blob out of a registration envelope's data payload."""
    if envelope.payload.content_type != "data":
        raise EnvelopeRejected(
            "registration envelope must have content_type='data'",
            envelope_id=envelope.envelope_id,
        )
    data = envelope.payload.data
    if not isinstance(data, dict) or "manifest" not in data:
        raise EnvelopeRejected(
            "registration payload must contain a 'manifest' field",
            envelope_id=envelope.envelope_id,
        )
    return AgentManifest.model_validate(data["manifest"])


async def _send_json(websocket: WebSocket, payload: dict[str, Any]) -> None:
    """Send a JSON payload to the WebSocket.

    Wrapped so we can swap in MessagePack or other encodings later
    without touching every call site.
    """
    text = json.dumps(payload, default=str)
    await websocket.send_text(text)


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------

@router.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket) -> None:
    """Handle a single agent's WebSocket session.

    Closes the socket with an appropriate ``close_code`` on protocol
    errors.  Successful sessions run until the client disconnects.
    """
    bus = _get_bus(websocket)
    registry = _get_registry(websocket)
    heartbeat = _get_heartbeat(websocket)

    await websocket.accept()
    agent_id: AgentIdStr | None = None

    try:
        # ---- 1. registration handshake ---------------------------------
        try:
            first_raw = await asyncio.wait_for(
                websocket.receive_text(), timeout=15.0
            )
        except asyncio.TimeoutError:
            await _send_json(
                websocket,
                {
                    "type": "error",
                    "code": "registration_timeout",
                    "message": "agent did not send registration envelope in time",
                },
            )
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
            return

        try:
            first_envelope = Envelope.model_validate_json(first_raw)
        except PydanticValidationError as exc:
            await _send_json(
                websocket,
                {
                    "type": "error",
                    "code": "envelope_rejected",
                    "message": "registration envelope failed schema validation",
                    "details": exc.errors(include_url=False),
                },
            )
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
            return

        try:
            manifest = _extract_manifest(first_envelope)
        except (EnvelopeRejected, PydanticValidationError) as exc:
            await _send_json(
                websocket,
                {
                    "type": "error",
                    "code": "registration_rejected",
                    "message": str(exc),
                },
            )
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
            return

        await registry.register(manifest, instance_id=manifest.agent_id)
        agent_id = manifest.agent_id

        # Send an ack so the agent knows the registration succeeded.
        await _send_json(
            websocket,
            {
                "type": "registered",
                "agent_id": agent_id,
                "envelope_id": str(first_envelope.envelope_id),
            },
        )
        logger.info("ws connected agent_id=%s", agent_id)

        # ---- 2. attach subscriber --------------------------------------
        async def subscriber(env: Envelope) -> None:
            # Skip echoing kernel events that the main-agent already sees
            # elsewhere (e.g. when this connection IS the main-agent).
            try:
                await _send_json(websocket, {"type": "envelope", "envelope": env.model_dump(mode="json")})
            except (WebSocketDisconnect, RuntimeError):
                raise

        await bus.register_subscriber(agent_id, subscriber)

        # ---- 3. main read loop -----------------------------------------
        while True:
            try:
                raw = await websocket.receive_text()
            except WebSocketDisconnect:
                break

            try:
                envelope = Envelope.model_validate_json(raw)
            except PydanticValidationError as exc:
                # Tell the agent about the bad envelope but keep the
                # connection open — a single malformed message should
                # not evict a live agent.
                await _send_json(
                    websocket,
                    {
                        "type": "envelope_rejected",
                        "code": "schema_validation_failed",
                        "details": exc.errors(include_url=False),
                    },
                )
                continue

            # Heartbeat shortcut: also feed the file-based monitor so the
            # two channels stay in sync.
            if envelope.envelope_type == "heartbeat":
                if heartbeat is not None:
                    await heartbeat.process_inbound_heartbeat(
                        envelope.sender.agent_id, envelope.created_at
                    )
                # Don't forward heartbeats through the bus — they're
                # control-plane chatter.
                continue

            # Refuse self-broadcast loops.
            if envelope.receiver.agent_id == agent_id and envelope.sender.agent_id == agent_id:
                await _send_json(
                    websocket,
                    {
                        "type": "error",
                        "code": "self_targeting_envelope",
                        "message": "envelopes addressed from an agent to itself are dropped",
                    },
                )
                continue

            # Reject broadcast envelopes from a non-orchestrator. The
            # main-agent is the only agent that may fan-out to the swarm.
            if (
                envelope.is_broadcast()
                and agent_id != bus._settings.main_agent_id
            ):
                await _send_json(
                    websocket,
                    {
                        "type": "error",
                        "code": "broadcast_forbidden",
                        "message": "only the main agent may broadcast",
                    },
                )
                continue

            try:
                await bus.send(envelope)
            except Exception as exc:  # noqa: BLE001 — surface to client
                await _send_json(
                    websocket,
                    {
                        "type": "error",
                        "code": exc.__class__.__name__,
                        "message": str(exc),
                        "envelope_id": str(envelope.envelope_id),
                    },
                )
                continue

            await _send_json(
                websocket,
                {
                    "type": "ack",
                    "envelope_id": str(envelope.envelope_id),
                },
            )

    except WebSocketDisconnect:
        logger.info("ws disconnected agent_id=%s", agent_id)
    except Exception:  # noqa: BLE001
        logger.exception("ws handler crashed for agent_id=%s", agent_id)
    finally:
        if agent_id is not None:
            try:
                await bus.unregister_subscriber(agent_id)
            except Exception:  # noqa: BLE001
                logger.debug("unregister_subscriber failed for %s", agent_id)
        try:
            await websocket.close()
        except (RuntimeError, WebSocketDisconnect):
            pass


__all__ = ["router", "websocket_endpoint"]
