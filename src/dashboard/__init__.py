"""OpenSwarm dashboard package.

The dashboard is split into two layers:

* :mod:`dashboard.backend` — Phase 7 FastAPI server. Read-only system
  introspection, real-time WebSocket event stream, and view/layout
  configuration storage. Normalises kernel state into a single queryable
  API, inspired by OpenClaw's channel adapters.
* :mod:`dashboard.frontend` — Phase 8 React + TypeScript client. Renders
  whatever views the operator configures.

This package is intentionally empty of behaviour; the heavy lifting
lives in the submodules.
"""
from __future__ import annotations

__all__: list[str] = []
