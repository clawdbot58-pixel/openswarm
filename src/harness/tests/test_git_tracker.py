"""Tests for GitTracker."""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from harness.git_tracker import CommitInfo, GitTracker
from harness.workspace import WorkspaceManager


pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _committed_lines(out: str) -> int:
    """Parse a ``git log --shortstat`` shortstat line into file-change totals."""
    out = out.strip()
    if not out:
        return 0
    return 1


# ---------------------------------------------------------------------------
# Init / commit
# ---------------------------------------------------------------------------

async def test_init_repo_creates_git_dir(workspace_manager, git_available):
    if not git_available:
        pytest.skip("git not available")
    ws = workspace_manager.create("git-init-1")
    tracker = GitTracker()
    tracker.init_repo(ws)
    assert (ws.root / ".git").exists()
    assert ws.git_initialized is True
    # The gitignore should be present.
    assert (ws.root / ".gitignore").exists()


async def test_init_repo_idempotent(workspace_manager, git_available):
    if not git_available:
        pytest.skip("git not available")
    ws = workspace_manager.create("git-init-2")
    tracker = GitTracker()
    tracker.init_repo(ws)
    # Calling again should not blow up.
    tracker.init_repo(ws)
    assert (ws.root / ".git").exists()


async def test_commit_after_file_write(workspace_manager, git_available):
    if not git_available:
        pytest.skip("git not available")
    ws = workspace_manager.create("git-commit-1")
    tracker = GitTracker()
    (ws.src_dir / "hello.py").write_text("print('hello')\n")
    info = tracker.commit(ws, "coder-python-fast", message="add hello")
    assert info.hash
    assert info.agent_id == "coder-python-fast"
    assert "add hello" in info.message
    assert any(p.endswith("hello.py") for p in info.files_changed)


async def test_commit_is_noop_when_clean(workspace_manager, git_available):
    if not git_available:
        pytest.skip("git not available")
    ws = workspace_manager.create("git-commit-2")
    tracker = GitTracker()
    info1 = tracker.commit(ws, "agent-a", message="initial")
    info2 = tracker.commit(ws, "agent-b", message="nothing to do")
    # No changes -> same hash, no new commit created.
    assert info1.hash == info2.hash


async def test_get_history_returns_commits_newest_first(
    workspace_manager, git_available
):
    if not git_available:
        pytest.skip("git not available")
    ws = workspace_manager.create("git-hist-1")
    tracker = GitTracker()
    (ws.src_dir / "a.txt").write_text("a\n")
    info_a = tracker.commit(ws, "agent-a", message="add a")
    (ws.src_dir / "b.txt").write_text("b\n")
    info_b = tracker.commit(ws, "agent-b", message="add b")
    history = tracker.get_history(ws)
    assert [c.hash for c in history] == [info_b.hash, info_a.hash]
    assert history[0].agent_id == "agent-b"
    assert history[1].agent_id == "agent-a"


async def test_get_diff_returns_unified_format(workspace_manager, git_available):
    if not git_available:
        pytest.skip("git not available")
    ws = workspace_manager.create("git-diff-1")
    tracker = GitTracker()
    (ws.src_dir / "x.py").write_text("print(1)\n")
    info = tracker.commit(ws, "agent-a", message="x=1")
    (ws.src_dir / "x.py").write_text("print(2)\n")
    info2 = tracker.commit(ws, "agent-b", message="x=2")
    diff = tracker.get_diff(ws, info2.hash)
    assert "diff --git" in diff
    assert "-print(1)" in diff or "print(1)" in diff
    assert "print(2)" in diff


async def test_get_diff_invalid_commit_raises(workspace_manager, git_available):
    if not git_available:
        pytest.skip("git not available")
    ws = workspace_manager.create("git-diff-bad")
    tracker = GitTracker()
    (ws.src_dir / "x.py").write_text("a")
    tracker.commit(ws, "agent", message="init")
    with pytest.raises(ValueError):
        tracker.get_diff(ws, "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef")


