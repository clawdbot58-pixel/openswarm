"""Tests for the :class:`~kernel.registry.AgentRegistry`.

Covers:

* register a valid manifest and read it back
* reject an invalid manifest
* status updates + heartbeats
* filtering by status
* soft delete via :meth:`AgentRegistry.unregister`
* audit log append/retrieve
"""
from __future__ import annotations

import pytest

from kernel.exceptions import AgentNotFound
from kernel.models import AgentManifest
from kernel.registry import AgentRegistry


def _manifest(agent_id: str = "coder-fast", **overrides) -> dict:
    base = {
        "agent_id": agent_id,
        "version": "1.0.0",
        "role": "executor",
        "intent": f"test {agent_id}",
        "capabilities": {
            "inference": {"provider": "anthropic"},
            "tools": [{"name": "fs.read", "description": "read a file"}],
        },
        "lifecycle": {"persistence": "ephemeral"},
        "registration_time": "2026-06-04T10:00:00Z",
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_register_valid_manifest_and_retrieve(kernel_test):
    manifest = AgentManifest.model_validate(_manifest())
    await kernel_test.registry.register(manifest)
    got = await kernel_test.registry.get("coder-fast")
    assert got.agent_id == "coder-fast"
    assert got.role == "executor"
    assert got.has_tool("fs.read")
    # Status row.
    status = await kernel_test.registry.get_status("coder-fast")
    assert status["agent_id"] == "coder-fast"
    assert status["connected_ws"] is False


@pytest.mark.asyncio
async def test_register_invalid_manifest_raises(kernel_test):
    """Pydantic validation surfaces as a Pydantic error (not a 500)."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        AgentManifest.model_validate(
            {
                "agent_id": "1bad-id",  # starts with digit → pattern fail
                "version": "1.0.0",
                "role": "executor",
                "intent": "x",
                "capabilities": {"inference": {"provider": "anthropic"}},
                "lifecycle": {"persistence": "ephemeral"},
                "registration_time": "2026-06-04T10:00:00Z",
            }
        )


@pytest.mark.asyncio
async def test_register_rejects_manifest_type_mismatch(kernel_test):
    """Passing a non-AgentManifest to register raises ManifestRejected."""
    from kernel.exceptions import ManifestRejected

    with pytest.raises(ManifestRejected):
        await kernel_test.registry.register({"agent_id": "x"})  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_update_status_and_heartbeat(kernel_test):
    await kernel_test.registry.register(AgentManifest.model_validate(_manifest()))
    await kernel_test.registry.update_status("coder-fast", "ready")
    st = await kernel_test.registry.get_status("coder-fast")
    assert st["status"] == "ready"
    await kernel_test.registry.update_heartbeat("coder-fast")
    st2 = await kernel_test.registry.get_status("coder-fast")
    assert st2["last_heartbeat"] is not None


@pytest.mark.asyncio
async def test_list_filtered_by_status(kernel_test):
    await kernel_test.registry.register(AgentManifest.model_validate(_manifest("a", role="executor")))
    await kernel_test.registry.register(AgentManifest.model_validate(_manifest("b", role="executor")))
    await kernel_test.registry.register(AgentManifest.model_validate(_manifest("c", role="orchestrator")))
    await kernel_test.registry.update_status("a", "ready")
    await kernel_test.registry.update_status("b", "busy")
    await kernel_test.registry.update_status("c", "ready")
    ready = await kernel_test.registry.list_status(status_filter="ready")  # type: ignore[arg-type]
    ready_ids = {r["agent_id"] for r in ready}
    assert ready_ids == {"a", "c"}
    busy = await kernel_test.registry.list_status(status_filter="busy")  # type: ignore[arg-type]
    assert {r["agent_id"] for r in busy} == {"b"}


@pytest.mark.asyncio
async def test_unregister_soft_deletes(kernel_test):
    await kernel_test.registry.register(AgentManifest.model_validate(_manifest()))
    await kernel_test.registry.unregister("coder-fast")
    st = await kernel_test.registry.get_status("coder-fast")
    assert st["status"] == "offline"
    assert st["connected_ws"] is False


@pytest.mark.asyncio
async def test_unregister_missing_agent_raises(kernel_test):
    with pytest.raises(AgentNotFound):
        await kernel_test.registry.unregister("ghost")


@pytest.mark.asyncio
async def test_audit_log_appends_and_retrieves(kernel_test):
    await kernel_test.registry.register(AgentManifest.model_validate(_manifest()))
    await kernel_test.registry.audit(
        action="test_action", result="ok", agent_id="coder-fast"
    )
    log = await kernel_test.registry.audit_log(agent_id="coder-fast")
    assert len(log) == 1
    assert log[0]["action"] == "test_action"
    assert log[0]["result"] == "ok"
    assert log[0]["agent_id"] == "coder-fast"


@pytest.mark.asyncio
async def test_healthcheck_reports_db_ok(kernel_test):
    assert await kernel_test.registry.healthcheck() is True


@pytest.mark.asyncio
async def test_set_connected_toggles_flag(kernel_test):
    await kernel_test.registry.register(AgentManifest.model_validate(_manifest()))
    await kernel_test.registry.set_connected("coder-fast", True)
    st = await kernel_test.registry.get_status("coder-fast")
    assert st["connected_ws"] is True
    await kernel_test.registry.set_connected("coder-fast", False)
    st2 = await kernel_test.registry.get_status("coder-fast")
    assert st2["connected_ws"] is False


@pytest.mark.asyncio
async def test_replace_existing_manifest(kernel_test):
    """Calling register() with the same agent_id overwrites the manifest."""
    m1 = AgentManifest.model_validate(_manifest())
    m2 = AgentManifest.model_validate(
        _manifest(human_readable_name="Renamed Agent")
    )
    await kernel_test.registry.register(m1)
    await kernel_test.registry.register(m2)
    got = await kernel_test.registry.get("coder-fast")
    assert got.human_readable_name == "Renamed Agent"
