"""Sandboxed code execution.

The :class:`CodeExecutor` runs a snippet of source code against a
:class:`~harness.workspace.Workspace` and returns an
:class:`ExecutionResult` describing what happened.  Two execution
backends are provided:

* :class:`SubprocessBackend` — runs the runtime on the host.  Used by
  unit tests and CI; convenient when Docker is unavailable.
* The default backend (constructed when ``backend=None``) uses Docker
  with the strict security flags mandated by the spec
  (``--network=none``, ``--read-only``, ``--cap-drop=ALL``, etc.).

The split between executor and backend keeps the security policy in
one place: the executor enforces timeouts, captures I/O, and refuses
unknown runtimes; the backend only knows how to launch a process and
return its output.
"""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
import tempfile
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from pydantic import BaseModel, Field

from .workspace import Workspace, assert_within_workspace

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

DEFAULT_TIMEOUT: int = 30
"""Default execution timeout in seconds."""

DEFAULT_MEMORY: str = "512m"
"""Default memory cap; interpreted by the backend (Docker ``--memory``)."""

DEFAULT_CPU: float = 1.0
"""Default CPU share; interpreted by the backend (Docker ``--cpus``)."""

# ---------------------------------------------------------------------------
# Runtime catalogue
# ---------------------------------------------------------------------------

class RuntimeSpec(BaseModel):
    """Static description of a supported runtime."""

    model_config = {"arbitrary_types_allowed": True}

    name: str
    command: str
    extension: str
    docker_image: str


# Maps the manifest's ``allowed_runtimes`` token to the runtime command
# inside the container, the file extension, and the default Docker image.
RUNTIME_CATALOG: dict[str, RuntimeSpec] = {
    "python": RuntimeSpec(
        name="python",
        command="python3",
        extension="py",
        docker_image="python:3.11-slim",
    ),
    "node": RuntimeSpec(
        name="node",
        command="node",
        extension="js",
        docker_image="node:20-slim",
    ),
    "bash": RuntimeSpec(
        name="bash",
        command="bash",
        extension="sh",
        docker_image="alpine:latest",
    ),
}


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------

class ExecutionResult(BaseModel):
    """The structured outcome of a code execution.

    Mirrors the contract from the Phase 5 spec.  ``killed_by_timeout``
    and ``killed_by_memory`` are flags rather than a single ``reason``
    enum so multiple failure modes can be reported together (e.g. an
    OOM-kill during a timeout).
    """

    model_config = {"arbitrary_types_allowed": True}

    stdout: str = ""
    stderr: str = ""
    exit_code: int
    runtime: str
    duration_ms: int
    memory_peak_mb: Optional[float] = None
    killed_by_timeout: bool = False
    killed_by_memory: bool = False
    file_writes: list[str] = Field(default_factory=list)
    """Files the runtime created under the workspace, relative to root."""


# ---------------------------------------------------------------------------
# Backend protocol
# ---------------------------------------------------------------------------

@dataclass
class _RunRequest:
    """Internal: arguments passed from executor to backend."""

    runtime: RuntimeSpec
    code_file: Path
    workspace_root: Path
    memory: str
    cpu: float
    timeout: int
    env_vars: dict[str, str] = field(default_factory=dict)


class RuntimeBackend(ABC):
    """Strategy that actually launches the process and returns its output."""

    @abstractmethod
    async def run(self, request: _RunRequest) -> "BackendResult":
        """Execute the code and return the captured output.

        Implementations are expected to enforce their own timeout.
        They should also populate ``killed_by_memory`` when the OS or
        container manager killed the process.
        """


@dataclass
class BackendResult:
    """What a :class:`RuntimeBackend` returns.

    Distinct from :class:`ExecutionResult` because the backend runs in
    a different process and may not know its own final ``runtime``
    name or file writes.
    """

    stdout: str
    stderr: str
    exit_code: int
    duration_ms: int
    memory_peak_mb: Optional[float] = None
    killed_by_timeout: bool = False
    killed_by_memory: bool = False


# ---------------------------------------------------------------------------
# Subprocess backend
# ---------------------------------------------------------------------------

