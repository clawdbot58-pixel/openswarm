"""Tests for the :class:`~kernel.permissions.PermissionEnforcer`.

Covers:

* allowed tool call
* tool not in capabilities → deny
* filesystem path outside ``fs.allow`` → deny
* network call without permission → deny
* audit log records every check
* non-tool payloads are not permission-checked
"""
from __future__ import annotations

import pytest

from kernel.models import AgentManifest, Endpoint, Envelope, Preamble
from kernel.permissions import PermissionEnforcer


def _manifest(
    agent_id: str = "coder",
    *,
    tools: list[dict] | None = None,
    permissions: dict | None = None,
) -> AgentManifest:
    return AgentManifest.model_validate(
        {
            "agent_id": agent_id,
            "version": "1.0.0",
            "role": "executor",
            "intent": f"test {agent_id}",
            "capabilities": {
                "inference": {"provider": "anthropic"},
                "tools": tools
                or [
                    {
                        "name": "fs.read",
                        "description": "read a file",
                        "side_effects": ["fs:read"],
                    },
                    {
                        "name": "http.get",
                        "description": "GET a URL",
                        "side_effects": ["network:egress"],
                    },
                ],
            },
            "permissions": permissions,
            "lifecycle": {"persistence": "ephemeral"},
            "registration_time": "2026-06-04T10:00:00Z",
        }
    )


