"""CLI-only data structures.

Kept separate from :mod:`config` so the CLI can import them without
loading the full Pydantic-settings machinery (Click subcommands
sometimes need a tiny, dependency-free value type).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path


class ProcessKind(str, Enum):
    """Names the :class:`ProcessManager` uses to label each child."""

    KERNEL = "kernel"
    DASHBOARD = "dashboard"
    WORKER = "worker"
    MAIN_AGENT = "main-agent"
    TELEGRAM = "telegram"


@dataclass(slots=True)
class StartupConfig:
    """User-facing start options. Mirrors the CLI flags."""

    kernel: bool = True
    dashboard: bool = True
    workers: list[str] = field(default_factory=list)
    telegram: bool = False
    port: int = 8000
    kernel_port: int = 8765
    config_path: Path | None = None
    log_dir: Path | None = None
    data_dir: Path | None = None
    detach: bool = True

    def describe(self) -> dict[str, object]:
        """Return a JSON-safe description for telemetry / debug output."""
        return {
            "kernel": self.kernel,
            "dashboard": self.dashboard,
            "workers": list(self.workers),
            "telegram": self.telegram,
            "port": self.port,
            "kernel_port": self.kernel_port,
            "detach": self.detach,
        }


@dataclass(slots=True)
class ProcessInfo:
    """A child process the :class:`ProcessManager` started."""

    kind: ProcessKind
    label: str
    pid: int | None
    cmd: list[str]
    log_path: Path | None
    started_at: float
    extra: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class SwarmStatus:
    """Aggregate state of all child processes."""

    kernel_running: bool
    kernel_pid: int | None
    kernel_url: str | None
    dashboard_running: bool
    dashboard_pid: int | None
    dashboard_url: str | None
    workers_running: int
    workers_total: int
    workers: list[ProcessInfo]
    main_agent_running: bool
    main_agent_pid: int | None
    telegram_running: bool
    telegram_pid: int | None
    agents_registered: int
    workflows_active: int


__all__ = [
    "ProcessInfo",
    "ProcessKind",
    "StartupConfig",
    "SwarmStatus",
]
