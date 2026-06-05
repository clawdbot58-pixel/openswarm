"""Context assembler — builds the full preamble for an LLM call.

This is Phase 6's headline deliverable.  It glues together:

* the **manifest** (the agent's static identity, capabilities, and
  permissions);
* the **temporary memory** (this-session working state);
* the **persistent memory** (cross-session long-term recall);
* the **skill loader** (domain knowledge);
* the **loop registry** (recommended thinking loops, if available);
* the **current task** (what the agent has been asked to do).

The output is a fully-formed :class:`kernel.models.Preamble` object
— or, via the :class:`PreambleAssembler` wrapper, a rendered
markdown string ready to drop into the LLM system prompt.

The contract for what a preamble must look like lives in
``contracts/envelope.json`` (the ``preamble`` definition) and
``vision/prompt-engineering.md`` (the rendered template).  The
assembler here implements both, using the manifest's
``configuration.memory`` block to bound the context window and
filter by relevance.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Optional

from kernel.models import (
    AgentManifest,
    IntentBlock,
    MemoryContext,
    MemoryItem as KernelMemoryItem,
    PermissionsBlock,
    Preamble,
    ThinkingLoopConfig,
)

from .persistent import PersistentMemory
from .skill_loader import Skill, SkillLoader
from .temporary import MemoryItem, TemporaryMemory

logger = logging.getLogger(__name__)


# Mapping from agent role to a sensible default phase.  The phase is
# the *first guess*; the conductor can override it via the envelope's
# ``intent.phase`` field.
_ROLE_PHASE: dict[str, str] = {
    "orchestrator": "planning",
    "executor": "execution",
    "specialist": "execution",
    "critic": "reflection",
    "meta": "planning",
    "harness": "execution",
    "kernel": "execution",
}


class ContextAssembler:
    """Builds a :class:`kernel.models.Preamble` for one inference call.

    Args:
        temp_memory: The per-agent :class:`TemporaryMemory` for this
            session.  Used to source ``recent_events`` and
            ``session_state``.
        persistent_memory: The cross-session :class:`PersistentMemory`.
            Used to source ``relevant_history`` via FTS5.
        loop_registry: Optional :class:`loops.registry.LoopRegistry`.
            When supplied, the assembler will look for a better
            recommended loop than the manifest's default.
        skill_loader: Optional :class:`SkillLoader`.  When supplied,
            the assembled :class:`Preamble` carries the loaded
            skills' summaries in ``session_state["loaded_skills"]``.
    """

    def __init__(
        self,
        temp_memory: TemporaryMemory,
        persistent_memory: PersistentMemory,
        loop_registry: Any | None = None,
        skill_loader: SkillLoader | None = None,
    ) -> None:
        self._temp = temp_memory
        self._persistent = persistent_memory
        self._loop_registry = loop_registry
        self._skill_loader = skill_loader

    # -- core assembly ----------------------------------------------------

    async def assemble(
        self,
        manifest: AgentManifest,
        current_task: str,
        workflow_id: Optional[str] = None,
        step_id: Optional[str] = None,
        overrides: Optional[dict[str, Any]] = None,
    ) -> Preamble:
        """Assemble a full :class:`Preamble` for ``current_task``.

        Args:
            manifest: The agent's validated manifest.
            current_task: The literal task text (used to drive the
                FTS5 search for ``relevant_history``).
            workflow_id: Optional workflow attribution for the
                memory writes this assembly may trigger.
            step_id: Optional step attribution.
            overrides: Optional dict of per-call overrides.  Two
                keys are recognised:

                * ``permissions`` — must be a subset of
                  ``manifest.permissions``-derived
                  :class:`PermissionsBlock`; the assembler will
                  raise :class:`PermissionOverrideError` otherwise.
                * ``thinking_loop_config`` — full or partial
                  :class:`ThinkingLoopConfig` to merge over the
                  manifest default.

        Returns:
            A fully-formed :class:`Preamble`.
        """
        overrides = overrides or {}
        mem_cfg = manifest.configuration.memory if manifest.configuration else None
        context_window = mem_cfg.context_window if mem_cfg else 10
        threshold = mem_cfg.relevance_threshold if mem_cfg else 0.5

        intent = self._build_intent(manifest, current_task, overrides)
        permissions = self._build_permissions(manifest, overrides)
        loop_config = await self._build_loop_config(manifest, overrides)
        memory_context = await self._build_memory_context(
            manifest=manifest,
            current_task=current_task,
            context_window=context_window,
            threshold=threshold,
            workflow_id=workflow_id,
            step_id=step_id,
        )

        return Preamble(
            intent=intent,
            permissions=permissions,
            thinking_loop_config=loop_config,
            memory_context=memory_context,
        )

    # -- section builders -------------------------------------------------

    def _build_intent(
        self,
        manifest: AgentManifest,
        current_task: str,
        overrides: dict[str, Any],
    ) -> IntentBlock:
        """Build the ``intent`` block from manifest + current task."""
        # Caller-provided override wins outright.
        if "intent" in overrides and isinstance(overrides["intent"], dict):
            return IntentBlock.model_validate(overrides["intent"])

        # Phase: prefer manifest's role mapping; let the caller narrow
        # it via the preamble itself in a future phase.
        phase = _ROLE_PHASE.get(manifest.role, "execution")
        return IntentBlock(
            goal=current_task,
            phase=phase,  # type: ignore[arg-type]
            constraints=[],
        )

    def _build_permissions(
        self,
        manifest: AgentManifest,
        overrides: dict[str, Any],
    ) -> PermissionsBlock:
        """Build the ``permissions`` block, validating any override.

        The base :class:`PermissionsBlock` is derived from the
        manifest's :class:`kernel.models.Permissions` blob — we
        flatten the rich permission model into the linear
        ``can_read``/``can_write``/``can_execute`` shape the
        preamble contract uses.

        Overrides must be a **subset** of the base permissions:
        an override may not grant ``can_write`` if the manifest
        marks the filesystem ``read_only``; it may not grant
        ``can_execute`` if the manifest has no ``harness`` block,
        etc.
        """
        base = self._permissions_from_manifest(manifest)
        if "permissions" not in overrides:
            return base
        override = PermissionsBlock.model_validate(overrides["permissions"])
        self._validate_permission_override(base, override)
        return override

    @staticmethod
    def _permissions_from_manifest(manifest: AgentManifest) -> PermissionsBlock:
        """Flatten the manifest's rich :class:`Permissions` into a block."""
        perms = manifest.permissions
        can_read: list[str] = []
        can_write: list[str] = []
        can_execute: list[str] = []
        can_delegate = False
        max_tokens: int | None = None

        if perms is not None:
            if perms.file_system is not None:
                can_read.extend(perms.file_system.allow or [])
                if not perms.file_system.read_only:
                    can_write.extend(perms.file_system.allow or [])
            if perms.process is not None and perms.process.can_spawn:
                can_delegate = True
            if perms.harness is not None and perms.harness.allowed_runtimes:
                can_execute.extend(perms.harness.allowed_runtimes)

        # Inference context window is a sensible max_tokens proxy.
        if (
            manifest.capabilities
            and manifest.capabilities.inference
            and manifest.capabilities.inference.max_context_tokens
        ):
            max_tokens = int(manifest.capabilities.inference.max_context_tokens)

        return PermissionsBlock(
            can_read=can_read,
            can_write=can_write,
            can_execute=can_execute,
            can_delegate=can_delegate,
            max_tokens=max_tokens,
        )

    @staticmethod
    def _validate_permission_override(
        base: PermissionsBlock, override: PermissionsBlock
    ) -> None:
        """Raise :class:`PermissionOverrideError` if ``override`` exceeds ``base``.

        The rule: every read pattern in ``override`` must also be
        in ``base``; every write pattern in ``override`` must also
        be in ``base``; every execute pattern in ``override``
        must also be in ``base``; and ``can_delegate`` may only
        be ``True`` if the base allows it.
        """
        base_read = set(base.can_read)
        base_write = set(base.can_write)
        base_exec = set(base.can_execute)

        for pattern in override.can_read:
            if not _glob_match_any(pattern, base_read):
                raise PermissionOverrideError(
                    f"override grants read access to {pattern!r} which "
                    f"is not in manifest"
                )
        for pattern in override.can_write:
            if not _glob_match_any(pattern, base_write):
                raise PermissionOverrideError(
                    f"override grants write access to {pattern!r} which "
                    f"is not in manifest"
                )
        for pattern in override.can_execute:
            if not _glob_match_any(pattern, base_exec):
                raise PermissionOverrideError(
                    f"override grants execute access to {pattern!r} which "
                    f"is not in manifest"
                )
        if override.can_delegate and not base.can_delegate:
            raise PermissionOverrideError(
                "override grants delegation but manifest does not"
            )

    async def _build_loop_config(
        self,
        manifest: AgentManifest,
        overrides: dict[str, Any],
    ) -> ThinkingLoopConfig:
        """Build the ``thinking_loop_config`` block.

        Default = the manifest's :class:`ThinkingProfile.default_loop`.
        If a ``LoopRegistry`` is wired in and the manifest declared
        a ``task_type`` via the calling :class:`PreambleAssembler`,
        the assembler may upgrade the recommendation to the
        registry's top-ranked template for that task type.
        """
        if "thinking_loop_config" in overrides and isinstance(
            overrides["thinking_loop_config"], dict
        ):
            return ThinkingLoopConfig.model_validate(overrides["thinking_loop_config"])

        profile = manifest.thinking_profile
        default_loop = profile.default_loop if profile else "direct"
        mode = _loop_to_mode(default_loop)
        loop_id: Optional[str] = default_loop

        if self._loop_registry is not None and manifest.category:
            try:
                recs = await self._loop_registry.aget_recommendation(
                    task_type=manifest.category, limit=1
                )
                if recs and recs[0].get("id"):
                    loop_id = str(recs[0]["id"])
                    mode = _loop_to_mode(loop_id)
            except Exception as exc:  # noqa: BLE001
                # Best-effort; a registry failure must not break
                # context assembly.  Log and fall through to the
                # default.
                logger.warning("loop recommendation failed: %s", exc)

        return ThinkingLoopConfig(
            mode=mode,  # type: ignore[arg-type]
            loop_id=loop_id,
            max_iterations=10,
            stop_conditions=[],
            confidence_threshold=0.8,
        )

    async def _build_memory_context(
        self,
        manifest: AgentManifest,
        current_task: str,
        context_window: int,
        threshold: float,
        workflow_id: Optional[str],
        step_id: Optional[str],
    ) -> MemoryContext:
        """Stitch together the three memory channels.

        * ``recent_events`` — last ``context_window`` items from
          the temporary store.
        * ``relevant_history`` — top-N FTS5 matches from the
          persistent store, filtered by ``threshold``.
        * ``session_state`` — the temporary store's flat
          ``context``-keyed dict.
        """
        recent_items = await self._temp.get_recent(n=context_window)
        relevant_items = await self._persistent.retrieve_relevant(
            agent_id=manifest.agent_id,
            query=current_task,
            threshold=threshold,
            n=min(context_window, 5),
        )
        session_state = await self._temp.get_state()

        # If a skill loader is wired in, expose the loaded skills as
        # a session-state key so the rendered preamble can include
        # their summaries.
        if self._skill_loader is not None:
            skill_ids = (
                manifest.capabilities.skills if manifest.capabilities else None
            ) or []
            if skill_ids:
                loaded = await self._skill_loader.load_multiple(skill_ids)
                if loaded:
                    session_state = {
                        **session_state,
                        "loaded_skills": {
                            sid: skill.summary() for sid, skill in loaded.items()
                        },
                    }

        # Convert to the wire-format MemoryItem so the contract is
        # the one the LLM actually sees.
        return MemoryContext(
            recent_events=[_to_kernel_item(it) for it in recent_items],
            relevant_history=[_to_kernel_item(it) for it in relevant_items],
            session_state=session_state,
        )


