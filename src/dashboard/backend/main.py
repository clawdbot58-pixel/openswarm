"""FastAPI entry point for the OpenSwarm dashboard backend.

Wiring order on startup (``lifespan``):

1. Apply the :class:`KernelSettings` and ensure directories exist.
2. Open the kernel's :class:`~kernel.registry.AgentRegistry` and
   :class:`~kernel.bus.MessageBus`.  These are shared with the kernel
   process; in a real deployment we either co-locate the dashboard
   with the kernel (in-process import) or talk over the kernel's REST
   API.  This module supports both — see :func:`create_app`.
3. Open the persistent-memory and loop-registry stores (when
   available in the current process).
4. Initialise the dashboard's :class:`ConfigAPI` (its own SQLite file).
5. Start the :class:`DataAggregator` and :class:`EventStream`.
6. Mount the REST + WebSocket routes.

The dashboard is **read-only** with respect to swarm state.  The only
state it mutates is the ``data/dashboard.db`` file owned by
:class:`ConfigAPI`.
"""
from __future__ import annotations

import asyncio
import logging
import signal
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import (
  FastAPI,
  HTTPException,
  Query,
  Request,
  WebSocket,
  status,
)
from fastapi.staticfiles import StaticFiles

from .aggregator import DataAggregator
from .cache import AggregateCache
from .config import ConfigAPI
from .introspection import IntrospectionAPI
from .models import (
    AgentDetail,
    AgentEvent,
    AgentMetrics,
    AgentSummary,
    CommitInfo,
    CycleReport,
    ErrorResponse,
    FileContent,
    FileEntry,
    HealthResponse,
    LayoutConfig,
    LayoutConfigInput,
    LeaderboardEntry,
    LogEntry,
    LoopPerformance,
    LoopTemplateSummary,
    MemoryItem,
    OptimizeRequest,
    SystemMetrics,
    TrialRecord,
    ViewConfig,
    ViewConfigInput,
    WorkflowDetail,
    WorkflowSummary,
    WorkspaceSummary,
)
from .stream import EventStream

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Tunables (env-overridable)
# ---------------------------------------------------------------------------


DEFAULT_DASHBOARD_PORT: int = 8765
DEFAULT_DB_PATH: Path = Path("data/dashboard.db")
DEFAULT_FAST_INTERVAL: float = 5.0
DEFAULT_SLOW_INTERVAL: float = 60.0
DEFAULT_STREAM_INTERVAL: float = 5.0


# ---------------------------------------------------------------------------
# Application factory
# ---------------------------------------------------------------------------


