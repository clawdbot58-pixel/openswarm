"""Dashboard backend — FastAPI server for system introspection.

Phase 7 deliverable. Exposes:

* Read-only REST endpoints for agents, workflows, logs, workspaces,
  loops, memory, and metrics (:mod:`dashboard.backend.introspection`).
* A WebSocket ``/stream`` that fans kernel events out to connected
  clients in real time (:mod:`dashboard.backend.stream`).
* Storage for user-defined view and layout configurations
  (:mod:`dashboard.backend.config`).

The backend is read-only with respect to swarm state. It may only
mutate the dashboard's own view/layout tables.
"""
from __future__ import annotations

__all__: list[str] = []
