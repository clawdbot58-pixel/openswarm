"""FastAPI surface for the harness.

The server is the **only** path agents use to touch the filesystem or
run code.  Every endpoint:

1. Validates input shape (FastAPI / pydantic).
2. Resolves the workflow's workspace, creating it on demand.
3. Calls the right collaborator (executor, git, …) and commits any
   resulting file changes.
4. Emits a kernel event describing the action.

The :class:`HarnessServer` class is the FastAPI-independent core so
tests can drive it directly without spinning up uvicorn.  The
module-level :func:`create_app` returns the FastAPI app that wraps it.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, status
from pydantic import BaseModel, Field

from .diff_generator import DiffGenerator, KernelEventSink
from .executor import CodeExecutor, ExecutionResult, RuntimeBackend
from .git_tracker import GitTracker
from .workspace import Workspace, WorkspaceManager, assert_within_workspace

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------

class ExecRequest(BaseModel):
    """Body for ``POST /tools/exec``."""

    workflow_id: str
    runtime: str
    code: str
    timeout: int | None = Field(default=None, ge=1, le=600)
    memory: str | None = None
    cpu: float | None = Field(default=None, gt=0, le=8)
    env_vars: dict[str, str] = Field(default_factory=dict)
    agent_id: str = "harness"
    allowed_runtimes: list[str] | None = None


class WriteRequest(BaseModel):
    """Body for ``POST /tools/write``."""

    workflow_id: str
    path: str
    content: str
    agent_id: str = "harness"


class ReadRequest(BaseModel):
    """Query parameters for ``GET /tools/read``."""

    workflow_id: str
    path: str


class ListRequest(BaseModel):
    """Query parameters for ``GET /tools/list``."""

    workflow_id: str
    path: str = "."


class ResetRequest(BaseModel):
    """Body for ``POST /tools/reset``."""

    workflow_id: str
    commit_hash: str
    agent_id: str = "harness"


class HistoryRequest(BaseModel):
    workflow_id: str


class DiffRequest(BaseModel):
    workflow_id: str
    commit: str


class HealthResponse(BaseModel):
    status: str = "ok"
    workspaces: int
    git_binary: str
    default_runtime: str


# ---------------------------------------------------------------------------
# HarnessServer
# ---------------------------------------------------------------------------

class HarnessServer:
    """The harness core, FastAPI-agnostic.

    A :class:`HarnessServer` bundles the four collaborators the spec
    calls out:

    * :class:`WorkspaceManager` — workflow directories
    * :class:`CodeExecutor`     — sandboxed execution
    * :class:`GitTracker`       — auto-commit + history
    * :class:`DiffGenerator`    — event emission

    It also tracks a permission policy (``default_allowed_runtimes``)
    that is consulted whenever a request does not bring its own
    ``allowed_runtimes`` list.
    """

    DEFAULT_ALLOWED_RUNTIMES: tuple[str, ...] = ("python", "node", "bash")
    """Fallback policy when a request does not pass its own list."""

    RESET_ROLES: frozenset[str] = frozenset({"orchestrator", "meta", "kernel"})
    """Endpoint roles allowed to invoke ``harness_reset``."""

    def __init__(
        self,
        workspace_manager: WorkspaceManager,
        executor: CodeExecutor,
        git_tracker: GitTracker,
        diff_generator: DiffGenerator,
        default_allowed_runtimes: list[str] | None = None,
    ) -> None:
        """Initialize the server.

        Args:
            workspace_manager: Manages workflow directories.
            executor: Runs code in a sandbox.
            git_tracker: Auto-commit helper.
            diff_generator: Emits diff events to the kernel.
            default_allowed_runtimes: Runtime allowlist used when a
                request does not pass its own.
        """
        self.workspaces = workspace_manager
        self.executor = executor
        self.git = git_tracker
        self.diff = diff_generator
        self.default_allowed_runtimes = list(
            default_allowed_runtimes or self.DEFAULT_ALLOWED_RUNTIMES
        )
        # Track which agent is allowed to call ``harness_reset``.
        self._reset_allowlist: set[str] = set()

    # -- role bookkeeping -------------------------------------------------

    def allow_reset(self, agent_id: str) -> None:
        """Whitelist ``agent_id`` for ``harness_reset`` calls."""
        self._reset_allowlist.add(agent_id)

    def revoke_reset(self, agent_id: str) -> None:
        """Remove ``agent_id`` from the ``harness_reset`` allowlist."""
        self._reset_allowlist.discard(agent_id)

    # -- handler methods (also reachable via HTTP) ------------------------

    async def health(self) -> dict[str, Any]:
        """Return the liveness payload used by ``GET /health``."""
        return {
            "status": "ok",
            "workspaces": len(self.workspaces.list_active()),
            "git_binary": self.git.git_binary,
            "default_runtime": self.executor.backend.__class__.__name__,
            "default_allowed_runtimes": self.default_allowed_runtimes,
        }

    async def handle_tool_exec(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Execute code.  Mirrors the ``harness_exec`` envelope."""
        request = ExecRequest.model_validate(payload)
        workspace = self._workspace_or_404(request.workflow_id)
        allowed = self._runtime_policy(request.allowed_runtimes)
        if request.runtime not in allowed:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"runtime {request.runtime!r} not in allowed list",
            )
        try:
            result: ExecutionResult = await self.executor.execute(
                workspace,
                request.runtime,
                request.code,
                timeout=request.timeout or self.executor.DEFAULT_TIMEOUT,
                memory=request.memory or self.executor.DEFAULT_MEMORY,
                cpu=request.cpu or self.executor.DEFAULT_CPU,
                env_vars=request.env_vars,
            )
        except (ValueError, PermissionError) as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
            ) from exc

        # Emit per-file diffs BEFORE the commit so the diff is computed
        # against the previous HEAD.
        for path in result.file_writes:
            await self.diff.on_file_change(workspace, request.agent_id, path)
        commit_info = self._commit_after(workspace, request.agent_id, f"exec:{request.runtime}")
        await self.diff.on_execution_complete(workspace, result, agent_id=request.agent_id)
        await self.diff.on_commit(workspace, commit_info)
        return {
            "ok": True,
            "execution": result.model_dump(mode="json"),
            "commit": commit_info.model_dump(mode="json"),
        }

    async def handle_tool_write(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Write a file to the workspace and commit."""
        request = WriteRequest.model_validate(payload)
        workspace = self._workspace_or_404(request.workflow_id)
        safe_path = self._safe_path_or_400(workspace, request.path)
        safe_path.parent.mkdir(parents=True, exist_ok=True)
        safe_path.write_text(request.content, encoding="utf-8")
        # Emit the diff BEFORE the commit so the previous HEAD is the
        # baseline.
        await self.diff.on_file_change(workspace, request.agent_id, request.path)
        commit_info = self._commit_after(
            workspace, request.agent_id, f"write:{request.path}"
        )
        await self.diff.on_commit(workspace, commit_info)
        return {
            "ok": True,
            "path": request.path,
            "bytes": len(request.content.encode("utf-8")),
            "commit": commit_info.model_dump(mode="json"),
        }

    async def handle_tool_read(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Read a file from the workspace."""
        request = ReadRequest.model_validate(payload)
        workspace = self._workspace_or_404(request.workflow_id)
        safe_path = self._safe_path_or_400(workspace, request.path)
        if not safe_path.exists() or not safe_path.is_file():
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"file not found: {request.path}",
            )
        try:
            content = safe_path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            content = safe_path.read_text(encoding="utf-8", errors="replace")
        return {
            "ok": True,
            "path": request.path,
            "content": content,
            "bytes": safe_path.stat().st_size,
        }

    async def handle_tool_list(self, payload: dict[str, Any]) -> dict[str, Any]:
        """List files under a workspace path."""
        request = ListRequest.model_validate(payload)
        workspace = self._workspace_or_404(request.workflow_id)
        safe_path = self._safe_path_or_400(workspace, request.path)
        if not safe_path.exists():
            return {"ok": True, "path": request.path, "entries": []}
        entries: list[dict[str, Any]] = []
        for child in sorted(safe_path.iterdir()):
            if child.name == ".git":
                continue
            entries.append(
                {
                    "name": child.name,
                    "is_dir": child.is_dir(),
                    "size": child.stat().st_size if child.is_file() else 0,
                    "path": str(child.relative_to(workspace.root)),
                }
            )
        return {
            "ok": True,
            "path": request.path,
            "entries": entries,
        }

    async def handle_tool_reset(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Reset the workspace to ``commit_hash``."""
        request = ResetRequest.model_validate(payload)
        if not self._is_reset_authorized(request.agent_id):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"agent {request.agent_id!r} not authorized to reset workspaces",
            )
        workspace = self._workspace_or_404(request.workflow_id)
        try:
            self.git.reset_to_commit(workspace, request.commit_hash)
        except (ValueError, subprocess_called_process_error()) as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"reset failed: {exc}",
            ) from exc
        commit_info = self._commit_after(workspace, request.agent_id, f"reset:{request.commit_hash[:8]}")
        await self.diff.on_commit(workspace, commit_info)
        return {
            "ok": True,
            "workflow_id": request.workflow_id,
            "reset_to": request.commit_hash,
            "commit": commit_info.model_dump(mode="json"),
        }

    async def handle_tool_history(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Return the workspace's commit history."""
        request = HistoryRequest.model_validate(payload)
        workspace = self._workspace_or_404(request.workflow_id)
        history = self.git.get_history(workspace)
        return {
            "ok": True,
            "workflow_id": request.workflow_id,
            "commits": [c.model_dump(mode="json") for c in history],
        }

    async def handle_tool_diff(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Return the diff for a commit."""
        request = DiffRequest.model_validate(payload)
        workspace = self._workspace_or_404(request.workflow_id)
        try:
            diff = self.git.get_diff(workspace, request.commit)
        except (ValueError, subprocess_called_process_error()) as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"diff failed: {exc}",
            ) from exc
        return {
            "ok": True,
            "workflow_id": request.workflow_id,
            "commit": request.commit,
            "diff": diff,
        }

    # -- envelope entry point --------------------------------------------

    async def handle_envelope(self, envelope_dict: dict[str, Any]) -> dict[str, Any]:
        """Process a kernel envelope dict and return a response dict.

        The envelope's ``payload.content_type`` must be ``"tool"`` and
        ``payload.tool_name`` must be one of the registered harness
        tools.  ``payload.action`` is currently ignored — the harness
        treats every call as ``invoke``.
        """
        payload = envelope_dict.get("payload", {})
        if not isinstance(payload, dict) or payload.get("content_type") != "tool":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="envelope payload must be of content_type=tool",
            )
        tool_name = payload.get("tool_name")
        params = payload.get("parameters", {}) or {}
        # The workflow_id is part of the tool params, not the envelope.
        params.setdefault("agent_id", envelope_dict.get("sender", {}).get("agent_id", "harness"))
        if tool_name == "harness_exec":
            return await self.handle_tool_exec(params)
        if tool_name == "harness_write_file":
            return await self.handle_tool_write(params)
        if tool_name == "harness_read_file":
            return await self.handle_tool_read(params)
        if tool_name == "harness_list_files":
            return await self.handle_tool_list(params)
        if tool_name == "harness_reset":
            return await self.handle_tool_reset(params)
        if tool_name == "harness_get_history":
            return await self.handle_tool_history(params)
        if tool_name == "harness_get_diff":
            return await self.handle_tool_diff(params)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"unsupported harness tool: {tool_name!r}",
        )

    # -- helpers ---------------------------------------------------------

    def _workspace_or_404(self, workflow_id: str) -> Workspace:
        ws = self.workspaces.get_or_create(workflow_id)
        # Ensure the git repo is initialised the first time the
        # workspace is touched.  This makes every other collaborator
        # happy without having to repeat the call.
        try:
            self.git.init_repo(ws)
        except FileNotFoundError:
            # git binary missing — surface as a 500.
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="git binary not available on this host",
            )
        return ws

    def _safe_path_or_400(self, workspace: Workspace, path: str) -> Path:
        try:
            return assert_within_workspace(workspace, path)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
            ) from exc

    def _runtime_policy(self, request_list: list[str] | None) -> set[str]:
        if request_list is not None:
            return set(request_list)
        return set(self.default_allowed_runtimes)

    def _is_reset_authorized(self, agent_id: str) -> bool:
        if agent_id in self._reset_allowlist:
            return True
        # Agents whose role is orchestrator / kernel / meta can always
        # reset.  In Phase 5 we accept a single string agent_id, so we
        # treat the well-known orchestrator ids as authorized.
        return agent_id in {"main-agent", "kernel", "conductor", "meta-agent"}

    def _commit_after(
        self,
        workspace: Workspace,
        agent_id: str,
        message: str,
    ):
        return self.git.commit(workspace, agent_id, message=message)


def subprocess_called_process_error() -> type[BaseException]:
    import subprocess

    return subprocess.CalledProcessError


# ---------------------------------------------------------------------------
# FastAPI app factory
# ---------------------------------------------------------------------------

def create_app(
    workspace_manager: WorkspaceManager | None = None,
    executor: CodeExecutor | None = None,
    git_tracker: GitTracker | None = None,
    diff_generator: DiffGenerator | None = None,
    kernel_sink: KernelEventSink | None = None,
    backend: RuntimeBackend | None = None,
) -> FastAPI:
    """Build a fully-wired FastAPI harness app.

    Tests pass in collaborators; production code uses the defaults
    (DockerBackend + on-disk workspaces + null kernel sink — wire a
    real sink from the kernel's :class:`~kernel.bus.MessageBus.emit_event`).
    """
    workspaces = workspace_manager or WorkspaceManager(
        base_dir=os.environ.get("OPENSWARM_WORKSPACE_DIR")
    )
    backend = backend or _default_backend()
    code_executor = executor or CodeExecutor(backend=backend)
    git_tracker = git_tracker or GitTracker()
    diff_generator = diff_generator or DiffGenerator(
        kernel_ws_client=kernel_sink, git_tracker=git_tracker
    )
    server = HarnessServer(
        workspace_manager=workspaces,
        executor=code_executor,
        git_tracker=git_tracker,
        diff_generator=diff_generator,
    )

    app = FastAPI(
        title="OpenSwarm Harness",
        version="0.1.0",
        description=(
            "Phase 5: sandboxed execution, git-tracked workspaces, "
            "diff streaming to the kernel."
        ),
    )
    app.state.server = server
    app.state.workspaces = workspaces
    app.state.executor = code_executor
    app.state.git_tracker = git_tracker
    app.state.diff_generator = diff_generator

    @app.get("/", include_in_schema=False)
    async def root() -> dict[str, str]:
        return {"service": "openswarm-harness", "phase": "5"}

    @app.get("/health", response_model=HealthResponse)
    async def health() -> dict[str, Any]:
        return await server.health()

    @app.post("/tools/exec")
    async def tools_exec(payload: ExecRequest) -> dict[str, Any]:
        return await server.handle_tool_exec(payload.model_dump())

    @app.post("/tools/write")
    async def tools_write(payload: WriteRequest) -> dict[str, Any]:
        return await server.handle_tool_write(payload.model_dump())

    @app.get("/tools/read")
    async def tools_read(workflow_id: str, path: str) -> dict[str, Any]:
        return await server.handle_tool_read(
            {"workflow_id": workflow_id, "path": path}
        )

    @app.get("/tools/list")
    async def tools_list(workflow_id: str, path: str = ".") -> dict[str, Any]:
        return await server.handle_tool_list(
            {"workflow_id": workflow_id, "path": path}
        )

    @app.post("/tools/reset")
    async def tools_reset(payload: ResetRequest) -> dict[str, Any]:
        return await server.handle_tool_reset(payload.model_dump())

    @app.get("/tools/history")
    async def tools_history(workflow_id: str) -> dict[str, Any]:
        return await server.handle_tool_history({"workflow_id": workflow_id})

    @app.get("/tools/diff")
    async def tools_diff(workflow_id: str, commit: str) -> dict[str, Any]:
        return await server.handle_tool_diff(
            {"workflow_id": workflow_id, "commit": commit}
        )

    @app.post("/envelope")
    async def tools_envelope(envelope: dict[str, Any]) -> dict[str, Any]:
        return await server.handle_envelope(envelope)

    return app


def _default_backend() -> RuntimeBackend:
    """Pick the runtime backend based on environment.

    ``OPENSWARM_HARNESS_BACKEND=docker|local`` overrides the default.
    ``local`` selects :class:`~harness.executor.SubprocessBackend` and
    is used by the integration test suite.
    """
    choice = os.environ.get("OPENSWARM_HARNESS_BACKEND", "docker").lower()
    if choice == "local" or choice == "subprocess":
        from .executor import SubprocessBackend

        return SubprocessBackend()
    from .executor import DockerBackend

    return DockerBackend()


__all__ = [
    "DiffRequest",
    "ExecRequest",
    "HarnessServer",
    "HealthResponse",
    "HistoryRequest",
    "ListRequest",
    "ReadRequest",
    "ResetRequest",
    "WriteRequest",
    "create_app",
]
