"""REST API for the kernel.

Exposes the registry, the bus, and basic health/metrics. Designed for
the dashboard, integration tests, and ad-hoc curl debugging. Every
endpoint pulls its dependencies from ``app.state`` so the same router
can be re-mounted in different processes (tests, staging, prod).
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field

from .exceptions import AgentNotFound, EnvelopeRejected, KernelError
from .models import Envelope

if TYPE_CHECKING:  # pragma: no cover
    from .bus import MessageBus
    from .heartbeat import HeartbeatMonitor
    from .registry import AgentRegistry

logger = logging.getLogger(__name__)


router = APIRouter()


# ---------------------------------------------------------------------------
# Pydantic request/response shapes
# ---------------------------------------------------------------------------

class HealthResponse(BaseModel):
    """Body of ``GET /health``."""

    status: str = "ok"
    db_ok: bool
    queue_total: int
    main_agent_id: str
    uptime_seconds: float


class AgentSummary(BaseModel):
    """Small projection of an agent for list views."""

    agent_id: str
    status: str
    last_heartbeat: str | None
    connected_ws: bool
    registered_at: str
    instance_id: str | None


class AgentStatusResponse(BaseModel):
    """Body of ``GET /registry/agents/{id}/status``."""

    agent_id: str
    status: str
    last_heartbeat: str | None
    connected_ws: bool
    registered_at: str
    instance_id: str | None


class ManifestEnvelope(BaseModel):
    """Wrapper for ``POST /registry/agents`` (manifest-only registration)."""

    manifest: dict[str, Any] = Field(..., description="Full AgentManifest blob")


class RegistrationResponse(BaseModel):
    """Body returned by ``POST /registry/agents``."""

    agent_id: str
    status: str


class SendEnvelopeRequest(BaseModel):
    """Body of ``POST /bus/send``. The envelope must validate against
    :class:`~kernel.models.Envelope`."""

    envelope: dict[str, Any]


class SendEnvelopeResponse(BaseModel):
    """Body returned by ``POST /bus/send``."""

    envelope_id: str
    routed: bool


class MetricsResponse(BaseModel):
    """Body of ``GET /metrics``."""

    uptime_seconds: float
    bus: dict[str, Any]
    registry_agent_count: int
    registry_status_counts: dict[str, int]
    queue_total: int


class ErrorResponse(BaseModel):
    """Standard error body returned for non-2xx responses."""

    code: str
    message: str
    details: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _bus(request: Request) -> "MessageBus":
    return request.app.state.bus


def _registry(request: Request) -> "AgentRegistry":
    return request.app.state.registry


def _heartbeat(request: Request) -> "HeartbeatMonitor | None":
    return getattr(request.app.state, "heartbeat", None)


def _main_agent_id(request: Request) -> str:
    return request.app.state.settings.main_agent_id


def _jsonify_pydantic_errors(errors: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Recursively coerce non-JSON-serializable values inside Pydantic errors.

    Pydantic v2's ``ctx`` dict for a custom ``field_validator`` that raises
    ``ValueError`` carries the original exception instance, which is not
    JSON-serializable. We walk the structure and ``repr()`` anything that
    isn't a primitive. Same for ``input`` — sometimes it's a complex
    object (e.g. a dataclass) that breaks ``JSONResponse``.
    """
    out: list[dict[str, Any]] = []
    for err in errors:
        clean: dict[str, Any] = {}
        for k, v in err.items():
            if k == "ctx" and isinstance(v, dict):
                clean[k] = {ck: _coerce(cv) for ck, cv in v.items()}
            else:
                clean[k] = _coerce(v)
        out.append(clean)
    return out


