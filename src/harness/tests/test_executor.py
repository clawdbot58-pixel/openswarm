"""Tests for CodeExecutor and its backends."""
from __future__ import annotations

import asyncio
import shutil
import time
from pathlib import Path

import pytest

from harness.executor import (
    BackendResult,
    CodeExecutor,
    DockerBackend,
    ExecutionResult,
    RUNTIME_CATALOG,
    RuntimeBackend,
    SubprocessBackend,
)
from harness.workspace import Workspace, WorkspaceManager


def asyncio_run(coro):
    """Run an awaitable in a fresh event loop. Used in sync test bodies."""
    return asyncio.new_event_loop().run_until_complete(coro)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_workspace(mgr: WorkspaceManager, name: str = "wf-exec") -> Workspace:
    return mgr.create(name)


# ---------------------------------------------------------------------------
# Runtime catalogue
# ---------------------------------------------------------------------------

def test_runtime_catalog_has_expected_runtimes():
    assert set(RUNTIME_CATALOG) >= {"python", "node", "bash"}
    assert RUNTIME_CATALOG["python"].command == "python3"
    assert RUNTIME_CATALOG["python"].extension == "py"
    assert RUNTIME_CATALOG["node"].extension == "js"
    assert RUNTIME_CATALOG["bash"].extension == "sh"


def test_executor_rejects_unknown_runtime(workspace_manager):
    ws = _make_workspace(workspace_manager, "bad-runtime")
    ex = CodeExecutor(backend=SubprocessBackend())
    import asyncio

    async def run():
        return await ex.execute(ws, "ruby", "puts 'hi'")

    with pytest.raises(ValueError):
        asyncio.run(run())


def test_executor_respects_allowed_runtimes(workspace_manager):
    ws = _make_workspace(workspace_manager, "no-node")
    ex = CodeExecutor(
        backend=SubprocessBackend(),
        allowed_runtimes=["python"],  # bash forbidden
    )
    import asyncio

    async def run():
        return await ex.execute(ws, "bash", "echo hi")

    with pytest.raises(PermissionError):
        asyncio.run(run())


# ---------------------------------------------------------------------------
# SubprocessBackend: behaviour
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_subprocess_runs_python_print(workspace_manager):
    ws = _make_workspace(workspace_manager, "py-print")
    ex = CodeExecutor(backend=SubprocessBackend())
    result = await ex.execute(ws, "python", "print('hello world')")
    assert isinstance(result, ExecutionResult)
    assert result.exit_code == 0
    assert "hello world" in result.stdout
    assert result.runtime == "python"
    assert result.duration_ms >= 0


@pytest.mark.asyncio
async def test_subprocess_captures_syntax_error(workspace_manager):
    ws = _make_workspace(workspace_manager, "py-syntax")
    ex = CodeExecutor(backend=SubprocessBackend())
    result = await ex.execute(ws, "python", "this is not python(:")
    assert result.exit_code != 0
    assert result.stderr.strip()  # non-empty
    assert not result.killed_by_timeout
    assert not result.killed_by_memory


@pytest.mark.asyncio
async def test_subprocess_runs_bash(workspace_manager):
    ws = _make_workspace(workspace_manager, "bash-1")
    ex = CodeExecutor(backend=SubprocessBackend())
    result = await ex.execute(ws, "bash", "echo from-bash")
    assert result.exit_code == 0
    assert "from-bash" in result.stdout


@pytest.mark.asyncio
async def test_subprocess_runs_node(workspace_manager):
    if not shutil.which("node"):
        pytest.skip("node not installed on this host")
    ws = _make_workspace(workspace_manager, "node-1")
    ex = CodeExecutor(backend=SubprocessBackend())
    result = await ex.execute(ws, "node", "console.log('hi from node');")
    assert result.exit_code == 0
    assert "hi from node" in result.stdout
    assert result.runtime == "node"


