"""Background process manager for ``openswarm start``.

This module is the engine behind the unified CLI. It owns the
lifecycle of every child process the user starts:

* the kernel (``uvicorn kernel.main:app``),
* the dashboard backend (``uvicorn dashboard.backend.main:app``),
* one or more generic workers (``python src/agent_worker.py``),
* the main agent (``python -m agents.main_agent``),
* (optionally) the Telegram bot.

Design choices
--------------
* All children are detached from the CLI's controlling terminal via
  ``start_new_session=True`` (POSIX) and ``CREATE_NEW_PROCESS_GROUP``
  on Windows. This lets the user ``Ctrl-C`` the CLI without killing
  the swarm.
* All stdout/stderr is teed to a per-process log file under
  ``data/logs/{process}.log`` so ``openswarm logs`` can replay them.
* A small JSON state file at ``data/state.json`` tracks PIDs and
  start times so ``openswarm status`` works even from a fresh shell.
* Shutdown is "best effort, polite then firm": SIGTERM with a 5-second
  grace period, then SIGKILL on any survivors.
* The module is **synchronous** on purpose — Click subcommands
  block on it; there's no benefit to async here.

The manager is also designed for tests: every dependency (PID
writer, log dir, port allocator) is constructor-injectable.
"""
from __future__ import annotations

import json
import logging
import os
import signal
import socket
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

from cli.types import ProcessInfo, ProcessKind, StartupConfig, SwarmStatus

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_STATE_FILE: str = "state.json"
DEFAULT_LOG_DIRNAME: str = "logs"
SHUTDOWN_GRACE_SECONDS: float = 5.0
STARTUP_WAIT_SECONDS: float = 0.25


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ProcessManagerError(RuntimeError):
    """Raised when the process manager can't perform a requested action."""


class PortInUseError(ProcessManagerError):
    """Raised when a child can't bind a port we expected to be free."""


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------


def _pid_alive(pid: int) -> bool:
    """True if a process with ``pid`` is currently running.

    On POSIX we send signal 0. On Windows we ``OpenProcess`` via
    :mod:`ctypes` — the only portable way to check liveness without
    external dependencies.
    """
    if pid <= 0:
        return False
    if sys.platform.startswith("win"):
        try:
            import ctypes  # type: ignore[import-not-found]

            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            STILL_ACTIVE = 259
            handle = ctypes.windll.kernel32.OpenProcess(  # type: ignore[attr-defined]
                PROCESS_QUERY_LIMITED_INFORMATION, False, pid
            )
            if not handle:
                return False
            try:
                code = ctypes.c_ulong()
                ok = ctypes.windll.kernel32.GetExitCodeProcess(  # type: ignore[attr-defined]
                    handle, ctypes.byref(code)
                )
                return bool(ok) and code.value == STILL_ACTIVE
            finally:
                ctypes.windll.kernel32.CloseHandle(handle)  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            return False
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def _find_free_port(preferred: int | None = None) -> int:
    """Return an OS-allocated free port, preferring ``preferred`` if free."""
    if preferred is not None:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", preferred))
                return preferred
            except OSError:
                pass
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _python_executable() -> str:
    """Return the Python interpreter that should run the children.

    In a venv this is ``sys.executable``. We deliberately use
    ``sys.executable`` rather than ``python`` so the children inherit
    the same interpreter (and any locally-installed packages).
    """
    return sys.executable


def _project_root() -> Path:
    """Resolve the project root.

    Looks for the first ancestor containing ``pyproject.toml`` starting
    from the current working directory. Falls back to the parent of
    ``src/`` if none is found.
    """
    cwd = Path.cwd().resolve()
    for ancestor in (cwd, *cwd.parents):
        if (ancestor / "pyproject.toml").is_file():
            return ancestor
    return Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# State persistence
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _PersistedState:
    """Shape of the on-disk ``data/state.json`` file."""

    processes: dict[str, dict] = field(default_factory=dict)
    started_at: float = field(default_factory=time.time)
    cli_version: str = "0.1.0"
    kernel_url: str | None = None
    dashboard_url: str | None = None