def create_dashboard_app(
    *,
    introspection: IntrospectionAPI | None = None,
    config: ConfigAPI | None = None,
    cache: AggregateCache | None = None,
    aggregator: DataAggregator | None = None,
    stream: EventStream | None = None,
    db_path: Path | None = None,
    fast_interval_seconds: float = DEFAULT_FAST_INTERVAL,
    slow_interval_seconds: float = DEFAULT_SLOW_INTERVAL,
    stream_interval_seconds: float = DEFAULT_STREAM_INTERVAL,
    enable_aggregator: bool = True,
    enable_stream: bool = True,
    trial_store: Any | None = None,
    loop_optimizer: Any | None = None,
) -> FastAPI:
    """Build a fully-wired dashboard FastAPI app.

    Every collaborator is overridable so tests can inject stubs.  When
    a collaborator is ``None`` and the corresponding feature flag is
    on, the factory constructs a real one (using the kernel
    subsystems already mounted on ``app.state.kernel`` by the caller).
    """
    cache = cache or AggregateCache()
    config = config or ConfigAPI(db_path or DEFAULT_DB_PATH)
    introspection = introspection  # may be None; the routes handle it

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # ---- startup ---------------------------------------------------
        await config.initialize()
        # Build subsystems that depend on introspection only if we
        # have a wired-up introspection layer.  Tests that pass a stub
        # introspection still get a working aggregator + stream.
        if introspection is not None and enable_aggregator and aggregator is None:
            agg = DataAggregator(
                introspection=introspection,
                cache=cache,
                fast_interval_seconds=fast_interval_seconds,
                slow_interval_seconds=slow_interval_seconds,
            )
            await agg.start()
        else:
            agg = aggregator
        if introspection is not None and enable_stream and stream is None:
            es = EventStream(
                introspection=introspection,
                heartbeat_interval_seconds=stream_interval_seconds,
            )
            # If the bus is available on app.state, attach.
            bus = getattr(app.state, "bus", None)
            if bus is not None:
                await es.attach(bus)
            await es.start()
        else:
            es = stream
            # When a stream was injected but enable_stream=False, it may
            # not have been attached to the bus yet (common in tests that
            # build the stream outside the factory).  Attach and start it
            # now so the WebSocket handler is functional.
            if es is not None and introspection is not None:
                bus = getattr(app.state, "bus", None)
                if bus is not None and not getattr(es, "_listener_registered", False):
                    await es.attach(bus)
                if not getattr(es, "_started", False):
                    await es.start()

        app.state.cache = cache
        app.state.config = config
        app.state.introspection = introspection
        app.state.aggregator = agg
        app.state.stream = es
        app.state.started_at = datetime.now(timezone.utc)

        # Signal handlers (best-effort; uvicorn also installs them).
        try:
            loop = asyncio.get_running_loop()
            for sig in (signal.SIGINT, signal.SIGTERM):
                try:
                    loop.add_signal_handler(sig, _signal_handler, app)
                except (NotImplementedError, RuntimeError):
                    pass
        except RuntimeError:
            pass

        try:
            yield
        finally:
            # ---- shutdown -----------------------------------------------
            if es is not None:
                await es.stop()
            if agg is not None:
                await agg.stop()
            await config.close()
            for attr in (
                "stream",
                "aggregator",
                "introspection",
                "config",
                "cache",
                "started_at",
            ):
                if hasattr(app.state, attr):
                    try:
                        delattr(app.state, attr)
                    except Exception:
                        pass

    app = FastAPI(
        title="OpenSwarm Dashboard Backend",
        version="0.1.0",
        description=(
            "Phase 7 dashboard backend: read-only system introspection, "
            "WebSocket event stream, and view/layout configuration storage."
        ),
        lifespan=lifespan,
    )

    # ---- health --------------------------------------------------------
    @app.get("/health", response_model=HealthResponse)
    async def health(request: Request) -> HealthResponse:
        cfg = request.app.state.config
        db_ok = True
        try:
            cfg._require_db()  # type: ignore[attr-defined]
        except Exception:
            db_ok = False
        started = getattr(request.app.state, "started_at", None) or datetime.now(timezone.utc)
        uptime = (datetime.now(timezone.utc) - started).total_seconds()
        return HealthResponse(
            status="ok",
            db_ok=db_ok,
            kernel_reachable=request.app.state.introspection is not None,
            started_at=started,
            uptime_seconds=uptime,
        )

    @app.get("/", include_in_schema=False)
    async def root() -> dict[str, str]:
        return {
            "service": "openswarm-dashboard-backend",
            "phase": "7",
            "ui": "/ui/",
            "docs": "/docs",
        }

    # ---- introspection ------------------------------------------------
    @app.get("/api/agents", response_model=list[AgentSummary])
    async def list_agents(
        request: Request,
        status_filter: str | None = Query(None, alias="status"),
        role: str | None = None,
        category: str | None = None,
    ) -> list[AgentSummary]:
        intro = _require_introspection(request)
        return await intro.get_agents(status=status_filter, role=role, category=category)

    @app.get("/api/agents/{agent_id}", response_model=AgentDetail)
    async def get_agent(request: Request, agent_id: str) -> AgentDetail:
        intro = _require_introspection(request)
        try:
            return await intro.get_agent_detail(agent_id)
        except Exception as exc:  # AgentNotFound
            raise _not_found("agent_not_found", str(exc), agent_id=agent_id) from exc

    @app.get("/api/agents/{agent_id}/history", response_model=list[AgentEvent])
    async def get_agent_history(
        request: Request,
        agent_id: str,
        limit: int = 50,
    ) -> list[AgentEvent]:
        intro = _require_introspection(request)
        return await intro.get_agent_history(agent_id, limit=limit)

    @app.get("/api/agents/{agent_id}/metrics", response_model=AgentMetrics)
    async def get_agent_metrics(request: Request, agent_id: str) -> AgentMetrics:
        intro = _require_introspection(request)
        return await intro.get_agent_metrics(agent_id)

    # ---- workflows -----------------------------------------------------
    @app.get("/api/workflows", response_model=list[WorkflowSummary])
    async def list_workflows(
        request: Request,
        status_filter: str | None = Query(None, alias="status"),
        owner: str | None = None,
    ) -> list[WorkflowSummary]:
        intro = _require_introspection(request)
        return await intro.get_workflows(status=status_filter, owner=owner)

    @app.get("/api/workflows/{workflow_id}", response_model=WorkflowDetail)
    async def get_workflow(request: Request, workflow_id: str) -> WorkflowDetail:
        intro = _require_introspection(request)
        try:
            return await intro.get_workflow_detail(workflow_id)
        except FileNotFoundError as exc:
            raise _not_found("workflow_not_found", str(exc), workflow_id=workflow_id) from exc

    @app.get("/api/workflows/{workflow_id}/logs", response_model=list[LogEntry])
    async def get_workflow_logs(
        request: Request,
        workflow_id: str,
        limit: int = 100,
    ) -> list[LogEntry]:
        intro = _require_introspection(request)
        return await intro.get_workflow_logs(workflow_id, limit=limit)

    # ---- logs ----------------------------------------------------------
    @app.get("/api/logs", response_model=list[LogEntry])
    async def list_logs(
        request: Request,
        agent_id: str | None = None,
        workflow_id: str | None = None,
        envelope_type: str | None = None,
        severity: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[LogEntry]:
        intro = _require_introspection(request)
        return await intro.get_logs(
            agent_id=agent_id,
            workflow_id=workflow_id,
            envelope_type=envelope_type,
            severity=severity,
            limit=limit,
            offset=offset,
        )

    # ---- workspaces ----------------------------------------------------
    @app.get("/api/workspaces", response_model=list[WorkspaceSummary])
    async def list_workspaces(request: Request) -> list[WorkspaceSummary]:
        intro = _require_introspection(request)
        return await intro.get_workspaces()

    @app.get(
        "/api/workspaces/{workflow_id}/files",
        response_model=list[FileEntry],
    )
    async def list_workspace_files(
        request: Request,
        workflow_id: str,
        path: str = "/",
    ) -> list[FileEntry]:
        intro = _require_introspection(request)
        return await intro.get_workspace_files(workflow_id, path=path)

    @app.get(
        "/api/workspaces/{workflow_id}/file",
        response_model=FileContent,
    )
    async def get_workspace_file(
        request: Request,
        workflow_id: str,
        path: str,
    ) -> FileContent:
        intro = _require_introspection(request)
        try:
            return await intro.get_workspace_file(workflow_id, path)
        except FileNotFoundError as exc:
            raise _not_found("file_not_found", str(exc), workflow_id=workflow_id, path=path) from exc

    @app.get(
        "/api/workspaces/{workflow_id}/diff",
        response_model=str,
    )
    async def get_workspace_diff(
        request: Request,
        workflow_id: str,
        commit: str,
    ) -> str:
        intro = _require_introspection(request)
        try:
            return await intro.get_workspace_diff(workflow_id, commit)
        except (FileNotFoundError, ValueError) as exc:
            raise _not_found("diff_unavailable", str(exc), workflow_id=workflow_id) from exc

    @app.get(
        "/api/workspaces/{workflow_id}/history",
        response_model=list[CommitInfo],
    )
    async def get_workspace_history(
        request: Request,
        workflow_id: str,
    ) -> list[CommitInfo]:
        intro = _require_introspection(request)
        return await intro.get_workspace_history(workflow_id)

    # ---- loops ---------------------------------------------------------
    @app.get("/api/loops", response_model=list[LoopTemplateSummary])
    async def list_loops(
        request: Request,
        task_type: str | None = None,
        min_success_rate: float = 0.0,
    ) -> list[LoopTemplateSummary]:
        intro = _require_introspection(request)
        return await intro.get_loop_templates(
            task_type=task_type,
            min_success_rate=min_success_rate,
        )

    @app.get("/api/loops/{template_id}/performance", response_model=LoopPerformance)
    async def get_loop_performance(
        request: Request,
        template_id: str,
    ) -> LoopPerformance:
        intro = _require_introspection(request)
        try:
            return await intro.get_loop_performance(template_id)
        except FileNotFoundError as exc:
            raise _not_found("loop_not_found", str(exc), template_id=template_id) from exc

    # ---- loops (Phase 10 trial/error cycle) ----------------------------
    @app.get("/api/loops/leaderboard", response_model=list[LeaderboardEntry])
    async def get_trial_leaderboard(
        request: Request,
        task_type: str | None = Query(None, description="Task type filter"),
        sort_by: str = Query("score", description="score, cost, speed, trials"),
        min_trials: int = Query(3, ge=1, le=100),
    ) -> list[LeaderboardEntry]:
        """Return the trial/error leaderboard aggregated by loop_id."""
        intro = _require_introspection(request)
        return await intro.get_trial_leaderboard(
            task_type=task_type, sort_by=sort_by, min_trials=min_trials
        )

    @app.get("/api/loops/leaderboard/{task_type}", response_model=list[LeaderboardEntry])
    async def get_trial_leaderboard_by_task(
        request: Request,
        task_type: str,
        sort_by: str = Query("score", description="score, cost, speed, trials"),
        min_trials: int = Query(3, ge=1, le=100),
    ) -> list[LeaderboardEntry]:
        """Convenience route — same as ``/api/loops/leaderboard?task_type=...``."""
        intro = _require_introspection(request)
        return await intro.get_trial_leaderboard(
            task_type=task_type, sort_by=sort_by, min_trials=min_trials
        )

    @app.get("/api/loops/{loop_id}/trials", response_model=list[TrialRecord])
    async def get_loop_trials(
        request: Request,
        loop_id: str,
        task_type: str | None = Query(None, description="Optional task-type filter"),
        limit: int = Query(50, ge=1, le=500),
    ) -> list[TrialRecord]:
        """Return the immutable trial records for ``loop_id``, newest first."""
        intro = _require_introspection(request)
        return await intro.get_loop_trials(
            loop_id=loop_id, task_type=task_type, limit=limit
        )

    @app.post("/api/loops/optimize", response_model=CycleReport)
    async def run_optimization(
        request: Request,
        body: OptimizeRequest,
    ) -> CycleReport:
        """Run one trial/error cycle and return the cycle report.

        The request body specifies the task type, an optional task
        sample, the number of trials, the base loop to mutate, and
        whether to include the unmodified premade loop in the cycle.
        """
        intro = _require_introspection(request)
        try:
            return await intro.run_optimization(
                task_type=body.task_type,
                task_sample=body.task_sample,
                n_trials=body.n_trials,
                base_loop=body.base_loop or "reflection",
                include_builtins=body.include_builtins,
            )
        except RuntimeError as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=ErrorResponse(
                    code="optimizer_unavailable", message=str(exc)
                ).model_dump(),
            ) from exc

    # ---- memory --------------------------------------------------------
    @app.get("/api/memory/{agent_id}", response_model=list[MemoryItem])
    async def get_agent_memory(
        request: Request,
        agent_id: str,
        type: str | None = None,
        workflow_id: str | None = None,
        query: str | None = None,
        limit: int = 20,
    ) -> list[MemoryItem]:
        intro = _require_introspection(request)
        return await intro.get_agent_memory(
            agent_id=agent_id,
            type=type,
            workflow_id=workflow_id,
            query=query,
            limit=limit,
        )

    # ---- metrics -------------------------------------------------------
    @app.get("/api/metrics", response_model=SystemMetrics)
    async def get_metrics(request: Request) -> SystemMetrics:
        intro = _require_introspection(request)
        return await intro.get_system_metrics()

    @app.get("/api/metrics/cached", response_model=SystemMetrics)
    async def get_cached_metrics(request: Request) -> SystemMetrics:
        """Return the aggregator's last-computed metrics snapshot, or 503.

        The endpoint is "best effort" — if the aggregator hasn't run
        yet, we surface a 503 so the frontend can fall back to the
        live ``/api/metrics`` endpoint.
        """
        cache = request.app.state.cache
        cached = await cache.get("system_metrics")
        if cached is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=ErrorResponse(
                    code="cache_empty",
                    message="aggregator has not produced a snapshot yet",
                ).model_dump(),
            )
        return SystemMetrics.model_validate(cached)

    # ---- configuration (views & layouts) -------------------------------
    @app.get("/api/views", response_model=list[ViewConfig])
    async def list_views(request: Request) -> list[ViewConfig]:
        cfg = request.app.state.config
        return await cfg.get_views()

    @app.post("/api/views", response_model=ViewConfig, status_code=status.HTTP_201_CREATED)
    async def create_view(request: Request, body: ViewConfigInput) -> ViewConfig:
        cfg = request.app.state.config
        view_id = await cfg.save_view(body)
        return await cfg.get_view(view_id)

    @app.get("/api/views/{view_id}", response_model=ViewConfig)
    async def get_view(request: Request, view_id: str) -> ViewConfig:
        cfg = request.app.state.config
        try:
            return await cfg.get_view(view_id)
        except KeyError as exc:
            raise _not_found("view_not_found", str(exc), view_id=view_id) from exc

    @app.put("/api/views/{view_id}", response_model=ViewConfig)
    async def update_view(
        request: Request, view_id: str, body: ViewConfigInput
    ) -> ViewConfig:
        cfg = request.app.state.config
        # Validate the id exists; KeyError → 404.
        try:
            existing = await cfg.get_view(view_id)
        except KeyError as exc:
            raise _not_found("view_not_found", str(exc), view_id=view_id) from exc
        # Build a new ViewConfig with the same id, updated content.
        from datetime import datetime

        merged = ViewConfig(
            view_id=existing.view_id,
            name=body.name,
            description=body.description,
            view_type=body.view_type,
            data_sources=body.data_sources,
            filters=body.filters,
            refresh_interval_ms=body.refresh_interval_ms,
            created_by=body.created_by or existing.created_by,
            created_at=existing.created_at,
            updated_at=datetime.now(timezone.utc),
        )
        await cfg.save_view(merged)
        return await cfg.get_view(view_id)

    @app.delete("/api/views/{view_id}", status_code=status.HTTP_204_NO_CONTENT)
    async def delete_view(request: Request, view_id: str) -> None:
        cfg = request.app.state.config
        try:
            await cfg.delete_view(view_id)
        except KeyError as exc:
            raise _not_found("view_not_found", str(exc), view_id=view_id) from exc

    @app.get("/api/layouts", response_model=list[LayoutConfig])
    async def list_layouts(request: Request) -> list[LayoutConfig]:
        cfg = request.app.state.config
        return await cfg.list_layouts()

    @app.post(
        "/api/layouts",
        response_model=LayoutConfig,
        status_code=status.HTTP_201_CREATED,
    )
    async def create_layout(
        request: Request, body: LayoutConfigInput
    ) -> LayoutConfig:
        cfg = request.app.state.config
        layout_id = await cfg.save_layout(body)
        return await cfg.get_layout(layout_id)

    @app.get("/api/layouts/{layout_id}", response_model=LayoutConfig)
    async def get_layout(request: Request, layout_id: str) -> LayoutConfig:
        cfg = request.app.state.config
        try:
            return await cfg.get_layout(layout_id)
        except KeyError as exc:
            raise _not_found("layout_not_found", str(exc), layout_id=layout_id) from exc

    @app.put("/api/layouts/{layout_id}", response_model=LayoutConfig)
    async def update_layout(
        request: Request, layout_id: str, body: LayoutConfigInput
    ) -> LayoutConfig:
        cfg = request.app.state.config
        try:
            existing = await cfg.get_layout(layout_id)
        except KeyError as exc:
            raise _not_found("layout_not_found", str(exc), layout_id=layout_id) from exc
        from datetime import datetime
        merged = LayoutConfig(
            layout_id=existing.layout_id,
            name=body.name,
            description=body.description,
            layout_type=body.layout_type,
            panels=body.panels,
            grid=body.grid,
            theme=body.theme,
            created_by=body.created_by or existing.created_by,
            created_at=existing.created_at,
            updated_at=datetime.now(timezone.utc),
        )
        await cfg.save_layout(merged)
        return await cfg.get_layout(layout_id)

    @app.delete("/api/layouts/{layout_id}", status_code=status.HTTP_204_NO_CONTENT)
    async def delete_layout(request: Request, layout_id: str) -> None:
        cfg = request.app.state.config
        try:
            await cfg.delete_layout(layout_id)
        except KeyError as exc:
            raise _not_found("layout_not_found", str(exc), layout_id=layout_id) from exc

    # ---- websocket stream ---------------------------------------------
    @app.websocket("/stream")
    async def stream_endpoint(websocket: WebSocket) -> None:
        """Fan kernel events out to the connected client.

        Query parameters:

        * ``subscribe=foo,bar`` — only forward events whose
          ``envelope_type`` (or kernel sub-event name) is in the set.
        """
        await websocket.accept()
        # Stash the introspection/state we need locally so we can
        # tolerate shutdown races.
        es = getattr(websocket.app.state, "stream", None)
        if es is None:
            await websocket.close(
                code=status.WS_1011_INTERNAL_ERROR,
                reason="stream not initialized",
            )
            return
        # FastAPI turns ``?subscribe=a&subscribe=b`` into a list.
        raw_sub = websocket.query_params.get("subscribe")
        subscribe: list[str] | None = None
        if raw_sub is not None:
            subscribe = [s.strip() for s in raw_sub.split(",") if s.strip()]
        await es.add_client(websocket, subscribe=subscribe, _accept=False)

    # ---- static UI ----------------------------------------------------
    static_dir = Path(__file__).parent / "static"
    if static_dir.is_dir():
        app.mount("/ui", StaticFiles(directory=str(static_dir), html=True), name="dashboard-ui")

    return app