@pytest.mark.asyncio
async def test_subprocess_timeout_kills_process(workspace_manager):
    ws = _make_workspace(workspace_manager, "py-timeout")
    ex = CodeExecutor(backend=SubprocessBackend())
    # Sleep for 5 seconds; allow only 1.
    start = time.perf_counter()
    result = await ex.execute(
        ws, "python", "import time; time.sleep(5)", timeout=1
    )
    elapsed = time.perf_counter() - start
    assert result.killed_by_timeout is True
    assert result.exit_code != 0
    # The process should have been killed long before the 5s sleep
    # would have completed.
    assert elapsed < 4.0


@pytest.mark.asyncio
async def test_subprocess_writes_are_tracked(workspace_manager):
    """Files the runtime writes under the workspace are reported."""
    ws = _make_workspace(workspace_manager, "py-write")
    ex = CodeExecutor(backend=SubprocessBackend())
    code = (
        "with open('output/result.txt', 'w') as f:\n"
        "    f.write('payload')\n"
    )
    result = await ex.execute(ws, "python", code)
    assert result.exit_code == 0
    assert any(p.endswith("result.txt") for p in result.file_writes)
    assert (ws.output_dir / "result.txt").read_text() == "payload"


@pytest.mark.asyncio
async def test_subprocess_env_vars_passed(workspace_manager):
    ws = _make_workspace(workspace_manager, "env-1")
    ex = CodeExecutor(backend=SubprocessBackend())
    code = (
        "import os\n"
        "print('VAL=' + os.environ.get('OPENSWARM_TEST_VAR', 'missing'))\n"
    )
    result = await ex.execute(
        ws, "python", code, env_vars={"OPENSWARM_TEST_VAR": "set-via-harness"}
    )
    assert result.exit_code == 0
    assert "VAL=set-via-harness" in result.stdout


# ---------------------------------------------------------------------------
# Custom backend (the spec's "pluggable" guarantee)
# ---------------------------------------------------------------------------

class _RecordingBackend(RuntimeBackend):
    """Backend that captures its request and returns a canned result."""

    def __init__(self) -> None:
        self.received = None
        self.calls = 0

    async def run(self, request) -> BackendResult:  # type: ignore[override]
        self.received = request
        self.calls += 1
        return BackendResult(
            stdout="ok",
            stderr="",
            exit_code=0,
            duration_ms=1,
        )


@pytest.mark.asyncio
async def test_executor_uses_pluggable_backend(workspace_manager):
    ws = _make_workspace(workspace_manager, "plug-1")
    backend = _RecordingBackend()
    ex = CodeExecutor(backend=backend)
    result = await ex.execute(ws, "python", "print('hi')")
    assert result.exit_code == 0
    assert result.stdout == "ok"
    assert backend.calls == 1
    assert backend.received is not None
    assert backend.received.runtime.name == "python"
    assert backend.received.workspace_root == ws.root


# ---------------------------------------------------------------------------
# DockerBackend: command construction & capability flags
# ---------------------------------------------------------------------------

def test_docker_backend_command_includes_security_flags(monkeypatch):
    """The constructed docker command applies every Phase 5 hardening flag."""
    backend = DockerBackend()
    captured: dict = {}

    class _StubProcess:
        returncode = 0

        async def communicate(self):
            return b"", b""

        def kill(self):
            pass

    async def fake_exec(*cmd, **kwargs):
        captured["cmd"] = list(cmd)
        return _StubProcess()

    monkeypatch.setattr(
        "harness.executor.asyncio.create_subprocess_exec", fake_exec
    )
    from harness.executor import _RunRequest, RuntimeSpec

    asyncio_run(
        backend.run(
            _RunRequest(
                runtime=RuntimeSpec(
                    name="python", command="python3",
                    extension="py", docker_image="python:3.11-slim",
                ),
                code_file=Path("/tmp/x.py"),
                workspace_root=Path("/tmp"),
                memory="256m",
                cpu=0.5,
                timeout=10,
            )
        )
    )
    cmd = captured["cmd"]
    for flag in (
        "--network=none",
        "--read-only",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "--memory=256m",
        "--cpus=0.5",
        "python:3.11-slim",
    ):
        assert flag in cmd, f"missing flag {flag!r} in {cmd}"