def _tool_envelope(
    sender_id: str, tool_name: str, params: dict | None = None
) -> Envelope:
    return Envelope(
        envelope_id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        created_at=__import__("datetime").datetime.now(
            __import__("datetime").timezone.utc
        ),
        envelope_type="request",
        sender=Endpoint(agent_id=sender_id, role="executor"),
        receiver=Endpoint(agent_id=sender_id, role="executor"),
        preamble=Preamble(intent={"goal": "x", "phase": "execution"}),
        payload={
            "content_type": "tool",
            "tool_name": tool_name,
            "action": "invoke",
            "parameters": params or {},
        },
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_valid_tool_call_allowed(kernel_test):
    await kernel_test.registry.register(
        _manifest(
            tools=[
                {
                    "name": "fs.read",
                    "description": "read a file",
                    "side_effects": ["fs:read"],
                }
            ],
            permissions={"file_system": {"allow": ["/tmp/*"], "read_only": True}},
        )
    )
    env = _tool_envelope("coder", "fs.read", {"path": "/tmp/foo.txt"})
    assert await kernel_test.permissions.check(env) is True


@pytest.mark.asyncio
async def test_tool_not_in_capabilities_denied(kernel_test):
    await kernel_test.registry.register(_manifest(tools=[]))
    env = _tool_envelope("coder", "shell.exec", {})
    assert await kernel_test.permissions.check(env) is False
    # Audit log records the deny.
    log = await kernel_test.registry.audit_log(agent_id="coder")
    assert any(
        r["action"] == "permission_check" and r["result"] == "deny"
        for r in log
    )


@pytest.mark.asyncio
async def test_path_outside_allow_denied(kernel_test):
    await kernel_test.registry.register(
        _manifest(
            tools=[
                {
                    "name": "fs.read",
                    "description": "read",
                    "side_effects": ["fs:read"],
                }
            ],
            permissions={"file_system": {"allow": ["/tmp/*"], "read_only": True}},
        )
    )
    env = _tool_envelope("coder", "fs.read", {"path": "/etc/passwd"})
    assert await kernel_test.permissions.check(env) is False


@pytest.mark.asyncio
async def test_deny_in_manifest_overrides_allow(kernel_test):
    """A pattern in `deny` should shadow the same pattern in `allow`."""
    await kernel_test.registry.register(
        _manifest(
            tools=[
                {
                    "name": "fs.read",
                    "description": "read",
                    "side_effects": ["fs:read"],
                }
            ],
            permissions={
                "file_system": {
                    "allow": ["/tmp/*"],
                    "deny": ["/tmp/secret/*"],
                    "read_only": True,
                }
            },
        )
    )
    # /tmp/foo is allowed.
    env1 = _tool_envelope("coder", "fs.read", {"path": "/tmp/foo"})
    assert await kernel_test.permissions.check(env1) is True
    # /tmp/secret/key is denied by the more specific rule.
    env2 = _tool_envelope(
        "coder", "fs.read", {"path": "/tmp/secret/key"}
    )
    assert await kernel_test.permissions.check(env2) is False


@pytest.mark.asyncio
async def test_network_call_without_permission_denied(kernel_test):
    await kernel_test.registry.register(
        _manifest(
            tools=[
                {
                    "name": "http.get",
                    "description": "http get",
                    "side_effects": ["network:egress"],
                }
            ],
            permissions=None,  # no network permission
        )
    )
    env = _tool_envelope("coder", "http.get", {"url": "https://example.com"})
    assert await kernel_test.permissions.check(env) is False


@pytest.mark.asyncio
async def test_network_call_with_allow_allowed(kernel_test):
    await kernel_test.registry.register(
        _manifest(
            tools=[
                {
                    "name": "http.get",
                    "description": "http get",
                    "side_effects": ["network:egress"],
                }
            ],
            permissions={"network": {"allow": ["https://api.example.com/*"]}},
        )
    )
    env = _tool_envelope(
        "coder", "http.get", {"url": "https://api.example.com/v1/x"}
    )
    assert await kernel_test.permissions.check(env) is True
    # Different host denied.
    env2 = _tool_envelope(
        "coder", "http.get", {"url": "https://other.example.org/x"}
    )
    assert await kernel_test.permissions.check(env2) is False


@pytest.mark.asyncio
async def test_fs_write_requires_writable_permission(kernel_test):
    """A write tool needs a non-read-only fs.allow."""
    await kernel_test.registry.register(
        _manifest(
            tools=[
                {
                    "name": "fs.write",
                    "description": "write",
                    "side_effects": ["fs:write"],
                }
            ],
            permissions={"file_system": {"allow": ["/tmp/*"], "read_only": True}},
        )
    )
    env = _tool_envelope("coder", "fs.write", {"path": "/tmp/foo"})
    assert await kernel_test.permissions.check(env) is False


@pytest.mark.asyncio
async def test_sender_not_registered_denied(kernel_test):
    """A tool call from a non-registered agent is denied by default."""
    env = _tool_envelope("ghost", "fs.read", {"path": "/tmp/foo"})
    assert await kernel_test.permissions.check(env) is False


@pytest.mark.asyncio
async def test_non_tool_payload_not_permission_checked(kernel_test):
    """Text/data/workflow payloads skip the enforcer entirely."""
    env = Envelope(
        envelope_id="bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
        created_at=__import__("datetime").datetime.now(
            __import__("datetime").timezone.utc
        ),
        envelope_type="request",
        sender=Endpoint(agent_id="ghost", role="executor"),
        receiver=Endpoint(agent_id="coder", role="executor"),
        preamble=Preamble(intent={"goal": "x", "phase": "execution"}),
        payload={"content_type": "text", "content": "hi"},
    )
    # Even with an unregistered sender, a text payload is allowed.
    assert await kernel_test.permissions.check(env) is True


@pytest.mark.asyncio
async def test_denial_emits_kernel_event_to_main(kernel_test):
    """A denial should produce a permission_denied event in the bus."""
    await kernel_test.registry.register(_manifest(tools=[]))
    received: list[Envelope] = []
    kernel_test.bus.add_event_listener(received.append)
    env = _tool_envelope("coder", "shell.exec", {})
    await kernel_test.permissions.check(env)
    # Allow the bus to fan out the event.
    import asyncio

    await asyncio.sleep(0.1)
    events = [
        e
        for e in received
        if e.payload.data.get("event") == "permission_denied"  # type: ignore[union-attr]
    ]
    assert events, "no permission_denied event emitted"


# ---------------------------------------------------------------------------
# Phase 5: harness side-effect tokens
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_harness_execute_allowed_when_can_execute_code(kernel_test):
    """``harness:execute`` is allowed when manifest grants ``can_execute_code``."""
    await kernel_test.registry.register(
        _manifest(
            tools=[
                {
                    "name": "harness.exec",
                    "description": "execute code in sandbox",
                    "side_effects": ["harness:execute"],
                }
            ],
            permissions={"harness": {"can_execute_code": True}},
        )
    )
    env = _tool_envelope("coder", "harness.exec", {"runtime": "python", "code": "print(1)"})
    assert await kernel_test.permissions.check(env) is True


@pytest.mark.asyncio
async def test_harness_execute_denied_without_can_execute_code(kernel_test):
    """``harness:execute`` is denied when the manifest lacks the flag."""
    await kernel_test.registry.register(
        _manifest(
            tools=[
                {
                    "name": "harness.exec",
                    "description": "execute code in sandbox",
                    "side_effects": ["harness:execute"],
                }
            ],
            permissions={"harness": {"can_execute_code": False}},
        )
    )
    env = _tool_envelope("coder", "harness.exec", {"runtime": "python", "code": "print(1)"})
    assert await kernel_test.permissions.check(env) is False


@pytest.mark.asyncio
async def test_harness_workspace_allowed_when_can_access_workspace(kernel_test):
    """``harness:workspace`` is allowed when manifest grants ``can_access_workspace``."""
    await kernel_test.registry.register(
        _manifest(
            tools=[
                {
                    "name": "harness.write_file",
                    "description": "write a file",
                    "side_effects": ["harness:workspace"],
                }
            ],
            permissions={"harness": {"can_access_workspace": True}},
        )
    )
    env = _tool_envelope("coder", "harness.write_file", {"path": "/workspace/x.py"})
    assert await kernel_test.permissions.check(env) is True


@pytest.mark.asyncio
async def test_harness_workspace_denied_without_can_access_workspace(kernel_test):
    """``harness:workspace`` is denied when the manifest lacks the flag."""
    await kernel_test.registry.register(
        _manifest(
            tools=[
                {
                    "name": "harness.write_file",
                    "description": "write a file",
                    "side_effects": ["harness:workspace"],
                }
            ],
            permissions={"harness": {"can_access_workspace": False}},
        )
    )
    env = _tool_envelope("coder", "harness.write_file", {"path": "/workspace/x.py"})
    assert await kernel_test.permissions.check(env) is False


@pytest.mark.asyncio
async def test_harness_permission_missing_block_all(kernel_test):
    """No harness block at all → both tokens are denied."""
    await kernel_test.registry.register(
        _manifest(
            tools=[
                {
                    "name": "harness.exec",
                    "description": "execute",
                    "side_effects": ["harness:execute"],
                }
            ],
            permissions=None,
        )
    )
    env = _tool_envelope("coder", "harness.exec", {"runtime": "python", "code": "x = 1"})
    assert await kernel_test.permissions.check(env) is False
