"""Tests for HarnessClient and InProcessHarnessClient."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from harness.client import (
    DEFAULT_BASE_URL,
    HarnessBadRequest,
    HarnessClient,
    HarnessError,
    HarnessPermissionDenied,
    InProcessHarnessClient,
)
from harness.executor import CodeExecutor, SubprocessBackend
from harness.git_tracker import GitTracker
from harness.diff_generator import DiffGenerator
from harness.server import HarnessServer
from harness.workspace import WorkspaceManager


# ---------------------------------------------------------------------------
# In-process client (the path agent workers use in dev)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_inprocess_client_write_and_read(tmp_path, git_available):
    if not git_available:
        pytest.skip("git not available")
    mgr = WorkspaceManager(base_dir=tmp_path)
    server = HarnessServer(
        mgr,
        CodeExecutor(backend=SubprocessBackend()),
        GitTracker(),
        DiffGenerator(),
    )
    client = InProcessHarnessClient(server)
    write = await client.write_file("wf-ip-1", "src/x.py", "print(1)", agent_id="t")
    assert write["ok"] is True
    read = await client.read_file("wf-ip-1", "src/x.py")
    assert read["content"] == "print(1)"


@pytest.mark.asyncio
async def test_inprocess_client_exec(tmp_path, git_available):
    if not git_available:
        pytest.skip("git not available")
    mgr = WorkspaceManager(base_dir=tmp_path)
    server = HarnessServer(
        mgr,
        CodeExecutor(backend=SubprocessBackend()),
        GitTracker(),
        DiffGenerator(),
    )
    client = InProcessHarnessClient(server)
    out = await client.exec("wf-ip-exec", "python", "print('via-client')", timeout=10)
    assert out["ok"] is True
    assert "via-client" in out["execution"]["stdout"]


@pytest.mark.asyncio
async def test_inprocess_client_list_and_history(tmp_path, git_available):
    if not git_available:
        pytest.skip("git not available")
    mgr = WorkspaceManager(base_dir=tmp_path)
    server = HarnessServer(
        mgr,
        CodeExecutor(backend=SubprocessBackend()),
        GitTracker(),
        DiffGenerator(),
    )
    client = InProcessHarnessClient(server)
    await client.write_file("wf-ip-list", "src/a.py", "a", agent_id="t")
    await client.write_file("wf-ip-list", "src/b.py", "b", agent_id="t")
    listing = await client.list_files("wf-ip-list", "src")
    assert listing["ok"] is True
    names = {e["name"] for e in listing["entries"]}
    assert {"a.py", "b.py"} <= names
    hist = await client.get_history("wf-ip-list")
    assert hist["ok"] is True
    assert hist["commits"]


@pytest.mark.asyncio
async def test_inprocess_client_diff(tmp_path, git_available):
    if not git_available:
        pytest.skip("git not available")
    mgr = WorkspaceManager(base_dir=tmp_path)
    server = HarnessServer(
        mgr,
        CodeExecutor(backend=SubprocessBackend()),
        GitTracker(),
        DiffGenerator(),
    )
    client = InProcessHarnessClient(server)
    await client.write_file("wf-ip-diff", "src/x.py", "first", agent_id="t")
    hist = await client.get_history("wf-ip-diff")
    commit = hist["commits"][0]["hash"]
    diff = await client.get_diff("wf-ip-diff", commit)
    assert diff["ok"] is True
    assert "diff --git" in diff["diff"]


@pytest.mark.asyncio
async def test_inprocess_client_rejects_unbound_routes(tmp_path, git_available):
    """A server that does not expose the right handler raises HarnessError."""
    if not git_available:
        pytest.skip("git not available")
    mgr = WorkspaceManager(base_dir=tmp_path)
    server = HarnessServer(
        mgr,
        CodeExecutor(backend=SubprocessBackend()),
        GitTracker(),
        DiffGenerator(),
    )
    client = InProcessHarnessClient(server)
    # A path that isn't mapped should error.
    with pytest.raises(HarnessError):
        await client._request("GET", "/no-such-route")


# ---------------------------------------------------------------------------
# HTTP client (uses a mocked httpx transport)
# ---------------------------------------------------------------------------

def _mock_response(status: int, body: dict | str) -> httpx.Response:
    """Build a synthetic :class:`httpx.Response` for testing."""
    if isinstance(body, dict):
        request = httpx.Request("GET", "http://test/x")
        return httpx.Response(status, json=body, request=request)
    request = httpx.Request("GET", "http://test/x")
    return httpx.Response(status, text=body, request=request)


@pytest.mark.asyncio
async def test_http_client_exec(monkeypatch):
    """The HTTP client POSTs to /tools/exec and parses the JSON response."""
    captured: dict = {}

    async def fake_request(self, method, path, **kwargs):
        captured["method"] = method
        captured["path"] = path
        captured["kwargs"] = kwargs
        return _mock_response(200, {"ok": True, "execution": {"exit_code": 0}})

    # Patch the AsyncClient.request method on the instance.
    client = HarnessClient(base_url="http://test")
    client._client = AsyncMock()
    client._client.request = AsyncMock(
        return_value=_mock_response(200, {"ok": True, "execution": {"exit_code": 0}})
    )
    result = await client.exec("wf-1", "python", "print(1)", timeout=10)
    assert result["ok"] is True
    client._client.request.assert_awaited()


@pytest.mark.asyncio
async def test_http_client_write(monkeypatch):
    client = HarnessClient(base_url="http://test")
    client._client = AsyncMock()
    client._client.request = AsyncMock(
        return_value=_mock_response(200, {"ok": True, "bytes": 4})
    )
    result = await client.write_file("wf-1", "src/x.py", "data", agent_id="coder")
    assert result["ok"] is True
    client._client.request.assert_awaited()


@pytest.mark.asyncio
async def test_http_client_read_uses_query_params():
    client = HarnessClient(base_url="http://test")
    client._client = AsyncMock()
    client._client.request = AsyncMock(
        return_value=_mock_response(200, {"ok": True, "content": "x"})
    )
    result = await client.read_file("wf-1", "src/x.py")
    assert result["content"] == "x"
    client._client.request.assert_awaited()


@pytest.mark.asyncio
async def test_http_client_bad_request_raises():
    client = HarnessClient(base_url="http://test")
    client._client = AsyncMock()
    client._client.request = AsyncMock(
        return_value=_mock_response(400, {"detail": "bad"})
    )
    with pytest.raises(HarnessBadRequest):
        await client.exec("wf-1", "python", "x")


@pytest.mark.asyncio
async def test_http_client_permission_denied_raises():
    client = HarnessClient(base_url="http://test")
    client._client = AsyncMock()
    client._client.request = AsyncMock(
        return_value=_mock_response(403, {"detail": "forbidden"})
    )
    with pytest.raises(HarnessPermissionDenied):
        await client.exec("wf-1", "python", "x")


@pytest.mark.asyncio
async def test_http_client_other_status_raises_harness_error():
    client = HarnessClient(base_url="http://test")
    client._client = AsyncMock()
    client._client.request = AsyncMock(
        return_value=_mock_response(500, "boom")
    )
    with pytest.raises(HarnessError) as excinfo:
        await client.exec("wf-1", "python", "x")
    assert excinfo.value.status_code == 500


@pytest.mark.asyncio
async def test_http_client_transport_error_raises():
    client = HarnessClient(base_url="http://test")
    client._client = AsyncMock()
    client._client.request = AsyncMock(side_effect=httpx.HTTPError("boom"))
    with pytest.raises(HarnessError):
        await client.exec("wf-1", "python", "x")


@pytest.mark.asyncio
async def test_http_client_invalid_json_raises():
    client = HarnessClient(base_url="http://test")
    client._client = AsyncMock()
    request = httpx.Request("GET", "http://test/x")
    bad_response = httpx.Response(200, text="not json {", request=request)
    client._client.request = AsyncMock(return_value=bad_response)
    with pytest.raises(HarnessError):
        await client.exec("wf-1", "python", "x")


def test_default_base_url_can_be_overridden_by_env(monkeypatch):
    monkeypatch.setenv("OPENSWARM_HARNESS_URL", "http://harness.test:9999")
    # Re-import to pick up the env var.
    import importlib
    from harness import client as client_mod
    importlib.reload(client_mod)
    assert client_mod.DEFAULT_BASE_URL == "http://harness.test:9999"
    importlib.reload(client_mod)  # restore