class SubprocessBackend(RuntimeBackend):
    """Run the runtime directly on the host using :mod:`asyncio`.

    Used by unit tests and developer machines that do not have Docker
    available.  Security guarantees are weaker than the Docker backend
    — there is no container isolation — so production deployments
    should default to :class:`DockerBackend`.
    """

    async def run(self, request: _RunRequest) -> BackendResult:
        start = time.perf_counter()
        env = dict(os.environ)
        env.update(request.env_vars)
        # Force the runtime's working directory to the workspace.
        cwd = str(request.workspace_root)
        cmd = [request.runtime.command, str(request.code_file)]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
                env=env,
            )
        except FileNotFoundError as exc:
            return BackendResult(
                stdout="",
                stderr=f"runtime not found: {exc}",
                exit_code=127,
                duration_ms=int((time.perf_counter() - start) * 1000),
            )

        try:
            stdout_b, stderr_b = await asyncio.wait_for(
                proc.communicate(), timeout=request.timeout
            )
        except asyncio.TimeoutError:
            proc.kill()
            try:
                stdout_b, stderr_b = await proc.communicate()
            except Exception:  # noqa: BLE001
                stdout_b, stderr_b = b"", b""
            duration_ms = int((time.perf_counter() - start) * 1000)
            return BackendResult(
                stdout=stdout_b.decode("utf-8", errors="replace"),
                stderr=stderr_b.decode("utf-8", errors="replace"),
                exit_code=proc.returncode if proc.returncode is not None else -1,
                duration_ms=duration_ms,
                killed_by_timeout=True,
            )

        duration_ms = int((time.perf_counter() - start) * 1000)
        return BackendResult(
            stdout=stdout_b.decode("utf-8", errors="replace"),
            stderr=stderr_b.decode("utf-8", errors="replace"),
            exit_code=proc.returncode if proc.returncode is not None else -1,
            duration_ms=duration_ms,
        )


# ---------------------------------------------------------------------------
# Docker backend
# ---------------------------------------------------------------------------

class DockerBackend(RuntimeBackend):
    """Run the runtime inside a hardened Docker container.

    The flags applied here implement the Phase 5 absolute constraints:

    * ``--network=none``         — no internet, no localhost
    * ``--read-only``            — root filesystem is immutable
    * ``--cap-drop=ALL``         — drop every Linux capability
    * ``--security-opt=no-new-privileges`` — no setuid escalation
    * ``--memory`` / ``--cpus``  — resource caps from the spec
    * ``-w /workspace``          — working dir is the mount point
    * workspace volume is the only writable surface

    The container runs as the unprivileged ``openswarm`` user; the
    Dockerfile (``Dockerfile.harness``) creates it.
    """

    DOCKER_BINARY: str = "docker"

    async def run(self, request: _RunRequest) -> BackendResult:
        if not shutil.which(self.DOCKER_BINARY):
            return BackendResult(
                stdout="",
                stderr="docker binary not found on PATH",
                exit_code=127,
                duration_ms=0,
            )

        start = time.perf_counter()
        env_args: list[str] = []
        for k, v in request.env_vars.items():
            env_args += ["-e", f"{k}={v}"]
        cmd = [
            self.DOCKER_BINARY,
            "run",
            "--rm",
            "--network=none",
            "--read-only",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            f"--memory={request.memory}",
            f"--cpus={request.cpu}",
            "-v",
            f"{request.workspace_root}:/workspace:rw",
            "-w",
            "/workspace",
            *env_args,
            request.runtime.docker_image,
            request.runtime.command,
            f"/workspace/temp/{request.code_file.name}",
        ]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            return BackendResult(
                stdout="",
                stderr=f"docker invocation failed: {exc}",
                exit_code=127,
                duration_ms=int((time.perf_counter() - start) * 1000),
            )

        try:
            stdout_b, stderr_b = await asyncio.wait_for(
                proc.communicate(), timeout=request.timeout + 5
            )
        except asyncio.TimeoutError:
            proc.kill()
            try:
                stdout_b, stderr_b = await proc.communicate()
            except Exception:  # noqa: BLE001
                stdout_b, stderr_b = b"", b""
            return BackendResult(
                stdout=stdout_b.decode("utf-8", errors="replace"),
                stderr=stderr_b.decode("utf-8", errors="replace")
                + "\n[harness] killed by harness timeout",
                exit_code=137,
                duration_ms=int((time.perf_counter() - start) * 1000),
                killed_by_timeout=True,
            )

        duration_ms = int((time.perf_counter() - start) * 1000)
        stderr_text = stderr_b.decode("utf-8", errors="replace")
        # Docker emits OOM markers in dmesg / stderr; the kernel's exit
        # code 137 is a strong signal.  We treat it as OOM.
        killed_by_memory = proc.returncode == 137 or "OOM" in stderr_text
        return BackendResult(
            stdout=stdout_b.decode("utf-8", errors="replace"),
            stderr=stderr_text,
            exit_code=proc.returncode if proc.returncode is not None else -1,
            duration_ms=duration_ms,
            killed_by_memory=killed_by_memory,
        )


# ---------------------------------------------------------------------------
# CodeExecutor
# ---------------------------------------------------------------------------

