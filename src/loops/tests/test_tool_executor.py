"""Tests for ToolExecutor (Phase 5)."""

import pytest

from loops import ToolExecutor, ToolResult


@pytest.fixture
def manifest():
    """Create a test manifest."""
    return {
        "agent_id": "test-agent",
        "capabilities": {
            "tools": [
                {
                    "name": "file_read",
                    "description": "Read a file",
                    "parameters": {"path": {"type": "string", "required": True}},
                },
                {
                    "name": "file_write",
                    "description": "Write a file",
                    "parameters": {
                        "path": {"type": "string", "required": True},
                        "content": {"type": "string", "required": True},
                    },
                },
                {
                    "name": "harness_exec",
                    "description": "Run code in the harness sandbox",
                    "parameters": {
                        "workflow_id": {"type": "string", "required": True},
                        "runtime": {"type": "string", "required": True},
                        "code": {"type": "string", "required": True},
                    },
                    "side_effects": ["harness:execute"],
                },
            ]
        },
    }


@pytest.fixture
def executor(manifest):
    """Create a tool executor (no harness client wired)."""
    return ToolExecutor(manifest)


@pytest.mark.asyncio
async def test_tool_executor_validates_tool_exists(executor):
    """Test that tool executor rejects unknown tools."""
    result = await executor.execute(
        "unknown_tool", {}, {"file_system": {"allow": ["/*"]}}
    )

    assert result.status == "error"
    assert "not in manifest" in result.message


@pytest.mark.asyncio
async def test_tool_executor_validates_params(executor):
    """Test that tool executor validates parameters."""
    result = await executor.execute(
        "file_read", {}, {"file_system": {"allow": ["/workspace/*"]}}
    )

    assert result.status == "error"
    assert "Invalid parameter" in result.message or "required" in result.message


@pytest.mark.asyncio
async def test_tool_executor_read_only_check(executor):
    """Test that tool executor rejects writes in read-only mode."""
    permissions = {"file_system": {"allow": ["/workspace/*"], "read_only": True}}

    result = await executor.execute("file_write", {"path": "test.txt", "content": "x"}, permissions)

    assert result.status == "error"
    assert "read-only" in result.message.lower() or "permission" in result.message.lower()


@pytest.mark.asyncio
async def test_tool_executor_path_validation(executor):
    """Test that tool executor validates file paths."""
    permissions = {"file_system": {"allow": ["/workspace/*"]}}

    result = await executor.execute(
        "file_read", {"path": "/etc/passwd"}, permissions
    )

    assert result.status == "error"
    assert "not in allowed" in result.message.lower() or "permission" in result.message.lower()


@pytest.mark.asyncio
async def test_tool_executor_accepts_valid_local_call(executor):
    """Phase 5: a valid local call returns ok (no harness involved)."""
    permissions = {"file_system": {"allow": ["/workspace/*"]}}

    result = await executor.execute(
        "file_read", {"path": "/workspace/test.py"}, permissions
    )

    assert result.status == "ok"
    assert "file_read" in result.message


def test_executor_lists_tools(executor):
    """Test that executor lists available tools."""
    tools = executor.list_tools()

    assert "file_read" in tools
    assert "file_write" in tools
    assert "harness_exec" in tools


@pytest.mark.asyncio
async def test_tool_executor_routes_harness_to_client(manifest):
    """Harness tools are dispatched to the configured client."""
    class FakeClient:
        def __init__(self):
            self.calls = []

        async def exec(self, **kwargs):
            self.calls.append(("exec", kwargs))
            return {"ok": True, "execution": {"exit_code": 0}, "commit": {"hash": "abc"}}

    fake = FakeClient()
    ex = ToolExecutor(manifest, harness_client=fake)
    result = await ex.execute(
        "harness_exec",
        {"workflow_id": "wf-1", "runtime": "python", "code": "print(1)"},
        {"harness": {"can_execute_code": True, "allowed_runtimes": ["python"]}},
    )
    assert result.status == "ok"
    assert fake.calls and fake.calls[0][0] == "exec"


@pytest.mark.asyncio
async def test_tool_executor_blocks_runtime_not_allowed(manifest):
    """Harness exec rejects a runtime not in the allowlist."""
    ex = ToolExecutor(manifest)
    result = await ex.execute(
        "harness_exec",
        {"workflow_id": "wf-1", "runtime": "node", "code": "console.log(1)"},
        {"harness": {"can_execute_code": True, "allowed_runtimes": ["python"]}},
    )
    assert result.status == "error"
    assert result.error == "permission_denied"