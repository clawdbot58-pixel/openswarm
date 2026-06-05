"""Top-level pytest configuration for the OpenSwarm project.

Provides the import path shim used by all subpackage conftest files.
Async test discovery is handled by ``pytest-asyncio``'s
``asyncio_mode=auto`` (configured in ``pyproject.toml`` if present,
otherwise via the auto-detected default in modern versions).
"""
from __future__ import annotations

import sys
from pathlib import Path

# Make ``src`` importable so test files can ``from harness import …``,
# ``from loops import …`` etc. without per-package sys.path munging.
_PROJECT_ROOT = Path(__file__).resolve().parent
_SRC = _PROJECT_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))
