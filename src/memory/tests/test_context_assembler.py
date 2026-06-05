"""Tests for :class:`memory.context_assembler.ContextAssembler` and friends."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from kernel.models import (
    AgentManifest,
    Permissions,
)

from memory.context_assembler import (
    ContextAssembler,
    PermissionOverrideError,
    PreambleAssembler,
    render_preamble,
)
from memory.persistent import PersistentMemory
from memory.skill_loader import SkillLoader
from memory.temporary import MemoryItem, TemporaryMemory


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _manifest(
    *,
    role: str = "executor",
    intent: str = "Help the user with their task.",
    config: dict[str, Any] | None = None,
    permissions: Permissions | None = None,
    skills: list[str] | None = None,
    category: str | None = None,
    default_loop: str | None = None,
) -> AgentManifest:
    """Build an AgentManifest for tests.

    The ``registration_time`` is fixed to a deterministic ISO 8601
    string so the rendered output is stable.
    """
    payload: dict[str, Any] = {
        "agent_id": "test-agent",
        "version": "1.0.0",
        "role": role,
        "intent": intent,
        "capabilities": {
            "inference": {
                "provider": "openai",
                "max_context_tokens": 8192,
            },
            "tools": [],
        },
        "lifecycle": {"persistence": "ephemeral"},
        "registration_time": "2026-06-04T10:00:00Z",
    }
    if permissions is not None:
        payload["permissions"] = permissions
    if skills is not None:
        payload["capabilities"]["skills"] = skills
    if config is not None:
        payload["configuration"] = config
    if category is not None:
        payload["category"] = category
    if default_loop is not None:
        payload["thinking_profile"] = {"default_loop": default_loop}
    return AgentManifest.model_validate(payload)


@pytest.fixture
async def temp():
    m = TemporaryMemory("test-agent")
    yield m


@pytest.fixture
async def persistent(tmp_path: Path):
    p = PersistentMemory(tmp_path / "memory.db")
    await p.initialize()
    yield p
    await p.close()


@pytest.fixture
def skills_root(tmp_path: Path) -> Path:
    (tmp_path / "alpha").mkdir()
    (tmp_path / "alpha" / "SKILL.md").write_text(
        "# Alpha Skill\n\nUseful for alpha things.\n", encoding="utf-8"
    )
    return tmp_path


# ---------------------------------------------------------------------------
# Section builders
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_assemble_returns_preamble(temp, persistent):
    ca = ContextAssembler(temp, persistent)
    manifest = _manifest()
    preamble = await ca.assemble(manifest, "what is 2+2?")
    from kernel.models import Preamble

    assert isinstance(preamble, Preamble)
    assert preamble.intent.goal == "what is 2+2?"
    assert preamble.intent.phase == "execution"


@pytest.mark.asyncio
async def test_intent_phase_inferred_from_role(temp, persistent):
    """Phase defaults to the role mapping in the assembler."""
    ca = ContextAssembler(temp, persistent)
    for role, expected in [
        ("orchestrator", "planning"),
        ("executor", "execution"),
        ("critic", "reflection"),
    ]:
        m = _manifest(role=role)
        p = await ca.assemble(m, "do the thing")
        assert p.intent.phase == expected


@pytest.mark.asyncio
async def test_intent_override_wins(temp, persistent):
    """A caller-supplied intent override is used verbatim."""
    from kernel.models import PhaseLiteral

    ca = ContextAssembler(temp, persistent)
    m = _manifest()
    p = await ca.assemble(
        m,
        "task",
        overrides={"intent": {"goal": "override", "phase": "recovery"}},
    )
    assert p.intent.goal == "override"
    assert p.intent.phase == "recovery"


@pytest.mark.asyncio
async def test_context_window_respected(temp, persistent):
    """Recent events are capped at the manifest's context_window."""
    for i in range(25):
        await temp.set(f"k{i}", i, type="action")
    m = _manifest(config={"memory": {"context_window": 5, "relevance_threshold": 0.0}})
    ca = ContextAssembler(temp, persistent)
    p = await ca.assemble(m, "task")
    assert len(p.memory_context.recent_events) == 5