def _coerce(value: Any) -> Any:
    """Best-effort conversion of arbitrary objects into JSON-safe primitives."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (list, tuple)):
        return [_coerce(x) for x in value]
    if isinstance(value, dict):
        return {str(k): _coerce(v) for k, v in value.items()}
    return repr(value)


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

@router.get("/health", response_model=HealthResponse)
async def health(request: Request) -> HealthResponse:
    """Liveness probe: checks DB connectivity and returns queue sizes."""
    registry = _registry(request)
    bus = _bus(request)
    db_ok = await registry.healthcheck()
    if not db_ok:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "db_unreachable", "message": "registry DB is not responsive"},
        )
    started = bus.metrics.started_at
    uptime = (started.now(tz=started.tzinfo) - started).total_seconds() if started.tzinfo else 0.0
    return HealthResponse(
        db_ok=db_ok,
        queue_total=bus.total_queued(),
        main_agent_id=_main_agent_id(request),
        uptime_seconds=uptime,
    )


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

@router.get(
    "/registry/agents",
    response_model=list[AgentSummary],
)
async def list_agents(
    request: Request,
    status_filter: str | None = None,
) -> list[AgentSummary]:
    """List registered agents, optionally filtered by status."""
    registry = _registry(request)
    rows = await registry.list_status(status_filter=status_filter)  # type: ignore[arg-type]
    return [AgentSummary(**row) for row in rows]


@router.get(
    "/registry/agents/{agent_id}",
    response_model=dict,
)
async def get_agent(request: Request, agent_id: str) -> dict[str, Any]:
    """Return the full manifest for an agent."""
    registry = _registry(request)
    try:
        manifest = await registry.get(agent_id)
    except AgentNotFound as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=ErrorResponse(
                code=exc.code, message=exc.message, details=exc.details
            ).model_dump(),
        ) from exc
    return manifest.model_dump(mode="json")


@router.get(
    "/registry/agents/{agent_id}/status",
    response_model=AgentStatusResponse,
)
async def get_agent_status(request: Request, agent_id: str) -> AgentStatusResponse:
    """Return just the status row for an agent."""
    registry = _registry(request)
    try:
        row = await registry.get_status(agent_id)
    except AgentNotFound as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=ErrorResponse(
                code=exc.code, message=exc.message, details=exc.details
            ).model_dump(),
        ) from exc
    return AgentStatusResponse(**row)


@router.post(
    "/registry/agents",
    response_model=RegistrationResponse,
    status_code=status.HTTP_201_CREATED,
)
async def register_agent(
    request: Request, body: ManifestEnvelope
) -> RegistrationResponse:
    """Register an agent from a manifest blob (no WebSocket required)."""
    from .models import AgentManifest

    registry = _registry(request)
    try:
        manifest = AgentManifest.model_validate(body.manifest)
    except Exception as exc:  # noqa: BLE001
        raw_errors = getattr(exc, "errors", lambda: [{"error": str(exc)}])()
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=ErrorResponse(
                code="registration_rejected",
                message="manifest failed schema validation",
                details={"errors": _jsonify_pydantic_errors(raw_errors)},
            ).model_dump(),
        ) from exc
    await registry.register(manifest)
    bus = _bus(request)
    bus.metrics.agents_registered += 1
    return RegistrationResponse(agent_id=manifest.agent_id, status=manifest.status)


@router.delete(
    "/registry/agents/{agent_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def unregister_agent(request: Request, agent_id: str) -> None:
    """Soft-delete an agent (sets status=offline)."""
    registry = _registry(request)
    try:
        await registry.unregister(agent_id)
    except AgentNotFound as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=ErrorResponse(
                code=exc.code, message=exc.message, details=exc.details
            ).model_dump(),
        ) from exc
    bus = _bus(request)
    bus.metrics.agents_unregistered += 1


# ---------------------------------------------------------------------------
# Bus
# ---------------------------------------------------------------------------

@router.post(
    "/bus/send",
    response_model=SendEnvelopeResponse,
)
async def bus_send(request: Request, body: SendEnvelopeRequest) -> SendEnvelopeResponse:
    """Inject an envelope into the bus.

    Used by the dashboard, integration tests, and external triggers. The
    envelope is validated against the contract schema before delivery.
    """
    bus = _bus(request)
    try:
        envelope = Envelope.model_validate(body.envelope)
    except Exception as exc:  # noqa: BLE001
        raw_errors = getattr(exc, "errors", lambda: [{"error": str(exc)}])()
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=ErrorResponse(
                code="envelope_rejected",
                message="envelope failed schema validation",
                details={"errors": _jsonify_pydantic_errors(raw_errors)},
            ).model_dump(),
        ) from exc
    try:
        await bus.send(envelope)
    except KernelError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=ErrorResponse(
                code=exc.code, message=exc.message, details=exc.details
            ).model_dump(),
        ) from exc
    return SendEnvelopeResponse(envelope_id=envelope.envelope_id, routed=True)


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------

@router.get("/metrics", response_model=MetricsResponse)
async def metrics(request: Request) -> MetricsResponse:
    """Return a JSON snapshot of bus + registry metrics."""
    registry = _registry(request)
    bus = _bus(request)
    rows = await registry.list_status()
    status_counts: dict[str, int] = {}
    for r in rows:
        status_counts[r["status"]] = status_counts.get(r["status"], 0) + 1
    started = bus.metrics.started_at
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    uptime = (datetime.now(started.tzinfo) - started).total_seconds()
    return MetricsResponse(
        uptime_seconds=uptime,
        bus=bus.metrics.to_dict(),
        registry_agent_count=len(rows),
        registry_status_counts=status_counts,
        queue_total=bus.total_queued(),
    )


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------

@router.get("/audit", response_model=list[dict])
async def audit_log(
    request: Request,
    agent_id: str | None = None,
    limit: int = 100,
) -> list[dict]:
    """Return recent audit-log entries (newest first)."""
    registry = _registry(request)
    return await registry.audit_log(agent_id=agent_id, limit=min(limit, 1000))


__all__ = ["router"]
