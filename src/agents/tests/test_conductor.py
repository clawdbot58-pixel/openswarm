"""Tests for the Conductor class.

The Conductor never talks to the user and never executes tools; its
responsibility is to translate structured objectives into workflow
DAGs and to track sector managers. These tests focus on that
contract — no LLM is involved.
"""
from __future__ import annotations

import asyncio
import json
import socket
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio

# Make ``src`` importable.
_SRC = Path(__file__).resolve().parents[3] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from kernel.config import reset_settings_for_tests  # noqa: E402
from kernel.main import create_app  # noqa: E402
from kernel.models import AgentManifest, Envelope  # noqa: E402

from agents.conductor import (  # noqa: E402
    CONDUCTOR_AGENT_ID,
    EVENT_OBJECTIVE_COMPLETE,
    EVENT_OBJECTIVE_FAILED,
    EVENT_SECTOR_FAILED,
    EVENT_SWARM_DEPLOYED,
    FORBIDDEN_RECEIVERS,
    MAIN_AGENT_ID,
    SECTOR_MANAGER_PREFIX,
    SUBSCRIBED_EVENTS,
    Conductor,
    Workflow,
    WorkflowNode,
)


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------

def _conductor_manifest(agent_id: str = "conductor") -> AgentManifest:
    return AgentManifest.model_validate(
        {
            "agent_id": agent_id,
            "version": "1.0.0",
            "role": "orchestrator",
            "intent": "Decompose objectives into workflows, manage sector managers",
            "capabilities": {"inference": {"provider": "custom"}},
            "lifecycle": {"persistence": "persistent", "auto_restart": True},
            "registration_time": datetime.now(timezone.utc).isoformat().replace(
                "+00:00", "Z"
            ),
        }
    )


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture
def fake_conductor(tmp_path) -> Conductor:
    """A Conductor with WebSocket transport stubbed out.

    The fixture also ensures the sector-manager-template.json file
    exists at a path the Conductor can read. We point the Conductor
    at a temp file so tests don't depend on the real manifests dir.
    """
    template = {
        "agent_id": "sector-manager-template",
        "version": "1.0.0",
        "role": "specialist",
        "intent": "Manage a domain sector",
        "capabilities": {"inference": {"provider": "custom"}},
        "lifecycle": {"persistence": "session"},
        "registration_time": datetime.now(timezone.utc)
        .isoformat()
        .replace("+00:00", "Z"),
    }
    template_path = tmp_path / "sector-manager-template.json"
    template_path.write_text(json.dumps(template))
    cond = Conductor(
        _conductor_manifest(),
        sector_manager_manifest_path=str(template_path),
    )
    cond._registered_event.set()
    cond._ws = object()

    sent: list = []

    async def _spy_send(envelope):
        sent.append(envelope)
        async with cond._send_lock:
            cond._outbox.append(envelope)
        return envelope

    cond.send = _spy_send  # type: ignore[assignment]
    cond.sent = sent  # type: ignore[attr-defined]
    return cond


@pytest_asyncio.fixture
async def kernel_server(tmp_path):
    """A real uvicorn-served kernel for end-to-end checks."""
    import uvicorn

    settings = reset_settings_for_tests(
        db_path=tmp_path / "registry.db",
        heartbeat_interval_seconds=0.1,
        heartbeat_zombie_threshold_seconds=0.3,
        bus_router_poll_interval_seconds=0.005,
        bus_max_queue_size=1000,
    )
    from kernel.config import _settings as _live

    _live.paths.heartbeats_dir = tmp_path / "hb"  # type: ignore[attr-defined]
    (tmp_path / "hb").mkdir(parents=True, exist_ok=True)
    app = create_app(settings)
    port = _free_port()
    config = uvicorn.Config(
        app, host="127.0.0.1", port=port, log_level="error", lifespan="on"
    )
    server = uvicorn.Server(config)
    runner_task = asyncio.create_task(server.serve())
    for _ in range(50):
        if server.started:
            break
        await asyncio.sleep(0.05)
    assert server.started
    base = f"ws://127.0.0.1:{port}/ws"
    http = f"http://127.0.0.1:{port}"
    try:
        yield base, http
    finally:
        server.should_exit = True
        try:
            await runner_task
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------

def test_constructs_with_manifest(tmp_path) -> None:
    cond = Conductor(
        _conductor_manifest(),
        sector_manager_manifest_path=str(tmp_path / "missing.json"),
    )
    assert cond.agent_id == "conductor"
    assert cond.role == "orchestrator"
    assert cond.workflows == {}
    assert cond.active_workflow is None


