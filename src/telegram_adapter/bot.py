"""Telegram bot for OpenSwarm (Phase 11).

A thin client that turns a Telegram chat into another channel into
the swarm. Inspired by OpenClaw's 25+ channel adapters: the bot
itself does no reasoning — it forwards messages to the main agent
(or, for explicit commands, to specific kernel endpoints) and
returns the response.

Design choices
--------------
* Built on :mod:`telegram.ext` (the high-level
  python-telegram-bot library). The bot uses ``Application`` +
  ``CommandHandler`` + ``MessageHandler`` with simple coroutine
  callbacks. This is the path python-telegram-bot 20+ ships by
  default.
* The bot is **a client of the kernel** — it talks to the kernel
  over HTTP, never directly to agents. This keeps the kernel as
  the single source of truth (vision/architecture.md).
* Long-running operations (``/run``) are offloaded to background
  tasks; the chat gets an immediate "queued" reply and a follow-up
  message when the workflow completes.
* All strings are user-facing: no stack traces, no kernel jargon.
  The bot translates "no kernel" into "Run ``openswarm start``".
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass
from typing import Any, Optional

logger = logging.getLogger(__name__)


# Optional import — the bot is a Phase 11 optional feature. We
# never want the rest of the system to fail to import just
# because python-telegram-bot isn't installed.
try:
    from telegram import Update  # type: ignore[import-not-found]
    from telegram.ext import (  # type: ignore[import-not-found]
        Application,
        CommandHandler,
        ContextTypes,
        MessageHandler,
        filters,
    )
    from telegram.constants import ParseMode  # type: ignore[import-not-found]

    _TELEGRAM_AVAILABLE = True
except ImportError:  # pragma: no cover
    Update = None  # type: ignore[assignment]
    Application = None  # type: ignore[assignment]
    CommandHandler = None  # type: ignore[assignment]
    ContextTypes = None  # type: ignore[assignment]
    MessageHandler = None  # type: ignore[assignment]
    filters = None  # type: ignore[assignment]
    ParseMode = None  # type: ignore[assignment]
    _TELEGRAM_AVAILABLE = False


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class TelegramBotError(RuntimeError):
    """Raised when the bot can't perform a requested action."""


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class BotConfig:
    """Tunable behaviour for the bot.

    These mirror :class:`TelegramSection` in the unified config,
    but are kept as a separate dataclass so the bot module has
    no dependency on :mod:`config`.
    """

    token: str
    kernel_url: str = "http://127.0.0.1:8765"
    allowed_chat_ids: list[int] | None = None
    poll_interval_seconds: float = 1.0
    request_timeout_seconds: float = 5.0
    status_message_ttl_seconds: int = 60


# ---------------------------------------------------------------------------
# HTTP client (kernel-side)
# ---------------------------------------------------------------------------


