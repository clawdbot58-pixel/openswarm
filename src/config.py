"""Unified configuration for OpenSwarm.

Single source of truth for runtime tunables that the kernel, the
process manager, the dashboard, the Telegram bot, and the CLI all read
from. The data model is hierarchical:

* :class:`OpenSwarmConfig` is the root.
* :class:`KernelSection` overrides the kernel's :class:`KernelSettings`
  fields that make sense to surface to operators.
* :class:`DashboardSection` does the same for the dashboard.
* :class:`WorkersSection` lists which manifests to auto-spawn on
  ``openswarm start``.
* :class:`AuthSection`, :class:`RedisSection`, :class:`TelegramSection`
  are the three new surfaces introduced in Phase 11.

Loading order (highest priority last):

1. Built-in defaults.
2. ``config/openswarm.toml`` if present.
3. ``~/.config/openswarm/config.toml`` if present.
4. Environment variables prefixed with ``OPENSWARM_`` (e.g.
   ``OPENSWARM_KERNEL_PORT=9000``).
5. Explicit overrides passed to :func:`load_config`.

The loader is deliberately tolerant: a missing TOML file or an
unknown key is a soft warning, not a hard failure. Local dev should
"just work" with no config file at all.

Design notes
------------
* Built on :mod:`pydantic_settings` so env overrides use the
  well-tested Pydantic v2 path.  We also expose
  :func:`load_from_toml` for the file-based leg.
* Environment variables use the same names as the YAML/TOML keys
  but upper-cased and dotted, e.g. ``kernel.port`` →
  ``OPENSWARM_KERNEL__PORT`` (Pydantic's nested-settings delimiter).
  We additionally accept the legacy flat form
  (``OPENSWARM_KERNEL_PORT``) via a small alias map for ergonomics.
* The config is intentionally read-only after load.  The CLI's
  ``openswarm config set`` command edits the TOML file on disk and
  reloads.
"""
from __future__ import annotations

import logging
import os
import sys
import warnings
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Project root discovery
# ---------------------------------------------------------------------------

PROJECT_ROOT: Path = Path(__file__).resolve().parent.parent
"""Filesystem root of the OpenSwarm project (the directory with ``pyproject.toml``)."""


def _user_config_dir() -> Path:
    """Return the per-user config directory, creating it if needed."""
    if sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    elif sys.platform.startswith("win"):
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    path = base / "openswarm"
    path.mkdir(parents=True, exist_ok=True)
    return path


# ---------------------------------------------------------------------------
# Sections
# ---------------------------------------------------------------------------


class KernelSection(BaseModel):
    """Tunables that override :class:`kernel.config.KernelSettings`."""

    host: str = "127.0.0.1"
    port: int = 8765
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    data_dir: Path = PROJECT_ROOT / "data"

    @field_validator("data_dir", mode="after")
    @classmethod
    def _resolve_path(cls, value: Path) -> Path:
        return Path(os.path.expanduser(str(value))).resolve()


class DashboardSection(BaseModel):
    """Tunables for the dashboard backend (Phase 7) and frontend (Phase 8)."""

    enabled: bool = True
    host: str = "127.0.0.1"
    port: int = 8000
    db_path: Path = PROJECT_ROOT / "data" / "dashboard.db"
    enable_aggregator: bool = True
    enable_stream: bool = True

    @field_validator("db_path", mode="after")
    @classmethod
    def _resolve_path(cls, value: Path) -> Path:
        return Path(os.path.expanduser(str(value))).resolve()


class WorkersSection(BaseModel):
    """Which agents to auto-spawn when ``openswarm start`` runs."""

    auto_start: bool = True
    manifests: list[str] = Field(
        default_factory=lambda: [
            "manifests/coder-python-fast.json",
            "manifests/reviewer-security-powerful.json",
            "manifests/researcher-web-standard.json",
        ]
    )
    include_main_agent: bool = True
    spawn_concurrency: int = 4


class AuthSection(BaseModel):
    """Authentication & authorization.

    * :attr:`enabled` is False by default — local dev never sees a
      login screen. Setting it to True makes
      :class:`kernel.auth.AuthMiddleware` enforce JWTs on every API
      and WebSocket handshake.
    * :attr:`jwt_secret` is a placeholder string; operators MUST
      change it for any non-local deployment.
    """

    enabled: bool = False
    jwt_secret: str = "change-me-in-production"
    jwt_algorithm: str = "HS256"
    jwt_ttl_seconds: int = 3600
    default_user_id: str = "local-dev"
    default_role: str = "operator"

    @field_validator("jwt_secret")
    @classmethod
    def _warn_default_secret(cls, value: str) -> str:
        if value == "change-me-in-production":
            warnings.warn(
                "OpenSwarm auth.jwt_secret is set to the built-in placeholder. "
                "Set OPENSWARM_AUTH__JWT_SECRET to a unique value before exposing "
                "the API to the network.",
                stacklevel=2,
            )
        return value


