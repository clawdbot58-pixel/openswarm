"""Kernel configuration settings.

Centralized, environment-overridable settings for every kernel subsystem.
Built on Pydantic v2 BaseSettings so values can be loaded from environment
variables (prefixed with ``OPENSWARM_``) or a local ``.env`` file without
changing call sites.
"""
from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


PROJECT_ROOT: Path = Path(__file__).resolve().parent.parent.parent
"""Filesystem root of the OpenSwarm project."""


class _Paths(BaseModel):
    """Resolved filesystem paths used by the kernel."""

    data_dir: Path = PROJECT_ROOT / "data"
    heartbeats_dir: Path = PROJECT_ROOT / "heartbeats"
    contracts_dir: Path = PROJECT_ROOT / "src" / "contracts"

    def ensure(self) -> None:
        """Create directories that must exist on disk."""
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.heartbeats_dir.mkdir(parents=True, exist_ok=True)


class KernelSettings(BaseSettings):
    """Runtime configuration for the OpenSwarm kernel.

    All fields can be overridden by environment variables with the
    ``OPENSWARM_`` prefix, e.g. ``OPENSWARM_DB_PATH=/var/lib/openswarm/registry.db``.
    """

    model_config = SettingsConfigDict(
        env_prefix="OPENSWARM_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    host: str = "127.0.0.1"
    port: int = 8765
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"

    db_path: Path = PROJECT_ROOT / "data" / "registry.db"
    """SQLite file backing the agent registry and audit log."""

    heartbeat_interval_seconds: float = 10.0
    heartbeat_zombie_threshold_seconds: float = 20.0

    bus_max_queue_size: int = 1000
    """Per-agent in-memory queue cap. Over it, the bus drops the oldest and
    emits a ``queue_overflow`` event to the main agent."""

    bus_router_poll_interval_seconds: float = 0.01
    """How often the background router task wakes to drain the heap."""

    metrics_max_history: int = 1024
    """Cap on in-memory counters the kernel keeps for ``GET /metrics``."""

    main_agent_id: str = "main-agent"
    """agent_id of the orchestrator. Kernel routes system events here."""

    @property
    def paths(self) -> _Paths:
        """Return a cached ``_Paths`` view derived from ``db_path``."""
        return _Paths(
            data_dir=self.db_path.parent,
            heartbeats_dir=PROJECT_ROOT / "heartbeats",
            contracts_dir=PROJECT_ROOT / "src" / "contracts",
        )


_settings: KernelSettings | None = None


def get_settings() -> KernelSettings:
    """Return a process-wide singleton :class:`KernelSettings` instance."""
    global _settings
    if _settings is None:
        _settings = KernelSettings()
        _settings.paths.ensure()
    return _settings


def reset_settings_for_tests(**overrides: object) -> KernelSettings:
    """Rebuild the settings singleton with overrides.

    Tests use this to point the kernel at a temporary SQLite file and a
    temporary heartbeats directory without polluting the real filesystem.
    """
    global _settings
    base = KernelSettings()
    merged = base.model_copy(update=overrides)
    _settings = merged
    _settings.paths.ensure()
    return _settings


__all__ = [
    "KernelSettings",
    "PROJECT_ROOT",
    "get_settings",
    "reset_settings_for_tests",
]
