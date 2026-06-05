"""Shared pytest fixtures for the harness test suite.

Every test runs against a fresh, on-disk workspace tree under
``tmp_path``.  We default to the :class:`SubprocessBackend` so the
suite does not require Docker; the Docker-specific tests
(``--network=none`` etc.) are gated behind a ``docker_required``
fixture and auto-skip when the binary is absent.
"""
from __future__ import annotations

import asyncio
import inspect
import os
import shutil
import sys
import tempfile
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

import pytest
import pytest_asyncio

# Ensure ``src`` is on the import path when pytest is run from the
# project root.
_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_SRC = _PROJECT_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from harness.diff_generator import DiffGenerator, RecordingSink  # noqa: E402
from harness.executor import (  # noqa: E402
    CodeExecutor,
    DockerBackend,
    SubprocessBackend,
)
from harness.git_tracker import GitTracker  # noqa: E402
from harness.server import HarnessServer  # noqa: E402
from harness.workspace import WorkspaceManager  # noqa: E402

# ---------------------------------------------------------------------------
# pytest configuration
# ---------------------------------------------------------------------------


def pytest_collection_modifyitems(config, items):
    """Mark async test functions for pytest-asyncio."""
    for item in items:
        if "asyncio" in item.keywords:
            continue
        if inspect.iscoroutinefunction(getattr(item, "function", None)):
            item.add_marker(pytest.mark.asyncio)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def git_available() -> bool:
    """True if a usable ``git`` binary is on PATH."""
    return shutil.which("git") is not None


@pytest.fixture
def docker_available() -> bool:
    """True if a usable ``docker`` binary is on PATH."""
    return shutil.which("docker") is not None


@pytest_asyncio.fixture
async def workspace_manager(tmp_path: Path) -> AsyncIterator[WorkspaceManager]:
    """Per-test :class:`WorkspaceManager` rooted at a tmp directory."""
    base = tmp_path / "workspaces"
    base.mkdir()
    mgr = WorkspaceManager(base_dir=base)
    try:
        yield mgr
    finally:
        # Best-effort cleanup; the tmp_path is wiped by pytest anyway.
        for ws in mgr.list_active():
            shutil.rmtree(ws.root, ignore_errors=True)


@pytest_asyncio.fixture
async def harness_server(
    workspace_manager: WorkspaceManager,
    git_available: bool,
) -> AsyncIterator[HarnessServer]:
    """A wired-up :class:`HarnessServer` against a tmp workspace.

    Uses :class:`SubprocessBackend` so Docker is not required.  When
    ``git`` is missing, the test session is skipped — every test in
    this fixture depends on the git tracker.
    """
    if not git_available:
        pytest.skip("git binary not available on this host")
    sink = RecordingSink()
    executor = CodeExecutor(backend=SubprocessBackend())
    git = GitTracker()
    diff = DiffGenerator(kernel_ws_client=sink, git_tracker=git)
    server = HarnessServer(
        workspace_manager=workspace_manager,
        executor=executor,
        git_tracker=git,
        diff_generator=diff,
    )
    yield server
    # Allow the main agent to call reset.
    server.allow_reset("main-agent")


@pytest.fixture
def docker_backend(docker_available: bool) -> DockerBackend:
    """A :class:`DockerBackend` instance; skips the test if docker is missing."""
    if not docker_available:
        pytest.skip("docker binary not available on this host")
    return DockerBackend()
