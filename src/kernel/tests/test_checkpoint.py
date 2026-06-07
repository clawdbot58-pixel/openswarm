"""Tests for the :class:`~kernel.checkpoint.CheckpointManager`.

Covers the operations the recovery executor and resume-on-boot code
rely on:

* writing a checkpoint, reading the latest one back
* listing every checkpoint for a workflow (oldest first)
* resume-from-checkpoint rebuilds a usable state dict
* mutate_count round-trips through the row
* delete-for-workflow wipes every row
"""
from __future__ import annotations

from pathlib import Path

import pytest

from kernel.checkpoint import Checkpoint, CheckpointManager


@pytest.fixture
async def cm(tmp_path: Path) -> CheckpointManager:
    """Fresh :class:`CheckpointManager` backed by a temp SQLite file."""
    mgr = CheckpointManager(tmp_path / "checkpoints.db")
    await mgr.initialize()
    try:
        yield mgr
    finally:
        await mgr.close()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_write_and_retrieve_latest_checkpoint(cm: CheckpointManager) -> None:
    cp = await cm.write_checkpoint(
        workflow_id="wf-1",
        step_id="step_a",
        state_blob={"counter": 1, "next_step_id": "step_b"},
        agent_outputs={"step_a": {"value": 42}},
    )
    assert cp.checkpoint_id > 0
    assert cp.workflow_id == "wf-1"
    assert cp.step_id == "step_a"
    assert cp.state_blob == {"counter": 1, "next_step_id": "step_b"}
    assert cp.agent_outputs == {"step_a": {"value": 42}}
    assert cp.mutate_count == 0
    assert cp.status == "completed"

    latest = await cm.get_latest_checkpoint("wf-1")
    assert latest is not None
    assert latest.checkpoint_id == cp.checkpoint_id
    assert latest.next_step_id == "step_b"


@pytest.mark.asyncio
async def test_get_latest_returns_none_for_unknown_workflow(
    cm: CheckpointManager,
) -> None:
    assert await cm.get_latest_checkpoint("does-not-exist") is None


@pytest.mark.asyncio
async def test_list_checkpoints_returns_all_in_order(
    cm: CheckpointManager,
) -> None:
    for i in range(5):
        await cm.write_checkpoint(
            workflow_id="wf-2",
            step_id=f"step_{i}",
            state_blob={"i": i},
            agent_outputs={f"step_{i}": i},
        )
    all_cps = await cm.list_checkpoints("wf-2")
    assert len(all_cps) == 5
    step_ids = [cp.step_id for cp in all_cps]
    assert step_ids == [f"step_{i}" for i in range(5)]


@pytest.mark.asyncio
async def test_get_checkpoint_by_id(cm: CheckpointManager) -> None:
    cp1 = await cm.write_checkpoint(
        workflow_id="wf-3",
        step_id="step_x",
        state_blob={"a": 1},
        agent_outputs={},
    )
    cp2 = await cm.write_checkpoint(
        workflow_id="wf-3",
        step_id="step_y",
        state_blob={"b": 2},
        agent_outputs={},
    )
    fetched = await cm.get_checkpoint(cp1.checkpoint_id)
    assert fetched is not None
    assert fetched.step_id == "step_x"
    other = await cm.get_checkpoint(cp2.checkpoint_id)
    assert other is not None
    assert other.step_id == "step_y"
    assert await cm.get_checkpoint(9999) is None


@pytest.mark.asyncio
async def test_resume_from_checkpoint_rebuilds_state(
    cm: CheckpointManager,
) -> None:
    cp = await cm.write_checkpoint(
        workflow_id="wf-4",
        step_id="step_resume",
        state_blob={"counter": 7, "next_step_id": "step_next"},
        agent_outputs={"step_resume": {"value": 99}},
        mutate_count=2,
    )
    state = await cm.resume_from_checkpoint("wf-4", cp)
    assert state["workflow_id"] == "wf-4"
    assert state["last_step_id"] == "step_resume"
    assert state["next_step_id"] == "step_next"
    assert state["state_blob"] == {"counter": 7, "next_step_id": "step_next"}
    assert state["agent_outputs"] == {"step_resume": {"value": 99}}
    assert state["mutate_count"] == 2
    assert state["resume_strategy"] == "continue_from_step"
    assert state["checkpoint_id"] == cp.checkpoint_id


@pytest.mark.asyncio
async def test_resume_from_checkpoint_without_next_step_hint(
    cm: CheckpointManager,
) -> None:
    cp = await cm.write_checkpoint(
        workflow_id="wf-5",
        step_id="only_step",
        state_blob={"counter": 1},
        agent_outputs={"only_step": "ok"},
    )
    state = await cm.resume_from_checkpoint("wf-5", cp)
    # No next_step_id in state_blob → re-run the current step.
    assert state["next_step_id"] == "only_step"


@pytest.mark.asyncio
async def test_mutate_count_round_trips(cm: CheckpointManager) -> None:
    for n in range(4):
        await cm.write_checkpoint(
            workflow_id="wf-6",
            step_id="step_m",
            state_blob={"i": n},
            agent_outputs={},
            mutate_count=n,
        )
    assert await cm.get_mutate_count("wf-6", "step_m") == 3
    # New step: zero.
    await cm.write_checkpoint(
        workflow_id="wf-6",
        step_id="step_n",
        state_blob={},
        agent_outputs={},
    )
    assert await cm.get_mutate_count("wf-6", "step_n") == 0


@pytest.mark.asyncio
async def test_count_filters_by_workflow(cm: CheckpointManager) -> None:
    for i in range(3):
        await cm.write_checkpoint("wf-7", f"s{i}", {}, {})
    for i in range(2):
        await cm.write_checkpoint("wf-8", f"s{i}", {}, {})
    assert await cm.count() == 5
    assert await cm.count("wf-7") == 3
    assert await cm.count("wf-8") == 2
    assert await cm.count("wf-missing") == 0


@pytest.mark.asyncio
async def test_delete_for_workflow_wipes_rows(
    cm: CheckpointManager,
) -> None:
    for i in range(4):
        await cm.write_checkpoint("wf-9", f"s{i}", {}, {})
    await cm.write_checkpoint("wf-other", "x", {}, {})
    deleted = await cm.delete_for_workflow("wf-9")
    assert deleted == 4
    assert await cm.count("wf-9") == 0
    assert await cm.count("wf-other") == 1


@pytest.mark.asyncio
async def test_status_field_round_trips(cm: CheckpointManager) -> None:
    cp = await cm.write_checkpoint(
        workflow_id="wf-10",
        step_id="s",
        state_blob={},
        agent_outputs={},
        status="rolled_back",
    )
    fetched = await cm.get_checkpoint(cp.checkpoint_id)
    assert fetched is not None
    assert fetched.status == "rolled_back"


@pytest.mark.asyncio
async def test_non_serializable_state_blob_raises(
    cm: CheckpointManager,
) -> None:
    with pytest.raises(RuntimeError):
        await cm.write_checkpoint(
            workflow_id="wf-11",
            step_id="s",
            state_blob={"bad": set([1, 2, 3])},  # set is not JSON-serialisable
            agent_outputs={},
        )


@pytest.mark.asyncio
async def test_last_step_id_alias(cm: CheckpointManager) -> None:
    cp = await cm.write_checkpoint(
        workflow_id="wf-12",
        step_id="step_alias",
        state_blob={},
        agent_outputs={},
    )
    assert cp.last_step_id == "step_alias"
    assert cp.next_step_id is None  # not set
