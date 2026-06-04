"""Custom exception hierarchy for the OpenSwarm kernel.

Each subsystem raises a focused exception type so the API/WS layer can map
failures to the right HTTP/WebSocket error without inspecting string
messages. The :class:`KernelError` base gives a stable ``code`` for
structured logging and a ``details`` dict for debugging context.
"""
from __future__ import annotations

from typing import Any


class KernelError(Exception):
    """Root of the kernel exception tree.

    All other kernel exceptions inherit from this class so callers can
    ``except KernelError`` as a catch-all without resorting to bare
    :class:`Exception` blocks.
    """

    code: str = "kernel_error"

    def __init__(self, message: str, **details: Any) -> None:
        super().__init__(message)
        self.message: str = message
        self.details: dict[str, Any] = dict(details)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable representation of this error."""
        return {"code": self.code, "message": self.message, "details": self.details}


class ValidationError(KernelError):
    """Raised when an envelope or manifest fails schema validation."""

    code = "validation_error"


class EnvelopeRejected(ValidationError):
    """An incoming envelope violated ``envelope.json``."""

    code = "envelope_rejected"


class ManifestRejected(ValidationError):
    """An agent manifest violated ``manifest.json``."""

    code = "registration_rejected"


class RegistrationError(KernelError):
    """Generic registry-level failure (DB, conflicts, etc.)."""

    code = "registration_error"


class AgentNotFound(RegistrationError):
    """Lookup against the registry returned no row."""

    code = "agent_not_found"


class AgentAlreadyRegistered(RegistrationError):
    """Attempted to register an agent_id that is already in the registry."""

    code = "agent_already_registered"


class PermissionDenied(KernelError):
    """A sender tried to act outside the permissions in its manifest."""

    code = "permission_denied"

    def __init__(
        self,
        message: str,
        *,
        agent_id: str = "",
        envelope_id: str = "",
        reason: str = "",
        **details: Any,
    ) -> None:
        super().__init__(message, **details)
        self.agent_id = agent_id
        self.envelope_id = envelope_id
        self.reason = reason


class QueueOverflow(KernelError):
    """A receiver's in-memory queue exceeded its configured cap."""

    code = "queue_overflow"


class ExpiredEnvelope(KernelError):
    """An envelope with an ``expires_at`` in the past was rejected."""

    code = "expired_envelope"


class RoutingError(KernelError):
    """The bus could not resolve a destination for an envelope."""

    code = "routing_error"


class HeartbeatError(KernelError):
    """A heartbeat file could not be read, parsed, or used for routing."""

    code = "heartbeat_error"


__all__ = [
    "AgentAlreadyRegistered",
    "AgentNotFound",
    "EnvelopeRejected",
    "ExpiredEnvelope",
    "HeartbeatError",
    "KernelError",
    "ManifestRejected",
    "PermissionDenied",
    "QueueOverflow",
    "RegistrationError",
    "RoutingError",
    "ValidationError",
]