async def test_get_file_at_commit(workspace_manager, git_available):
    if not git_available:
        pytest.skip("git not available")
    ws = workspace_manager.create("git-cat-1")
    tracker = GitTracker()
    (ws.src_dir / "f.txt").write_text("first\n")
    info1 = tracker.commit(ws, "agent-a", message="first")
    (ws.src_dir / "f.txt").write_text("second\n")
    tracker.commit(ws, "agent-b", message="second")
    content = tracker.get_file_at_commit(ws, "src/f.txt", info1.hash)
    assert content == "first\n"


async def test_get_file_at_commit_missing(workspace_manager, git_available):
    if not git_available:
        pytest.skip("git not available")
    ws = workspace_manager.create("git-cat-2")
    tracker = GitTracker()
    (ws.src_dir / "f.txt").write_text("x")
    info = tracker.commit(ws, "agent", message="x")
    with pytest.raises(FileNotFoundError):
        tracker.get_file_at_commit(ws, "nope.txt", info.hash)


async def test_reset_to_commit_restores_files(workspace_manager, git_available):
    if not git_available:
        pytest.skip("git not available")
    ws = workspace_manager.create("git-reset-1")
    tracker = GitTracker()
    (ws.src_dir / "x.py").write_text("v1\n")
    info1 = tracker.commit(ws, "agent-a", message="v1")
    (ws.src_dir / "x.py").write_text("v2\n")
    tracker.commit(ws, "agent-b", message="v2")
    assert (ws.src_dir / "x.py").read_text() == "v2\n"
    tracker.reset_to_commit(ws, info1.hash)
    assert (ws.src_dir / "x.py").read_text() == "v1\n"


async def test_reset_to_commit_invalid_raises(workspace_manager, git_available):
    if not git_available:
        pytest.skip("git not available")
    ws = workspace_manager.create("git-reset-bad")
    tracker = GitTracker()
    (ws.src_dir / "x.py").write_text("v1")
    tracker.commit(ws, "agent", message="v1")
    with pytest.raises(ValueError):
        tracker.reset_to_commit(ws, "deadbeef" * 5)


async def test_multiple_agents_attribution(workspace_manager, git_available):
    """Commits from different agents are correctly attributed."""
    if not git_available:
        pytest.skip("git not available")
    ws = workspace_manager.create("git-multi-1")
    tracker = GitTracker()
    for agent, content in [
        ("coder-python-fast", "alpha\n"),
        ("coder-python-standard", "beta\n"),
        ("reviewer-security", "gamma\n"),
    ]:
        (ws.src_dir / "log.txt").write_text(content)
        tracker.commit(ws, agent, message=f"set {content.strip()}")
    history = tracker.get_history(ws)
    agents = [c.agent_id for c in history]
    assert agents == ["reviewer-security", "coder-python-standard", "coder-python-fast"]


async def test_gitignore_excludes_temp(workspace_manager, git_available):
    """``temp/`` should be ignored so runtime code drops don't pollute history."""
    if not git_available:
        pytest.skip("git not available")
    ws = workspace_manager.create("git-ig-1")
    tracker = GitTracker()
    (ws.src_dir / "real.py").write_text("real\n")
    (ws.temp_dir / "scratch.py").write_text("scratch\n")
    info = tracker.commit(ws, "agent", message="initial")
    # Only real.py should be in the commit.
    assert any(p.endswith("real.py") for p in info.files_changed)
    assert not any(p.startswith("temp/") for p in info.files_changed)


async def test_commit_info_includes_insertion_deletion_counts(
    workspace_manager, git_available
):
    if not git_available:
        pytest.skip("git not available")
    ws = workspace_manager.create("git-stat-1")
    tracker = GitTracker()
    (ws.src_dir / "stat.txt").write_text("one\n")
    tracker.commit(ws, "agent-a", message="add line")
    (ws.src_dir / "stat.txt").write_text("one\ntwo\nthree\n")
    info = tracker.commit(ws, "agent-b", message="add more")
    assert info.insertions >= 2