class PermissionOverrideError(ValueError):
    """Raised when an override would grant permissions the manifest denies."""


# ---------------------------------------------------------------------------
# PreambleAssembler — the LLM-facing wrapper
# ---------------------------------------------------------------------------


# The render template.  Order matches ``vision/prompt-engineering.md``
# §1; section names are uppercase so the existing tests can still find
# ``"ROLE"``, ``"PERMISSIONS"``, etc.
_PREAMBLE_TEMPLATE = """# ROLE
You are {agent_id} ({role}). {intent}
{human_readable}

# PHASE
Current phase: {phase}
Goal: {goal}
{constraints_block}

# PERMISSIONS
Read: {can_read}
Write: {can_write}
Execute: {can_execute}
Can delegate: {can_delegate}
Max tokens: {max_tokens}

# THINKING LOOP
Mode: {loop_mode}
Loop id: {loop_id}
Max iterations: {max_iterations}
Confidence threshold: {confidence_threshold}
{stop_conditions_block}

# RECENT EVENTS (last {context_window})
{recent_events_block}

# RELEVANT HISTORY (score >= {threshold})
{relevant_history_block}

# SESSION STATE
{session_state_block}

# SKILLS
{skills_block}

# CURRENT TASK
{task}
"""


class PreambleAssembler:
    """Lifts a :class:`Preamble` into a string the LLM can read.

    This is the function-shaped wrapper that the agent worker calls
    before every inference.  It keeps the legacy
    ``assemble(preamble, manifest) -> str`` and
    ``assemble_minimal(task, intent) -> str`` helpers (Phase 3) for
    backward compatibility, and adds the async
    :meth:`assemble(manifest, task) -> str` method the Phase 6
    contract calls for.
    """

    def __init__(self, context_assembler: ContextAssembler) -> None:
        self._ca = context_assembler

    @property
    def context_assembler(self) -> ContextAssembler:
        """The wrapped :class:`ContextAssembler`."""
        return self._ca

    # -- Phase 6 async API -----------------------------------------------

    async def assemble(
        self,
        manifest: AgentManifest,
        task: str,
        workflow_id: Optional[str] = None,
        step_id: Optional[str] = None,
        overrides: Optional[dict[str, Any]] = None,
    ) -> str:
        """Build the full preamble string for ``task``."""
        preamble = await self._ca.assemble(
            manifest=manifest,
            current_task=task,
            workflow_id=workflow_id,
            step_id=step_id,
            overrides=overrides,
        )
        return render_preamble(preamble, manifest)

    # -- Phase 3 sync API (backward compat) ------------------------------

    @staticmethod
    def assemble_sync(preamble: dict[str, Any], manifest: dict[str, Any]) -> str:
        """Sync helper used by the existing Phase 3 callers and tests.

        The shape of the inputs is the loose dict format from the
        Phase 3 assembler (``preamble`` is a dict, ``manifest`` is
        a dict).  This is kept for backward compatibility; new code
        should use :meth:`assemble`.
        """
        return _legacy_assemble(preamble, manifest)

    @staticmethod
    def assemble_minimal(task: str, intent: str = "") -> str:
        """Tiny preamble for the cheap path; matches Phase 3 behaviour."""
        parts: list[str] = ["# TASK"]
        if intent:
            parts.append(f"Intent: {intent}")
        parts.append(f"Task: {task}")
        return "\n".join(parts)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def render_preamble(preamble: Preamble, manifest: AgentManifest) -> str:
    """Render a :class:`Preamble` and :class:`AgentManifest` as a string.

    This is the public rendering entry point — separate from
    :class:`PreambleAssembler` so tests can render arbitrary
    preambles without standing up the full assembler stack.
    """
    mem = preamble.memory_context or MemoryContext()
    constraints_block = (
        "\n".join(f"- {c}" for c in preamble.intent.constraints)
        if preamble.intent.constraints
        else "- (none)"
    )
    stop_block = (
        "\n".join(f"- {c}" for c in preamble.thinking_loop_config.stop_conditions)
        if preamble.thinking_loop_config.stop_conditions
        else "- (none)"
    )
    recent_block = _render_memory_items(mem.recent_events) or "- (none)"
    history_block = _render_memory_items(mem.relevant_history) or "- (none)"
    state_block = _render_session_state(mem.session_state) or "- (empty)"
    skills_block = _render_loaded_skills(mem.session_state.get("loaded_skills"))

    return _PREAMBLE_TEMPLATE.format(
        agent_id=manifest.agent_id,
        role=manifest.role,
        intent=manifest.intent,
        human_readable=(
            f"Description: {manifest.description}" if manifest.description else ""
        ),
        phase=preamble.intent.phase,
        goal=preamble.intent.goal,
        constraints_block=constraints_block,
        can_read=preamble.permissions.can_read or "[]",
        can_write=preamble.permissions.can_write or "[]",
        can_execute=preamble.permissions.can_execute or "[]",
        can_delegate=preamble.permissions.can_delegate,
        max_tokens=preamble.permissions.max_tokens or "(unset)",
        loop_mode=preamble.thinking_loop_config.mode,
        loop_id=preamble.thinking_loop_config.loop_id or "(default)",
        max_iterations=preamble.thinking_loop_config.max_iterations,
        confidence_threshold=preamble.thinking_loop_config.confidence_threshold,
        stop_conditions_block=stop_block,
        context_window=(
            manifest.configuration.memory.context_window
            if manifest.configuration and manifest.configuration.memory
            else 10
        ),
        recent_events_block=recent_block,
        threshold=(
            manifest.configuration.memory.relevance_threshold
            if manifest.configuration and manifest.configuration.memory
            else 0.5
        ),
        relevant_history_block=history_block,
        session_state_block=state_block,
        skills_block=skills_block,
        task=preamble.intent.goal,
    )


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _render_memory_items(items: list[KernelMemoryItem]) -> str:
    """Render a list of memory items as ``- [type] ts: content`` lines."""
    if not items:
        return ""
    lines: list[str] = []
    for it in items:
        ts = it.timestamp.isoformat().replace("+00:00", "Z")
        content = it.content
        if not isinstance(content, str):
            content = json.dumps(content, default=str, ensure_ascii=False)
        if len(content) > 200:
            content = content[:199] + "…"
        score = (
            f" (relevance: {it.relevance_score:.2f})"
            if it.relevance_score is not None
            else ""
        )
        lines.append(f"- [{it.type}] {ts}{score}: {content}")
    return "\n".join(lines)


