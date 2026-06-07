"""Workflow resume on kernel boot.

When the kernel starts (or restarts), it scans the workflow table for
rows whose status is one of ``running``, ``paused``, ``recovering`` and
emits a ``workflow_resume`` event per row to the Main Agent. The Main
Agent then decides — per the recovery hierarchy in
``vision/self-healing.md`` — whether to:

* ``continue_from_step``   — re-execute from the last checkpoint
* ``rollback_n_steps``     — unwind N steps and try again
* ``respawn_all_agents``   — kill the agents and start fresh

The kernel does **not** decide strategy. It only rebuilds the
checkpoint-backed state and hands it to the Main Agent.

This module is pure glue: it has no business logic of its own, only
the boot-time scan and emission. It is safe to call repeatedly; the
status filter ensures a row is only resumed once per boot.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from .checkpoint import Checkpoint, CheckpointManager
from .registry import AgentRegistry, RESUMABLE_WORKFLOW_STATUSES

if TYPE_CHECKING:  # pragma: no cover
    from .bus import MessageBus

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------

async def resume_on_boot(
    *,
    bus: "MessageBus",
    registry: AgentRegistry,
    checkpoints: CheckpointManager,
    main_agent_id: str = "main-agent",
    statuses: list[str] | None = None,
) -> int:
    """Scan for in-flight workflows and emit ``workflow_resume`` events.

    Parameters
    ----------
    bus
        The kernel :class:`~kernel.bus.MessageBus` used to emit events.
    registry
        The :class:`~kernel.registry.AgentRegistry` that owns the
        workflow table.
    checkpoints
        The :class:`~kernel.checkpoint.CheckpointManager` to read
        per-workflow checkpoints from.
    main_agent_id
        The agent_id the events are addressed to. Defaults to
        ``"main-agent"``.
    statuses
        Override for the set of statuses considered "in flight". When
        ``None`` (default), the kernel's :data:`RESUMABLE_WORKFLOW_STATUSES`
        tuple (``"running"``, ``"paused"``, ``"recovering"``) is used.

    Returns the number of workflows for which a ``workflow_resume``
    event was emitted.
    """
    statuses_to_resume = list(statuses) if statuses is not None else list(RESUMABLE_WORKFLOW_STATUSES)
    if not statuses_to_resume:
        return 0
    workflows = await registry.list_workflows(status=statuses_to_resume)
    if not workflows:
        logger.info(
            "resume_on_boot: no in-flight workflows (statuses=%s)",
            statuses_to_resume,
        )
        return 0
    logger.info(
        "resume_on_boot: found %d in-flight workflow(s) statuses=%s",
        len(workflows), statuses_to_resume,
    )
    resumed = 0
    for wf in workflows:
        ok = await _resume_one(
            bus=bus,
            registry=registry,
            checkpoints=checkpoints,
            workflow_row=wf,
            main_agent_id=main_agent_id,
        )
        if ok:
            resumed += 1
    logger.info("resume_on_boot: emitted workflow_resume for %d workflow(s)", resumed)
    return resumed


# ---------------------------------------------------------------------------
# Single workflow resume
# ---------------------------------------------------------------------------

async def resume_workflow(
    *,
    bus: "MessageBus",
    registry: AgentRegistry,
    checkpoints: CheckpointManager,
    workflow_id: str,
    main_agent_id: str = "main-agent",
) -> bool:
    """Resume a single workflow by id. Returns ``True`` if emitted.

    Public helper used by the dashboard "resume now" button and by
    tests. ``False`` when the workflow does not exist or is not in a
    resumable status.
    """
    wf = await registry.get_workflow(workflow_id)
    if wf is None:
        logger.warning("resume_workflow: workflow_id=%s not found", workflow_id)
        return False
    if wf["status"] not in RESUMABLE_WORKFLOW_STATUSES:
        logger.info(
            "resume_workflow: workflow_id=%s status=%s not resumable",
            workflow_id, wf["status"],
        )
        return False
    return await _resume_one(
        bus=bus,
        registry=registry,
        checkpoints=checkpoints,
        workflow_row=wf,
        main_agent_id=main_agent_id,
    )


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------

async def _resume_one(
    *,
    bus: "MessageBus",
    registry: AgentRegistry,
    checkpoints: CheckpointManager,
    workflow_row: dict[str, Any],
    main_agent_id: str,
) -> bool:
    """Emit the ``workflow_resume`` event for a single workflow row."""
    workflow_id = str(workflow_row.get("workflow_id") or "")
    if not workflow_id:
        return False
    cp: Checkpoint | None = None
    try:
        cp = await checkpoints.get_latest_checkpoint(workflow_id)
    except Exception:  # noqa: BLE001
        logger.exception(
            "resume_on_boot: failed to read checkpoint for %s", workflow_id
        )
    # Build the rebuilt state from the checkpoint, if any.
    resumed_state: dict[str, Any]
    if cp is not None:
        resumed_state = await checkpoints.resume_from_checkpoint(workflow_id, cp)
    else:
        resumed_state = {
            "workflow_id": workflow_id,
            "last_step_id": None,
            "next_step_id": None,
            "state_blob": {},
            "agent_outputs": {},
            "mutate_count": 0,
            "resume_strategy": "continue_from_step",
        }
    # Annotate with the original manifest blob.
    resumed_state["manifest"] = workflow_row.get("manifest", {})
    resumed_state["status"] = workflow_row.get("status", "recovering")
    resumed_state["owner_agent"] = workflow_row.get("owner_agent")
    resumed_state["parent_workflow_id"] = workflow_row.get("parent_workflow_id")
    # Tag the workflow as "recovering" so the resume is observable.
    try:
        await registry.update_workflow_status(
            workflow_id, "recovering", last_step_id=cp.step_id if cp else None
        )
    except Exception:  # noqa: BLE001
        logger.exception(
            "resume_on_boot: failed to mark %s recovering", workflow_id
        )
    payload: dict[str, Any] = {
        "workflow_id": workflow_id,
        "checkpoint": cp.model_dump(mode="json") if cp is not None else None,
        "last_step": cp.step_id if cp else None,
        "resumed_state": resumed_state,
        "resumed_at": _utcnow_iso(),
        "previous_status": workflow_row.get("status"),
    }
    # Write an audit row so the dashboard surfaces the resume.
    try:
        await registry.audit(
            action="workflow_resume",
            result="emitted",
            agent_id=workflow_row.get("owner_agent"),
            details={
                "workflow_id": workflow_id,
                "checkpoint_id": cp.checkpoint_id if cp else None,
                "previous_status": workflow_row.get("status"),
            },
        )
    except Exception:  # noqa: BLE001
        logger.exception("resume_on_boot: audit write failed for %s", workflow_id)
    try:
        await bus.emit_event(
            "workflow_resume",
            payload,
            recipient=main_agent_id,  # type: ignore[arg-type]
        )
    except Exception:  # noqa: BLE001
        logger.exception(
            "resume_on_boot: failed to emit workflow_resume for %s", workflow_id
        )
        return False
    logger.info(
        "resume_on_boot: emitted workflow_resume workflow_id=%s last_step=%s",
        workflow_id, payload["last_step"],
    )
    return True


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


__all__ = [
    "resume_on_boot",
    "resume_workflow",
]
