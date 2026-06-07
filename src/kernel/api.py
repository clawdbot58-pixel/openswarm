"""REST API for the kernel.

Exposes the registry, the bus, and basic health/metrics. Designed for
the dashboard, integration tests, and ad-hoc curl debugging. Every
endpoint pulls its dependencies from ``app.state`` so the same router
can be re-mounted in different processes (tests, staging, prod).
"""
from __future__ import annotations

import logging
import uuid as _uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field

from .exceptions import AgentNotFound, EnvelopeRejected, KernelError
from .models import Envelope, Endpoint, EnvelopeMetadata, Preamble

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
# Goals & workflows (Phase 11 CLI integration)
# ---------------------------------------------------------------------------


class SubmitGoalRequest(BaseModel):
    """Body of ``POST /goals``."""

    goal: str = Field(..., min_length=1, description="Free-form natural-language goal")
    model: str | None = Field(default=None, description="Optional model override")
    workflow_id: str | None = Field(default=None, description="Optional caller-supplied id")
    async_: bool | None = Field(default=None, alias="async", description="Run async")


class SubmitGoalResponse(BaseModel):
    """Body returned by ``POST /goals``."""

    workflow_id: str
    status: str
    goal: str
    submitted_at: str


class WorkflowStatusResponse(BaseModel):
    """Body returned by ``GET /workflows/{id}``."""

    workflow_id: str
    status: str
    goal: str
    submitted_at: str
    updated_at: str
    result: dict[str, Any] | None = None
    error: str | None = None


def _workflows_store(request: Request) -> dict[str, dict[str, Any]]:
    """Return the in-memory workflows store attached to ``app.state``.

    Lazily created so the existing ``create_app`` callers don't need
    to know about it. The store is a process-local dict; Phase 12+
    can move it to SQLite.
    """
    store = getattr(request.app.state, "workflows", None)
    if store is None:
        store = {}
        request.app.state.workflows = store
    return store


@router.post("/goals", response_model=SubmitGoalResponse)
async def submit_goal(
    request: Request, body: SubmitGoalRequest
) -> SubmitGoalResponse:
    """Submit a goal to the swarm.

    The kernel itself does not orchestrate the workflow — that's
    the main agent's job. We register the workflow in an in-memory
    store, fan an intent envelope to the main agent, and return a
    ``workflow_id`` the caller can poll via ``GET /workflows/{id}``.

    The endpoint is intentionally tolerant: it always succeeds as
    long as the goal is non-empty, even if the main agent is not
    connected, so the CLI can produce a useful id for asynchronous
    monitoring.
    """
    workflow_id = body.workflow_id or str(_uuid.uuid4())
    store = _workflows_store(request)
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    store[workflow_id] = {
        "workflow_id": workflow_id,
        "status": "queued",
        "goal": body.goal,
        "submitted_at": now,
        "updated_at": now,
        "model": body.model,
        "async": bool(body.async_) if body.async_ is not None else False,
        "result": None,
        "error": None,
    }
    # Best-effort: notify the main agent via the bus. Failure here is
    # not fatal; the CLI can still poll the workflow id.
    try:
        bus = _bus(request)
        env = Envelope(
            envelope_id=str(_uuid.uuid4()),
            created_at=datetime.now(timezone.utc),
            envelope_type="intent",
            sender=Endpoint(agent_id="cli", role="external"),
            receiver=Endpoint(
                agent_id=_main_agent_id(request), role="orchestrator"
            ),
            preamble=Preamble(
                intent={"goal": "spawn_initial_swarm", "phase": "planning"},
            ),
            payload={
                "content_type": "data",
                "data": {
                    "workflow_id": workflow_id,
                    "goal": body.goal,
                    "model": body.model,
                },
            },
            metadata=EnvelopeMetadata(priority=5),
        )
        await bus.send(env)
    except Exception:  # noqa: BLE001
        logger.debug("main agent not reachable from /goals; workflow queued anyway")
        if workflow_id in store:
            store[workflow_id]["status"] = "queued_no_main_agent"
    return SubmitGoalResponse(
        workflow_id=workflow_id,
        status="queued",
        goal=body.goal,
        submitted_at=now,
    )


@router.get("/workflows/{workflow_id}", response_model=WorkflowStatusResponse)
async def get_workflow(
    request: Request, workflow_id: str
) -> WorkflowStatusResponse:
    """Return the current state of a workflow."""
    store = _workflows_store(request)
    record = store.get(workflow_id)
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=ErrorResponse(
                code="workflow_not_found",
                message=f"no workflow with id {workflow_id!r}",
                details={"workflow_id": workflow_id},
            ).model_dump(),
        )
    return WorkflowStatusResponse(
        workflow_id=record["workflow_id"],
        status=record["status"],
        goal=record["goal"],
        submitted_at=record["submitted_at"],
        updated_at=record["updated_at"],
        result=record.get("result"),
        error=record.get("error"),
    )


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


# ---------------------------------------------------------------------------
# Auth (Phase 11)
# ---------------------------------------------------------------------------

from fastapi import Depends  # noqa: E402
from .auth import AuthError, AuthMiddleware  # noqa: E402