def _render_session_state(state: dict[str, Any]) -> str:
    if not state:
        return ""
    rendered: list[str] = []
    for key, value in state.items():
        if key == "loaded_skills":
            continue
        rendered.append(f"- {key}: {value}")
    return "\n".join(rendered)


def _render_loaded_skills(skills: Any) -> str:
    if not skills:
        return "- (none)"
    if not isinstance(skills, dict):
        return "- (malformed)"
    return "\n".join(f"- {sid}: {summary}" for sid, summary in skills.items())


def _to_kernel_item(item: MemoryItem) -> KernelMemoryItem:
    """Convert the rich :class:`MemoryItem` to the wire-format one."""
    return item.to_kernel()


def _loop_to_mode(loop_id: str) -> str:
    """Map a loop id to a :class:`ThinkingLoopConfig.mode` value."""
    if loop_id in {"direct", "fast"}:
        return "fast"
    if loop_id in {"memo", "cot"}:
        return "memo"
    if loop_id in {"reflection", "tree", "debate", "ensemble"}:
        return "thorough"
    return "custom"


def _glob_match_any(pattern: str, candidates: Iterable[str]) -> bool:
    """``fnmatch``-style match: does ``pattern`` match any ``candidate``?

    This is the same matcher the kernel's
    :class:`~kernel.permissions.PermissionEnforcer` uses; we re-export
    it locally so this module stays importable in unit tests without
    the kernel.
    """
    import fnmatch

    return any(fnmatch.fnmatchcase(pattern, c) for c in candidates)


