"""Tests for the process manager (Phase 11)."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from cli.types import ProcessInfo, ProcessKind, StartupConfig
from process_manager import (
    DEFAULT_LOG_DIRNAME,
    DEFAULT_STATE_FILE,
    PortInUseError,
    ProcessManager,
    ProcessManagerError,
    SHUTDOWN_GRACE_SECONDS,
    _find_free_port,
    _pid_alive,
    _project_root,
)


@pytest.fixture
def pm(tmp_path: Path) -> ProcessManager:
    """Return a ProcessManager pointing at a temporary data dir."""
    return ProcessManager(
        project_root=tmp_path,
        data_dir=tmp_path / "data",
        log_dir=tmp_path / "data" / "logs",
        state_file=tmp_path / "data" / "state.json",
    )


@pytest.fixture
def startup_config() -> StartupConfig:
    """Return a minimal StartupConfig."""
    return StartupConfig(
        kernel=True,
        dashboard=True,
        workers=["manifests/coder-python-fast.json"],
        telegram=False,
        port=8000,
        kernel_port=8765,
        detach=True,
    )


class TestProcessManagerPaths:
    def test_default_paths(self, pm: ProcessManager, tmp_path: Path) -> None:
        assert pm.project_root == tmp_path
        assert pm.data_dir == tmp_path / "data"
        assert pm.log_dir == tmp_path / "data" / "logs"
        assert pm.state_file == tmp_path / "data" / "state.json"

    def test_log_path_sanitizes_label(self, pm: ProcessManager) -> None:
        log = pm.log_path("kernel")
        assert log.name == "kernel.log"

        log = pm.log_path("worker_coder-python-fast")
        assert log.name == "worker_coder-python-fast.log"


class TestProjectRoot:
    def test_finds_pyproject(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        (tmp_path / "pyproject.toml").write_text("[project]\nname = 'test'\n")
        monkeypatch.chdir(tmp_path)
        assert _project_root() == tmp_path

    def test_falls_back_to_src_parent(self) -> None:
        root = _project_root()
        assert (root / "src").is_dir()


class TestPidAlive:
    def test_self_is_alive(self) -> None:
        import os

        assert _pid_alive(os.getpid())

    def test_none_is_not_alive(self) -> None:
        assert not _pid_alive(0)
        assert not _pid_alive(-1)


class TestFindFreePort:
    def test_returns_free_port(self) -> None:
        port = _find_free_port()
        assert 1024 <= port <= 65535

    def test_respects_preferred(self) -> None:
        port = _find_free_port(preferred=18765)
        assert port == 18765

    def test_falls_back_on_busy(self) -> None:
        import socket

        # Bind a known port to make it busy.
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 19999))
            port = _find_free_port(preferred=19999)
            # Should pick a different port.
            assert port != 19999


class TestStatePersistence:
    def test_save_and_load(self, pm: ProcessManager) -> None:
        info = ProcessInfo(
            kind=ProcessKind.KERNEL,
            label="kernel",
            pid=1234,
            cmd=["python", "-m", "uvicorn"],
            log_path=pm.log_path("kernel"),
            started_at=1234567890.0,
        )

        # Manually write state.
        state = pm._state.load()
        state.processes["kernel"] = pm._proc_to_dict(info)
        pm._state.save(state)

        # Reload.
        state2 = pm._state.load()
        assert "kernel" in state2.processes
        assert state2.processes["kernel"]["pid"] == 1234

    def test_clear(self, pm: ProcessManager) -> None:
        state = pm._state.load()
        state.processes["kernel"] = {"pid": 1234}
        pm._state.save(state)

        pm._state.clear()
        state2 = pm._state.load()
        assert "kernel" not in state2.processes


class TestLoadLiveProcesses:
    def test_returns_empty_when_none_recorded(self, pm: ProcessManager) -> None:
        live = pm._load_live_processes()
        assert live == []

    def test_filters_dead_pids(self, pm: ProcessManager) -> None:
        state = pm._state.load()
        state.processes["kernel"] = {
            "kind": "kernel",
            "pid": 999999,  # Non-existent PID
            "cmd": ["python"],
            "log_path": "/tmp/kernel.log",
            "started_at": 1234567890.0,
            "extra": {},
        }
        pm._state.save(state)

        live = pm._load_live_processes()
        assert live == []


class TestResolveManifest:
    def test_absolute_path(self, pm: ProcessManager, tmp_path: Path) -> None:
        manifest = tmp_path / "custom.json"
        manifest.write_text("{}")
        resolved = pm._resolve_manifest(str(manifest))
        assert resolved == str(manifest)

    def test_missing_raises(self, pm: ProcessManager) -> None:
        with pytest.raises(ProcessManagerError):
            pm._resolve_manifest("nonexistent")


class TestGetStatus:
    def test_empty_status(self, pm: ProcessManager) -> None:
        status = pm.get_status()
        assert status.kernel_running is False
        assert status.dashboard_running is False
        assert status.workers_running == 0


class TestSeekToTail:
    def test_empty_file(self, pm: ProcessManager, tmp_path: Path) -> None:
        log_path = tmp_path / "test.log"
        log_path.write_text("")
        offset = pm._seek_to_tail(log_path, lines=10)
        assert offset == 0


class TestStreamLogs:
    def test_empty_logs(self, pm: ProcessManager, tmp_path: Path) -> None:
        # No state file yet.
        logs = list(pm.stream_logs(follow=False, tail=10))
        assert logs == []

    def test_from_state(self, pm: ProcessManager, tmp_path: Path) -> None:
        # Create a state file with a recorded process.
        state = pm._state.load()
        state.processes["kernel"] = {
            "kind": "kernel",
            "pid": 1234,
            "cmd": ["python"],
            "log_path": str(tmp_path / "logs" / "kernel.log"),
            "started_at": 1234567890.0,
            "extra": {},
        }
        pm._state.save(state)

        # Create the log file.
        pm.log_dir.mkdir(parents=True, exist_ok=True)
        log_path = pm.log_path("kernel")
        log_path.write_text("test line\n")

        logs = list(pm.stream_logs(labels=["kernel"], follow=False, tail=10))
        assert len(logs) == 1
        assert "[kernel]" in logs[0]


class TestConstants:
    def test_defaults_are_defined(self) -> None:
        assert DEFAULT_LOG_DIRNAME == "logs"
        assert DEFAULT_STATE_FILE == "state.json"
        assert SHUTDOWN_GRACE_SECONDS == 5.0


class TestPopen:
    def test_popen_creates_process_info(self, pm: ProcessManager, tmp_path: Path) -> None:
        pm._logs = tmp_path / "logs"
        pm._logs.mkdir(parents=True, exist_ok=True)

        info = pm._popen(
            cmd=["echo", "hello"],
            env={},
            label="test",
            kind=ProcessKind.WORKER,
        )
        assert info.kind == ProcessKind.WORKER
        assert info.label == "test"
        assert info.pid is not None
        assert info.log_path.name == "test.log"
