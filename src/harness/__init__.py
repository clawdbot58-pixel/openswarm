"""OpenSwarm Coding Harness (Phase 5).

Sandboxed execution environment for code-generating agents.  Every
workflow gets its own workspace, every file write is tracked by git,
every code execution runs in an isolated container (or a configurable
backend for tests).  All agent ↔ filesystem and agent ↔ interpreter
contact goes through this package.

Modules
-------
* :mod:`harness.workspace`      — workflow-scoped directories + git init.
* :mod:`harness.executor`       — pluggable code runner (Docker default).
* :mod:`harness.git_tracker`    — auto-commit / history / diff / reset.
* :mod:`harness.diff_generator` — diff emission to the kernel bus.
* :mod:`harness.server`         — FastAPI surface that agents call into.
* :mod:`harness.client`         — async HTTP client used by agent workers.

The :class:`ToolExecutor` in :mod:`loops.tool_executor` is the public
seam: agent code calls ``await ToolExecutor(...).execute(...)`` and the
executor dispatches to harness tools when the manifest allows.
"""
from __future__ import annotations

from .client import HarnessClient, HarnessError, InProcessHarnessClient
from .diff_generator import DiffGenerator, KernelEventSink, RecordingSink
from .executor import (
    DEFAULT_CPU,
    DEFAULT_MEMORY,
    DEFAULT_TIMEOUT,
    CodeExecutor,
    ExecutionResult,
    RuntimeBackend,
    RuntimeSpec,
    SubprocessBackend,
)
from .git_tracker import CommitInfo, GitTracker
from .server import HarnessServer, create_app
from .workspace import Workspace, WorkspaceManager, assert_within_workspace

__all__ = [
    "CodeExecutor",
    "CommitInfo",
    "DEFAULT_CPU",
    "DEFAULT_MEMORY",
    "DEFAULT_TIMEOUT",
    "DiffGenerator",
    "ExecutionResult",
    "GitTracker",
    "HarnessClient",
    "HarnessError",
    "HarnessServer",
    "InProcessHarnessClient",
    "KernelEventSink",
    "RecordingSink",
    "RuntimeBackend",
    "RuntimeSpec",
    "SubprocessBackend",
    "Workspace",
    "WorkspaceManager",
    "assert_within_workspace",
    "create_app",
]