# ---------------------------------------------------------------------------
# Backward-compatible Phase 3 helpers
# ---------------------------------------------------------------------------


def _legacy_assemble(preamble: dict[str, Any], manifest: dict[str, Any]) -> str:
    """The original Phase 3 ``assemble`` function, kept verbatim-ish.

    New code should use :meth:`PreambleAssembler.assemble`; this is
    only here so the existing tests and the in-process agent worker
    that don't speak the new async API keep working.
    """
    parts: list[str] = []
    role = manifest.get("role", "executor")
    intent = manifest.get("intent", "")
    agent_id = manifest.get("agent_id", "unknown")
    parts.append(f"# ROLE\nYou are {agent_id}, a {role}. {intent}")

    permissions = preamble.get("permissions", {})
    parts.append("\n# PERMISSIONS")
    if "can_read" in permissions:
        parts.append(f"Read: {permissions['can_read']}")
    if "can_write" in permissions:
        parts.append(f"Write: {permissions['can_write']}")
    if "can_execute" in permissions:
        parts.append(f"Execute: {permissions['can_execute']}")
    if "can_delegate" in permissions:
        parts.append(f"Delegate: {permissions['can_delegate']}")

    loop_config = preamble.get("thinking_loop_config", {})
    if loop_config:
        parts.append("\n# THINKING LOOP")
        if "mode" in loop_config:
            parts.append(f"Mode: {loop_config['mode']}")
        if "max_iterations" in loop_config:
            parts.append(f"Max iterations: {loop_config['max_iterations']}")
        if "confidence_threshold" in loop_config:
            parts.append(f"Confidence threshold: {loop_config['confidence_threshold']}")

    memory_context = preamble.get("memory_context", {})
    if memory_context:
        parts.append("\n# MEMORY")
        recent_events = memory_context.get("recent_events", [])
        if recent_events:
            parts.append("Recent events:")
            for event in recent_events[-5:]:
                event_type = event.get("type", "unknown")
                timestamp = event.get("timestamp", "")
                parts.append(f"  - [{timestamp}] {event_type}")
        relevant_history = memory_context.get("relevant_history", [])
        if relevant_history:
            parts.append("\nRelevant history:")
            for item in relevant_history[:3]:
                content = item.get("content", "")
                if isinstance(content, dict):
                    content = str(content)[:100]
                parts.append(f"  - {str(content)[:100]}")
        session_state = memory_context.get("session_state", {})
        if session_state:
            parts.append(
                f"\nSession state: {session_state.get('workflow_id', 'unknown')}"
            )

    intent_info = preamble.get("intent", {})
    if intent_info:
        parts.append("\n# TASK")
        if "goal" in intent_info:
            parts.append(f"Goal: {intent_info['goal']}")
        if "phase" in intent_info:
            parts.append(f"Phase: {intent_info['phase']}")
        if "constraints" in intent_info:
            parts.append(f"Constraints: {', '.join(intent_info['constraints'])}")

    return "\n".join(parts)


# Backward-compatible shims for the original Phase 3 public surface.
def assemble(preamble: dict[str, Any], manifest: dict[str, Any]) -> str:
    """Phase 3 sync ``assemble`` (kept for backward compat)."""
    return _legacy_assemble(preamble, manifest)


def assemble_minimal(task: str, intent: str = "") -> str:
    """Phase 3 sync ``assemble_minimal`` (kept for backward compat)."""
    return PreambleAssembler.assemble_minimal(task, intent)


__all__ = [
    "ContextAssembler",
    "PermissionOverrideError",
    "PreambleAssembler",
    "assemble",
    "assemble_minimal",
    "render_preamble",
]
