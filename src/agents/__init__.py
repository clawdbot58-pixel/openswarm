"""OpenSwarm user-facing agents — Phase 2.

Three agent classes, all built on the same :class:`BaseAgent` foundation:

* :class:`MainAgent` — singleton orchestrator, the only agent the user talks to.
* :class:`Conductor` — workflow decomposer, manages sector managers.
* :class:`SectorManager` — domain manager template, delegates to workers.

Plus the supporting infrastructure (:class:`LLMClient`,
:class:`ObjectiveParser`, the :class:`BaseAgent` WebSocket client).
"""
from __future__ import annotations

__all__: list[str] = []