class LoginRequest(BaseModel):
    """Body of ``POST /auth/login``."""

    user_id: str = Field(..., min_length=1, max_length=128)
    role: str = Field(default="operator")
    ttl_seconds: int | None = Field(default=None, ge=60, le=86_400)


class LoginResponse(BaseModel):
    """Body returned by ``POST /auth/login``."""

    access_token: str
    token_type: str = "Bearer"
    expires_in: int
    user_id: str
    role: str


class MeResponse(BaseModel):
    """Body returned by ``GET /auth/me``."""

    user_id: str
    role: str
    expires_at: int
    synthetic: bool


def _auth(request: Request) -> AuthMiddleware:
    auth = getattr(request.app.state, "auth", None)
    if auth is None:  # pragma: no cover — defensive
        from .auth import AuthMiddleware as _AM

        auth = _AM(enabled=False)
    return auth


async def _claims(request: Request):
    """FastAPI dependency: extract claims via the middleware."""
    auth = _auth(request)
    return await auth(request)


@router.post("/auth/login", response_model=LoginResponse)
async def login(request: Request, body: LoginRequest) -> LoginResponse:
    """Mint a JWT for a user.

    Local dev (auth disabled) mints a token unconditionally — the
    same caller identity it would have used without auth. In
    production, the caller must present a valid upstream credential
    (out of scope for Phase 11); the kernel simply translates a
    successful auth into a JWT.
    """
    auth = _auth(request)
    token = auth.issue_token(
        user_id=body.user_id,
        role=body.role,
        ttl_seconds=body.ttl_seconds,
    )
    return LoginResponse(
        access_token=token,
        token_type="Bearer",
        expires_in=body.ttl_seconds or 3600,
        user_id=body.user_id,
        role=body.role,
    )


@router.get("/auth/me", response_model=MeResponse)
async def auth_me(
    request: Request,
    claims=Depends(_claims),
) -> MeResponse:
    """Return the claims attached to the current request."""
    return MeResponse(
        user_id=claims.sub,
        role=claims.role,
        expires_at=claims.expires_at,
        synthetic=bool(claims.raw.get("synthetic")),
    )


# ---------------------------------------------------------------------------
# Billing (Phase 11)
# ---------------------------------------------------------------------------

from .billing import BillingTracker  # noqa: E402


def _billing(request: Request) -> BillingTracker:
    tracker = getattr(request.app.state, "billing", None)
    if tracker is None:
        from config import get_config

        unified = get_config()
        tracker = BillingTracker(
            unified.billing.db_path,
            default_costs=unified.billing.default_costs,
        )
        request.app.state.billing = tracker
    return tracker


class RecordUsageRequest(BaseModel):
    """Body of ``POST /billing/record``."""

    workflow_id: str = Field(..., min_length=1)
    agent_id: str = Field(..., min_length=1)
    model: str = Field(..., min_length=1)
    tokens_in: int = Field(..., ge=0)
    tokens_out: int = Field(..., ge=0)
    cost_usd: float | None = None
    notes: str | None = None
    session_id: str | None = None


class RecordUsageResponse(BaseModel):
    """Body returned by ``POST /billing/record``."""

    event_id: int
    cost_usd: float


@router.post("/billing/record", response_model=RecordUsageResponse)
async def billing_record(
    request: Request, body: RecordUsageRequest
) -> RecordUsageResponse:
    """Append a usage event. The cost is auto-estimated if absent."""
    tracker = _billing(request)
    event_id = await tracker.record(
        workflow_id=body.workflow_id,
        agent_id=body.agent_id,
        model=body.model,
        tokens_in=body.tokens_in,
        tokens_out=body.tokens_out,
        cost_usd=body.cost_usd,
        notes=body.notes,
        session_id=body.session_id,
    )
    cost = body.cost_usd
    if cost is None:
        cost = tracker._estimate_cost(  # noqa: SLF001 — internal helper
            body.model, body.tokens_in, body.tokens_out
        )
    return RecordUsageResponse(event_id=event_id, cost_usd=cost)


@router.get(
    "/billing/workflow/{workflow_id}",
    response_model=dict,
)
async def billing_workflow(
    request: Request, workflow_id: str
) -> dict:
    """Return the per-workflow cost breakdown."""
    tracker = _billing(request)
    cost = await tracker.get_workflow_cost(workflow_id)
    return cost.to_dict()


@router.get(
    "/billing/user/{user_id}/daily",
    response_model=dict,
)
async def billing_user_daily(
    request: Request,
    user_id: str,
    day: str | None = None,
) -> dict:
    """Return the daily summary for ``user_id`` (default: today)."""
    from datetime import date as _date

    tracker = _billing(request)
    target = _date.fromisoformat(day) if day else _date.today()
    summary = await tracker.get_user_daily(user_id, target)
    return summary.to_dict()


@router.get("/billing/export")
async def billing_export(request: Request) -> dict:
    """Return billing data as a CSV string under the ``csv`` key."""
    from fastapi.responses import PlainTextResponse

    tracker = _billing(request)
    csv = await tracker.export_csv()
    return PlainTextResponse(content=csv, media_type="text/csv")


__all__ = ["router"]
