"""End-to-end integration tests for the Phase 6 memory stack.

These tests glue the moving parts together: a router, temporary and
persistent memory, the context assembler, and the loop registry.
The goal is to make sure a realistic flow works:

1. An agent writes a memory (temporary and persistent).
2. A subsequent call to ``ContextAssembler.assemble`` reads those
   memories back into the preamble.
3. Persistent state survives a process restart (close + reopen).
4. Errors written to memory surface in the next preamble.
5. The loop registry upgrades the manifest default when it has a
   better recommendation.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from kernel.models import Endpoint, Envelope, Preamble

from memory.context_assembler import ContextAssembler, PreambleAssembler
from memory.persistent import PersistentMemory
from memory.router import MemoryRouter
from memory.skill_loader import SkillLoader
from memory.temporary import MemoryItem, TemporaryMemory


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _manifest(
    *,
    role: str = "executor",
    intent: str = "Help the user with their task.",
    config: dict[str, Any] | None = None,
    skills: list[str] | None = None,
    category: str | None = None,
    default_loop: str | None = None,
) -> Any:
    payload: dict[str, Any] = {
        "agent_id": "test-agent",
        "version": "1.0.0",
        "role": role,
        "intent": intent,
        "capabilities": {
            "inference": {"provider": "openai", "max_context_tokens": 8192},
            "tools": [],
        },
        "lifecycle": {"persistence": "ephemeral"},
        "registration_time": "2026-06-04T10:00:00Z",
    }
    if config is not None:
        payload["configuration"] = config
    if skills is not None:
        payload["capabilities"]["skills"] = skills
    if category is not None:
        payload["category"] = category
    if default_loop is not None:
        payload["thinking_profile"] = {"default_loop": default_loop}
    from kernel.models import AgentManifest
    return AgentManifest.model_validate(payload)


def _make_write_envelope(
    *,
    sender_id: str = "agent-1",
    persistence: str,
    item: dict[str, Any],
    workflow_id: str | None = None,
    step_id: str | None = None,
) -> Envelope:
    data: dict[str, Any] = {
        "action": "memory_write",
        "persistence": persistence,
        "item": item,
    }
    if workflow_id is not None:
        data["workflow_id"] = workflow_id
    if step_id is not None:
        data["step_id"] = step_id
    return Envelope(
        envelope_id=str(uuid4()),
        created_at=datetime.now(timezone.utc),
        envelope_type="request",
        sender=Endpoint(agent_id=sender_id, role="executor"),
        receiver=Endpoint(agent_id="memory-router", role="kernel"),
        preamble=Preamble(
            intent={"goal": "store memory", "phase": "execution"},
        ),
        payload={"content_type": "data", "data": data},
    )


# ---------------------------------------------------------------------------
# End-to-end flow
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_full_flow_write_then_assemble(tmp_path: Path):
    """Write a memory, then assemble a preamble that retrieves it."""
    # --- arrange: shared stores + router + assembler -----------------
    temp = TemporaryMemory("test-agent")
    persistent = PersistentMemory(tmp_path / "memory.db")
    await persistent.initialize()
    router = MemoryRouter(temp, persistent, agent_id="test-agent")
    ca = ContextAssembler(temp, persistent)

    # --- act 1: agent emits a memory envelope -------------------------
    env = _make_write_envelope(
        persistence="persistent",
        item={
            "type": "result",
            "content": "wrote a python module called alpha.py",
            "relevance_score": 0.9,
        },
    )
    ack = await router.handle_envelope(env)
    assert ack is not None

    # --- act 2: next inference call assembles a preamble --------------
    manifest = _manifest(
        config={"memory": {"context_window": 5, "relevance_threshold": 0.3}}
    )
    preamble = await ca.assemble(manifest, "python alpha")
    # The result must be in relevant_history.
    assert any(
        "alpha.py" in (it.content or "")
        for it in preamble.memory_context.relevant_history
    )


@pytest.mark.asyncio
async def test_error_memory_surfaces_in_subsequent_preamble(tmp_path: Path):
    """An error written to memory appears in recent_events of the next preamble."""
    temp = TemporaryMemory("test-agent")
    persistent = PersistentMemory(tmp_path / "memory.db")
    await persistent.initialize()
    router = MemoryRouter(temp, persistent, agent_id="test-agent")
    ca = ContextAssembler(temp, persistent)

    env = _make_write_envelope(
        persistence="temporary",
        item={"type": "error", "content": "permission denied on /etc"},
    )
    await router.handle_envelope(env)

    manifest = _manifest(
        config={"memory": {"context_window": 10, "relevance_threshold": 0.0}}
    )
    preamble = await ca.assemble(manifest, "next task")
    assert any(
        "permission denied" in (it.content or "")
        for it in preamble.memory_context.recent_events
    )


@pytest.mark.asyncio
async def test_persistent_survives_restart(tmp_path: Path):
    """Close and reopen the DB; memories are still there."""
    db_path = tmp_path / "memory.db"

    # First "process": write a memory.
    p1 = PersistentMemory(db_path)
    await p1.initialize()
    await p1.store(
        "test-agent",
        MemoryItem(type="result", content="survives restart", relevance_score=0.8),
    )
    await p1.close()

    # Second "process": reopen the DB and read it back.
    p2 = PersistentMemory(db_path)
    await p2.initialize()
    try:
        items = await p2.retrieve_recent("test-agent", n=10)
        assert len(items) == 1
        assert items[0].content == "survives restart"
    finally:
        await p2.close()


# ---------------------------------------------------------------------------
# Skill + assembly integration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_skills_rendered_in_preamble_string(tmp_path: Path):
    """Loaded skills end up in the rendered preamble."""
    # Make a skill.
    skill_root = tmp_path / "skills"
    (skill_root / "python").mkdir(parents=True)
    (skill_root / "python" / "SKILL.md").write_text(
        "# Python Skill\n\nUseful for python work.\n", encoding="utf-8"
    )
    loader = SkillLoader(skill_root)

    temp = TemporaryMemory("test-agent")
    persistent = PersistentMemory(":memory:")
    await persistent.initialize()
    ca = ContextAssembler(temp, persistent, skill_loader=loader)
    pa = PreambleAssembler(ca)

    manifest = _manifest(skills=["python"])
    rendered = await pa.assemble(manifest, "do python thing")
    assert "# SKILLS" in rendered
    assert "python" in rendered
    assert "Useful for python work" in rendered


# ---------------------------------------------------------------------------
# Loop registry + assembly integration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_registry_upgrades_default_loop(tmp_path: Path):
    """A registry recommendation wins over the manifest default."""
    from loops.graph import LoopGraph
    from loops.registry import LoopRegistry

    reg = LoopRegistry(str(tmp_path / "loops.db"))
    reg.register_template(
        LoopGraph.reflection_graph("reflection"), task_type="coding"
    )
    reg.update_stats("reflection", score=9.0, cost=0.01, latency=100, success=True)

    temp = TemporaryMemory("test-agent")
    persistent = PersistentMemory(":memory:")
    await persistent.initialize()
    ca = ContextAssembler(temp, persistent, loop_registry=reg)

    manifest = _manifest(category="coding", default_loop="direct")
    preamble = await ca.assemble(manifest, "do coding")
    assert preamble.thinking_loop_config.loop_id == "reflection"


# ---------------------------------------------------------------------------
# Router + assembly round-trip
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_router_and_assembler_see_same_data(tmp_path: Path):
    """Data written via the router is what the assembler reads back."""
    temp = TemporaryMemory("test-agent")
    persistent = PersistentMemory(tmp_path / "memory.db")
    await persistent.initialize()
    router = MemoryRouter(temp, persistent, agent_id="test-agent")
    ca = ContextAssembler(temp, persistent)

    env = _make_write_envelope(
        persistence="temporary",
        item={"type": "decision", "content": "use python 3.14"},
    )
    await router.handle_envelope(env)

    manifest = _manifest(
        config={"memory": {"context_window": 10, "relevance_threshold": 0.0}}
    )
    preamble = await ca.assemble(manifest, "next")
    contents = " ".join(str(it.content) for it in preamble.memory_context.recent_events)
    assert "python 3.14" in contents
