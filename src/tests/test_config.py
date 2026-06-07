"""Tests for the unified ``src.config`` module."""
from __future__ import annotations

from pathlib import Path

import pytest

from config import (
    AuthSection,
    BillingSection,
    CLISection,
    DashboardSection,
    KernelSection,
    MarketplaceSection,
    OpenSwarmConfig,
    RedisSection,
    TelegramSection,
    WorkersSection,
    get_config,
    load_config,
    reset_config_for_tests,
    write_config,
)


def test_default_config_is_well_formed() -> None:
    cfg = OpenSwarmConfig()
    assert cfg.version == "0.1.0"
    assert cfg.kernel.port == 8765
    assert cfg.dashboard.port == 8000
    assert cfg.auth.enabled is False
    assert cfg.redis.enabled is False
    assert cfg.telegram.enabled is False
    assert cfg.billing.enabled is True
    assert cfg.marketplace.enabled is True
    assert len(cfg.workers.manifests) >= 1


def test_section_singletons_share_project_root(tmp_path: Path) -> None:
    import warnings

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        cfg = OpenSwarmConfig()
    assert cfg.kernel.data_dir == cfg.kernel.data_dir
    assert cfg.dashboard.db_path.parent.parent == cfg.kernel.data_dir.parent


def test_load_config_with_overrides(tmp_path: Path) -> None:
    cfg = load_config(overrides={"kernel": {"port": 9999}, "auth": {"enabled": True}})
    assert cfg.kernel.port == 9999
    assert cfg.auth.enabled is True


def test_load_config_from_toml(tmp_path: Path) -> None:
    toml_path = tmp_path / "openswarm.toml"
    toml_path.write_text(
        """
[kernel]
port = 9123
log_level = "DEBUG"

[auth]
enabled = true
jwt_secret = "unit-test-secret"

[workers]
manifests = ["custom-agent.json"]
""".strip(),
        encoding="utf-8",
    )
    cfg = load_config(config_path=toml_path)
    assert cfg.kernel.port == 9123
    assert cfg.kernel.log_level == "DEBUG"
    assert cfg.auth.enabled is True
    assert cfg.auth.jwt_secret == "unit-test-secret"
    assert cfg.workers.manifests == ["custom-agent.json"]


def test_write_and_reload_round_trip(tmp_path: Path) -> None:
    import warnings

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        cfg = OpenSwarmConfig()
    target = tmp_path / "config" / "openswarm.toml"
    target.parent.mkdir(parents=True, exist_ok=True)
    write_config(cfg, target)
    assert target.is_file()


def test_env_var_override(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("OPENSWARM_KERNEL__PORT", "12345")
    monkeypatch.setenv("OPENSWARM_AUTH__ENABLED", "true")
    cfg = load_config(config_path=tmp_path / "missing.toml")
    # Env wins over TOML.
    assert cfg.kernel.port == 12345
    assert cfg.auth.enabled is True


def test_jwt_secret_placeholder_exists() -> None:
    from config import AuthSection

    cfg = AuthSection()
    assert cfg.jwt_secret == "change-me-in-production"


def test_safe_jwt_secret_accepts_custom() -> None:
    from config import AuthSection

    cfg = AuthSection(jwt_secret="my-secret-123")
    assert cfg.jwt_secret == "my-secret-123"


def test_get_config_singleton() -> None:
    a = get_config()
    b = get_config()
    assert a is b


def test_reset_config_for_tests() -> None:
    reset_config_for_tests()
    from config import get_config

    cfg = get_config()
    assert cfg.kernel.port == 8765


def test_to_toml_basic_output() -> None:
    import warnings

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        cfg = OpenSwarmConfig()
    text = cfg.to_toml()
    assert "kernel" in text.lower()


def test_section_types_present() -> None:
    cfg = OpenSwarmConfig()
    assert isinstance(cfg.kernel, KernelSection)
    assert isinstance(cfg.dashboard, DashboardSection)
    assert isinstance(cfg.workers, WorkersSection)
    assert isinstance(cfg.auth, AuthSection)
    assert isinstance(cfg.redis, RedisSection)
    assert isinstance(cfg.telegram, TelegramSection)
    assert isinstance(cfg.billing, BillingSection)
    assert isinstance(cfg.marketplace, MarketplaceSection)
    assert isinstance(cfg.cli, CLISection)


def test_invalid_log_level_rejected() -> None:
    with pytest.raises(ValueError):
        KernelSection(log_level="NOPE")


def test_dashboard_disabled() -> None:
    cfg = OpenSwarmConfig(dashboard=DashboardSection(enabled=False))
    assert cfg.dashboard.enabled is False


def test_workers_manifests_are_strings() -> None:
    cfg = OpenSwarmConfig()
    for m in cfg.workers.manifests:
        assert isinstance(m, str)
