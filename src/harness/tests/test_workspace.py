"""Tests for WorkspaceManager and Workspace."""
from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from harness.workspace import (
    Workspace,
    WorkspaceManager,
    assert_within_workspace,
)


@pytest.mark.asyncio
async def test_create_workspace_creates_subdirs(workspace_manager):
    """Creating a workspace also creates src/output/logs/temp."""
    ws = workspace_manager.create("wf-create-1")

    assert ws.root.exists()
    assert ws.src_dir.exists() and ws.src_dir.is_dir()
    assert ws.output_dir.exists() and ws.output_dir.is_dir()
    assert ws.logs_dir.exists() and ws.logs_dir.is_dir()
    assert ws.temp_dir.exists() and ws.temp_dir.is_dir()


@pytest.mark.asyncio
async def test_create_workspace_returns_dataclass(workspace_manager):
    """The Workspace is a populated Pydantic model."""
    ws = workspace_manager.create("wf-create-2")
    assert isinstance(ws, Workspace)
    assert ws.workflow_id == "wf-create-2"
    assert ws.root.name == "wf-create-2"
    assert not ws.git_initialized
    assert ws.created_at <= ws.last_accessed


@pytest.mark.asyncio
async def test_create_duplicate_raises(workspace_manager):
    """Creating twice for the same id raises."""
    workspace_manager.create("dup")
    with pytest.raises(FileExistsError):
        workspace_manager.create("dup")


@pytest.mark.asyncio
async def test_create_generates_uuid_when_no_id(workspace_manager):
    """When workflow_id is None a UUID4 is generated."""
    ws = workspace_manager.create()
    assert ws.workflow_id
    # 32 hex chars + 4 dashes = 36 chars total.
    assert len(ws.workflow_id) == 36


@pytest.mark.asyncio
async def test_create_rejects_unsafe_ids(workspace_manager):
    """Path-traversal and special chars are rejected."""
    for bad in ("../etc", "with space", "ümlaut", "", "..", "."):
        with pytest.raises(ValueError):
            workspace_manager.create(bad)


@pytest.mark.asyncio
async def test_get_returns_existing(workspace_manager):
    ws = workspace_manager.create("get-1")
    fetched = workspace_manager.get("get-1")
    assert fetched is not None
    assert fetched.workflow_id == "get-1"
    assert fetched.root == ws.root


@pytest.mark.asyncio
async def test_get_missing_returns_none(workspace_manager):
    assert workspace_manager.get("does-not-exist") is None


@pytest.mark.asyncio
async def test_get_updates_last_accessed(workspace_manager):
    """Calling get bumps last_accessed."""
    ws = workspace_manager.create("access-1")
    before = ws.last_accessed
    time.sleep(0.01)
    fetched = workspace_manager.get("access-1")
    assert fetched is not None
    assert fetched.last_accessed > before


@pytest.mark.asyncio
async def test_get_or_create_creates_when_missing(workspace_manager):
    ws = workspace_manager.get_or_create("lazy-1")
    assert ws.workflow_id == "lazy-1"
    assert ws.root.exists()


@pytest.mark.asyncio
async def test_cleanup_removes_workspace(workspace_manager):
    ws = workspace_manager.create("clean-1")
    assert ws.root.exists()
    removed = workspace_manager.cleanup("clean-1")
    assert removed is True
    assert not ws.root.exists()
    assert "clean-1" not in workspace_manager


@pytest.mark.asyncio
async def test_cleanup_missing_returns_false(workspace_manager):
    assert workspace_manager.cleanup("nope") is False


@pytest.mark.asyncio
async def test_list_active_returns_only_known(workspace_manager):
    workspace_manager.create("a")
    workspace_manager.create("b")
    workspace_manager.create("c")
    active = {w.workflow_id for w in workspace_manager.list_active()}
    assert active == {"a", "b", "c"}


@pytest.mark.asyncio
async def test_exists_and_contains(workspace_manager):
    workspace_manager.create("present")
    assert workspace_manager.exists("present")
    assert "present" in workspace_manager
    assert not workspace_manager.exists("absent")
    assert "absent" not in workspace_manager


@pytest.mark.asyncio
async def test_workspace_relative_to_blocks_traversal(workspace_manager):
    """Paths that resolve outside the workspace are rejected."""
    ws = workspace_manager.create("traverse-1")
    with pytest.raises(ValueError):
        ws.relative_to("../etc")
    with pytest.raises(ValueError):
        ws.relative_to("../../outside")


@pytest.mark.asyncio
async def test_workspace_relative_to_accepts_inside(workspace_manager):
    ws = workspace_manager.create("safe-1")
    resolved = ws.relative_to("src/main.py")
    assert resolved == ws.root / "src" / "main.py"


@pytest.mark.asyncio
async def test_assert_within_workspace_helper(workspace_manager):
    ws = workspace_manager.create("helper-1")
    assert assert_within_workspace(ws, "src/foo.py").name == "foo.py"
    with pytest.raises(ValueError):
        assert_within_workspace(ws, "../foo.py")


@pytest.mark.asyncio
async def test_cleanup_stale_removes_old_only(workspace_manager):
    """Stale cleanup respects the max_age_hours cutoff."""
    ws_old = workspace_manager.create("old-1")
    workspace_manager.create("new-1")
    # Backdate the old one beyond the cutoff.
    past = time.time() - (48 * 3600)
    os.utime(ws_old.root, (past, past))
    removed = workspace_manager.cleanup_stale(max_age_hours=24)
    assert "old-1" in removed
    assert "new-1" not in removed
    assert not ws_old.root.exists()
    assert workspace_manager.exists("new-1")