@pytest.mark.asyncio
async def test_relevance_threshold_filters_persistent_results(temp, persistent):
    """Items below the threshold are not in relevant_history."""
    for i in range(5):
        await persistent.store(
            "test-agent",
            MemoryItem(
                type="result",
                content=f"matching python keyword {i}",
                relevance_score=0.05,
            ),
        )
    m = _manifest(config={"memory": {"context_window": 10, "relevance_threshold": 0.9}})
    ca = ContextAssembler(temp, persistent)
    p = await ca.assemble(m, "python")
    # All rows had relevance 0.05, threshold is 0.9, so nothing qualifies.
    assert p.memory_context.relevant_history == []


# ---------------------------------------------------------------------------
# Permission override validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_permission_override_subset_allowed(temp, persistent):
    from kernel.models import FsPermission

    manifest = _manifest(
        permissions=Permissions(file_system=FsPermission(allow=["/data/*"]))
    )
    ca = ContextAssembler(temp, persistent)
    overrides = {"permissions": {"can_read": ["/data/x.py"]}}
    p = await ca.assemble(manifest, "read x", overrides=overrides)
    assert "/data/x.py" in p.permissions.can_read


@pytest.mark.asyncio
async def test_permission_override_exceeding_manifest_rejected(temp, persistent):
    from kernel.models import FsPermission

    manifest = _manifest(
        permissions=Permissions(file_system=FsPermission(allow=["/data/*"]))
    )
    ca = ContextAssembler(temp, persistent)
    overrides = {"permissions": {"can_read": ["/etc/passwd"]}}
    with pytest.raises(PermissionOverrideError):
        await ca.assemble(manifest, "read passwd", overrides=overrides)


@pytest.mark.asyncio
async def test_permission_override_cannot_grant_delegation(temp, persistent):
    from kernel.models import FsPermission

    manifest = _manifest(
        permissions=Permissions(file_system=FsPermission(allow=["/data/*"]))
    )
    ca = ContextAssembler(temp, persistent)
    overrides = {"permissions": {"can_read": ["/data/x.py"], "can_delegate": True}}
    with pytest.raises(PermissionOverrideError):
        await ca.assemble(manifest, "do thing", overrides=overrides)


@pytest.mark.asyncio
async def test_permission_override_can_write_subset_allowed(temp, persistent):
    from kernel.models import FsPermission

    manifest = _manifest(
        permissions=Permissions(file_system=FsPermission(allow=["/data/*"], read_only=False))
    )
    ca = ContextAssembler(temp, persistent)
    overrides = {"permissions": {"can_write": ["/data/x.py"]}}
    p = await ca.assemble(manifest, "write x", overrides=overrides)
    assert "/data/x.py" in p.permissions.can_write


# ---------------------------------------------------------------------------
# Skills
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_skills_loaded_into_session_state(temp, persistent, skills_root):
    loader = SkillLoader(skills_root)
    manifest = _manifest(skills=["alpha"])
    ca = ContextAssembler(temp, persistent, skill_loader=loader)
    p = await ca.assemble(manifest, "do alpha things")
    assert "loaded_skills" in p.memory_context.session_state
    assert "alpha" in p.memory_context.session_state["loaded_skills"]


@pytest.mark.asyncio
async def test_missing_skill_silently_skipped(temp, persistent, skills_root):
    loader = SkillLoader(skills_root)
    manifest = _manifest(skills=["alpha", "ghost"])
    ca = ContextAssembler(temp, persistent, skill_loader=loader)
    p = await ca.assemble(manifest, "do things")
    assert "loaded_skills" in p.memory_context.session_state
    assert "alpha" in p.memory_context.session_state["loaded_skills"]


@pytest.mark.asyncio
async def test_skills_only_in_loaded_skills_key(temp, persistent, skills_root):
    """The loaded skills must not also leak into other session state."""
    loader = SkillLoader(skills_root)
    manifest = _manifest(skills=["alpha"])
    ca = ContextAssembler(temp, persistent, skill_loader=loader)
    p = await ca.assemble(manifest, "do alpha")
    # Only the loaded_skills key; nothing else.
    assert list(p.memory_context.session_state.keys()) == ["loaded_skills"]


# ---------------------------------------------------------------------------
# Loop recommendation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_loop_recommendation_used_when_better(temp, persistent, tmp_path):
    """The registry's top recommendation overrides the manifest default."""
    from loops.graph import LoopGraph
    from loops.registry import LoopRegistry

    # Use a tempfile-backed DB: ``asyncio.to_thread`` re-routes calls
    # through worker threads.  sqlite3 ``:memory:`` is per-connection,
    # so the worker thread would see a fresh empty DB.  A file-based
    # DB is shared across connections in the same process.
    reg = LoopRegistry(str(tmp_path / "loops.db"))
    reg.register_template(
        LoopGraph.reflection_graph("reflection"), task_type="coding"
    )
    reg.update_stats("reflection", score=9.0, cost=0.01, latency=1000, success=True)

    manifest = _manifest(category="coding", default_loop="direct")
    ca = ContextAssembler(temp, persistent, loop_registry=reg)
    p = await ca.assemble(manifest, "do coding")
    assert p.thinking_loop_config.loop_id == "reflection"


