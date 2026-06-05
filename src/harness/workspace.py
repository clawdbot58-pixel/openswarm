"""Workflow-scoped workspaces.

Every workflow gets its own on-disk directory under
``workspaces/{workflow_id}/`` with the standard layout::

    workspaces/<workflow_id>/
    ├── .git/                # auto-init'd by GitTracker
    ├── src/                 # agent-written source code
    ├── output/              # artifacts intended for downstream consumers
    ├── logs/                # execution logs (one file per run)
    └── temp/                # scratch space for executor code drops

The :class:`WorkspaceManager` is the only object that creates or destroys
these trees.  It is intentionally cheap to instantiate — it does not
hold long-lived resources, only an in-memory cache of discovered
workspaces.
"""
from __future__ import annotations

import logging
import os
import shutil
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Path validation
# ---------------------------------------------------------------------------

# Characters we refuse in workflow_id — same as the agent_id pattern.
_WORKFLOW_ID_PATTERN = r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,127}$"


def _is_safe_workflow_id(workflow_id: str) -> bool:
    """Reject path-traversal and control characters in workflow_id."""
    import re

    if not isinstance(workflow_id, str):
        return False
    if not re.match(_WORKFLOW_ID_PATTERN, workflow_id):
        return False
    if ".." in workflow_id:
        return False
    return True


# ---------------------------------------------------------------------------
# Workspace model
# ---------------------------------------------------------------------------

class Workspace(BaseModel):
    """The on-disk layout for a single workflow's harness directory.

    All paths are absolute.  ``last_accessed`` is bumped on every
    :meth:`WorkspaceManager.get` so the cleanup routine can prune
    idle workspaces by age.
    """

    # Pydantic v2: keep defaults permissive so the manager can populate
    # paths without fighting the model.
    model_config = {"arbitrary_types_allowed": True}

    workflow_id: str
    root: Path
    src_dir: Path
    output_dir: Path
    logs_dir: Path
    temp_dir: Path
    created_at: datetime
    last_accessed: datetime
    git_initialized: bool = False

    def touch(self) -> None:
        """Update ``last_accessed`` to now.  Does not touch the filesystem."""
        self.last_accessed = datetime.now(timezone.utc)

    def relative_to(self, path: Path | str) -> Path:
        """Resolve ``path`` against the workspace root and verify it stays inside.

        Raises ``ValueError`` if the resolved path escapes the workspace.
        """
        candidate = (self.root / path).resolve()
        root = self.root.resolve()
        try:
            candidate.relative_to(root)
        except ValueError as exc:
            raise ValueError(
                f"path {path!r} escapes workspace root {root}"
            ) from exc
        return candidate


# ---------------------------------------------------------------------------
# WorkspaceManager
# ---------------------------------------------------------------------------

