"""Tests for AgentWorker."""

import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from agent_worker import AgentWorker


@pytest.fixture
def manifest_path(tmp_path):
    """Create a temporary manifest file."""
    manifest = {
        "agent_id": "test-coder",
        "version": "1.0.0",
        "role": "executor",
        "intent": "Write Python code",
        "capabilities": {
            "inference": {
                "provider": "openai",
                "models": ["gpt-4o-mini"],
                "default_model": "gpt-4o-mini",
            },
            "tools": [
                {"name": "file_write", "description": "Write file"},
                {"name": "file_read", "description": "Read file"},
            ],
        },
        "lifecycle": {"persistence": "ephemeral"},
        "thinking_profile": {
            "available_loops": ["direct", "cot", "reflection"],
            "default_loop": "direct",
        },
    }
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest))
    return str(path)


def test_load_manifest(manifest_path):
    """Test loading manifest from file."""
    worker = AgentWorker(manifest_path)
    manifest = worker._load_manifest()

    assert manifest["agent_id"] == "test-coder"
    assert manifest["role"] == "executor"


def test_load_manifest_missing_file():
    """Test loading non-existent manifest raises error."""
    worker = AgentWorker("/nonexistent/path.json")

    with pytest.raises(FileNotFoundError):
        worker._load_manifest()


def test_load_manifest_invalid():
    """Test loading invalid manifest raises error."""
    import tempfile

    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump({"role": "executor"}, f)  # Missing required fields
        f.flush()

        worker = AgentWorker(f.name)

        with pytest.raises(ValueError):
            worker._load_manifest()


@pytest.mark.asyncio
async def test_register_envelope():
    """Test registration envelope format."""
    manifest_path = "/tmp/test_manifest.json"
    with open(manifest_path, "w") as f:
        json.dump(
            {
                "agent_id": "test-agent",
                "role": "executor",
                "intent": "Test",
                "capabilities": {
                    "inference": {"provider": "openai", "models": ["gpt-4o-mini"]},
                    "tools": [],
                },
                "lifecycle": {"persistence": "ephemeral"},
            },
            f,
        )

    worker = AgentWorker(manifest_path)
    worker.agent_id = "test-agent"

    with patch("agent_worker.websockets") as mock_ws:
        mock_ws.connect = AsyncMock()
        mock_ws.connect.return_value.__aenter__ = AsyncMock()
        mock_ws.connect.return_value.__aexit__ = AsyncMock()
        mock_ws.connect.return_value.send = AsyncMock()
        mock_ws.connect.return_value.close = AsyncMock()

        worker.ws = mock_ws.connect.return_value

        # Create a mock for the send method
        worker._send = AsyncMock()

        await worker._register()

        # Check that send was called
        assert worker._send.called


@pytest.mark.asyncio
async def test_message_loop_processes_request(manifest_path):
    """Test message loop processes request envelopes."""
    worker = AgentWorker(manifest_path)
    worker.agent_id = "test-coder"

    # Mock WebSocket
    worker.ws = MagicMock()

    test_envelope = {
        "envelope_id": "test-123",
        "envelope_type": "request",
        "sender": {"agent_id": "main-agent", "role": "orchestrator"},
        "receiver": {"agent_id": "test-coder", "role": "executor"},
        "payload": {
            "content_type": "text",
            "content": "Write hello world",
            "format": "plain",
        },
        "preamble": {
            "intent": {"goal": "Write hello world", "phase": "execution"},
            "permissions": {"can_read": ["/workspace"], "can_write": ["/workspace"]},
            "thinking_loop_config": {"mode": "fast"},
        },
    }

    with patch.object(worker, "_handle_request", new_callable=AsyncMock) as mock_handle:
        # Simulate receiving the envelope
        await worker._process_envelope(test_envelope)

        # _handle_request should be called for request type
        mock_handle.assert_called_once()


@pytest.mark.asyncio
async def test_process_envelope_ignores_wrong_receiver(manifest_path):
    """Test that envelope for other agents are ignored."""
    worker = AgentWorker(manifest_path)
    worker.agent_id = "test-coder"

    other_envelope = {
        "envelope_id": "test-456",
        "envelope_type": "request",
        "receiver": {"agent_id": "other-agent", "role": "executor"},
        "payload": {"content_type": "text", "content": "Task"},
        "preamble": {"intent": {}, "permissions": {}, "thinking_loop_config": {}},
    }

    with patch.object(worker, "_handle_request", new_callable=AsyncMock) as mock_handle:
        await worker._process_envelope(other_envelope)
        mock_handle.assert_not_called()


@pytest.mark.asyncio
async def test_handle_spawn_request():
    """Test spawn request handling."""
    manifest_path = "/tmp/test_manifest2.json"
    with open(manifest_path, "w") as f:
        json.dump(
            {
                "agent_id": "test-agent",
                "role": "executor",
                "intent": "Test",
                "capabilities": {
                    "inference": {"provider": "openai", "models": ["gpt-4o-mini"]},
                    "tools": [],
                },
                "lifecycle": {"persistence": "ephemeral"},
            },
            f,
        )

    worker = AgentWorker(manifest_path)
    worker.agent_id = "test-agent"
    worker._send = AsyncMock()

    request_envelope = {
        "envelope_id": "spawn-123",
        "sender": {"agent_id": "main-agent", "role": "orchestrator"},
        "receiver": {"agent_id": "test-agent", "role": "executor"},
        "payload": {
            "content_type": "spawn_request",
            "manifest_delta": {"model_tier": {"tier": "powerful"}},
            "base_manifest_id": "coder-python-fast",
        },
        "preamble": {},
    }

    await worker._handle_spawn_request(request_envelope)

    # Should have sent a response
    assert worker._send.called


@pytest.mark.asyncio
async def test_heartbeat_sends_envelope():
    """Test heartbeat loop runs and sends an envelope.

    The loop sleeps 10 s between beats and only calls
    ``_send_heartbeat`` when ``self.ws`` is set.  To keep the test
    fast we:

    * set ``worker.ws`` to a sentinel truthy value,
    * patch ``asyncio.sleep`` to a no-op,
    * have the mock heartbeat flip ``worker.running`` to ``False`` so
      the loop terminates after one beat.
    """
    import asyncio

    manifest_path = "/tmp/test_manifest3.json"
    with open(manifest_path, "w") as f:
        json.dump(
            {
                "agent_id": "test-agent",
                "role": "executor",
                "intent": "Test",
                "capabilities": {
                    "inference": {"provider": "openai", "models": ["gpt-4o-mini"]},
                    "tools": [],
                },
                "lifecycle": {"persistence": "ephemeral"},
            },
            f,
        )

    worker = AgentWorker(manifest_path)
    worker.agent_id = "test-agent"
    worker.running = True
    worker.ws = MagicMock(name="ws-connection")
    real_heartbeat = AsyncMock()
    async def stop_after_heartbeat(*args, **kwargs):
        await real_heartbeat(*args, **kwargs)
        worker.running = False
    worker._send_heartbeat = stop_after_heartbeat

    # Patch the ``asyncio.sleep`` used inside the loop so the 10s
    # inter-beat delay becomes a no-op.
    real_sleep = asyncio.sleep
    async def fast_sleep(_seconds: float) -> None:
        await real_sleep(0)
    with patch("asyncio.sleep", side_effect=fast_sleep):
        await asyncio.wait_for(worker._heartbeat_loop(), timeout=2.0)

    real_heartbeat.assert_awaited_once()