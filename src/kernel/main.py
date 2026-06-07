"""FastAPI entry point for the OpenSwarm kernel.

Wiring order on startup (``lifespan``):

1. Apply :class:`KernelSettings`, ensure data directories exist.
2. Open the :class:`AgentRegistry` (SQLite) and apply schema.
3. Construct the :class:`PermissionEnforcer`.
4. Construct the :class:`MessageBus` and start its router task.
5. Construct the :class:`HeartbeatMonitor` and start its polling task.
6. Mount the REST and WebSocket routers.

On shutdown the lifespan reverses the order, closes WebSockets, drains
queues, and stops background tasks before closing the DB.

Running locally::

    uvicorn kernel.main:app --host 0.0.0.0 --port 8765
"""
from __future__ import annotations

import asyncio
import logging
import signal
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI

from . import api, websocket
from .bus import MessageBus
from .config import KernelSettings, get_settings
from .heartbeat import HeartbeatMonitor
from .permissions import PermissionEnforcer
from .registry import AgentRegistry


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Application factory
# ---------------------------------------------------------------------------

def _configure_logging(settings: KernelSettings) -> None:
    """Configure root logging. Idempotent — safe to call from tests."""
    level = getattr(logging, settings.log_level.upper(), logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        force=True,
    )


def create_app(settings: KernelSettings | None = None) -> FastAPI:
    """Build a fully-wired FastAPI app.

    Tests can pass a :class:`KernelSettings` with overrides (temporary DB
    path, fast heartbeat interval, etc.) and get a fully isolated app.
    """
    settings = settings or get_settings()
    _configure_logging(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # ---- startup ---------------------------------------------------
        logger.info("kernel starting host=%s port=%s", settings.host, settings.port)
        registry = AgentRegistry(settings.db_path)
        await registry.initialize()
        permissions = PermissionEnforcer(registry)
        bus = MessageBus(registry, permissions, settings)
        # Plug the bus into the registry so the permission enforcer can
        # emit kernel events through it.
        registry._bus = bus  # type: ignore[attr-defined]
        await bus.start()
        heartbeat = HeartbeatMonitor(registry, bus, settings)
        await heartbeat.start()

        app.state.settings = settings
        app.state.registry = registry
        app.state.permissions = permissions
        app.state.bus = bus
        app.state.heartbeat = heartbeat

        # Phase 11: opt-in auth. Local dev (the default) leaves this
        # disabled; setting OPENSWARM_AUTH__ENABLED=true wires the
        # middleware into every FastAPI dependency that opts in.
        from .auth import make_middleware_from_config
        from config import get_config

        unified = get_config()
        app.state.auth = make_middleware_from_config(
            enabled=unified.auth.enabled,
            secret=unified.auth.jwt_secret,
            algorithm=unified.auth.jwt_algorithm,
            ttl_seconds=unified.auth.jwt_ttl_seconds,
        )

        # Best-effort: install a signal handler so SIGTERM triggers a
        # graceful shutdown. Uvicorn normally handles this, but tests
        # that drive the app directly may need the hook.
        try:
            loop = asyncio.get_running_loop()
            for sig in (signal.SIGINT, signal.SIGTERM):
                try:
                    loop.add_signal_handler(sig, _signal_handler, app)
                except (NotImplementedError, RuntimeError):
                    # add_signal_handler is unavailable on Windows / inside
                    # non-main threads. We don't care; uvicorn handles it.
                    pass
        except RuntimeError:
            pass

        try:
            yield
        finally:
            # ---- shutdown -----------------------------------------------
            logger.info("kernel shutting down")
            # Stop accepting new envelopes first.
            await heartbeat.stop()
            await bus.stop()
            await registry.close()
            # Best-effort close of any stragglers.
            for attr in ("heartbeat", "bus", "permissions", "registry", "settings"):
                if hasattr(app.state, attr):
                    try:
                        delattr(app.state, attr)
                    except Exception:  # noqa: BLE001
                        pass
            logger.info("kernel shutdown complete")

    app = FastAPI(
        title="OpenSwarm Kernel",
        version="0.1.0",
        description=(
            "Phase 1 control plane: message bus, agent registry, "
            "permission enforcer, heartbeat monitor."
        ),
        lifespan=lifespan,
    )

    # REST + WebSocket routers.
    app.include_router(api.router)
    app.include_router(websocket.router)

    @app.get("/", include_in_schema=False)
    async def root() -> dict[str, str]:
        return {
            "service": "openswarm-kernel",
            "phase": "1",
            "docs": "/docs",
        }

    return app


def _signal_handler(app: FastAPI) -> None:
    """Trigger a clean shutdown when the process is signalled."""
    logger.info("received signal; shutting down")
    # Uvicorn owns the actual loop teardown; this just nudges logs.
    raise SystemExit(0)


# ---------------------------------------------------------------------------
# Module-level app instance for ``uvicorn kernel.main:app``
# ---------------------------------------------------------------------------

app: FastAPI = create_app()


__all__ = ["app", "create_app"]