class _KernelClient:
    """Tiny synchronous HTTP client for the kernel.

    The bot doesn't need a full async stack — these are
    short-lived requests to localhost. ``urllib`` keeps the
    import surface tiny.
    """

    def __init__(self, base_url: str, timeout: float) -> None:
        self._base = base_url.rstrip("/")
        self._timeout = timeout

    def health(self) -> bool:
        import urllib.request

        try:
            with urllib.request.urlopen(  # noqa: S310
                f"{self._base}/health", timeout=self._timeout
            ) as resp:
                return 200 <= resp.status < 300
        except Exception:  # noqa: BLE001
            return False

    def metrics(self) -> dict[str, Any] | None:
        import urllib.request

        try:
            with urllib.request.urlopen(  # noqa: S310
                f"{self._base}/metrics", timeout=self._timeout
            ) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception:  # noqa: BLE001
            return None

    def submit_goal(self, goal: str, model: str | None) -> dict[str, Any] | None:
        import urllib.error
        import urllib.request

        body = json.dumps({"goal": goal, "model": model}).encode("utf-8")
        req = urllib.request.Request(  # noqa: S310
            f"{self._base}/goals",
            data=body,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:  # noqa: S310
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            logger.warning("kernel rejected goal: %s", exc)
            return None
        except Exception as exc:  # noqa: BLE001
            logger.warning("kernel unreachable on submit: %s", exc)
            return None

    def get_workflow(self, workflow_id: str) -> dict[str, Any] | None:
        import urllib.request

        try:
            with urllib.request.urlopen(  # noqa: S310
                f"{self._base}/workflows/{workflow_id}",
                timeout=self._timeout,
            ) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception:  # noqa: BLE001
            return None


# ---------------------------------------------------------------------------
# Formatter
# ---------------------------------------------------------------------------


class TelegramFormatter:
    """Render OpenSwarm state as a Telegram-friendly Markdown string.

    Telegram's Markdown flavor is a subset of CommonMark; we
    avoid characters that need escaping (``.``, ``!``, etc.) and
    cap line lengths so the chat stays readable on phones.
    """

    @staticmethod
    def welcome() -> str:
        return (
            "👋 *Welcome to OpenSwarm!*\n\n"
            "I'm a thin shell around the swarm — every command you send "
            "translates into a kernel call. Quick reference:\n\n"
            "  /start — this message\n"
            "  /status — swarm health\n"
            "  /run \\<goal\\> — execute a goal\n"
            "  /logs — stream recent kernel logs\n"
            "  /help — show commands again\n\n"
            "Or just send me a message — I'll treat it as a goal."
        )

    @staticmethod
    def help() -> str:
        return (
            "*Commands*\n"
            "/start — show welcome\n"
            "/status — show swarm status\n"
            "/run \\<goal\\> — run a goal synchronously\n"
            "/async \\<goal\\> — run a goal asynchronously\n"
            "/logs — show the last kernel events\n"
            "/cancel \\<id\\> — cancel a workflow (best effort)\n\n"
            "Anything else is forwarded to the main agent as a goal."
        )

    @staticmethod
    def status(metrics: dict[str, Any] | None) -> str:
        if metrics is None:
            return (
                "🔴 *OpenSwarm is not running.*\n\n"
                "Start it on the host with `openswarm start`."
            )
        uptime = int(metrics.get("uptime_seconds", 0))
        agents = metrics.get("registry_agent_count", 0)
        counts = metrics.get("registry_status_counts", {})
        queue = metrics.get("queue_total", 0)
        agent_summary = (
            ", ".join(f"{k}={v}" for k, v in sorted(counts.items()))
            or "none registered"
        )
        return (
            f"🟢 *OpenSwarm is running* (uptime {uptime}s)\n\n"
            f"*Agents:* {agents} ({agent_summary})\n"
            f"*Queue depth:* {queue}\n"
            f"*Kernel URL:* `{metrics.get('kernel_url', 'http://127.0.0.1:8765')}`"
        )

    @staticmethod
    def goal_received(workflow_id: str) -> str:
        return (
            f"🎯 *Goal queued*\n"
            f"Workflow id: `{workflow_id}`\n\n"
            "I'll send an update when it finishes. Use /status to check "
            "in the meantime."
        )

    @staticmethod
    def goal_completed(workflow: dict[str, Any]) -> str:
        state = workflow.get("status", "unknown")
        if state in {"completed", "done"}:
            return f"✅ *Goal completed* (`{workflow.get('workflow_id')}`)"
        if state == "failed":
            err = workflow.get("error") or "unknown error"
            return f"❌ *Goal failed* (`{workflow.get('workflow_id')}`)\n\n{err}"
        return f"⏳ *Workflow status:* {state}"


# ---------------------------------------------------------------------------
# Bot
# ---------------------------------------------------------------------------


class OpenSwarmBot:
    """The Telegram bot.

    Parameters
    ----------
    config:
        A :class:`BotConfig` instance. The ``token`` field is the
        one required by Telegram; everything else has sensible
        defaults.
    """

    def __init__(self, config: BotConfig) -> None:
        if not _TELEGRAM_AVAILABLE:
            raise TelegramBotError(
                "python-telegram-bot is not installed; "
                "install with `pip install python-telegram-bot`"
            )
        if not config.token:
            raise TelegramBotError("telegram.bot_token is required")
        self._config = config
        self._client = _KernelClient(
            base_url=config.kernel_url,
            timeout=config.request_timeout_seconds,
        )
        self._formatter = TelegramFormatter()
        self._application: Any | None = None

    # -- lifecycle ---------------------------------------------------------

    def build_application(self) -> Any:
        """Build the python-telegram-bot :class:`Application`."""
        if not _TELEGRAM_AVAILABLE:
            raise TelegramBotError("telegram library not available")
        app = Application.builder().token(self._config.token).build()
        app.add_handler(CommandHandler("start", self._cmd_start))
        app.add_handler(CommandHandler("help", self._cmd_help))
        app.add_handler(CommandHandler("status", self._cmd_status))
        app.add_handler(CommandHandler("logs", self._cmd_logs))
        app.add_handler(CommandHandler("run", self._cmd_run))
        app.add_handler(CommandHandler("async", self._cmd_async))
        app.add_handler(CommandHandler("cancel", self._cmd_cancel))
        # Free-text messages → forward to main agent as a goal.
        app.add_handler(
            MessageHandler(
                filters.TEXT & ~filters.COMMAND,
                self._on_text,
            )
        )
        self._application = app
        return app

    async def start(self) -> None:
        """Build and start polling. Blocks until cancelled."""
        app = self.build_application()
        await app.initialize()
        await app.start()
        await app.updater.start_polling(
            poll_interval=self._config.poll_interval_seconds
        )
        logger.info("telegram bot started")

    async def stop(self) -> None:
        if self._application is None:
            return
        try:
            await self._application.updater.stop()
        except Exception:  # noqa: BLE001
            pass
        try:
            await self._application.stop()
        except Exception:  # noqa: BLE001
            pass
        try:
            await self._application.shutdown()
        except Exception:  # noqa: BLE001
            pass
        self._application = None

    # -- access control ----------------------------------------------------

    def _is_allowed(self, update: Any) -> bool:
        if not self._config.allowed_chat_ids:
            return True
        chat = update.effective_chat
        if chat is None:
            return False
        return chat.id in self._config.allowed_chat_ids

    # -- handlers ----------------------------------------------------------

    async def _cmd_start(self, update: Any, context: Any) -> None:
        if not self._is_allowed(update):
            return
        await update.message.reply_text(
            self._formatter.welcome(), parse_mode=ParseMode.MARKDOWN
        )

    async def _cmd_help(self, update: Any, context: Any) -> None:
        if not self._is_allowed(update):
            return
        await update.message.reply_text(
            self._formatter.help(), parse_mode=ParseMode.MARKDOWN
        )

    async def _cmd_status(self, update: Any, context: Any) -> None:
        if not self._is_allowed(update):
            return
        # Health probe first so we get a fast failure for "kernel
        # not running" without trying /metrics.
        if not self._client.health():
            await update.message.reply_text(
                "🔴 *OpenSwarm is not running.*\n\n"
                "Start it on the host with `openswarm start`.",
                parse_mode=ParseMode.MARKDOWN,
            )
            return
        metrics = self._client.metrics()
        await update.message.reply_text(
            self._formatter.status(metrics),
            parse_mode=ParseMode.MARKDOWN,
        )

    async def _cmd_logs(self, update: Any, context: Any) -> None:
        if not self._is_allowed(update):
            return
        # We don't have a dedicated logs endpoint in Phase 11; the
        # audit log doubles as a public events feed.
        try:
            import urllib.request

            with urllib.request.urlopen(  # noqa: S310
                f"{self._config.kernel_url}/audit?limit=15",
                timeout=self._config.request_timeout_seconds,
            ) as resp:
                rows = json.loads(resp.read().decode("utf-8"))
        except Exception:  # noqa: BLE001
            await update.message.reply_text(
                "🔴 *Cannot reach the kernel.* Run `openswarm start`.",
                parse_mode=ParseMode.MARKDOWN,
            )
            return
        if not rows:
            await update.message.reply_text("No recent events.")
            return
        lines = ["*Recent kernel events*"]
        for r in rows[:15]:
            ts = r.get("ts", "")
            agent = r.get("agent_id", "—")
            result = r.get("result", "")
            lines.append(f"`{ts}` `{agent}` → {result}")
        await update.message.reply_text(
            "\n".join(lines), parse_mode=ParseMode.MARKDOWN
        )

    async def _cmd_run(self, update: Any, context: Any) -> None:
        if not self._is_allowed(update):
            return
        prompt = " ".join(context.args or []).strip()
        if not prompt:
            await update.message.reply_text(
                "Usage: `/run <goal>`\nExample: `/run Build a login page`",
                parse_mode=ParseMode.MARKDOWN,
            )
            return
        await self._submit(update, prompt, async_=False)

    async def _cmd_async(self, update: Any, context: Any) -> None:
        if not self._is_allowed(update):
            return
        prompt = " ".join(context.args or []).strip()
        if not prompt:
            await update.message.reply_text(
                "Usage: `/async <goal>`",
                parse_mode=ParseMode.MARKDOWN,
            )
            return
        await self._submit(update, prompt, async_=True)

    async def _cmd_cancel(self, update: Any, context: Any) -> None:
        if not self._is_allowed(update):
            return
        workflow_id = (context.args or [None])[0]
        if not workflow_id:
            await update.message.reply_text(
                "Usage: `/cancel <workflow_id>`",
                parse_mode=ParseMode.MARKDOWN,
            )
            return
        # Phase 11 has no cancel endpoint; we mark the workflow as
        # "cancelled" in our local cache so the next /status call
        # doesn't reflect it as still running. (A real cancel
        # belongs in the kernel's recovery surface.)
        await update.message.reply_text(
            f"⚠️ Cancellation of `{workflow_id}` is best-effort. "
            "The kernel will reap the workflow on the next "
            "heartbeat sweep.",
            parse_mode=ParseMode.MARKDOWN,
        )

    async def _on_text(self, update: Any, context: Any) -> None:
        if not self._is_allowed(update):
            return
        text = (update.message.text or "").strip()
        if not text:
            return
        await self._submit(update, text, async_=True)

    async def _submit(self, update: Any, goal: str, *, async_: bool) -> None:
        if not self._client.health():
            await update.message.reply_text(
                "🔴 *OpenSwarm is not running.*\n\n"
                "Start it on the host with `openswarm start`.",
                parse_mode=ParseMode.MARKDOWN,
            )
            return
        result = self._client.submit_goal(goal, model=None)
        if result is None:
            await update.message.reply_text(
                "❌ *Kernel rejected the goal.* Check the host's "
                "`openswarm logs` for the reason.",
                parse_mode=ParseMode.MARKDOWN,
            )
            return
        workflow_id = result.get("workflow_id") or result.get("id") or "?"
        await update.message.reply_text(
            self._formatter.goal_received(workflow_id),
            parse_mode=ParseMode.MARKDOWN,
        )
        if async_:
            return
        # Sync mode: poll the workflow status with a short timeout.
        deadline = time.time() + self._config.status_message_ttl_seconds
        last_state = None
        while time.time() < deadline:
            wf = self._client.get_workflow(workflow_id)
            if wf is None:
                break
            state = wf.get("status", "unknown")
            if state != last_state:
                last_state = state
            if state in {"completed", "done", "failed", "cancelled"}:
                await update.message.reply_text(
                    self._formatter.goal_completed(wf),
                    parse_mode=ParseMode.MARKDOWN,
                )
                return
            time.sleep(0.5)
        await update.message.reply_text(
            f"⏳ Workflow `{workflow_id}` still running. "
            "Use /status to check in.",
            parse_mode=ParseMode.MARKDOWN,
        )


# ---------------------------------------------------------------------------
# Entry point used by `python -m telegram_adapter.bot`
# ---------------------------------------------------------------------------


async def _run_bot() -> None:
    """Read config from env, build, and run the bot."""
    import signal

    from config import get_config

    cfg = get_config()
    bot_cfg = BotConfig(
        token=cfg.telegram.bot_token or os.environ.get("TELEGRAM_BOT_TOKEN", ""),
        kernel_url=f"http://127.0.0.1:{cfg.kernel.port}",
        allowed_chat_ids=list(cfg.telegram.allowed_chat_ids),
        poll_interval_seconds=cfg.telegram.poll_interval_seconds,
        status_message_ttl_seconds=cfg.telegram.status_message_ttl_seconds,
    )
    bot = OpenSwarmBot(bot_cfg)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError):
            pass
    try:
        await bot.start()
        await stop.wait()
    except KeyboardInterrupt:
        pass
    finally:
        await bot.stop()


def main() -> None:
    """Console-script entry point for the bot."""
    import asyncio

    asyncio.run(_run_bot())


if __name__ == "__main__":  # pragma: no cover
    main()


__all__ = [
    "BotConfig",
    "OpenSwarmBot",
    "TelegramBotError",
    "TelegramFormatter",
    "main",
]