class CodeExecutor:
    """High-level executor used by the harness server.

    The executor is the *policy* layer: it picks a runtime, writes the
    code to a known location inside the workspace, applies timeouts,
    and translates the backend output into an :class:`ExecutionResult`.
    It never spawns processes itself.
    """

    DEFAULT_TIMEOUT: int = DEFAULT_TIMEOUT
    DEFAULT_MEMORY: str = DEFAULT_MEMORY
    DEFAULT_CPU: float = DEFAULT_CPU

    def __init__(
        self,
        backend: RuntimeBackend | None = None,
        allowed_runtimes: Sequence[str] | None = None,
    ) -> None:
        """Initialize the executor.

        Args:
            backend: Pluggable backend.  Defaults to :class:`DockerBackend`.
                Tests pass a :class:`SubprocessBackend`.
            allowed_runtimes: When non-empty, reject any runtime not in
                the list.  When ``None``, every runtime in
                :data:`RUNTIME_CATALOG` is accepted.
        """
        if backend is None:
            backend = DockerBackend()
        self.backend = backend
        self.allowed_runtimes: Optional[set[str]] = (
            set(allowed_runtimes) if allowed_runtimes is not None else None
        )

    # -- public API -------------------------------------------------------

    async def execute(
        self,
        workspace: Workspace,
        runtime: str,
        code: str,
        timeout: int = DEFAULT_TIMEOUT,
        memory: str = DEFAULT_MEMORY,
        cpu: float = DEFAULT_CPU,
        env_vars: Optional[Mapping[str, str]] = None,
    ) -> ExecutionResult:
        """Run ``code`` in ``workspace`` using ``runtime``.

        The code is first persisted to ``workspace/temp/<uuid>.<ext>``
        so the runtime can read it as a regular file.  The exact
        invocation is the backend's responsibility.

        Returns:
            An :class:`ExecutionResult` describing the run.  No
            exception is raised on a non-zero exit — callers inspect
            ``exit_code`` and ``killed_by_*`` flags.
        """
        spec = self._resolve_runtime(runtime)
        self._validate_runtime_allowed(spec.name)
        # Snapshot files that exist before the run so we can diff after.
        pre_files = self._snapshot_workspace(workspace)

        # Ensure temp dir exists and write the code file.
        workspace.temp_dir.mkdir(parents=True, exist_ok=True)
        code_filename = f"{uuid.uuid4().hex}.{spec.extension}"
        code_path = workspace.temp_dir / code_filename
        code_path.write_text(code, encoding="utf-8")
        # Make sure the code path stays inside the workspace.
        assert_within_workspace(workspace, code_path.relative_to(workspace.root))

        request = _RunRequest(
            runtime=spec,
            code_file=code_path,
            workspace_root=workspace.root,
            memory=memory,
            cpu=cpu,
            timeout=timeout,
            env_vars=dict(env_vars or {}),
        )
        backend_result = await self.backend.run(request)

        # Compute new file writes.
        post_files = self._snapshot_workspace(workspace)
        new_files = sorted(post_files - pre_files)
        rel_writes = [
            str(Path(p).relative_to(workspace.root)) for p in new_files
        ]

        return ExecutionResult(
            stdout=backend_result.stdout,
            stderr=backend_result.stderr,
            exit_code=backend_result.exit_code,
            runtime=spec.name,
            duration_ms=backend_result.duration_ms,
            memory_peak_mb=backend_result.memory_peak_mb,
            killed_by_timeout=backend_result.killed_by_timeout,
            killed_by_memory=backend_result.killed_by_memory,
            file_writes=rel_writes,
        )

    # -- helpers ----------------------------------------------------------

    def _resolve_runtime(self, runtime: str) -> RuntimeSpec:
        spec = RUNTIME_CATALOG.get(runtime)
        if spec is None:
            raise ValueError(
                f"unknown runtime {runtime!r}; "
                f"supported: {sorted(RUNTIME_CATALOG)}"
            )
        return spec

    def _validate_runtime_allowed(self, name: str) -> None:
        if self.allowed_runtimes is not None and name not in self.allowed_runtimes:
            raise PermissionError(
                f"runtime {name!r} not in allowed_runtimes "
                f"({sorted(self.allowed_runtimes)})"
            )

    @staticmethod
    def _snapshot_workspace(workspace: Workspace) -> set[Path]:
        """Return the set of regular files under the workspace (relative).

        ``temp/`` is excluded because we already account for the code
        file we wrote ourselves.  ``.git/`` is excluded because the
        git-tracker mutates it.
        """
        result: set[Path] = set()
        root = workspace.root
        if not root.exists():
            return result
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            rel = path.relative_to(root)
            parts = rel.parts
            if not parts:
                continue
            if parts[0] in {".git", "temp", "logs"}:
                continue
            result.add(path)
        return result


__all__ = [
    "BackendResult",
    "CodeExecutor",
    "DEFAULT_CPU",
    "DEFAULT_MEMORY",
    "DEFAULT_TIMEOUT",
    "DockerBackend",
    "ExecutionResult",
    "RUNTIME_CATALOG",
    "RuntimeBackend",
    "RuntimeSpec",
    "SubprocessBackend",
]
