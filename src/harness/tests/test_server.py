"""Tests for HarnessServer + FastAPI app."""
from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from harness.diff_generator import DiffGenerator, RecordingSink
from harness.executor import CodeExecutor, SubprocessBackend
from harness.git_tracker import GitTracker
from harness.server import HarnessServer, create_app
from harness.workspace import WorkspaceManager


# ---------------------------------------------------------------------------
# In-process (no HTTP) — direct handler invocations
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_harness_exec_runs_python(harness_server):
    server: HarnessServer = harness_server
    out = await server.handle_tool_exec(
        {
            "workflow_id": "exec-py-1",
            "runtime": "python",
            "code": "print('from-test')",
            "agent_id": "tester",
        }
    )
    assert out["ok"] is True
    assert out["execution"]["exit_code"] == 0
    assert "from-test" in out["execution"]["stdout"]
    assert out["commit"]["hash"]


@pytest.mark.asyncio
async def test_harness_exec_rejects_disallowed_runtime(harness_server):
    server: HarnessServer = harness_server
    # The default allowlist is python/node/bash but the per-request
    # allowed_runtimes takes precedence.
    with pytest.raises(Exception) as excinfo:
        await server.handle_tool_exec(
            {
                "workflow_id": "exec-reject-1",
                "runtime": "python",
                "code": "print(1)",
                "agent_id": "tester",
                "allowed_runtimes": ["bash"],
            }
        )
    assert "not in allowed" in str(excinfo.value)


@pytest.mark.asyncio
async def test_harness_exec_rejects_unknown_runtime(harness_server):
    server: HarnessServer = harness_server
    with pytest.raises(Exception) as excinfo:
        await server.handle_tool_exec(
            {
                "workflow_id": "exec-rb-1",
                "runtime": "ruby",
                "code": "puts 'hi'",
                "agent_id": "tester",
            }
        )
    assert "runtime" in str(excinfo.value).lower() or "unknown" in str(excinfo.value).lower()


@pytest.mark.asyncio
async def test_harness_exec_rejects_missing_params(harness_server):
    server: HarnessServer = harness_server
    # The FastAPI layer rejects malformed payloads.  We invoke the
    # lower-level executor in a way that should still raise.
    from fastapi import HTTPException
    try:
        await server.handle_tool_exec(
            {
                "workflow_id": "exec-bad-1",
                "runtime": "python",
                # missing code
                "agent_id": "tester",
            }
        )
    except Exception as exc:
        assert "code" in str(exc).lower() or isinstance(exc, HTTPException)
    else:
        pytest.fail("expected exception for missing code parameter")


@pytest.mark.asyncio
async def test_harness_write_and_read_file(harness_server):
    server: HarnessServer = harness_server
    out = await server.handle_tool_write(
        {
            "workflow_id": "wf-1",
            "path": "src/hello.py",
            "content": "print(42)\n",
            "agent_id": "coder",
        }
    )
    assert out["ok"] is True
    assert out["bytes"] == len("print(42)\n")
    assert out["commit"]["hash"]

    read = await server.handle_tool_read(
        {"workflow_id": "wf-1", "path": "src/hello.py"}
    )
    assert read["ok"] is True
    assert read["content"] == "print(42)\n"


@pytest.mark.asyncio
async def test_harness_write_blocks_path_traversal(harness_server):
    server: HarnessServer = harness_server
    with pytest.raises(Exception) as excinfo:
        await server.handle_tool_write(
            {
                "workflow_id": "wf-bad-1",
                "path": "../escape.py",
                "content": "x",
                "agent_id": "coder",
            }
        )
    assert "escape" in str(excinfo.value).lower() or "outside" in str(excinfo.value).lower()


@pytest.mark.asyncio
async def test_harness_read_missing_returns_404(harness_server):
    server: HarnessServer = harness_server
    with pytest.raises(Exception) as excinfo:
        await server.handle_tool_read(
            {"workflow_id": "wf-2", "path": "src/never.py"}
        )
    assert excinfo.value.status_code == 404


@pytest.mark.asyncio
async def test_harness_list_files(harness_server):
    server: HarnessServer = harness_server
    await server.handle_tool_write(
        {
            "workflow_id": "wf-list-1",
            "path": "src/a.py",
            "content": "a",
            "agent_id": "coder",
        }
    )
    await server.handle_tool_write(
        {
            "workflow_id": "wf-list-1",
            "path": "src/b.py",
            "content": "b",
            "agent_id": "coder",
        }
    )
    out = await server.handle_tool_list({"workflow_id": "wf-list-1", "path": "src"})
    assert out["ok"] is True
    names = sorted(e["name"] for e in out["entries"])
    assert "a.py" in names
    assert "b.py" in names


@pytest.mark.asyncio
async def test_harness_reset_requires_authorization(harness_server):
    server: HarnessServer = harness_server
    # Setup: write a file, commit, modify.
    await server.handle_tool_write(
        {
            "workflow_id": "wf-reset-1",
            "path": "src/keep.py",
            "content": "v1",
            "agent_id": "coder",
        }
    )
    out1 = await server.handle_tool_history({"workflow_id": "wf-reset-1"})
    head_hash = out1["commits"][-1]["hash"]
    await server.handle_tool_write(
        {
            "workflow_id": "wf-reset-1",
            "path": "src/keep.py",
            "content": "v2",
            "agent_id": "coder",
        }
    )
    # Use a non-allowlisted agent — should be denied.
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as excinfo:
        await server.handle_tool_reset(
            {
                "workflow_id": "wf-reset-1",
                "commit_hash": head_hash,
                "agent_id": "unauthorized-agent",
            }
        )
    assert excinfo.value.status_code == 403
    # Now allow main-agent (already added by the fixture).
    out = await server.handle_tool_reset(
        {
            "workflow_id": "wf-reset-1",
            "commit_hash": head_hash,
            "agent_id": "main-agent",
        }
    )
    assert out["ok"] is True
    read = await server.handle_tool_read(
        {"workflow_id": "wf-reset-1", "path": "src/keep.py"}
    )
    assert read["content"] == "v1"


