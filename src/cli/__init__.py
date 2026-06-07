"""OpenSwarm CLI — unified user-facing command surface."""
from __future__ import annotations

__all__ = ["__version__", "cli", "main"]


def __getattr__(name: str):
    if name in __all__:
        from importlib import import_module

        _main = import_module("cli.main")
        return getattr(_main, name)
    raise AttributeError(name)