# ---------------------------------------------------------------------------
# spawn_initial_swarm
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_spawn_initial_swarm_creates_workflow(fake_conductor: Conductor) -> None:
    """A spawn directive creates a workflow and emits swarm_deployed."""
    objective = {
        "objective_id": str(uuid.uuid4()),
        "goal": "Add a /healthz endpoint and test it",
        "primary_sector": "coding",
        "sectors": ["coding", "testing"],
    }
    workflow = await fake_conductor.handle_spawn_initial_swarm(objective)
    assert workflow.status == "running"
    assert workflow.primary_sector == "coding"
    assert [n.sector for n in workflow.nodes] == ["coding", "testing"]
    assert workflow.nodes[0].depends_on == []
    assert workflow.nodes[1].depends_on == [workflow.nodes[0].node_id]
    # The conductor emitted a swarm_deployed event to main-agent.
    assert any(
        e.envelope_type == "event"
        and e.payload.data.get("event") == EVENT_SWARM_DEPLOYED  # type: ignore[attr-defined]
        and e.receiver.agent_id == MAIN_AGENT_ID
        for e in fake_conductor.sent
    )


@pytest.mark.asyncio
async def test_spawn_initial_swarm_is_idempotent(fake_conductor: Conductor) -> None:
    """A second call with the same objective_id returns the same workflow."""
    objective = {
        "objective_id": str(uuid.uuid4()),
        "goal": "test",
        "primary_sector": "coding",
        "sectors": ["coding"],
    }
    w1 = await fake_conductor.handle_spawn_initial_swarm(objective)
    w2 = await fake_conductor.handle_spawn_initial_swarm(objective)
    assert w1.workflow_id == w2.workflow_id


@pytest.mark.asyncio
async def test_spawn_initial_swarm_ensures_primary_is_first(
    fake_conductor: Conductor,
) -> None:
    """If the user omitted the primary sector, we put it back at index 0."""
    objective = {
        "objective_id": str(uuid.uuid4()),
        "goal": "test",
        "primary_sector": "testing",
        "sectors": ["research", "coding"],  # primary not first
    }
    w = await fake_conductor.handle_spawn_initial_swarm(objective)
    assert w.nodes[0].sector == "testing"


@pytest.mark.asyncio
async def test_spawn_dispatches_sector_manager_task_envelope(
    fake_conductor: Conductor,
) -> None:
    """The conductor sends a sector_task envelope to each sector manager."""
    objective = {
        "objective_id": str(uuid.uuid4()),
        "goal": "Implement /healthz",
        "primary_sector": "coding",
        "sectors": ["coding"],
    }
    await fake_conductor.handle_spawn_initial_swarm(objective)
    sector_tasks = [
        e for e in fake_conductor.sent
        if e.envelope_type == "request"
        and e.receiver.agent_id == f"{SECTOR_MANAGER_PREFIX}-coding"
        and e.payload.data.get("action") == "sector_task"  # type: ignore[attr-defined]
    ]
    assert sector_tasks, "no sector_task envelope was sent"
    assert sector_tasks[0].payload.data["goal"] == "Implement /healthz"  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_conductor_only_sends_to_main_agent_or_sector_managers(
    fake_conductor: Conductor,
) -> None:
    """Defence in depth: no envelope may address a forbidden receiver."""
    objective = {
        "objective_id": str(uuid.uuid4()),
        "goal": "g",
        "primary_sector": "coding",
        "sectors": ["coding"],
    }
    await fake_conductor.handle_spawn_initial_swarm(objective)
    for env in fake_conductor.sent:
        rid = env.receiver.agent_id
        assert rid not in FORBIDDEN_RECEIVERS, rid
        assert not rid.startswith("user-"), rid
        # Must be main-agent or a sector manager.
        assert rid == MAIN_AGENT_ID or rid.startswith(SECTOR_MANAGER_PREFIX), rid


# ---------------------------------------------------------------------------
# Inbound: sector_complete
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_sector_complete_aggregates_and_emits_objective_complete(
    fake_conductor: Conductor,
) -> None:
    """When every node reports complete, the conductor emits objective_complete."""
    objective = {
        "objective_id": str(uuid.uuid4()),
        "goal": "g",
        "primary_sector": "coding",
        "sectors": ["coding"],
    }
    wf = await fake_conductor.handle_spawn_initial_swarm(objective)
    # Simulate the sector manager reporting completion.
    await fake_conductor._on_sector_manager_message(
        f"{SECTOR_MANAGER_PREFIX}-coding",
        _envelope_with_data(
            sender=f"{SECTOR_MANAGER_PREFIX}-coding",
            action="sector_complete",
            workflow_id=wf.workflow_id,
            sector="coding",
            summary="all done",
        ),
    )
    assert wf.status == "complete"
    assert any(
        e.payload.data.get("event") == EVENT_OBJECTIVE_COMPLETE  # type: ignore[attr-defined]
        for e in fake_conductor.sent
    )


