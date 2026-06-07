"""Tests for the Telegram bot (Phase 11)."""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from telegram_adapter.bot import (
    BotConfig,
    OpenSwarmBot,
    TelegramFormatter,
    _TELEGRAM_AVAILABLE,
)


class TestBotConfig:
    def test_defaults(self) -> None:
        cfg = BotConfig(token="test-token")
        assert cfg.token == "test-token"
        assert cfg.kernel_url == "http://127.0.0.1:8765"
        assert cfg.poll_interval_seconds == 1.0
        assert cfg.status_message_ttl_seconds == 60


class TestTelegramFormatter:
    def test_welcome_message(self) -> None:
        msg = TelegramFormatter.welcome()
        assert "Welcome to OpenSwarm" in msg
        assert "/start" in msg
        assert "/status" in msg
        assert "/run" in msg

    def test_help_message(self) -> None:
        msg = TelegramFormatter.help()
        assert "/start" in msg
        assert "/status" in msg
        assert "/run" in msg

    def test_status_running(self) -> None:
        metrics = {
            "uptime_seconds": 3600,
            "registry_agent_count": 5,
            "registry_status_counts": {"ready": 3, "busy": 2},
            "queue_total": 3,
            "kernel_url": "http://127.0.0.1:8765",
        }
        msg = TelegramFormatter.status(metrics)
        assert "running" in msg.lower()
        assert "5" in msg

    def test_status_not_running(self) -> None:
        msg = TelegramFormatter.status(None)
        assert "not running" in msg.lower()

    def test_goal_received(self) -> None:
        msg = TelegramFormatter.goal_received("wf-123")
        assert "wf-123" in msg
        assert "queued" in msg.lower()

    def test_goal_completed(self) -> None:
        wf = {"workflow_id": "wf-123", "status": "completed"}
        msg = TelegramFormatter.goal_completed(wf)
        assert "completed" in msg.lower()
        assert "wf-123" in msg

    def test_goal_failed(self) -> None:
        wf = {"workflow_id": "wf-456", "status": "failed", "error": "timeout"}
        msg = TelegramFormatter.goal_completed(wf)
        assert "failed" in msg.lower()
        assert "timeout" in msg


class TestKernelClient:
    def test_health_true(self) -> None:
        from telegram_adapter.bot import _KernelClient

        client = _KernelClient("http://127.0.0.1:8765", timeout=1.0)

        with patch.object(client, "health", return_value=True):
            assert client.health() is True

    def test_health_false_on_exception(self) -> None:
        from telegram_adapter.bot import _KernelClient

        client = _KernelClient("http://127.0.0.1:8765", timeout=1.0)

        with patch("urllib.request.urlopen", side_effect=Exception("boom")):
            assert client.health() is False

    def test_metrics_parsing(self) -> None:
        from telegram_adapter.bot import _KernelClient

        client = _KernelClient("http://127.0.0.1:8765", timeout=1.0)

        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_resp = MagicMock()
            mock_resp.status = 200
            mock_resp.read.return_value = b'{"agents": 5}'
            mock_urlopen.return_value.__enter__ = MagicMock(return_value=mock_resp)
            mock_urlopen.return_value.__exit__ = MagicMock(return_value=False)

            metrics = client.metrics()
            assert metrics == {"agents": 5}

    def test_submit_goal(self) -> None:
        from telegram_adapter.bot import _KernelClient

        client = _KernelClient("http://127.0.0.1:8765", timeout=1.0)

        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_resp = MagicMock()
            mock_resp.status = 200
            mock_resp.read.return_value = b'{"workflow_id": "wf-789"}'
            mock_urlopen.return_value.__enter__ = MagicMock(return_value=mock_resp)
            mock_urlopen.return_value.__exit__ = MagicMock(return_value=False)

            result = client.submit_goal("Build a login", None)
            assert result["workflow_id"] == "wf-789"


@pytest.mark.skipif(not _TELEGRAM_AVAILABLE, reason="python-telegram-bot not installed")
class TestOpenSwarmBot:
    @pytest.fixture
    def bot_config(self) -> BotConfig:
        return BotConfig(token="test-token", kernel_url="http://127.0.0.1:8765")

    @pytest.fixture
    def bot(self, bot_config: BotConfig) -> OpenSwarmBot:
        return OpenSwarmBot(bot_config)

    def test_init_requires_token(self) -> None:
        with pytest.raises(Exception):
            OpenSwarmBot(BotConfig(token=""))

    def test_is_allowed_no_whitelist(self, bot: OpenSwarmBot) -> None:
        update = MagicMock()
        update.effective_chat = MagicMock()
        update.effective_chat.id = 12345
        assert bot._is_allowed(update) is True

    def test_is_allowed_with_whitelist(self, bot: OpenSwarmBot) -> None:
        bot._config.allowed_chat_ids = [111, 222]
        update = MagicMock()
        update.effective_chat = MagicMock()
        update.effective_chat.id = 222
        assert bot._is_allowed(update) is True

    def test_is_allowed_rejects_unknown(self, bot: OpenSwarmBot) -> None:
        bot._config.allowed_chat_ids = [111]
        update = MagicMock()
        update.effective_chat = MagicMock()
        update.effective_chat.id = 999
        assert bot._is_allowed(update) is False

    def test_build_application(self, bot: OpenSwarmBot) -> None:
        app = bot.build_application()
        assert app is not None

    @pytest.mark.asyncio
    async def test_stop_handles_none(self, bot: OpenSwarmBot) -> None:
        bot._application = None
        await bot.stop()