class _StateStore:
    """JSON file backing the process manager's PID bookkeeping.

    Writes are atomic-ish: we write to a temp file in the same
    directory, then ``os.replace`` over the real one. Concurrent
    writers (the user running ``openswarm start`` twice) will lose
    a race, but the manager is single-instance per project root by
    design.
    """

    def __init__(self, path: Path):
        self._path = path

    def load(self) -> _PersistedState:
        if not self._path.is_file():
            return _PersistedState()
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return _PersistedState()
        return _PersistedState(
            processes=data.get("processes", {}),
            started_at=float(data.get("started_at", time.time())),
            cli_version=data.get("cli_version", "0.1.0"),
            kernel_url=data.get("kernel_url"),
            dashboard_url=data.get("dashboard_url"),
        )

    def save(self, state: _PersistedState) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(asdict(state), indent=2, sort_keys=True),
            encoding="utf-8",
        )
        os.replace(tmp, self._path)

    def clear(self) -> None:
        if self._path.is_file():
            self._path.unlink()


# ---------------------------------------------------------------------------
# Process manager
# ---------------------------------------------------------------------------


class ProcessManager:
    """Manage kernel, workers, dashboard, and main agent as children.

    Parameters
    ----------
    project_root:
        Root of the OpenSwarm checkout. The manager resolves paths
        like ``manifests/`` and ``data/`` relative to it.
    data_dir:
        Override for the ``data/`` directory. Defaults to
        ``<project_root>/data``.
    log_dir:
        Override for the per-process log directory. Defaults to
        ``<data_dir>/logs``.
    state_file:
        Override for the JSON file that tracks PIDs.
    """

    def __init__(
        self,
        project_root: Path | None = None,
        *,
        data_dir: Path | None = None,
        log_dir: Path | None = None,
        state_file: Path | None = None,
    ) -> None:
        self._root = Path(project_root or _project_root()).resolve()
        self._data = Path(data_dir or (self._root / "data")).resolve()
        self._logs = Path(log_dir or (self._data / DEFAULT_LOG_DIRNAME)).resolve()
        self._state_path = Path(
            state_file or (self._data / DEFAULT_STATE_FILE)
        ).resolve()
        self._state = _StateStore(self._state_path)
        self._env_base = self._build_env_base()
        self._children: dict[str, subprocess.Popen] = {}

    # -- env ----------------------------------------------------------------

    def _build_env_base(self) -> dict[str, str]:
        """Build the env dict every child inherits.

        We start with the current process's env (so PATH, VIRTUAL_ENV,
        API keys, etc. are preserved) and then layer OpenSwarm
        overrides on top.
        """
        env = os.environ.copy()
        env.setdefault("OPENSWARM_PROJECT_ROOT", str(self._root))
        env.setdefault("PYTHONPATH", str(self._root / "src") + os.pathsep + env.get("PYTHONPATH", ""))
        env.setdefault("PYTHONUNBUFFERED", "1")
        return env

    # -- paths --------------------------------------------------------------

    @property
    def data_dir(self) -> Path:
        return self._data

    @property
    def log_dir(self) -> Path:
        return self._logs

    @property
    def state_file(self) -> Path:
        return self._state_path

    @property
    def project_root(self) -> Path:
        return self._root

    def log_path(self, label: str) -> Path:
        """Return the log file path for a child with ``label``."""
        safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in label)
        return self._logs / f"{safe}.log"

    # -- lifecycle ----------------------------------------------------------

    def start_all(self, config: StartupConfig) -> list[ProcessInfo]:
        """Start every component described by ``config``.

        Returns the list of :class:`ProcessInfo` records for the
        children spawned in this call. The on-disk state file is
        updated atomically at the end.
        """
        self._data.mkdir(parents=True, exist_ok=True)
        self._logs.mkdir(parents=True, exist_ok=True)

        # Detect already-running processes; refuse to double-start.
        existing = self._load_live_processes()
        if existing:
            labels = ", ".join(f"{p.kind.value}:{p.label}" for p in existing)
            raise ProcessManagerError(
                f"OpenSwarm is already running ({labels}). "
                "Run 'openswarm stop' first."
            )

        # Allocate ports up front.
        kernel_port = _find_free_port(config.kernel_port)
        if config.kernel_port != kernel_port:
            logger.warning(
                "kernel port %d busy; using %d", config.kernel_port, kernel_port
            )
        dashboard_port = _find_free_port(config.port) if config.dashboard else 0

        env = dict(self._env_base)
        kernel_host = env.get("OPENSWARM_KERNEL_HOST", env.get("OPENSWARM_HOST", "127.0.0.1"))
        dashboard_host = env.get(
            "OPENSWARM_DASHBOARD_HOST",
            env.get("OPENSWARM_DASHBOARD__HOST", "127.0.0.1"),
        )
        env["OPENSWARM_KERNEL_PORT"] = str(kernel_port)
        env["OPENSWARM_KERNEL__PORT"] = str(kernel_port)
        env["OPENSWARM_HOST"] = kernel_host
        env["OPENSWARM_PORT"] = str(kernel_port)
        env["OPENSWARM_DASHBOARD_PORT"] = str(dashboard_port)
        env["OPENSWARM_DASHBOARD__PORT"] = str(dashboard_port)
        env["OPENSWARM_DASHBOARD__HOST"] = dashboard_host
        env["OPENSWARM_DATA_DIR"] = str(self._data)
        env["OPENSWARM_DB_PATH"] = str(self._data / "registry.db")
        env["KERNEL_WS"] = f"ws://{kernel_host}:{kernel_port}/ws"
        env["KERNEL_REST_URL"] = f"http://{kernel_host}:{kernel_port}"
        agent_ws = self._root / "workspaces" / "agent"
        agent_ws.mkdir(parents=True, exist_ok=True)
        env["OPENSWARM_AGENT_WORKSPACE"] = str(agent_ws)
        env["OPENSWARM_HARNESS_DIR"] = str(self._data / "workspaces")
        if config.dashboard:
            env["OPENSWARM_DASHBOARD_URL"] = f"http://{dashboard_host}:{dashboard_port}/ui/"

        # Propagate secrets from unified config / .env into child env.
        try:
            from config import get_config

            cfg = get_config()
            if cfg.telegram.bot_token:
                env["TELEGRAM_BOT_TOKEN"] = cfg.telegram.bot_token
                env["OPENSWARM_TELEGRAM__BOT_TOKEN"] = cfg.telegram.bot_token
            if cfg.websearch.api_key:
                env["EXA_API_KEY"] = cfg.websearch.api_key
            env.setdefault("OPENSWARM_LLM_PROFILE", cfg.llm.profile)
        except Exception:  # noqa: BLE001
            pass

        started: list[ProcessInfo] = []

        try:
            from workspace.taskboard import ensure_agent_workspace

            ensure_agent_workspace(self._root)
        except Exception:  # noqa: BLE001
            pass

        if config.kernel:
            info = self._spawn_kernel(env, kernel_port)
            started.append(info)
            if not self._wait_for_http_health(
                f"http://{kernel_host}:{kernel_port}/health",
                timeout=10.0,
            ):
                self._state.clear()
                self._terminate(info, timeout=1.0)
                raise ProcessManagerError(
                    "Kernel did not become healthy during startup.\n"
                    + self._log_excerpt(info.log_path)
                )

        if config.dashboard:
            info = self._spawn_dashboard(env, dashboard_port)
            started.append(info)

        if config.kernel:
            info = self._spawn_conductor(env)
            started.append(info)

        for manifest in config.workers:
            info = self._spawn_worker(env, manifest)
            started.append(info)

        if config.workers or config.kernel or config.dashboard:
            # Main agent is the orchestrator; spawn it last so all
            # other endpoints are reachable.
            info = self._spawn_main_agent(env)
            started.append(info)

        if config.telegram:
            info = self._spawn_telegram(env, kernel_port)
            started.append(info)

        # Persist.
        state = self._state.load()
        for proc in started:
            state.processes[proc.label] = self._proc_to_dict(proc)
        kernel_host = env.get("OPENSWARM_KERNEL_HOST", "127.0.0.1")
        dash_host = env.get("OPENSWARM_DASHBOARD__HOST", "127.0.0.1")
        state.kernel_url = f"http://{kernel_host}:{kernel_port}"
        if config.dashboard:
            state.dashboard_url = f"http://{dash_host}:{dashboard_port}"
        self._state.save(state)

        # Give children a beat to bind their sockets; users get fewer
        # spurious "not running" errors on the very first ``status``.
        time.sleep(STARTUP_WAIT_SECONDS)
        crashed = self._started_failures(started)
        if crashed:
            for proc in started:
                if proc.label not in crashed:
                    self._terminate(proc, timeout=1.0)
            self._state.clear()
            details = "\n\n".join(
                f"{label} exited during startup.\n{excerpt}"
                for label, excerpt in crashed.items()
            )
            raise ProcessManagerError(
                "OpenSwarm failed to start cleanly:\n" + details
            )

        return started

    def stop_all(self, *, timeout: float = SHUTDOWN_GRACE_SECONDS) -> list[ProcessInfo]:
        """Terminate every child recorded in the state file.

        Returns the list of processes that were actually alive at
        shutdown time. The state file is cleared at the end.
        """
        state = self._state.load()
        live = self._load_live_processes()
        survivors: list[ProcessInfo] = []
        for proc in live:
            logger.info("stopping %s pid=%s", proc.label, proc.pid)
            self._terminate(proc, timeout=timeout)
            if proc.pid is not None and _pid_alive(proc.pid):
                survivors.append(proc)
        self._state.clear()
        return survivors

    def get_status(self, config: StartupConfig | None = None) -> SwarmStatus:
        """Build a :class:`SwarmStatus` snapshot."""
        live = self._load_live_processes()
        kernel = self._find(live, ProcessKind.KERNEL)
        dashboard = self._find(live, ProcessKind.DASHBOARD)
        main_agent = self._find(live, ProcessKind.MAIN_AGENT)
        telegram = self._find(live, ProcessKind.TELEGRAM)
        workers = [p for p in live if p.kind is ProcessKind.WORKER]

        kernel_running = bool(kernel and kernel.pid and _pid_alive(kernel.pid))
        dashboard_running = bool(dashboard and dashboard.pid and _pid_alive(dashboard.pid))
        main_agent_running = bool(main_agent and main_agent.pid and _pid_alive(main_agent.pid))
        telegram_running = bool(telegram and telegram.pid and _pid_alive(telegram.pid))

        agents_registered, workflows_active = self._peek_kernel_stats(
            kernel, config
        )

        return SwarmStatus(
            kernel_running=kernel_running,
            kernel_pid=kernel.pid if kernel else None,
            kernel_url=f"http://{self._kernel_host(config)}:{kernel.extra.get('port', '8765')}"
            if kernel_running
            else None,
            dashboard_running=dashboard_running,
            dashboard_pid=dashboard.pid if dashboard else None,
            dashboard_url=f"http://{self._dashboard_host(config)}:{dashboard.extra.get('port', '8000')}"
            if dashboard_running
            else None,
            workers_running=sum(
                1 for w in workers if w.pid is not None and _pid_alive(w.pid)
            ),
            workers_total=len(workers),
            workers=workers,
            main_agent_running=main_agent_running,
            main_agent_pid=main_agent.pid if main_agent else None,
            telegram_running=telegram_running,
            telegram_pid=telegram.pid if telegram else None,
            agents_registered=agents_registered,
            workflows_active=workflows_active,
        )

    def stream_logs(
        self,
        *,
        labels: Iterable[str] | None = None,
        follow: bool = True,
        tail: int = 200,
    ) -> Iterable[str]:
        """Yield log lines from the recorded processes.

        ``follow=True`` blocks and emits new lines as they're appended
        (used by ``openswarm logs``). When ``follow=False`` only the
        last ``tail`` lines are returned, then iteration stops.
        """
        state = self._state.load()
        labels_seq = list(labels) if labels else list(state.processes.keys())
        if not labels_seq:
            return
        # Use the inotify-free "poll" path so the function works on
        # every OS without external deps. The poll interval is short
        # enough that the user perceives it as live.

        # Track the current read position per file.
        positions: dict[str, int] = {}
        for label in labels_seq:
            log_path = self.log_path(label)
            if not log_path.is_file():
                continue
            # Seek to "tail" lines from the end.
            positions[label] = self._seek_to_tail(log_path, tail)

        seen_done = False
        last_emit = time.time()
        while True:
            any_data = False
            for label in list(positions.keys()):
                log_path = self.log_path(label)
                if not log_path.is_file():
                    continue
                size = log_path.stat().st_size
                pos = positions[label]
                if size > pos:
                    with log_path.open("r", encoding="utf-8", errors="replace") as f:
                        f.seek(pos)
                        chunk = f.read()
                    positions[label] = size
                    for line in chunk.splitlines():
                        any_data = True
                        yield f"[{label}] {line}"
            if not follow:
                seen_done = True
            if seen_done:
                return
            if not any_data:
                # Idle: sleep a touch so we don't peg the CPU.
                time.sleep(0.2)
                last_emit = time.time()
            else:
                # Throttle to ~50 lines/sec to keep the terminal usable.
                now = time.time()
                if now - last_emit < 0.02:
                    time.sleep(0.02 - (now - last_emit))
                last_emit = time.time()

    # -- spawning helpers ---------------------------------------------------

    def _spawn_kernel(self, env: dict[str, str], port: int) -> ProcessInfo:
        cmd = [
            _python_executable(),
            "-m",
            "uvicorn",
            "kernel.main:app",
            "--host",
            env.get("OPENSWARM_KERNEL_HOST", "127.0.0.1"),
            "--port",
            str(port),
        ]
        info = self._popen(
            cmd,
            env=env,
            label="kernel",
            kind=ProcessKind.KERNEL,
            extra={"port": str(port)},
        )
        return info

    def _spawn_dashboard(self, env: dict[str, str], port: int) -> ProcessInfo:
        cmd = [
            _python_executable(),
            "-m",
            "uvicorn",
            "dashboard.backend.main:app",
            "--host",
            env.get("OPENSWARM_DASHBOARD_HOST", "127.0.0.1"),
            "--port",
            str(port),
        ]
        info = self._popen(
            cmd,
            env=env,
            label="dashboard",
            kind=ProcessKind.DASHBOARD,
            extra={"port": str(port)},
        )
        return info

    def _spawn_worker(self, env: dict[str, str], manifest: str) -> ProcessInfo:
        manifest_path = self._resolve_manifest(manifest)
        env = dict(env)
        env["AGENT_MANIFEST_PATH"] = str(manifest_path)
        cmd = [
            _python_executable(),
            "-m",
            "agent_worker",
        ]
        info = self._popen(
            cmd,
            env=env,
            label=f"worker:{Path(manifest_path).stem}",
            kind=ProcessKind.WORKER,
            extra={"manifest": manifest_path},
        )
        return info

    def _spawn_conductor(self, env: dict[str, str]) -> ProcessInfo:
        cmd = [
            _python_executable(),
            "-m",
            "agents.conductor",
        ]
        return self._popen(
            cmd,
            env=env,
            label="conductor",
            kind=ProcessKind.WORKER,
            extra={"role": "conductor"},
        )

    def _spawn_main_agent(self, env: dict[str, str]) -> ProcessInfo:
        cmd = [
            _python_executable(),
            "-m",
            "agents.main_agent",
        ]
        info = self._popen(
            cmd,
            env=env,
            label="main-agent",
            kind=ProcessKind.MAIN_AGENT,
        )
        return info

    def _spawn_telegram(self, env: dict[str, str], kernel_port: int) -> ProcessInfo:
        cmd = [
            _python_executable(),
            "-m",
            "telegram_adapter.bot",
        ]
        info = self._popen(
            cmd,
            env=env,
            label="telegram",
            kind=ProcessKind.TELEGRAM,
            extra={"kernel_port": str(kernel_port)},
        )
        return info

    def _resolve_manifest(self, manifest: str) -> str:
        """Resolve a manifest path relative to the project root.

        Accepts:

        * An absolute path — used as-is.
        * A path that exists in ``manifests/`` — used as-is.
        * A bare stem like ``coder-python-fast`` — resolved to
          ``manifests/coder-python-fast.json``.
        """
        path = Path(manifest)
        if path.is_absolute() and path.is_file():
            return str(path)
        candidate = (self._root / manifest).resolve()
        if candidate.is_file():
            return str(candidate)
        candidate = (self._root / "manifests" / manifest).resolve()
        if candidate.is_file():
            return str(candidate)
        candidate = (self._root / "manifests" / f"{manifest}.json").resolve()
        if candidate.is_file():
            return str(candidate)
        raise ProcessManagerError(f"Could not locate manifest: {manifest!r}")

    def _popen(
        self,
        cmd: Sequence[str],
        *,
        env: dict[str, str],
        label: str,
        kind: ProcessKind,
        extra: dict[str, str] | None = None,
    ) -> ProcessInfo:
        """Spawn a child and return a :class:`ProcessInfo` record."""
        log_path = self.log_path(label)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_handle = log_path.open("ab", buffering=0)
        logger.info("spawning %s: %s", label, " ".join(cmd))
        try:
            proc = subprocess.Popen(  # noqa: S603
                list(cmd),
                cwd=str(self._root),
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        except FileNotFoundError as exc:
            log_handle.close()
            raise ProcessManagerError(
                f"Failed to spawn {label}: {exc}. "
                "Is the venv activated?"
            ) from exc
        self._children[label] = proc
        return ProcessInfo(
            kind=kind,
            label=label,
            pid=proc.pid,
            cmd=list(cmd),
            log_path=log_path,
            started_at=time.time(),
            extra=extra or {},
        )

    def _started_failures(self, started: list[ProcessInfo]) -> dict[str, str]:
        """Return children from this run that exited immediately."""
        failures: dict[str, str] = {}
        for info in started:
            proc = self._children.get(info.label)
            if proc is None or proc.poll() is None:
                continue
            failures[info.label] = self._log_excerpt(info.log_path)
        return failures

    def _log_excerpt(self, path: Path | None, *, lines: int = 40) -> str:
        if path is None or not path.is_file():
            return "(no log output)"
        try:
            content = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return "(could not read log output)"
        excerpt = "\n".join(content[-lines:]).strip()
        return excerpt or "(no log output)"

    def _wait_for_http_health(self, url: str, *, timeout: float) -> bool:
        """Wait for an HTTP health endpoint to return 2xx."""
        import urllib.error
        import urllib.request

        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(url, timeout=0.5) as resp:  # noqa: S310
                    if 200 <= resp.status < 300:
                        return True
            except (OSError, urllib.error.URLError):
                time.sleep(0.1)
        return False

    def _terminate(self, proc: ProcessInfo, *, timeout: float) -> None:
        """Politely then firmly terminate ``proc``."""
        if proc.pid is None or not _pid_alive(proc.pid):
            return
        try:
            if sys.platform.startswith("win"):
                proc_killed = subprocess.Popen(  # noqa: S603
                    ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                proc_killed.wait(timeout=timeout)
            else:
                os.killpg(proc.pid, signal.SIGTERM)
                deadline = time.time() + timeout
                while time.time() < deadline:
                    if not _pid_alive(proc.pid):
                        return
                    time.sleep(0.1)
                # Still alive after grace — escalate.
                os.killpg(proc.pid, signal.SIGKILL)
        except (OSError, ProcessLookupError):
            return

    # -- state helpers ------------------------------------------------------

    def _load_live_processes(self) -> list[ProcessInfo]:
        """Return the recorded children that are still alive."""
        state = self._state.load()
        procs: list[ProcessInfo] = []
        for label, raw in state.processes.items():
            kind = ProcessKind(raw.get("kind", "worker"))
            try:
                pid = int(raw["pid"]) if raw.get("pid") else None
            except (TypeError, ValueError):
                pid = None
            procs.append(
                ProcessInfo(
                    kind=kind,
                    label=label,
                    pid=pid,
                    cmd=list(raw.get("cmd", [])),
                    log_path=Path(raw["log_path"]) if raw.get("log_path") else None,
                    started_at=float(raw.get("started_at", time.time())),
                    extra=dict(raw.get("extra", {})),
                )
            )
        return [p for p in procs if p.pid is None or _pid_alive(p.pid)]

    def _find(self, procs: list[ProcessInfo], kind: ProcessKind) -> ProcessInfo | None:
        for p in procs:
            if p.kind is kind:
                return p
        return None

    def _proc_to_dict(self, proc: ProcessInfo) -> dict:
        return {
            "kind": proc.kind.value,
            "pid": proc.pid,
            "cmd": list(proc.cmd),
            "log_path": str(proc.log_path) if proc.log_path else None,
            "started_at": proc.started_at,
            "extra": dict(proc.extra),
        }

    # -- kernel-side introspection -----------------------------------------

    def _kernel_host(self, config: StartupConfig | None) -> str:
        if config is not None and config.kernel_port:
            return "127.0.0.1"
        return "127.0.0.1"

    def _dashboard_host(self, config: StartupConfig | None) -> str:
        return "127.0.0.1"

    def _peek_kernel_stats(
        self,
        kernel: ProcessInfo | None,
        config: StartupConfig | None,
    ) -> tuple[int, int]:
        """Best-effort peek at the kernel's ``/metrics`` endpoint.

        Returns ``(agents_registered, workflows_active)`` — both
        default to 0 when the kernel is unreachable.  We don't raise:
        a missing kernel is a perfectly legitimate state for a fresh
        ``openswarm status`` (the user hasn't started it yet).
        """
        if kernel is None or not _pid_alive(kernel.pid or 0):
            return (0, 0)
        import urllib.request

        port = int(kernel.extra.get("port", "8765"))
        url = f"http://127.0.0.1:{port}/metrics"
        try:
            with urllib.request.urlopen(url, timeout=1.0) as resp:  # noqa: S310
                payload = json.loads(resp.read().decode("utf-8"))
        except Exception:  # noqa: BLE001
            return (0, 0)
        reg = payload.get("registry_agent_count", 0) or 0
        # The kernel doesn't track workflows yet; report queue_total
        # as a proxy.  Real workflow counts come from the dashboard.
        wf = payload.get("queue_total", 0) or 0
        return (int(reg), int(wf))

    def _seek_to_tail(self, path: Path, lines: int) -> int:
        """Return the byte offset of the start of the last ``lines`` lines."""
        try:
            size = path.stat().st_size
            if size == 0:
                return 0
            with path.open("rb") as f:
                # Walk backwards in 4KB blocks counting newlines.
                block = 4096
                offset = size
                newline_count = 0
                while offset > 0 and newline_count < lines + 1:
                    read = min(block, offset)
                    offset -= read
                    f.seek(offset)
                    data = f.read(read)
                    newline_count += data.count(b"\n")
                return offset
        except OSError:
            return 0


__all__ = [
    "DEFAULT_LOG_DIRNAME",
    "DEFAULT_STATE_FILE",
    "PortInUseError",
    "ProcessInfo",
    "ProcessManager",
    "ProcessManagerError",
    "SHUTDOWN_GRACE_SECONDS",
]
