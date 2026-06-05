"""Preamble assembly — Phase 3 sync surface preserved, Phase 6 async surface added.

This module used to be the entire preamble assembler.  In Phase 6
the real work moved to :mod:`memory.context_assembler`, which has
the :class:`ContextAssembler` and :class:`PreambleAssembler` class
implementations.  This file is now a thin shim that:

* re-exports ``assemble`` and ``assemble_minimal`` so the Phase 3
  callers (the in-process :mod:`agents` worker, the existing tests)
  keep working unchanged;
* exposes :class:`PreambleAssembler` (the new async class) at the
  loops-package level so agent code can ``from loops import
  PreambleAssembler`` and use it.

The new code should depend on the ``memory`` package directly.  The
``loops`` package keeps the legacy function-shaped surface because
that's what the rest of the agent worker and the Phase 3 tests
already import.
"""
from __future__ import annotations

from memory.context_assembler import (
    ContextAssembler,
    PermissionOverrideError,
    PreambleAssembler,
    assemble,
    assemble_minimal,
    render_preamble,
)

# ``PreambleAssembler.assemble_sync`` is the dict-shaped legacy entry
# point.  Re-export as ``assemble_from_dicts`` for clarity in callers
# that want to be explicit.
assemble_from_dicts = PreambleAssembler.assemble_sync

__all__ = [
    "ContextAssembler",
    "PermissionOverrideError",
    "PreambleAssembler",
    "assemble",
    "assemble_from_dicts",
    "assemble_minimal",
    "render_preamble",
]