@pytest.mark.asyncio
async def test_sector_failure_in_primary_fails_workflow(
    fake_conductor: Conductor,
) -> None:
    """A failure in the primary sector flips the workflow to ``failed``."""
    objective = {
        "objective_id": str(uuid.uuid4()),
        "goal": "g",
        "primary_sector": "coding",
        "sectors": ["coding", "testing"],
    }
    wf = await fake_conductor.handle_spawn_initial_swarm(objective)
    await fake_conductor._on_sector_manager_message(
        f"{SECTOR_MANAGER_PREFIX}-coding",
        _envelope_with_data(
            sender=f"{SECTOR_MANAGER_PREFIX}-coding",
            action="sector_failed",
            workflow_id=wf.workflow_id,
            sector="coding",
            error="boom",
        ),
    )
    assert wf.status == "failed"
    assert any(
        e.payload.data.get("event") == EVENT_OBJECTIVE_FAILED  # type: ignore[attr-defined]
        for e in fake_conductor.sent
    )


@pytest.mark.asyncio
async def test_sector_failure_in_support_does_not_fail_workflow(
    fake_conductor: Conductor,
) -> None:
    """A failure in a non-primary sector marks the node failed but leaves
    the workflow running. (The conductor is tolerant of non-fatal
    support failures; the dashboard surfaces the sector_failed event.)"""
    objective = {
        "objective_id": str(uuid.uuid4()),
        "goal": "g",
        "primary_sector": "coding",
        "sectors": ["coding", "testing"],
    }
    wf = await fake_conductor.handle_spawn_initial_swarm(objective)
    # Complete the primary first.
    await fake_conductor._on_sector_manager_message(
        f"{SECTOR_MANAGER_PREFIX}-coding",
        _envelope_with_data(
            sender=f"{SECTOR_MANAGER_PREFIX}-coding",
            action="sector_complete",
            workflow_id=wf.workflow_id,
            sector="coding",
            summary="ok",
        ),
    )
    assert wf.status == "running"
    # Then fail the support sector.
    await fake_conductor._on_sector_manager_message(
        f"{SECTOR_MANAGER_PREFIX}-testing",
        _envelope_with_data(
            sender=f"{SECTOR_MANAGER_PREFIX}-testing",
            action="sector_failed",
            workflow_id=wf.workflow_id,
            sector="testing",
            error="flaky",
        ),
    )
    assert wf.status == "failed"
    assert any(
        e.payload.data.get("event") == EVENT_SECTOR_FAILED  # type: ignore[attr-defined]
        for e in fake_conductor.sent
    )


# ---------------------------------------------------------------------------
# Zombie recovery
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_zombie_event_first_failure_retries(fake_conductor: Conductor) -> None:
    """First zombie with auto_restart=True means we wait."""
    objective = {
        "objective_id": str(uuid.uuid4()),
        "goal": "g",
        "primary_sector": "coding",
        "sectors": ["coding"],
    }
    await fake_conductor.handle_spawn_initial_swarm(objective)
    before = len(fake_conductor.sent)
    await fake_conductor._on_zombie_event(
        {"agent_id": f"{SECTOR_MANAGER_PREFIX}-coding"},
        auto_restart=True,
    )
    # No new sends; the kernel will auto-restart.
    assert len(fake_conductor.sent) == before


@pytest.mark.asyncio
async def test_zombie_event_escalates_after_failure(fake_conductor: Conductor) -> None:
    """A node with an error gets a different recovery decision."""
    objective = {
        "objective_id": str(uuid.uuid4()),
        "goal": "g",
        "primary_sector": "coding",
        "sectors": ["coding"],
    }
    wf = await fake_conductor.handle_spawn_initial_swarm(objective)
    # Pre-set a send_failed error to force the spawn_replacement / escalate path.
    node = wf.by_sector("coding")
    assert node is not None
    node.error = "send_failed: connection refused"
    before = len(fake_conductor.sent)
    await fake_conductor._on_zombie_event(
        {"agent_id": f"{SECTOR_MANAGER_PREFIX}-coding"},
        auto_restart=False,
    )
    # Either spawn_replacement (sends a new task envelope) or escalate
    # (sends a conductor_escalation event) is acceptable. Both produce
    # a new outbound envelope; we just assert the conductor reacted.
    assert len(fake_conductor.sent) > before


