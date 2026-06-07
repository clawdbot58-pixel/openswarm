"""Telegram adapter for OpenSwarm (Phase 11).

Wraps the :class:`OpenSwarmBot` so the rest of the system can
import a single name. The actual implementation lives in
:mod:`telegram_adapter.bot`; this module is the public re-export point.
"""
from __future__ import annotations

__all__ = [
    "BotConfig",
    "OpenSwarmBot",
    "TelegramBotError",
    "TelegramFormatter",
    "_TELEGRAM_AVAILABLE",
    "main",
]


def __getattr__(name: str):
    if name in __all__:
        from importlib import import_module

        _bot = import_module("telegram_adapter.bot")
        return getattr(_bot, name)
    raise AttributeError(name)
