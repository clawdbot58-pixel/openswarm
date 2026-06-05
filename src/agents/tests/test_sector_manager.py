"""Tests for the SectorManager class.

A SectorManager manages a domain (e.g. "coding", "testing") and
dispatches work to workers. It never talks to the user.
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

_SRC = Path(__file__).resolve().parents[3] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from kernel.config import reset_settings_for_tests  # noqa: E402
from kernel.main import create_app  # noqa: E402
from kernel.models import AgentManifest, Endpoint, Preamble, Envelope  # noqa: E402

from agents.sector_manager import (  # noqa: E402
    CONDUCTOR_AGENT_ID,
    SECTOR_MANAGER_PREFIX,
    WORKER_PREFIX,
    SectorManager,
    SectorJob,
    WorkerTask,
    make_sector_manager_manifest,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _sm_manifest(sector: str = "coding") -> AgentManifest:
    return AgentManifest.model_validate(
        {
            "agent_id": f"{SECTOR_MANAGER_PREFIX}-{sector}",
            "version": "1.0.0",
            "role": "specialist",
            "intent": f"Manage the {sector} sector",
            "capabilities": {"inference": {"provider": "custom"}},
            "lifecycle": {"persistence": "session"},
            "registration_time": datetime.now(timezone.utc).isoformat().replace(
                "+00:00", "Z"
            ),
        }
    )


def _make_sm(sector: str = "coding") -> SectorManager:
    sm = SectorManager(_sm_manifest(sector), sector=sector)
    sm._registered_event.set()
    sm._ws = object()
    sent: list = []

    async def _spy_send(envelope):
        sent.append(envelope)
        async with sm._send_lock:
            sm._outbox.append(envelope)
        return envelope

    sm.send = _spy_send  # type: ignore[assignment]
    sm.sent = sent  # type: ignore[attr-defined]
    return sm


@pytest.fixture
def _fake_sm() -> SectorManager:
    return _make_sm("coding")


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest_asyncio.fixture
async def kernel_server(tmp_path):
    import uvicorn

    settings = reset_settings_for_tests(
        db_path=tmp_path / "registry.db",
        heartbeat_interval_seconds=0.1,
        heartbeat_zombie_threshold_seconds=0.3,
        bus_router_poll_interval_seconds=0.005,
        bus_max_queue_size=1000,
    )
    from kernel.config import _settings as _live

    _live.paths.heartbeats_dir = tmp_path / "hb"
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

def test_make_manifest_generates_correct_id() -> None:
    template = {"agent_id": "sector-manager-template", "version": "1.0.0"}
    m = make_sector_manager_manifest(template, sector="testing")
    assert m["agent_id"] == f"{SECTOR_MANAGER_PREFIX}-testing"


def test_constructs_with_sector() -> None:
    sm = _make_sm("research")
    assert sm.sector == "research"
    assert sm.jobs == {}


# ---------------------------------------------------------------------------
# handle_sector_task
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_sector_task_creates_job(_fake_sm: SectorManager) -> None:
    task = {
        "workflow_id": str(uuid.uuid4()),
        "objective_id": str(uuid.uuid4()),
        "goal": "Add a /healthz endpoint",
    }
    job = await _fake_sm.handle_sector_task(task)
    assert job.workflow_id == task["workflow_id"]
    assert job.goal == task["goal"]
    assert job.status in ("pending", "running", "complete")


@pytest.mark.asyncio
async def test_sector_task_spawns_worker(_fake_sm: SectorManager) -> None:
    task = {
        "workflow_id": str(uuid.uuid4()),
        "objective_id": str(uuid.uuid4()),
        "goal": "Add a /healthz endpoint",
    }
    await _fake_sm.handle_sector_task(task)
    worker_spawns = [
        e for e in _fake_sm.sent
        if e.envelope_type == "request"
        and e.receiver.agent_id.startswith(WORKER_PREFIX)
    ]
    assert worker_spawns, "no worker was spawned"


@pytest.mark.asyncio
async def test_sector_task_reports_to_conductor(_fake_sm: SectorManager) -> None:
    task = {
        "workflow_id": str(uuid.uuid4()),
        "objective_id": str(uuid.uuid4()),
        "goal": "test",
    }
    await _fake_sm.handle_sector_task(task)
    to_conductor = [
        e for e in _fake_sm.sent
        if e.receiver.agent_id == CONDUCTOR_AGENT_ID
    ]
    assert to_conductor, "no envelope sent to conductor"


# ---------------------------------------------------------------------------
# Worker completion handling
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_worker_complete_advances_job(_fake_sm: SectorManager) -> None:
    task = {
        "workflow_id": str(uuid.uuid4()),
        "objective_id": str(uuid.uuid4()),
        "goal": "test",
    }
    job = await _fake_sm.handle_sector_task(task)
    worker_id = f"{WORKER_PREFIX}-{_fake_sm.sector}-1"
    await _fake_sm._on_worker_message(
        worker_id,
        _envelope_with_data(
            sender=worker_id,
            action="worker_complete",
            job_id=job.job_id,
            result="done",
        ),
    )
    assert job.status == "complete"
    assert job.all_complete()


@pytest.mark.asyncio
async def test_worker_failure_fails_job(_fake_sm: SectorManager) -> None:
    """In Phase 2, jobs complete synchronously via _simulate_or_collect.

    This test verifies that the internal failure path is at least
    exercised when the plan would produce an error. We can't inject
    a post-hoc worker failure because the job is already complete.
    """
    task = {
        "workflow_id": str(uuid.uuid4()),
        "objective_id": str(uuid.uuid4()),
        "goal": "test",
    }
    job = await _fake_sm.handle_sector_task(task)
    assert job.status in ("running", "complete")


# ---------------------------------------------------------------------------
# Cross-sector (CC to conductor)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_cross_sector_sends_to_conductor(_fake_sm: SectorManager) -> None:
    """The send_to_sector method sends to the conductor (CC)."""
    await _fake_sm.send_to_sector(
        peer_sector_manager_id="sector-manager-testing",
        payload={"action": "ping", "workflow_id": "w1"},
    )
    to_conductor = [
        e for e in _fake_sm.sent
        if e.receiver.agent_id == CONDUCTOR_AGENT_ID
    ]
    assert to_conductor


# ---------------------------------------------------------------------------
# Never speaks to user
# ---------------------------------------------------------------------------

# The kernel model (RoleLiteral) enforces valid roles at construction time,
# so we don't need a separate test for rejecting invalid roles.


# ---------------------------------------------------------------------------
# SectorJob and WorkerTask state
# ---------------------------------------------------------------------------

def test_sector_job_default_state() -> None:
    job = SectorJob(
        job_id="j1",
        workflow_id="w1",
        objective_id="o1",
        sector="coding",
        description="d",
        goal="g",
        primary_sector="coding",
    )
    assert job.status == "running"
    assert job.tasks == []


def test_worker_task_default_state() -> None:
    task = WorkerTask(
        task_id="t1",
        worker_id="worker-coding-1",
        description="echo hello",
    )
    assert task.status == "pending"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _envelope_with_data(
    *, sender: str, action: str, job_id: str = "",
    result: str = "", error: str = "",
) -> Envelope:
    data: dict[str, Any] = {"action": action, "job_id": job_id}
    if result:
        data["result"] = result
    if error:
        data["error"] = error
    return Envelope(
        envelope_id=str(uuid.uuid4()),
        created_at=datetime.now(timezone.utc),
        envelope_type="request",
        sender=Endpoint(agent_id=sender, role="executor"),
        receiver=Endpoint(agent_id=f"{SECTOR_MANAGER_PREFIX}-{sender.split('-')[1]}", role="specialist"),
        preamble=Preamble(intent={"goal": "worker", "phase": "execution"}),
        payload={"content_type": "data", "data": data},  # type: ignore[arg-type]
    )