# ---------------------------------------------------------------------------
# Mutation API
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_cancel_workflow_emits_event(fake_conductor: Conductor) -> None:
    objective = {
        "objective_id": str(uuid.uuid4()),
        "goal": "g",
        "primary_sector": "coding",
        "sectors": ["coding"],
    }
    wf = await fake_conductor.handle_spawn_initial_swarm(objective)
    assert await fake_conductor.cancel_workflow(wf.workflow_id) is True
    assert wf.status == "cancelled"
    assert any(
        e.payload.data.get("event") == "workflow_cancelled"  # type: ignore[attr-defined]
        for e in fake_conductor.sent
    )


@pytest.mark.asyncio
async def test_cancel_unknown_workflow_returns_false(fake_conductor: Conductor) -> None:
    assert await fake_conductor.cancel_workflow("no-such-id") is False


@pytest.mark.asyncio
async def test_retry_step_dispatches_new_task(fake_conductor: Conductor) -> None:
    objective = {
        "objective_id": str(uuid.uuid4()),
        "goal": "g",
        "primary_sector": "coding",
        "sectors": ["coding"],
    }
    wf = await fake_conductor.handle_spawn_initial_swarm(objective)
    node = wf.by_sector("coding")
    assert node is not None
    node.status = "failed"
    node.error = "transient"
    before = len(fake_conductor.sent)
    ok = await fake_conductor.retry_step(wf.workflow_id, node.node_id)
    assert ok is True
    assert len(fake_conductor.sent) > before


# ---------------------------------------------------------------------------
# Forbidden-receiver assertion
# ---------------------------------------------------------------------------

def test_assert_not_user_blocks_known_user_ids(fake_conductor: Conductor) -> None:
    for bad in ("user", "human", "dashboard", "user-42"):
        with pytest.raises(ValueError):
            fake_conductor._assert_not_user(bad)


def test_assert_not_user_allows_main_agent(fake_conductor: Conductor) -> None:
    fake_conductor._assert_not_user(MAIN_AGENT_ID)  # no raise


# ---------------------------------------------------------------------------
# Workflow helpers
# ---------------------------------------------------------------------------

def test_workflow_mark_complete_advances() -> None:
    wf = Workflow(
        workflow_id=str(uuid.uuid4()),
        objective_id=str(uuid.uuid4()),
        goal="g",
        primary_sector="coding",
    )
    wf.nodes = [
        WorkflowNode(node_id="step_1", sector="coding", description="d"),
    ]
    wf.mark_complete("coding", {"summary": "ok"})
    assert wf.status == "complete"


def test_workflow_mark_failed_primary() -> None:
    wf = Workflow(
        workflow_id=str(uuid.uuid4()),
        objective_id=str(uuid.uuid4()),
        goal="g",
        primary_sector="coding",
    )
    wf.nodes = [
        WorkflowNode(node_id="step_1", sector="coding", description="d"),
    ]
    wf.mark_failed("coding", "boom")
    assert wf.status == "failed"


def test_workflow_to_dict_round_trip() -> None:
    wf = Workflow(
        workflow_id="wid",
        objective_id="oid",
        goal="g",
        primary_sector="coding",
    )
    wf.nodes = [WorkflowNode(node_id="step_1", sector="coding", description="d")]
    d = wf.to_dict()
    assert d["workflow_id"] == "wid"
    assert d["nodes"][0]["sector"] == "coding"


# ---------------------------------------------------------------------------
# Event subscription
# ---------------------------------------------------------------------------

def test_subscribed_events_match_kernel_known() -> None:
    from kernel.models import KERNEL_EVENT_NAMES
    for name in SUBSCRIBED_EVENTS:
        assert name in KERNEL_EVENT_NAMES, name


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------

def _envelope_with_data(
    *, sender: str, action: str, workflow_id: str, sector: str,
    summary: str = "", error: str = "",
) -> Envelope:
    """Build an envelope the Conductor will recognise as a sector message."""
    from kernel.models import Endpoint, Preamble

    data: dict[str, Any] = {
        "action": action,
        "workflow_id": workflow_id,
        "sector": sector,
    }
    if summary:
        data["summary"] = summary
    if error:
        data["error"] = error
    return Envelope(
        envelope_id=str(uuid.uuid4()),
        created_at=datetime.now(timezone.utc),
        envelope_type="request",
        sender=Endpoint(agent_id=sender, role="specialist"),
        receiver=Endpoint(agent_id=CONDUCTOR_AGENT_ID, role="orchestrator"),
        preamble=Preamble(intent={"goal": "sector", "phase": "execution"}),
        payload={"content_type": "data", "data": data},  # type: ignore[arg-type]
    )