def _require_introspection(request: Request) -> IntrospectionAPI:
    intro = getattr(request.app.state, "introspection", None)
    if intro is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=ErrorResponse(
                code="introspection_unavailable",
                message=(
                    "dashboard backend is not wired to a kernel/registry; "
                    "pass an IntrospectionAPI when constructing the app"
                ),
            ).model_dump(),
        )
    return intro


def _not_found(code: str, message: str, **details: Any) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail=ErrorResponse(
            code=code, message=message, details=dict(details)
        ).model_dump(),
    )


def _signal_handler(app: FastAPI) -> None:
    """Trigger a clean shutdown when the process is signalled."""
    logger.info("dashboard received signal; shutting down")
    raise SystemExit(0)


# ---------------------------------------------------------------------------
# Module-level app
# ---------------------------------------------------------------------------


def _build_default_app() -> FastAPI:
    """Production app: HTTP introspection when ``KERNEL_REST_URL`` is set."""
    import os
    from pathlib import Path

    kernel_url = os.environ.get("KERNEL_REST_URL", "").strip()
    if kernel_url:
        from .http_introspection import HttpIntrospectionAPI

        project_root = Path(
            os.environ.get("OPENSWARM_PROJECT_ROOT", Path.cwd())
        ).resolve()
        agent_ws = project_root / "workspaces" / "agent"
        harness_dir = Path(
            os.environ.get("OPENSWARM_HARNESS_DIR", project_root / "data" / "workspaces")
        )
        intro = HttpIntrospectionAPI(
            kernel_url,
            workspaces_dir=agent_ws if agent_ws.is_dir() else None,
            harness_dir=harness_dir,
        )
        return create_dashboard_app(
            introspection=intro,
            enable_aggregator=True,
            enable_stream=True,
        )
    return create_dashboard_app(enable_aggregator=False, enable_stream=False)


