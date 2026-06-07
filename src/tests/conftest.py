"""Shared pytest fixtures for top-level tests."""
from __future__ import annotations

import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_SRC = _PROJECT_ROOT
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))