@pytest.mark.asyncio
async def test_harness_get_history_and_diff(harness_server):
    server: HarnessServer = harness_server
    await server.handle_tool_write(
        {
            "workflow_id": "wf-hist-1",
            "path": "src/f.py",
            "content": "one\n",
            "agent_id": "coder",
        }
    )
    history = await server.handle_tool_history({"workflow_id": "wf-hist-1"})
    assert history["ok"] is True
    assert history["commits"]
    commit = history["commits"][0]
    diff = await server.handle_tool_diff(
        {"workflow_id": "wf-hist-1", "commit": commit["hash"]}
    )
    assert diff["ok"] is True
    assert "diff --git" in diff["diff"]


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

@pytest.fixture
def client(harness_server):
    """A FastAPI TestClient for the harness app."""
    server: HarnessServer = harness_server
    app = create_app(
        workspace_manager=server.workspaces,
        executor=server.executor,
        git_tracker=server.git,
        diff_generator=server.diff,
    )
    return TestClient(app)


def test_health_endpoint(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"


def test_root_endpoint(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert resp.json()["service"] == "openswarm-harness"


def test_exec_endpoint(client):
    resp = client.post(
        "/tools/exec",
        json={
            "workflow_id": "http-exec-1",
            "runtime": "python",
            "code": "print('via-http')",
            "agent_id": "tester",
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert "via-http" in body["execution"]["stdout"]


def test_write_endpoint(client):
    resp = client.post(
        "/tools/write",
        json={
            "workflow_id": "http-write-1",
            "path": "src/hello.py",
            "content": "print(1)\n",
            "agent_id": "tester",
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert body["bytes"] == len("print(1)\n")


def test_read_endpoint(client):
    client.post(
        "/tools/write",
        json={
            "workflow_id": "http-read-1",
            "path": "src/hello.py",
            "content": "print(2)\n",
            "agent_id": "tester",
        },
    )
    resp = client.get("/tools/read", params={"workflow_id": "http-read-1", "path": "src/hello.py"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["content"] == "print(2)\n"


def test_list_endpoint(client):
    client.post(
        "/tools/write",
        json={
            "workflow_id": "http-list-1",
            "path": "src/file.py",
            "content": "x",
            "agent_id": "tester",
        },
    )
    resp = client.get("/tools/list", params={"workflow_id": "http-list-1", "path": "src"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    names = {e["name"] for e in body["entries"]}
    assert "file.py" in names


def test_history_endpoint(client):
    client.post(
        "/tools/write",
        json={
            "workflow_id": "http-hist-1",
            "path": "src/f.py",
            "content": "y",
            "agent_id": "tester",
        },
    )
    resp = client.get("/tools/history", params={"workflow_id": "http-hist-1"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["commits"]


def test_diff_endpoint(client):
    client.post(
        "/tools/write",
        json={
            "workflow_id": "http-diff-1",
            "path": "src/f.py",
            "content": "y",
            "agent_id": "tester",
        },
    )
    history = client.get(
        "/tools/history", params={"workflow_id": "http-diff-1"}
    ).json()
    commit = history["commits"][0]["hash"]
    resp = client.get(
        "/tools/diff",
        params={"workflow_id": "http-diff-1", "commit": commit},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "diff --git" in body["diff"]


def test_reset_endpoint(client):
    client.post(
        "/tools/write",
        json={
            "workflow_id": "http-reset-1",
            "path": "src/x.py",
            "content": "v1",
            "agent_id": "tester",
        },
    )
    history = client.get(
        "/tools/history", params={"workflow_id": "http-reset-1"}
    ).json()
    head = history["commits"][-1]["hash"]
    client.post(
        "/tools/write",
        json={
            "workflow_id": "http-reset-1",
            "path": "src/x.py",
            "content": "v2",
            "agent_id": "tester",
        },
    )
    resp = client.post(
        "/tools/reset",
        json={
            "workflow_id": "http-reset-1",
            "commit_hash": head,
            "agent_id": "main-agent",
        },
    )
    assert resp.status_code == 200, resp.text
    body = client.get(
        "/tools/read", params={"workflow_id": "http-reset-1", "path": "src/x.py"}
    ).json()
    assert body["content"] == "v1"


def test_envelope_dispatch(client):
    """A tool envelope routed via /envelope is dispatched correctly."""
    client.post(
        "/tools/write",
        json={
            "workflow_id": "http-env-1",
            "path": "src/main.py",
            "content": "v1",
            "agent_id": "tester",
        },
    )
    envelope = {
        "envelope_type": "request",
        "sender": {"agent_id": "coder", "role": "executor"},
        "receiver": {"agent_id": "harness", "role": "harness"},
        "payload": {
            "content_type": "tool",
            "tool_name": "harness_read_file",
            "action": "invoke",
            "parameters": {
                "workflow_id": "http-env-1",
                "path": "src/main.py",
            },
        },
    }
    resp = client.post("/envelope", json=envelope)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert body["content"] == "v1"


def test_envelope_unsupported_tool(client):
    envelope = {
        "envelope_type": "request",
        "sender": {"agent_id": "coder", "role": "executor"},
        "receiver": {"agent_id": "harness", "role": "harness"},
        "payload": {
            "content_type": "tool",
            "tool_name": "harness_unknown",
            "action": "invoke",
            "parameters": {"workflow_id": "x"},
        },
    }
    resp = client.post("/envelope", json=envelope)
    assert resp.status_code == 400