# A module-level app is convenient for `uvicorn dashboard.backend.main:app`.
# Tests build their own app via :func:`create_dashboard_app`.
app: FastAPI = _build_default_app()


# ---------------------------------------------------------------------------
# Kernel-co-located entry point
# ---------------------------------------------------------------------------


def build_introspection_for_kernel(
    *,
    registry: Any,
    bus: Any,
    settings: Any,
    heartbeat: Any = None,
    persistent_memory: Any = None,
    loop_registry: Any = None,
    workspaces_dir: Path | None = None,
    trial_store: Any = None,
    loop_optimizer: Any = None,
) -> IntrospectionAPI:
    """Build an :class:`IntrospectionAPI` against a live kernel.

    Convenience used by ``demos/phase_7_demo.py`` and by anyone who
    wants to run the dashboard backend in-process with the kernel.
    """
    return IntrospectionAPI(
        registry=registry,
        bus=bus,
        settings=settings,
        heartbeat=heartbeat,
        persistent_memory=persistent_memory,
        loop_registry=loop_registry,
        workspaces_dir=workspaces_dir,
        trial_store=trial_store,
        loop_optimizer=loop_optimizer,
    )


__all__ = [
    "DEFAULT_DASHBOARD_PORT",
    "DEFAULT_DB_PATH",
    "DEFAULT_FAST_INTERVAL",
    "DEFAULT_SLOW_INTERVAL",
    "DEFAULT_STREAM_INTERVAL",
    "app",
    "build_introspection_for_kernel",
    "create_dashboard_app",
]
