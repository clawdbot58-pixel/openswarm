"""Diff emission to the kernel bus.

When the harness writes a file or finishes a code execution it asks
:class:`DiffGenerator` to compute the resulting diff and forward it as
a kernel event.  The dashboard subscribes to that event stream to
populate the workspace explorer and live-diff views.

The generator is intentionally decoupled from the bus: it talks to a
:class:`KernelEventSink` so tests can plug in a recorder without
spinning up the real kernel.
"""
from __future__ import annotations

import asyncio
import difflib
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, Protocol

from pydantic import BaseModel, Field

from .executor import ExecutionResult
from .git_tracker import CommitInfo, GitTracker
from .workspace import Workspace, assert_within_workspace

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Kernel sink protocol
# ---------------------------------------------------------------------------

class KernelEventSink(Protocol):
    """Anything that can accept a kernel ``event`` envelope.

    The kernel's :class:`~kernel.bus.MessageBus.emit_event` matches
    this signature, so the bus itself can be passed in.  Tests use a
    recorder that just stores the envelopes.
    """

    async def emit_event(
        self,
        event_type: str,
        details: dict[str, Any] | None = None,
        *,
        recipient: str = "main-agent",
        sender: Any | None = None,
    ) -> Any:
        ...


class _NullSink:
    """Default no-op sink.  Swallows events silently."""

    async def emit_event(
        self,
        event_type: str,
        details: dict[str, Any] | None = None,
        *,
        recipient: str = "main-agent",
        sender: Any | None = None,
    ) -> None:
        return None


# ---------------------------------------------------------------------------
# DiffGenerator
# ---------------------------------------------------------------------------