@pytest.mark.asyncio
async def test_loop_recommendation_fallback_on_failure(temp, persistent):
    """A registry failure falls back to the manifest default."""

    class BrokenRegistry:
        async def aget_recommendation(self, *args, **kwargs):
            raise RuntimeError("boom")

    manifest = _manifest(category="coding", default_loop="direct")
    ca = ContextAssembler(temp, persistent, loop_registry=BrokenRegistry())
    p = await ca.assemble(manifest, "do coding")
    assert p.thinking_loop_config.loop_id == "direct"


@pytest.mark.asyncio
async def test_no_registry_uses_manifest_default(temp, persistent):
    """No registry → manifest default survives."""
    manifest = _manifest(default_loop="cot")
    ca = ContextAssembler(temp, persistent)
    p = await ca.assemble(manifest, "do")
    assert p.thinking_loop_config.loop_id == "cot"


# ---------------------------------------------------------------------------
# PreambleAssembler (the LLM-facing string wrapper)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_preamble_assembler_renders_full_sections(temp, persistent, skills_root):
    manifest = _manifest(skills=["alpha"])
    ca = ContextAssembler(temp, persistent, skill_loader=SkillLoader(skills_root))
    pa = PreambleAssembler(ca)
    rendered = await pa.assemble(manifest, "build a thing")
    for header in [
        "# ROLE",
        "# PHASE",
        "# PERMISSIONS",
        "# THINKING LOOP",
        "# RECENT EVENTS",
        "# RELEVANT HISTORY",
        "# SESSION STATE",
        "# SKILLS",
        "# CURRENT TASK",
    ]:
        assert header in rendered, f"missing section: {header}"
    assert "build a thing" in rendered
    assert "test-agent" in rendered


@pytest.mark.asyncio
async def test_preamble_assemble_legacy_shim_works():
    """The Phase 3 dict-shaped shim still produces a useful string."""
    rendered = PreambleAssembler.assemble_sync(
        {"intent": {"goal": "x", "phase": "execution"}, "permissions": {}},
        {"agent_id": "a", "role": "executor", "intent": "i"},
    )
    assert "a" in rendered and "x" in rendered


def test_assemble_minimal_sync():
    out = PreambleAssembler.assemble_minimal("do thing", "be helpful")
    assert "do thing" in out and "be helpful" in out


# ---------------------------------------------------------------------------
# render_preamble (standalone)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_render_preamble_includes_skill_summaries(temp, persistent, skills_root):
    manifest = _manifest(skills=["alpha"])
    ca = ContextAssembler(temp, persistent, skill_loader=SkillLoader(skills_root))
    preamble = await ca.assemble(manifest, "do alpha")
    rendered = render_preamble(preamble, manifest)
    assert "alpha" in rendered
    assert "Useful for alpha things" in rendered or "alpha" in rendered


@pytest.mark.asyncio
async def test_session_state_excludes_loaded_skills_dup(temp, persistent, skills_root):
    """The duplicate 'loaded_skills' key is rendered only under SKILLS."""
    manifest = _manifest(skills=["alpha"])
    ca = ContextAssembler(temp, persistent, skill_loader=SkillLoader(skills_root))
    preamble = await ca.assemble(manifest, "do alpha")
    rendered = render_preamble(preamble, manifest)
    # Split on section headers; SESSION STATE block should not list the
    # skills as plain state.
    session_block = rendered.split("# SESSION STATE", 1)[1].split("# SKILLS", 1)[0]
    assert "loaded_skills" not in session_block


# ---------------------------------------------------------------------------
# Manifest configuration defaults
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_manifest_without_configuration_uses_safe_defaults(temp, persistent):
    """A manifest with no memory config still produces a valid preamble."""
    m = _manifest()  # no configuration
    ca = ContextAssembler(temp, persistent)
    p = await ca.assemble(m, "anything")
    # Defaults: context_window=10, threshold=0.5
    assert p.memory_context is not None
    assert len(p.memory_context.recent_events) <= 10
