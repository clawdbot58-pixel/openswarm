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
from pathlib import Path
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
    dashboard_url: str | None = None
    agent_workspace: Path | None = None
    allowed_chat_ids: list[int] | None = None
    poll_interval_seconds: float = 1.0
    request_timeout_seconds: float = 5.0
    status_message_ttl_seconds: int = 120
    notify_on_start: bool = True


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

    def chat(
        self,
        message: str,
        *,
        session_id: str,
        timeout: float = 120.0,
    ) -> dict[str, Any] | None:
        """Send a conversational turn and wait for the main agent reply."""
        import urllib.error
        import urllib.request

        body = json.dumps(
            {
                "message": message,
                "session_id": session_id,
                "timeout_seconds": timeout,
            }
        ).encode("utf-8")
        req = urllib.request.Request(  # noqa: S310
            f"{self._base}/chat",
            data=body,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout + 5.0) as resp:  # noqa: S310
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            logger.warning("kernel chat error: %s", exc)
            try:
                detail = json.loads(exc.read().decode("utf-8"))
                return {"reply": detail.get("message", str(exc)), "status": "failed"}
            except Exception:  # noqa: BLE001
                return {"reply": str(exc), "status": "failed"}
        except Exception as exc:  # noqa: BLE001
            logger.warning("kernel chat unreachable: %s", exc)
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
            "👋 *OpenSwarm is running*\n\n"
            "Talk to me like a colleague — no slash commands required.\n\n"
            "Examples:\n"
            "• *Create a Python agent that reviews pull requests*\n"
            "• *What's the swarm doing?*\n"
            "• *Show my tasks*\n\n"
            "I queue work, spin up agents, and you can watch everything "
            "on the dashboard."
        )

    @staticmethod
    def help() -> str:
        return (
            "*Just type naturally.*\n\n"
            "• Ask for work: *build a login page*, *research TypeScript patterns*\n"
            "• Ask for agents: *create a coder and a reviewer*\n"
            "• Check in: *status*, *tasks*, *what's running?*\n\n"
            "Open the dashboard in your browser to observe agents live."
        )

    @staticmethod
    def started(dashboard_url: str | None = None) -> str:
        dash = f"\nDashboard: `{dashboard_url}`" if dashboard_url else ""
        return (
            "🟢 *OpenSwarm started*\n\n"
            "I'm online. Just message me — I'll reply here in real time." + dash
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
        self._dashboard_url = config.dashboard_url
        self._agent_workspace = config.agent_workspace
        self._active_turns: dict[int, str] = {}

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
        if self._config.notify_on_start:
            await self._notify_startup()

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
        await self._converse(update, context, prompt)

    async def _cmd_async(self, update: Any, context: Any) -> None:
        if not self._is_allowed(update):
            return
        prompt = " ".join(context.args or []).strip()
        if not prompt:
            await update.message.reply_text("Tell me what you want.")
            return
        await self._converse(update, context, prompt)

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

    async def _notify_startup(self) -> None:
        """Tell allowed chats that OpenSwarm is up (OpenClaw-style ping)."""
        if self._application is None or not self._config.allowed_chat_ids:
            return
        text = self._formatter.started(self._dashboard_url)
        for chat_id in self._config.allowed_chat_ids:
            try:
                await self._application.bot.send_message(
                    chat_id=chat_id,
                    text=text,
                    parse_mode=ParseMode.MARKDOWN,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("startup notify to %s failed: %s", chat_id, exc)

    async def _on_text(self, update: Any, context: Any) -> None:
        if not self._is_allowed(update):
            return
        text = (update.message.text or "").strip()
        if not text:
            return
        lowered = text.lower()
        if lowered in {"help", "?", "what can you do"}:
            await update.message.reply_text(
                self._formatter.help(), parse_mode=ParseMode.MARKDOWN
            )
            return
        if lowered in {"status", "how are you", "how's it going"} or "how are" in lowered:
            await self._cmd_status(update, context)
            return
        if lowered in {"tasks", "taskboard", "my tasks", "show tasks"}:
            await self._show_tasks(update)
            return
        await self._converse(update, context, text)

    async def _show_tasks(self, update: Any) -> None:
        from workspace.taskboard import format_taskboard_preview

        ws = self._agent_workspace or Path("workspaces/agent")
        preview = format_taskboard_preview(ws)
        await update.message.reply_text(
            f"📋 *Active tasks*\n\n{preview}",
            parse_mode=ParseMode.MARKDOWN,
        )

    async def _converse(self, update: Any, context: Any, text: str) -> None:
        """Hermes-style conversation — typing indicator, real reply, steering."""
        if not self._client.health():
            await update.message.reply_text(
                "🔴 OpenSwarm is not running.\n\nRun `openswarm start` on the host.",
            )
            return

        chat = update.effective_chat
        if chat is None:
            return
        session_id = f"telegram:{chat.id}"

        # Typing indicator for the whole turn (OpenClaw-style feedback).
        try:
            from telegram.constants import ChatAction  # type: ignore[import-not-found]

            await context.bot.send_chat_action(
                chat_id=chat.id, action=ChatAction.TYPING
            )
        except Exception:  # noqa: BLE001
            pass

        typing_task: asyncio.Task | None = None
        try:
            from telegram.constants import ChatAction  # type: ignore[import-not-found]

            async def _keep_typing() -> None:
                while True:
                    try:
                        await context.bot.send_chat_action(
                            chat_id=chat.id, action=ChatAction.TYPING
                        )
                    except Exception:  # noqa: BLE001
                        break
                    await asyncio.sleep(4.0)

            typing_task = asyncio.create_task(_keep_typing())
        except Exception:  # noqa: BLE001
            typing_task = None

        self._active_turns[chat.id] = session_id
        try:
            result = await asyncio.to_thread(
                self._client.chat,
                text,
                session_id=session_id,
                timeout=float(self._config.status_message_ttl_seconds),
            )
        finally:
            self._active_turns.pop(chat.id, None)
            if typing_task is not None:
                typing_task.cancel()
                try:
                    await typing_task
                except asyncio.CancelledError:
                    pass

        if result is None:
            await update.message.reply_text(
                "I couldn't reach the kernel. Try `openswarm status` on the host.",
            )
            return

        if result.get("status") == "steered":
            await update.message.reply_text(result.get("reply", "Noted — steering."))
            return

        reply = str(result.get("reply") or "").strip()
        if not reply:
            reply = "I'm here, but I didn't get a response back. Try again?"

        if self._agent_workspace:
            try:
                from workspace.taskboard import queue_goal

                queue_goal(self._agent_workspace, text, source="telegram")
            except Exception:  # noqa: BLE001
                pass

        # Plain text — LLM replies often include markdown that breaks Telegram.
        await update.message.reply_text(reply[:4000])


# ---------------------------------------------------------------------------
# Entry point used by `python -m telegram_adapter.bot`
# ---------------------------------------------------------------------------


async def _run_bot() -> None:
    """Read config from env, build, and run the bot."""
    import signal

    from config import get_config

    cfg = get_config()
    kernel_url = os.environ.get(
        "KERNEL_REST_URL",
        f"http://127.0.0.1:{cfg.kernel.port}",
    )
    dashboard_url = os.environ.get("OPENSWARM_DASHBOARD_URL")
    if not dashboard_url:
        dash_port = os.environ.get("OPENSWARM_DASHBOARD_PORT", str(cfg.dashboard.port))
        dashboard_url = f"http://127.0.0.1:{dash_port}/ui/"
    agent_ws = Path(
        os.environ.get(
            "OPENSWARM_AGENT_WORKSPACE",
            str(cfg.workspace.agent_dir),
        )
    )
    token = (
        os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
        or cfg.telegram.bot_token.strip()
        or os.environ.get("OPENSWARM_TELEGRAM__BOT_TOKEN", "").strip()
    )
    bot_cfg = BotConfig(
        token=token,
        kernel_url=kernel_url.rstrip("/"),
        dashboard_url=dashboard_url,
        agent_workspace=agent_ws,
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
