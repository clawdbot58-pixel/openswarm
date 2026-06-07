"""Tests for the unified CLI (Phase 11).

These tests exercise the Click commands without actually spawning
long-lived processes. We mock the :class:`ProcessManager` so the
tests are fast and deterministic.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from cli.main import cli
from cli.types import ProcessInfo, ProcessKind, SwarmStatus
from process_manager import ProcessManager


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def mock_pm() -> MagicMock:
    """Return a mocked :class:`ProcessManager`."""
    pm = MagicMock(spec=ProcessManager)
    pm.project_root = Path(__file__).resolve().parents[2]
    pm.data_dir = pm.project_root / "data"
    pm.log_dir = pm.data_dir / "logs"
    pm.state_file = pm.data_dir / "state.json"
    return pm


@pytest.fixture
def mock_config(tmp_path: Path) -> MagicMock:
    """Return a mocked :class:`OpenSwarmConfig`."""
    cfg = MagicMock()
    cfg.project_root = tmp_path
    cfg.kernel.data_dir = tmp_path / "data"
    cfg.dashboard.db_path = tmp_path / "data" / "dashboard.db"
    cfg.billing.db_path = tmp_path / "data" / "billing.db"
    cfg.marketplace.db_path = tmp_path / "data" / "marketplace.db"
    cfg.workers.manifests = ["coder-python-fast.json"]
    cfg.telegram.enabled = False
    cfg.cli.color = True
    cfg.to_toml.return_value = "[kernel]\nport = 8765\n"
    return cfg


class TestInit:
    def test_init_creates_directories(
        self, runner: CliRunner, mock_config: MagicMock, tmp_path: Path
    ) -> None:
        mock_config.project_root = tmp_path
        result = runner.invoke(cli, ["init"], obj={"config": mock_config, "pm": None})
        assert result.exit_code == 0
        assert (tmp_path / "data").is_dir()
        assert (tmp_path / "workspaces").is_dir()

    def test_init_creates_config_file(
        self, runner: CliRunner, mock_config: MagicMock, tmp_path: Path
    ) -> None:
        mock_config.project_root = tmp_path
        result = runner.invoke(cli, ["init"], obj={"config": mock_config, "pm": None})
        assert result.exit_code == 0
        config_path = tmp_path / "config" / "openswarm.toml"
        assert config_path.is_file()
        content = config_path.read_text(encoding="utf-8")
        assert "[kernel]" in content


class TestStart:
    def test_start_spawns_processes(
        self,
        runner: CliRunner,
        mock_pm: MagicMock,
        mock_config: MagicMock,
        tmp_path: Path,
    ) -> None:
        procs = [
            ProcessInfo(
                kind=ProcessKind.KERNEL,
                label="kernel",
                pid=1234,
                cmd=["python", "-m", "uvicorn", "kernel.main:app"],
                log_path=tmp_path / "logs" / "kernel.log",
                started_at=1234567890.0,
            )
        ]
        mock_pm.start_all.return_value = procs

        result = runner.invoke(
            cli,
            ["start"],
            obj={"config": mock_config, "pm": mock_pm},
        )
        assert result.exit_code == 0
        mock_pm.start_all.assert_called_once()
        assert "Started kernel" in result.output

    def test_start_fails_when_already_running(
        self,
        runner: CliRunner,
        mock_pm: MagicMock,
        mock_config: MagicMock,
    ) -> None:
        from process_manager import ProcessManagerError

        mock_pm.start_all.side_effect = ProcessManagerError(
            "OpenSwarm is already running"
        )

        result = runner.invoke(
            cli,
            ["start"],
            obj={"config": mock_config, "pm": mock_pm},
        )
        assert result.exit_code == 1
        assert "already running" in result.output


class TestStop:
    def test_stop_cleans_up(
        self,
        runner: CliRunner,
        mock_pm: MagicMock,
        mock_config: MagicMock,
    ) -> None:
        mock_pm.stop_all.return_value = []

        result = runner.invoke(
            cli,
            ["stop"],
            obj={"config": mock_config, "pm": mock_pm},
        )
        assert result.exit_code == 0
        mock_pm.stop_all.assert_called_once()
        assert "stopped cleanly" in result.output


class TestStatus:
    def test_status_shows_table(
        self,
        runner: CliRunner,
        mock_pm: MagicMock,
        mock_config: MagicMock,
    ) -> None:
        snap = SwarmStatus(
            kernel_running=True,
            kernel_pid=1234,
            kernel_url="http://127.0.0.1:8765",
            dashboard_running=True,
            dashboard_pid=5678,
            dashboard_url="http://127.0.0.1:8000",
            workers_running=2,
            workers_total=2,
            workers=[],
            main_agent_running=True,
            main_agent_pid=9999,
            telegram_running=False,
            telegram_pid=None,
            agents_registered=3,
            workflows_active=0,
        )
        mock_pm.get_status.return_value = snap

        result = runner.invoke(
            cli,
            ["status"],
            obj={"config": mock_config, "pm": mock_pm},
        )
        assert result.exit_code == 0
        assert "Kernel" in result.output
        assert "running" in result.output

    def test_status_json_output(
        self,
        runner: CliRunner,
        mock_pm: MagicMock,
        mock_config: MagicMock,
    ) -> None:
        snap = SwarmStatus(
            kernel_running=True,
            kernel_pid=1234,
            kernel_url="http://127.0.0.1:8765",
            dashboard_running=False,
            dashboard_pid=None,
            dashboard_url=None,
            workers_running=0,
            workers_total=0,
            workers=[],
            main_agent_running=False,
            main_agent_pid=None,
            telegram_running=False,
            telegram_pid=None,
            agents_registered=0,
            workflows_active=0,
        )
        mock_pm.get_status.return_value = snap

        result = runner.invoke(
            cli,
            ["status", "--json"],
            obj={"config": mock_config, "pm": mock_pm},
        )
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["kernel"]["running"] is True
        assert data["dashboard"]["running"] is False


class TestRun:
    def test_run_submits_goal(
        self,
        runner: CliRunner,
        mock_config: MagicMock,
        tmp_path: Path,
    ) -> None:
        # Mock the kernel health check.
        with patch("cli.main._check_kernel_reachable", return_value=True):
            # Mock urllib to return a fake workflow response.
            import urllib.request

            original_urlopen = urllib.request.urlopen

            def mock_urlopen(*args: any, **kwargs: any) -> any:
                # noqa: ANN401
                class FakeResp:
                    status = 200

                    def read(self) -> bytes:
                        return b'{"workflow_id": "wf-123", "status": "queued"}'

                    def __enter__(self: any) -> "FakeResp":  # noqa: ANN401
                        return self

                    def __exit__(self, *args: any) -> None:  # noqa: ANN401
                        pass

                return FakeResp()

            with patch("urllib.request.urlopen", side_effect=mock_urlopen):
                result = runner.invoke(
                    cli,
                    ["run", "Build a login page"],
                    obj={"config": mock_config, "pm": None},
                )

        assert result.exit_code == 0
        assert "Workflow wf-123" in result.output


class TestLogs:
    def test_logs_streams(
        self,
        runner: CliRunner,
        mock_pm: MagicMock,
        mock_config: MagicMock,
    ) -> None:
        def log_generator():
            yield "[kernel] Starting..."
            yield "[kernel] Ready"

        mock_pm.stream_logs.return_value = log_generator()

        result = runner.invoke(
            cli,
            ["logs", "--no-follow"],
            obj={"config": mock_config, "pm": mock_pm},
        )
        assert result.exit_code == 0
        assert "Starting..." in result.output


class TestConfig:
    def test_config_show_prints_toml(
        self,
        runner: CliRunner,
        mock_config: MagicMock,
    ) -> None:
        mock_config.to_toml.return_value = "[kernel]\nport = 9999\n"

        result = runner.invoke(
            cli,
            ["config", "show"],
            obj={"config": mock_config, "pm": None},
        )
        assert result.exit_code == 0
        assert "[kernel]" in result.output

    def test_config_set_writes_file(
        self,
        runner: CliRunner,
        mock_config: MagicMock,
        tmp_path: Path,
    ) -> None:
        config_path = tmp_path / "config" / "openswarm.toml"
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text("[kernel]\nport = 8765\n", encoding="utf-8")
        mock_config.to_toml.return_value = "[kernel]\nport = 9000\n"

        result = runner.invoke(
            cli,
            ["config", "set", "kernel.port", "9000"],
            obj={"config": mock_config, "pm": None, "config_path": config_path},
        )
        assert result.exit_code == 0
        content = config_path.read_text(encoding="utf-8")
        assert "port = 9000" in content