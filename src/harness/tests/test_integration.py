"""End-to-end integration tests for the harness.

These tests drive the full flow: workspace creation, agent file
write, git commit, diff event, code execution, file read, rollback,
cleanup.  They use the in-process :class:`HarnessServer` (no HTTP)
and the :class:`SubprocessBackend` (no Docker).
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from harness.diff_generator import DiffGenerator, RecordingSink
from harness.executor import CodeExecutor, SubprocessBackend
from harness.git_tracker import GitTracker
from harness.server import HarnessServer
from harness.workspace import WorkspaceManager


@pytest.fixture
def integration_harness(tmp_path):
    """A fully-wired :class:`HarnessServer` for integration tests."""
    workspaces = tmp_path / "workspaces"
    workspaces.mkdir()
    mgr = WorkspaceManager(base_dir=workspaces)
    sink = RecordingSink()
    git = GitTracker()
    diff = DiffGenerator(kernel_ws_client=sink, git_tracker=git)
    executor = CodeExecutor(backend=SubprocessBackend())
    server = HarnessServer(
        workspace_manager=mgr,
        executor=executor,
        git_tracker=git,
        diff_generator=diff,
    )
    server.allow_reset("main-agent")
    return {
        "server": server,
        "manager": mgr,
        "sink": sink,
        "git": git,
        "diff": diff,
        "executor": executor,
    }


@pytest.mark.asyncio
async def test_full_flow_write_exec_read_reset(integration_harness):
    """End-to-end happy path: write, exec, read, reset."""
    h = integration_harness
    server: HarnessServer = h["server"]
    sink: RecordingSink = h["sink"]
    manager: WorkspaceManager = h["manager"]
    workflow_id = "integration-1"

    # 1. Write a file.  Triggers an auto-commit + file_changed event.
    write = await server.handle_tool_write(
        {
            "workflow_id": workflow_id,
            "path": "src/hello.py",
            "content": "def hello():\n    return 'hi'\n",
            "agent_id": "coder",
        }
    )
    assert write["ok"] is True
    assert write["commit"]["hash"]
    assert any(e["event_type"] == "file_changed" for e in sink.events)

    # 2. Run a Python script that imports the file.  The SubprocessBackend
    # sets the runtime's CWD to the workspace root, so the import is
    # relative.
    code = (
        "import sys\n"
        "sys.path.insert(0, 'src')\n"
        "from hello import hello\n"
        "print(hello())\n"
    )
    exec_out = await server.handle_tool_exec(
        {
            "workflow_id": workflow_id,
            "runtime": "python",
            "code": code,
            "agent_id": "coder",
        }
    )
    assert exec_out["ok"] is True
    assert exec_out["execution"]["exit_code"] == 0
    assert "hi" in exec_out["execution"]["stdout"]

    # 3. Read the file back.
    read = await server.handle_tool_read(
        {"workflow_id": workflow_id, "path": "src/hello.py"}
    )
    assert read["content"] == "def hello():\n    return 'hi'\n"

    # 4. Modify the file, then reset to its previous commit.
    history = await server.handle_tool_history({"workflow_id": workflow_id})
    write_commit = history["commits"][-1]["hash"]
    await server.handle_tool_write(
        {
            "workflow_id": workflow_id,
            "path": "src/hello.py",
            "content": "def hello():\n    return 'changed'\n",
            "agent_id": "reviewer",
        }
    )
    reset = await server.handle_tool_reset(
        {
            "workflow_id": workflow_id,
            "commit_hash": write_commit,
            "agent_id": "main-agent",
        }
    )
    assert reset["ok"] is True
    read_after_reset = await server.handle_tool_read(
        {"workflow_id": workflow_id, "path": "src/hello.py"}
    )
    assert "hi" in read_after_reset["content"]
    assert "changed" not in read_after_reset["content"]


@pytest.mark.asyncio
async def test_full_flow_multiple_agents_concurrent_writes(integration_harness):
    """Multiple agents writing to the same workspace produce attributed commits."""
    h = integration_harness
    server: HarnessServer = h["server"]
    workflow_id = "integration-2"

    await server.handle_tool_write(
        {"workflow_id": workflow_id, "path": "src/a.py", "content": "a", "agent_id": "coder-a"}
    )
    await server.handle_tool_write(
        {"workflow_id": workflow_id, "path": "src/b.py", "content": "b", "agent_id": "coder-b"}
    )
    await server.handle_tool_write(
        {"workflow_id": workflow_id, "path": "src/c.py", "content": "c", "agent_id": "reviewer-c"}
    )

    history = await server.handle_tool_history({"workflow_id": workflow_id})
    agents = [c["agent_id"] for c in history["commits"]]
    assert "reviewer-c" in agents
    assert "coder-b" in agents
    assert "coder-a" in agents


@pytest.mark.asyncio
async def test_full_flow_execution_writes_files(integration_harness):
    """A runtime that creates a file shows up in the file_writes list."""
    h = integration_harness
    server: HarnessServer = h["server"]
    code = (
        "with open('src/result.txt', 'w') as f:\n"
        "    f.write('runtime-output')\n"
        "print('wrote')\n"
    )
    out = await server.handle_tool_exec(
        {
            "workflow_id": "integration-3",
            "runtime": "python",
            "code": code,
            "agent_id": "coder",
        }
    )
    assert out["ok"] is True
    writes = out["execution"]["file_writes"]
    assert any(p.endswith("result.txt") for p in writes)
    read = await server.handle_tool_read(
        {"workflow_id": "integration-3", "path": "src/result.txt"}
    )
    assert read["content"] == "runtime-output"


@pytest.mark.asyncio
async def test_full_flow_exec_emits_events(integration_harness):
    """An execution produces both execution_complete and git_commit events."""
    h = integration_harness
    server: HarnessServer = h["server"]
    sink: RecordingSink = h["sink"]
    sink.clear()

    code = "print('events-test')\nwith open('src/out.txt', 'w') as f: f.write('e')"
    out = await server.handle_tool_exec(
        {
            "workflow_id": "integration-events-1",
            "runtime": "python",
            "code": code,
            "agent_id": "coder",
        }
    )
    assert out["ok"] is True
    event_types = {e["event_type"] for e in sink.events}
    assert "execution_complete" in event_types
    assert "git_commit" in event_types
    assert "file_changed" in event_types  # the runtime wrote a file


@pytest.mark.asyncio
async def test_full_flow_cleanup_removes_workspace(integration_harness):
    h = integration_harness
    server: HarnessServer = h["server"]
    manager: WorkspaceManager = h["manager"]
    await server.handle_tool_write(
        {
            "workflow_id": "integration-cleanup-1",
            "path": "src/keep.py",
            "content": "x",
            "agent_id": "coder",
        }
    )
    ws = manager.get("integration-cleanup-1")
    assert ws is not None and ws.root.exists()
    removed = manager.cleanup("integration-cleanup-1")
    assert removed is True
    assert not ws.root.exists()


@pytest.mark.asyncio
async def test_full_flow_reset_invalid_commit_raises(integration_harness):
    h = integration_harness
    server: HarnessServer = h["server"]
    await server.handle_tool_write(
        {
            "workflow_id": "integration-bad-reset",
            "path": "src/x.py",
            "content": "x",
            "agent_id": "coder",
        }
    )
    with pytest.raises(Exception):
        await server.handle_tool_reset(
            {
                "workflow_id": "integration-bad-reset",
                "commit_hash": "deadbeef" * 5,
                "agent_id": "main-agent",
            }
        )


@pytest.mark.asyncio
async def test_full_flow_diff_visible_in_history(integration_harness):
    """The history includes file lists and insertion/deletion counts."""
    h = integration_harness
    server: HarnessServer = h["server"]
    await server.handle_tool_write(
        {
            "workflow_id": "integration-diff-1",
            "path": "src/calc.py",
            "content": "def add(a, b):\n    return a + b\n",
            "agent_id": "coder",
        }
    )
    history = await server.handle_tool_history({"workflow_id": "integration-diff-1"})
    assert history["ok"] is True
    assert history["commits"]
    # The most recent commit should be the write above.
    latest = history["commits"][0]
    assert any(p.endswith("calc.py") for p in latest["files_changed"])
    assert latest["insertions"] >= 2
    assert latest["deletions"] >= 0


@pytest.mark.asyncio
async def test_full_flow_execution_failure_logged(integration_harness):
    """A failing exit code is captured, not raised."""
    h = integration_harness
    server: HarnessServer = h["server"]
    out = await server.handle_tool_exec(
        {
            "workflow_id": "integration-fail-1",
            "runtime": "python",
            "code": "import sys; sys.exit(7)",
            "agent_id": "coder",
        }
    )
    assert out["ok"] is True
    assert out["execution"]["exit_code"] == 7
    assert out["execution"]["runtime"] == "python"


@pytest.mark.asyncio
async def test_full_flow_workspace_persists_across_calls(integration_harness):
    """Files written in one call are visible in the next."""
    h = integration_harness
    server: HarnessServer = h["server"]
    workflow_id = "integration-persist-1"
    await server.handle_tool_write(
        {
            "workflow_id": workflow_id,
            "path": "src/persist.py",
            "content": "PERSISTED = True\n",
            "agent_id": "coder",
        }
    )
    # Read in a separate call.
    read = await server.handle_tool_read(
        {"workflow_id": workflow_id, "path": "src/persist.py"}
    )
    assert read["content"] == "PERSISTED = True\n"
    # And verify the file exists on disk.
    manager: WorkspaceManager = h["manager"]
    ws = manager.get(workflow_id)
    assert ws is not None
    on_disk = (ws.src_dir / "persist.py").read_text()
    assert on_disk == "PERSISTED = True\n"


@pytest.mark.asyncio
async def test_full_flow_listing_returns_written_files(integration_harness):
    """list_files surfaces everything the agents wrote."""
    h = integration_harness
    server: HarnessServer = h["server"]
    workflow_id = "integration-list-1"
    for name in ("alpha.py", "beta.py", "gamma.py"):
        await server.handle_tool_write(
            {
                "workflow_id": workflow_id,
                "path": f"src/{name}",
                "content": f"# {name}",
                "agent_id": "coder",
            }
        )
    listing = await server.handle_tool_list({"workflow_id": workflow_id, "path": "src"})
    assert listing["ok"] is True
    names = {e["name"] for e in listing["entries"]}
    assert {"alpha.py", "beta.py", "gamma.py"} <= names