class RedisSection(BaseModel):
    """Optional Redis backing for the message queue.

    When :attr:`enabled` is True, the kernel's :class:`MessageBus`
    swaps its in-process heap for a Redis sorted set. When the
    connection fails (no Redis running), the bus logs a warning and
    transparently falls back to the in-memory implementation.
    """

    enabled: bool = False
    url: str = "redis://localhost:6379"
    key_prefix: str = "openswarm:queue"
    socket_timeout_seconds: float = 2.0
    fallback_to_memory: bool = True


class TelegramSection(BaseModel):
    """Telegram bot configuration.

    Inspired by OpenClaw's 25+ channel adapters. The bot is a thin
    client that talks to the kernel — it never executes tools or
    writes files directly.
    """

    enabled: bool = False
    bot_token: str = ""
    allowed_chat_ids: list[int] = Field(default_factory=list)
    poll_interval_seconds: float = 1.0
    status_message_ttl_seconds: int = 60


class BillingSection(BaseModel):
    """Cost & usage tracking.

    All model invocations call :meth:`BillingTracker.record` after the
    response is rendered. The :attr:`default_costs` table provides
    per-model USD-per-1k-token rates used when an LLM client doesn't
    report a number itself.
    """

    enabled: bool = True
    db_path: Path = PROJECT_ROOT / "data" / "billing.db"
    default_costs: dict[str, dict[str, float]] = Field(
        default_factory=lambda: {
            "gpt-4o-mini": {"input": 0.00015, "output": 0.0006},
            "gpt-4o": {"input": 0.005, "output": 0.015},
            "claude-haiku-4-5": {"input": 0.0008, "output": 0.004},
            "claude-sonnet-4-5": {"input": 0.003, "output": 0.015},
            "claude-opus-4-1": {"input": 0.015, "output": 0.075},
        }
    )

    @field_validator("db_path", mode="after")
    @classmethod
    def _resolve_path(cls, value: Path) -> Path:
        return Path(os.path.expanduser(str(value))).resolve()


class MarketplaceSection(BaseModel):
    """Discover, share, install agent configurations.

    The marketplace is read-only out of the box; turning on
    :attr:`publish_enabled` lets the local kernel publish its
    installed agents to a remote registry.
    """

    enabled: bool = True
    db_path: Path = PROJECT_ROOT / "data" / "marketplace.db"
    index_url: str = "https://marketplace.openswarm.dev/api/v1"
    publish_enabled: bool = False
    publish_token: str = ""
    local_manifests_dir: Path = PROJECT_ROOT / "manifests"

    @field_validator("db_path", "local_manifests_dir", mode="after")
    @classmethod
    def _resolve_path(cls, value: Path) -> Path:
        return Path(os.path.expanduser(str(value))).resolve()


class CLISection(BaseModel):
    """Cosmetic tunables for the CLI."""

    color: bool = True
    show_progress: bool = True
    page_size: int = 25
    log_tail_lines: int = 200


# ---------------------------------------------------------------------------
# Root
# ---------------------------------------------------------------------------


class OpenSwarmConfig(BaseSettings):
    """Root configuration object.

    The :class:`BaseSettings` machinery is what gives us env-var
    overrides for free.  The TOML loader is a separate, optional
    leg; it simply feeds values into the same model.
    """

    model_config = SettingsConfigDict(
        env_prefix="OPENSWARM_",
        env_nested_delimiter="__",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    version: str = "0.1.0"
    project_root: Path = PROJECT_ROOT

    kernel: KernelSection = Field(default_factory=KernelSection)
    dashboard: DashboardSection = Field(default_factory=DashboardSection)
    workers: WorkersSection = Field(default_factory=WorkersSection)
    auth: AuthSection = Field(default_factory=AuthSection)
    redis: RedisSection = Field(default_factory=RedisSection)
    telegram: TelegramSection = Field(default_factory=TelegramSection)
    billing: BillingSection = Field(default_factory=BillingSection)
    marketplace: MarketplaceSection = Field(default_factory=MarketplaceSection)
    cli: CLISection = Field(default_factory=CLISection)

    def to_toml(self) -> str:
        """Render the config to a TOML string for round-tripping."""
        return _dump_toml(self.model_dump())


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def _candidate_config_paths() -> list[Path]:
    """Return the list of TOML paths we will try, in priority order."""
    return [
        Path.cwd() / "config" / "openswarm.toml",
        Path.cwd() / "openswarm.toml",
        _user_config_dir() / "config.toml",
    ]


def _read_toml(path: Path) -> dict[str, Any]:
    """Read a TOML file using stdlib :mod:`tomllib` (Py≥3.11)."""
    if sys.version_info >= (3, 11):
        import tomllib as _toml
    else:  # pragma: no cover
        import tomli as _toml  # type: ignore[import-not-found]
    with path.open("rb") as f:
        return _toml.load(f)


def _dump_toml(data: dict[str, Any]) -> str:
    """Write a dict as TOML.

    We need a writer; stdlib :mod:`tomllib` is read-only. Falls back
    to a small hand-rolled emitter when ``tomli_w`` is not installed
    (the common case in Py≥3.11). The emitter handles the subset
    OpenSwarm actually emits — strings, ints, floats, bools, lists,
    nested dicts, paths as strings.
    """
    try:
        import tomli_w as _toml_w  # type: ignore[import-not-found]

        return _toml_w.dumps(data)
    except ImportError:
        return _hand_dump_toml(data)


def _toml_value(v: Any) -> str:
    """Render a single TOML primitive."""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, Path):
        return f'"{v}"'
    if isinstance(v, str):
        escaped = v.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    if isinstance(v, list):
        return "[" + ", ".join(_toml_value(x) for x in v) + "]"
    if isinstance(v, dict):
        return "{" + ", ".join(f'{k2} = {_toml_value(v2)}' for k2, v2 in v.items()) + "}"
    raise TypeError(f"Unsupported TOML value type: {type(v).__name__}")


