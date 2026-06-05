"""Tests for DiffGenerator."""
from __future__ import annotations

from pathlib import Path

import pytest

from harness.diff_generator import DiffGenerator, RecordingSink
from harness.executor import ExecutionResult, SubprocessBackend
from harness.git_tracker import GitTracker
from harness.workspace import WorkspaceManager


# ---------------------------------------------------------------------------
# File change
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_file_change_emits_event(workspace_manager, git_available):
    if not git_available:
        pytest.skip("git not available")
    sink = RecordingSink()
    diff = DiffGenerator(kernel_ws_client=sink, git_tracker=GitTracker())
    ws = workspace_manager.create("diff-fc-1")
    diff.git.init_repo(ws)
    # Need an initial commit so the diff against HEAD works.
    diff.git.commit(ws, "agent-init", message="init")
    (ws.src_dir / "hello.py").write_text("print('hello')\n")
    payload = await diff.on_file_change(ws, "agent-a", "src/hello.py")
    assert payload is not None
    assert payload["event"] == "file_changed"
    assert payload["agent_id"] == "agent-a"
    assert "src/hello.py" in payload["path"]
    assert "diff" in payload
    # The sink should have captured the event.
    events = sink.of_type("file_changed")
    assert len(events) == 1
    assert events[0]["details"]["workflow_id"] == "diff-fc-1"


@pytest.mark.asyncio
async def test_file_change_no_op_when_unchanged(workspace_manager, git_available):
    if not git_available:
        pytest.skip("git not available")
    sink = RecordingSink()
    diff = DiffGenerator(kernel_ws_client=sink, git_tracker=GitTracker())
    ws = workspace_manager.create("diff-fc-2")
    diff.git.init_repo(ws)
    (ws.src_dir / "x.py").write_text("x")
    diff.git.commit(ws, "agent", message="init")
    # Emit twice — the second call should not re-broadcast.
    await diff.on_file_change(ws, "agent", "src/x.py")
    sink.clear()
    payload = await diff.on_file_change(ws, "agent", "src/x.py")
    assert payload is None
    assert sink.of_type("file_changed") == []


@pytest.mark.asyncio
async def test_file_change_missing_path_returns_none(workspace_manager, git_available):
    if not git_available:
        pytest.skip("git not available")
    sink = RecordingSink()
    diff = DiffGenerator(kernel_ws_client=sink, git_tracker=GitTracker())
    ws = workspace_manager.create("diff-fc-3")
    diff.git.init_repo(ws)
    payload = await diff.on_file_change(ws, "agent", "src/missing.py")
    assert payload is None
    assert sink.of_type("file_changed") == []


# ---------------------------------------------------------------------------
# Execution complete
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_execution_complete_emits_event(workspace_manager, git_available):
    if not git_available:
        pytest.skip("git not available")
    sink = RecordingSink()
    diff = DiffGenerator(kernel_ws_client=sink, git_tracker=GitTracker())
    ws = workspace_manager.create("diff-exec-1")
    diff.git.init_repo(ws)
    result = ExecutionResult(
        stdout="hello\n",
        stderr="",
        exit_code=0,
        runtime="python",
        duration_ms=42,
        file_writes=["src/out.txt"],
    )
    payload = await diff.on_execution_complete(ws, result, agent_id="coder")
    assert payload["event"] == "execution_complete"
    assert payload["runtime"] == "python"
    assert payload["exit_code"] == 0
    assert "hello" in payload["stdout_preview"]
    assert payload["agent_id"] == "coder"
    events = sink.of_type("execution_complete")
    assert len(events) == 1


@pytest.mark.asyncio
async def test_execution_complete_includes_failure_flags(
    workspace_manager, git_available
):
    if not git_available:
        pytest.skip("git not available")
    sink = RecordingSink()
    diff = DiffGenerator(kernel_ws_client=sink, git_tracker=GitTracker())
    ws = workspace_manager.create("diff-exec-2")
    diff.git.init_repo(ws)
    result = ExecutionResult(
        stdout="",
        stderr="boom",
        exit_code=137,
        runtime="python",
        duration_ms=1000,
        killed_by_timeout=True,
        killed_by_memory=True,
    )
    payload = await diff.on_execution_complete(ws, result, agent_id="agent")
    assert payload["killed_by_timeout"] is True
    assert payload["killed_by_memory"] is True
    assert payload["exit_code"] == 137


# ---------------------------------------------------------------------------
# Commit event
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_commit_event_emitted(workspace_manager, git_available):
    if not git_available:
        pytest.skip("git not available")
    sink = RecordingSink()
    diff = DiffGenerator(kernel_ws_client=sink, git_tracker=GitTracker())
    ws = workspace_manager.create("diff-commit-1")
    (ws.src_dir / "x.py").write_text("x")
    commit = diff.git.commit(ws, "agent", message="x")
    payload = await diff.on_commit(ws, commit)
    assert payload["event"] == "git_commit"
    assert payload["agent_id"] == "agent"
    assert payload["commit_hash"] == commit.hash
    assert any(p.endswith("x.py") for p in payload["files_changed"])


# ---------------------------------------------------------------------------
# Multiple events
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_multiple_changes_emit_multiple_events(
    workspace_manager, git_available
):
    if not git_available:
        pytest.skip("git not available")
    sink = RecordingSink()
    diff = DiffGenerator(kernel_ws_client=sink, git_tracker=GitTracker())
    ws = workspace_manager.create("diff-multi-1")
    diff.git.init_repo(ws)
    diff.git.commit(ws, "agent", message="init")
    for name in ("a.py", "b.py", "c.py"):
        (ws.src_dir / name).write_text(name)
        await diff.on_file_change(ws, "agent", f"src/{name}")
    events = sink.of_type("file_changed")
    assert len(events) == 3
    paths = {e["details"]["path"] for e in events}
    assert paths == {"src/a.py", "src/b.py", "src/c.py"}


# ---------------------------------------------------------------------------
# Null sink default
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_default_sink_does_not_raise(workspace_manager, git_available):
    """A generator constructed without a sink should not blow up."""
    if not git_available:
        pytest.skip("git not available")
    diff = DiffGenerator()  # no kernel_ws_client
    ws = workspace_manager.create("diff-null-1")
    diff.git.init_repo(ws)
    diff.git.commit(ws, "agent", message="init")
    (ws.src_dir / "x.py").write_text("x")
    # Should not raise.
    await diff.on_file_change(ws, "agent", "src/x.py")
    await diff.on_execution_complete(
        ws,
        ExecutionResult(stdout="", stderr="", exit_code=0, runtime="python", duration_ms=0),
    )