class DiffGenerator:
    """Compute and broadcast diffs for workspace changes."""

    PREVIEW_BYTES: int = 2000
    """How many bytes of stdout/stderr to include in dashboard previews."""

    def __init__(
        self,
        kernel_ws_client: KernelEventSink | None = None,
        git_tracker: GitTracker | None = None,
    ) -> None:
        """Initialize the generator.

        Args:
            kernel_ws_client: Sink that accepts :class:`KernelEventSink`
                ``emit_event`` calls.  When ``None`` a null sink is
                used and no events are emitted.
            git_tracker: Tracker used to compute the diff against the
                last commit.  When ``None`` a default
                :class:`GitTracker` is constructed.
        """
        self.kernel = kernel_ws_client or _NullSink()
        self.git = git_tracker or GitTracker()
        # Cache the last diff we emitted per (workflow, file) so a
        # caller that polls multiple times in a row does not flood the
        # bus with identical events.
        self._last_emitted: dict[tuple[str, str], str] = {}

    # -- public API -------------------------------------------------------

    async def on_file_change(
        self,
        workspace: Workspace,
        agent_id: str,
        path: str,
    ) -> dict[str, Any] | None:
        """Emit a ``file_changed`` event describing ``path`` in ``workspace``.

        Returns the event payload that was broadcast (useful for tests)
        or ``None`` when the path does not exist.
        """
        safe_path = assert_within_workspace(workspace, path)
        if not safe_path.exists():
            return None
        new_content = self._read_text(safe_path)
        old_content = self._old_content_for(workspace, safe_path)
        if old_content == new_content:
            return None

        diff = self._make_unified_diff(
            old=old_content,
            new=new_content,
            from_file=f"a/{path}",
            to_file=f"b/{path}",
        )
        latest_commit = self._safe_head(workspace)
        payload = {
            "event": "file_changed",
            "workflow_id": workspace.workflow_id,
            "agent_id": agent_id,
            "path": path,
            "diff": diff,
            "commit_hash": latest_commit,
            "size": safe_path.stat().st_size,
            "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        }
        key = (workspace.workflow_id, path)
        if self._last_emitted.get(key) == diff:
            return payload
        self._last_emitted[key] = diff
        await self.kernel.emit_event("file_changed", payload)
        return payload

    async def on_execution_complete(
        self,
        workspace: Workspace,
        result: ExecutionResult,
        agent_id: str = "harness",
    ) -> dict[str, Any]:
        """Emit an ``execution_complete`` event for ``result``."""
        latest_commit = self._safe_head(workspace)
        payload = {
            "event": "execution_complete",
            "workflow_id": workspace.workflow_id,
            "agent_id": agent_id,
            "runtime": result.runtime,
            "exit_code": result.exit_code,
            "duration_ms": result.duration_ms,
            "killed_by_timeout": result.killed_by_timeout,
            "killed_by_memory": result.killed_by_memory,
            "stdout_preview": result.stdout[: self.PREVIEW_BYTES],
            "stderr_preview": result.stderr[: self.PREVIEW_BYTES],
            "file_writes": result.file_writes,
            "commit_hash": latest_commit,
            "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        }
        await self.kernel.emit_event("execution_complete", payload)
        return payload

    async def on_commit(
        self,
        workspace: Workspace,
        commit: CommitInfo,
    ) -> dict[str, Any]:
        """Emit a ``git_commit`` event after a successful commit."""
        payload = {
            "event": "git_commit",
            "workflow_id": workspace.workflow_id,
            "agent_id": commit.agent_id,
            "commit_hash": commit.hash,
            "message": commit.message,
            "files_changed": commit.files_changed,
            "insertions": commit.insertions,
            "deletions": commit.deletions,
            "timestamp": commit.timestamp.isoformat().replace("+00:00", "Z"),
        }
        await self.kernel.emit_event("git_commit", payload)
        return payload

    # -- helpers ----------------------------------------------------------

    def _old_content_for(self, workspace: Workspace, safe_path: Path) -> str:
        """Return the file's contents at the previous commit, or empty string."""
        try:
            self.git.init_repo(workspace)
        except FileNotFoundError:
            return ""
        head = self._safe_head(workspace)
        if not head:
            return ""
        try:
            rel = safe_path.relative_to(workspace.root)
            return self.git.get_file_at_commit(workspace, str(rel), head)
        except (FileNotFoundError, ValueError, subprocess_called_process_error()):
            return ""

    def _safe_head(self, workspace: Workspace) -> str:
        try:
            self.git.init_repo(workspace)
        except FileNotFoundError:
            return ""
        import subprocess

        result = subprocess.run(
            [self.git.git_binary, "rev-parse", "HEAD"],
            cwd=str(workspace.root),
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            return ""
        return result.stdout.strip()

    @staticmethod
    def _read_text(path: Path) -> str:
        try:
            return path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return path.read_text(encoding="utf-8", errors="replace")
        except FileNotFoundError:
            return ""

    @staticmethod
    def _make_unified_diff(old: str, new: str, *, from_file: str, to_file: str) -> str:
        diff = difflib.unified_diff(
            old.splitlines(keepends=True),
            new.splitlines(keepends=True),
            fromfile=from_file,
            tofile=to_file,
        )
        return "".join(diff)


def subprocess_called_process_error() -> type[BaseException]:
    import subprocess

    return subprocess.CalledProcessError


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------

class RecordingSink:
    """In-memory :class:`KernelEventSink` for tests.

    Use :attr:`events` to inspect the captured envelopes.
    """

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    async def emit_event(
        self,
        event_type: str,
        details: dict[str, Any] | None = None,
        *,
        recipient: str = "main-agent",
        sender: Any | None = None,
    ) -> None:
        record = {
            "event_type": event_type,
            "details": details or {},
        }
        self.events.append(record)

    def of_type(self, event_type: str) -> list[dict[str, Any]]:
        return [e for e in self.events if e["event_type"] == event_type]

    def clear(self) -> None:
        self.events.clear()


__all__ = [
    "DiffGenerator",
    "KernelEventSink",
    "RecordingSink",
]
