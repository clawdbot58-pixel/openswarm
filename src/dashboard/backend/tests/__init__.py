"""Tests for the dashboard backend.

The tests are split into:

* :mod:`test_introspection` — read-only data queries.
* :mod:`test_stream` — WebSocket broadcast.
* :mod:`test_config` — view/layout storage.
* :mod:`test_aggregator` — background aggregation.
* :mod:`test_integration` — end-to-end with a real kernel harness.
"""
from __future__ import annotations

__all__: list[str] = []
