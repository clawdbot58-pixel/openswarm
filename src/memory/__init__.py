"""Memory & Context Assembly (Phase 6).

This package is the engine that assembles context before every LLM call.
It is split into four collaborating components:

* :mod:`memory.temporary` — In-process key-value store with TTL, scoped
  to one agent session.  Fast, ephemeral, never touches disk.
* :mod:`memory.persistent` — SQLite + FTS5 store.  Survives restarts
  and is queryable across sessions, workflows, and steps.
* :mod:`memory.skill_loader` — Reads ``SKILL.md`` files from
  ``skills/{skill_id}/`` and renders them for preamble injection.
* :mod:`memory.context_assembler` — Builds the full
  :class:`kernel.models.Preamble` object by stitching manifest
  permissions, memory channels, and loaded skills together.  This is
  the OpenClaw-style "context bootstrap" that pre-pends to every
  inference call.
* :mod:`memory.router` — Kernel-side service that receives
  ``memory_write`` envelopes and routes them to the right store.

The :class:`memory.context_assembler.PreambleAssembler` class is the
public face of Phase 6: every other component is plumbed in the
background so that the agent side just gets a fully-formed preamble
string to drop into the LLM system prompt.
"""
from __future__ import annotations

from .context_assembler import ContextAssembler, PreambleAssembler
from .persistent import PersistentMemory
from .router import MemoryRouter
from .skill_loader import SkillLoader
from .temporary import MemoryItem, TemporaryMemory

__all__ = [
    "ContextAssembler",
    "MemoryItem",
    "MemoryRouter",
    "PersistentMemory",
    "PreambleAssembler",
    "SkillLoader",
    "TemporaryMemory",
]