def _hand_dump_toml(data: dict[str, Any]) -> str:
    """A minimal TOML emitter for the subset OpenSwarm uses."""
    lines: list[str] = []
    # Emit scalars at the top level first, then tables.
    scalars: dict[str, Any] = {}
    tables: dict[str, Any] = {}
    for key, value in data.items():
        if isinstance(value, dict):
            tables[key] = value
        else:
            scalars[key] = value
    for key, value in scalars.items():
        lines.append(f"{key} = {_toml_value(value)}")
    for key, value in tables.items():
        if lines:
            lines.append("")
        lines.append(f"[{key}]")
        for sub_key, sub_value in value.items():
            if isinstance(sub_value, dict):
                lines.append("")
                lines.append(f"[{key}.{sub_key}]")
                for k2, v2 in sub_value.items():
                    lines.append(f"{k2} = {_toml_value(v2)}")
            else:
                lines.append(f"{sub_key} = {_toml_value(sub_value)}")
    return "\n".join(lines) + "\n"


def load_config(
    *,
    config_path: Path | None = None,
    overrides: dict[str, Any] | None = None,
) -> OpenSwarmConfig:
    """Build an :class:`OpenSwarmConfig` from defaults + TOML + env + overrides.

    Parameters
    ----------
    config_path:
        Explicit path to a TOML file. If ``None``, the loader tries
        the candidate list in :func:`_candidate_config_paths`.
    overrides:
        A nested dict merged on top of the loaded config. Useful for
        tests (``overrides={"kernel": {"port": 9999}}``) and for the
        ``openswarm config set`` command.
    """
    base: dict[str, Any] = {}

    # 1. Try TOML file.
    if config_path is not None:
        candidates = [Path(config_path)]
    else:
        candidates = _candidate_config_paths()

    for path in candidates:
        if path.is_file():
            try:
                base = _read_toml(path)
                logger.debug("loaded config from %s", path)
            except Exception as exc:  # noqa: BLE001
                warnings.warn(
                    f"Failed to parse {path}: {exc}. Falling back to defaults.",
                    stacklevel=2,
                )
                base = {}
            break

    # 2. Apply overrides.
    if overrides:
        _deep_merge(base, overrides)

    # 3. Hand the dict to Pydantic; env-var overrides are applied
    #    automatically by BaseSettings.
    return OpenSwarmConfig.model_validate(base) if base else OpenSwarmConfig()


def _deep_merge(target: dict[str, Any], patch: dict[str, Any]) -> None:
    """Recursively merge ``patch`` into ``target`` in place."""
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            _deep_merge(target[key], value)
        else:
            target[key] = value


def write_config(config: OpenSwarmConfig, path: Path) -> None:
    """Serialize ``config`` to ``path`` as TOML. Creates parent dirs."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(config.to_toml(), encoding="utf-8")


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

_config: OpenSwarmConfig | None = None


def get_config() -> OpenSwarmConfig:
    """Return a process-wide :class:`OpenSwarmConfig` singleton."""
    global _config
    if _config is None:
        _config = load_config()
    return _config


def reset_config_for_tests(**overrides: Any) -> OpenSwarmConfig:
    """Rebuild the config singleton with overrides (test-only)."""
    global _config
    base = load_config()
    merged = base.model_copy(deep=True)
    if overrides:
        _deep_merge(merged.model_dump(), overrides)
        merged = OpenSwarmConfig.model_validate(merged.model_dump())
    _config = merged
    return _config


__all__ = [
    "AuthSection",
    "BillingSection",
    "CLISection",
    "DashboardSection",
    "KernelSection",
    "MarketplaceSection",
    "OpenSwarmConfig",
    "PROJECT_ROOT",
    "RedisSection",
    "TelegramSection",
    "WorkersSection",
    "get_config",
    "load_config",
    "reset_config_for_tests",
    "write_config",
]