class WorkspaceManager:
    """Create, fetch, and reap workflow workspaces.

    The manager is **process-local**: it has no kernel coupling.  Tests
    that need isolation can construct a new manager against a temporary
    :attr:`BASE_DIR` without touching the project tree.
    """

    BASE_DIR: Path = Path("workspaces")
    """Default base directory relative to CWD.  Override per-instance."""

    def __init__(self, base_dir: Path | str | None = None) -> None:
        """Initialize the manager.

        Args:
            base_dir: Optional override for the workspaces root.  When
                ``None`` we use :attr:`BASE_DIR` resolved against the
                current working directory.
        """
        if base_dir is None:
            self.base_dir: Path = Path(self.BASE_DIR).resolve()
        else:
            self.base_dir = Path(base_dir).resolve()
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._cache: dict[str, Workspace] = {}
        # Warm the cache from disk.
        for child in self.base_dir.iterdir():
            if child.is_dir() and (child / ".git").exists():
                self._cache[child.name] = self._build_from_disk(child)

    # -- public API -------------------------------------------------------

    def create(self, workflow_id: str | None = None) -> Workspace:
        """Create a new workspace.

        Args:
            workflow_id: Optional explicit ID.  When ``None`` a UUID4
                string is generated.

        Returns:
            The freshly created :class:`Workspace`.

        Raises:
            ValueError: If ``workflow_id`` is unsafe.
            FileExistsError: If a workspace with the same ID already exists.
        """
        if workflow_id is None:
            workflow_id = str(uuid.uuid4())
        if not _is_safe_workflow_id(workflow_id):
            raise ValueError(f"unsafe workflow_id: {workflow_id!r}")

        with self._lock:
            if workflow_id in self._cache:
                raise FileExistsError(
                    f"workspace already exists: {workflow_id}"
                )
            root = self.base_dir / workflow_id
            if root.exists():
                # Recover from a half-initialized leftover.
                shutil.rmtree(root)
            root.mkdir(parents=True)
            now = datetime.now(timezone.utc)
            ws = Workspace(
                workflow_id=workflow_id,
                root=root,
                src_dir=root / "src",
                output_dir=root / "output",
                logs_dir=root / "logs",
                temp_dir=root / "temp",
                created_at=now,
                last_accessed=now,
                git_initialized=False,
            )
            for sub in (ws.src_dir, ws.output_dir, ws.logs_dir, ws.temp_dir):
                sub.mkdir(parents=True, exist_ok=True)
            self._cache[workflow_id] = ws
            logger.info("workspace created workflow_id=%s root=%s", workflow_id, root)
            return ws

    def get(self, workflow_id: str) -> Optional[Workspace]:
        """Return an existing workspace, or ``None`` when missing.

        The ``last_accessed`` timestamp is refreshed so cleanup logic
        treats the workspace as recently used.
        """
        ws = self._cache.get(workflow_id)
        if ws is None:
            candidate = self.base_dir / workflow_id
            if not candidate.exists():
                return None
            ws = self._build_from_disk(candidate)
            self._cache[workflow_id] = ws
        ws.touch()
        return ws

    def get_or_create(self, workflow_id: str) -> Workspace:
        """Fetch an existing workspace or create a new one."""
        ws = self.get(workflow_id)
        if ws is None:
            ws = self.create(workflow_id)
        return ws

    def cleanup(self, workflow_id: str, max_age_hours: int = 24) -> bool:
        """Remove a single workspace.

        Returns ``True`` when something was deleted.  The ``max_age_hours``
        argument is informational: callers that want strict age gating
        should use :meth:`cleanup_stale` instead.
        """
        ws = self._cache.get(workflow_id)
        if ws is None:
            candidate = self.base_dir / workflow_id
            if not candidate.exists():
                return False
            self._cache.pop(workflow_id, None)
            shutil.rmtree(candidate, ignore_errors=True)
            return True
        shutil.rmtree(ws.root, ignore_errors=True)
        self._cache.pop(workflow_id, None)
        logger.info(
            "workspace removed workflow_id=%s max_age_hours=%d", workflow_id, max_age_hours
        )
        return True

    def cleanup_stale(self, max_age_hours: int = 24) -> list[str]:
        """Delete every workspace whose last access is older than the threshold.

        Returns:
            The list of workflow IDs that were removed.
        """
        cutoff = datetime.now(timezone.utc).timestamp() - (max_age_hours * 3600)
        removed: list[str] = []
        for wf_id in list(self._cache.keys()):
            ws = self._cache[wf_id]
            try:
                mtime = ws.root.stat().st_mtime
            except FileNotFoundError:
                self._cache.pop(wf_id, None)
                continue
            if mtime < cutoff:
                shutil.rmtree(ws.root, ignore_errors=True)
                self._cache.pop(wf_id, None)
                removed.append(wf_id)
        return removed

    def list_active(self) -> list[Workspace]:
        """Return a snapshot of every cached workspace."""
        return list(self._cache.values())

    def exists(self, workflow_id: str) -> bool:
        """Cheap existence check that does not mutate the cache."""
        if workflow_id in self._cache:
            return True
        return (self.base_dir / workflow_id).exists()

    def __contains__(self, workflow_id: object) -> bool:
        return self.exists(workflow_id) if isinstance(workflow_id, str) else False

    # -- helpers ----------------------------------------------------------

    def _build_from_disk(self, root: Path) -> Workspace:
        stat = root.stat()
        ts = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
        return Workspace(
            workflow_id=root.name,
            root=root,
            src_dir=root / "src",
            output_dir=root / "output",
            logs_dir=root / "logs",
            temp_dir=root / "temp",
            created_at=ts,
            last_accessed=ts,
            git_initialized=(root / ".git").exists(),
        )


# ---------------------------------------------------------------------------
# Path safety helper — used by every other module too
# ---------------------------------------------------------------------------

def assert_within_workspace(workspace: Workspace, path: Path | str) -> Path:
    """Return ``path`` resolved against the workspace, refusing escapes.

    This is the canonical "is the agent trying to break out of its
    sandbox?" guard.  It is exported so the server and executor can
    reuse it without duplicating the resolution logic.
    """
    return workspace.relative_to(path)


__all__ = [
    "Workspace",
    "WorkspaceManager",
    "assert_within_workspace",
]