def test_docker_backend_mounts_workspace(monkeypatch):
    """The workspace root is bind-mounted at /workspace."""
    backend = DockerBackend()
    captured: dict = {}

    class _StubProcess:
        returncode = 0

        async def communicate(self):
            return b"", b""

        def kill(self):
            pass

    async def fake_exec(*cmd, **kwargs):
        captured["cmd"] = list(cmd)
        return _StubProcess()

    monkeypatch.setattr(
        "harness.executor.asyncio.create_subprocess_exec", fake_exec
    )
    from harness.executor import _RunRequest, RuntimeSpec

    asyncio_run(
        backend.run(
            _RunRequest(
                runtime=RuntimeSpec(
                    name="bash", command="bash",
                    extension="sh", docker_image="alpine:latest",
                ),
                code_file=Path("/workspace/temp/x.sh"),
                workspace_root=Path("/var/lib/wf1"),
                memory="512m",
                cpu=1.0,
                timeout=10,
            )
        )
    )
    cmd = captured["cmd"]
    for i, token in enumerate(cmd):
        if token == "-v" and i + 1 < len(cmd):
            mount = cmd[i + 1]
            assert mount == "/var/lib/wf1:/workspace:rw"
            break
    else:
        pytest.fail("no -v bind mount in docker command")


def test_subprocess_backend_uses_workspace_cwd(workspace_manager):
    """The runtime's CWD is the workspace root."""
    ws = _make_workspace(workspace_manager, "cwd-1")
    (ws.src_dir / "marker.txt").write_text("here")
    ex = CodeExecutor(backend=SubprocessBackend())
    code = "import os; print(os.getcwd())"
    result = asyncio_run(ex.execute(ws, "python", code))
    assert str(ws.root) in result.stdout


# ---------------------------------------------------------------------------
# DockerBackend: OOM detection + missing docker binary
# ---------------------------------------------------------------------------

def test_docker_backend_marks_oom_kill(monkeypatch):
    """When the container exits with 137 (OOM) the backend reports it."""
    backend = DockerBackend()

    class _OomProcess:
        returncode = 137

        async def communicate(self):
            return b"", b"Killed\n"

        def kill(self):
            pass

    async def fake_exec(*cmd, **kwargs):
        return _OomProcess()

    monkeypatch.setattr(
        "harness.executor.asyncio.create_subprocess_exec", fake_exec
    )
    from harness.executor import _RunRequest, RuntimeSpec

    result = asyncio_run(
        backend.run(
            _RunRequest(
                runtime=RuntimeSpec(
                    name="python", command="python3",
                    extension="py", docker_image="python:3.11-slim",
                ),
                code_file=Path("/tmp/x.py"),
                workspace_root=Path("/tmp"),
                memory="64m",
                cpu=1.0,
                timeout=10,
            )
        )
    )
    assert result.killed_by_memory is True
    assert result.exit_code == 137


def test_docker_backend_reports_missing_binary(monkeypatch):
    """When docker is not on PATH we return a 127-style error."""
    backend = DockerBackend()
    monkeypatch.setattr(backend, "DOCKER_BINARY", "/nonexistent/docker")
    from harness.executor import _RunRequest, RuntimeSpec

    result = asyncio_run(
        backend.run(
            _RunRequest(
                runtime=RuntimeSpec(
                    name="python", command="python3",
                    extension="py", docker_image="python:3.11-slim",
                ),
                code_file=Path("/tmp/x.py"),
                workspace_root=Path("/tmp"),
                memory="256m",
                cpu=1.0,
                timeout=10,
            )
        )
    )
    assert result.exit_code == 127
    assert "not found" in result.stderr


# ---------------------------------------------------------------------------
# Network disabled
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_subprocess_network_visibility_note(workspace_manager, tmp_path):
    """Document the local-backend network caveat.

    The SubprocessBackend runs on the host so it inherits the host's
    network access.  The Docker backend is the one that enforces
    ``--network=none``.  This test simply records the difference so
    the contract is visible in the test output.
    """
    ws = _make_workspace(workspace_manager, "net-doc-1")
    ex = CodeExecutor(backend=SubprocessBackend())
    # A trivial network-less call to confirm the backend is functional.
    result = await ex.execute(ws, "python", "print('ok')")
    assert result.exit_code == 0
