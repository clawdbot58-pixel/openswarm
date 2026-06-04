"""OpenSwarm Kernel — Phase 1 control plane.

The kernel is the **only** communication bus in the swarm. It is composed of
four cooperating subsystems:

* :mod:`kernel.bus`         — priority-queue message router
* :mod:`kernel.registry`    — SQLite-backed agent registry
* :mod:`kernel.permissions` — default-deny policy enforcer
* :mod:`kernel.heartbeat`   — file-polling liveness monitor

The :mod:`kernel.main` module wires them together as a FastAPI app and is
the recommended entry point: ``uvicorn kernel.main:app``.
"""
from __future__ import annotations

__all__: list[str] = []
