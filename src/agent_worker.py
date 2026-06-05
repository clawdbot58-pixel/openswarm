#!/usr/bin/env python3
"""Generic Agent Worker - ONE executable for all non-orchestrator agents.

Behavior is 100% determined by its manifest. No hardcoded roles.
Inspired by OpenClaw's agent loop (input → context → LLM → tool → output).
"""

import asyncio
import json
import logging
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import websockets

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


class AgentWorker:
    """Generic agent worker that runs any agent type based on manifest."""

    def __init__(self, manifest_path: str):
        """Initialize the agent worker.

        Args:
            manifest_path: Path to the agent manifest JSON file.
        """
        self.manifest_path = manifest_path
        self.manifest: dict[str, Any] = {}
        self.ws: websockets.WebSocketServerProtocol | None = None
        self.agent_id: str = ""
        self.running = True
        self._heartbeat_task: asyncio.Task | None = None

    async def start(self) -> None:
        """Start the agent worker."""
        manifest = self._load_manifest()
        self.manifest = manifest
        self.agent_id = manifest.get("agent_id", "unknown")

        logger.info(f"Starting agent worker for: {self.agent_id}")

        kernel_ws = os.environ.get("KERNEL_WS", "ws://localhost:8765/ws")

        try:
            async with websockets.connect(kernel_ws) as ws:
                self.ws = ws
                await self._register()
                await self._start_heartbeat()
                await self._message_loop()
        except websockets.exceptions.ConnectionClosed:
            logger.warning("Connection to kernel closed")
        except Exception as e:
            logger.error(f"Fatal error: {e}")
            raise
        finally:
            self.running = False
            if self._heartbeat_task:
                self._heartbeat_task.cancel()

    def _load_manifest(self) -> dict[str, Any]:
        """Load and validate manifest from file.

        Returns:
            The validated manifest.

        Raises:
            FileNotFoundError: If manifest file not found.
            ValueError: If manifest is invalid.
        """
        path = Path(self.manifest_path)
        if not path.exists():
            raise FileNotFoundError(f"Manifest not found: {self.manifest_path}")

        with open(path) as f:
            manifest = json.load(f)

        required = ["agent_id", "role", "intent", "capabilities", "lifecycle"]
        for field in required:
            if field not in manifest:
                raise ValueError(f"Manifest missing required field: {field}")

        logger.info(f"Loaded manifest for: {manifest.get('agent_id')}")
        return manifest

    async def _register(self) -> None:
        """Register with the kernel."""
        envelope = {
            "envelope_id": str(uuid.uuid4()),
            "created_at": datetime.now(timezone.utc).isoformat(),
            "envelope_type": "request",
            "sender": {
                "agent_id": self.agent_id,
                "role": self.manifest.get("role", "executor"),
            },
            "receiver": {"agent_id": "kernel", "role": "kernel"},
            "payload": {
                "content_type": "data",
                "data": {"action": "register", "manifest": self.manifest},
            },
            "preamble": {
                "intent": {"goal": "register", "phase": "execution"},
                "permissions": {},
                "thinking_loop_config": {},
            },
        }

        await self._send(envelope)
        logger.info(f"Registered with kernel as: {self.agent_id}")

    async def _start_heartbeat(self) -> None:
        """Start the heartbeat loop."""
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

    async def _heartbeat_loop(self) -> None:
        """Send heartbeat every 10 seconds."""
        while self.running:
            await asyncio.sleep(10)
            if self.running and self.ws:
                await self._send_heartbeat()

    async def _send_heartbeat(self) -> None:
        """Send a heartbeat envelope to the kernel."""
        envelope = {
            "envelope_id": str(uuid.uuid4()),
            "created_at": datetime.now(timezone.utc).isoformat(),
            "envelope_type": "heartbeat",
            "sender": {
                "agent_id": self.agent_id,
                "role": self.manifest.get("role", "executor"),
            },
            "receiver": {"agent_id": "kernel", "role": "kernel"},
            "payload": {
                "content_type": "data",
                "data": {"status": "alive", "agent_id": self.agent_id},
            },
            "preamble": {
                "intent": {"goal": "heartbeat", "phase": "execution"},
                "permissions": {},
                "thinking_loop_config": {},
            },
        }

        try:
            await self._send(envelope)
        except Exception as e:
            logger.warning(f"Heartbeat failed: {e}")

    async def _message_loop(self) -> None:
        """Main message loop - receive and process envelopes."""
        logger.info("Entering message loop")

        async for raw_message in self.ws:
            try:
                envelope = json.loads(raw_message)
                await self._process_envelope(envelope)
            except json.JSONDecodeError as e:
                logger.error(f"Invalid JSON: {e}")
            except Exception as e:
                logger.error(f"Error processing envelope: {e}")

    async def _process_envelope(self, envelope: dict[str, Any]) -> None:
        """Process an incoming envelope.

        Args:
            envelope: The envelope to process.
        """
        receiver = envelope.get("receiver", {})
        envelope_type = envelope.get("envelope_type")

        # Check if this envelope is for us
        if receiver.get("agent_id") != self.agent_id:
            return

        logger.info(f"Received envelope type: {envelope_type}")

        if envelope_type == "request":
            await self._handle_request(envelope)
        elif envelope_type == "heartbeat":
            await self._handle_heartbeat(envelope)
        elif envelope_type == "event":
            await self._handle_event(envelope)
        else:
            logger.warning(f"Unknown envelope type: {envelope_type}")

    async def _handle_request(self, envelope: dict[str, Any]) -> None:
        """Handle a task request envelope.

        Args:
            envelope: The request envelope.
        """
        payload = envelope.get("payload", {})
        preamble = envelope.get("preamble", {})
        content_type = payload.get("content_type")

        # Handle spawn_request
        if content_type == "spawn_request":
            await self._handle_spawn_request(envelope)
            return

        # Handle text task
        if content_type == "text":
            task_content = payload.get("content", "")
            await self._execute_task(envelope, task_content, preamble, payload)
        else:
            logger.warning(f"Unknown content_type: {content_type}")

    async def _execute_task(
        self,
        request_envelope: dict[str, Any],
        task_content: str,
        preamble: dict[str, Any],
        payload: dict[str, Any],
    ) -> None:
        """Execute a task using the thinking loop.

        Args:
            request_envelope: The original request envelope.
            task_content: The task to execute.
            preamble: The task preamble.
            payload: The original payload.
        """
        # Determine thinking loop from preamble or use default
        loop_config = preamble.get("thinking_loop_config", {})
        loop_mode = loop_config.get("mode", "thorough")

        # Map mode to loop type
        loop_map = {
            "fast": "direct",
            "thorough": "reflection",
            "memo": "cot",
        }
        loop_type = loop_map.get(loop_mode, "direct")

        # Check if loop is available in manifest
        thinking_profile = self.manifest.get("thinking_profile", {})
        available_loops = thinking_profile.get("available_loops", ["direct"])

        if loop_type not in available_loops:
            loop_type = thinking_profile.get("default_loop", "direct")

        # Get models from manifest
        capabilities = self.manifest.get("capabilities", {})
        inference = capabilities.get("inference", {})
        models = inference.get("models", ["gpt-4o-mini"])
        provider = inference.get("provider", "openai")

        # Import here to avoid circular imports
        from loops import LLMClient, LoopRouter

        model_client = LLMClient(models, provider)
        router = LoopRouter(model_client)

        try:
            loop = router.get_loop(loop_type)
        except ValueError as e:
            logger.error(f"Loop not available: {e}")
            loop = router.get_loop("direct")

        # Execute the loop
        logger.info(f"Executing loop: {loop_type}")
        try:
            result = await loop.run(task_content, preamble, model_client)
        except Exception as e:
            logger.error(f"Loop execution failed: {e}")
            result = None

        # Build response
        await self._send_response(request_envelope, result, payload)

    async def _send_response(
        self,
        request_envelope: dict[str, Any],
        result: Any,
        original_payload: dict[str, Any],
    ) -> None:
        """Send a response envelope back to the kernel.

        Args:
            request_envelope: The original request.
            result: The loop result.
            original_payload: The original payload.
        """
        # Determine if streaming
        streaming = original_payload.get("streaming", False)

        if streaming:
            # Send chunks
            await self._send_chunk(request_envelope, result, is_final=True)
        else:
            # Send single response
            response_envelope = {
                "envelope_id": str(uuid.uuid4()),
                "created_at": datetime.now(timezone.utc).isoformat(),
                "envelope_type": "response",
                "reply_to": request_envelope.get("envelope_id"),
                "sender": {
                    "agent_id": self.agent_id,
                    "role": self.manifest.get("role", "executor"),
                },
                "receiver": request_envelope.get("sender"),
                "payload": {
                    "content_type": "text",
                    "content": result.output if result else "Task failed",
                    "format": "plain",
                },
                "preamble": {
                    "intent": {"goal": "respond", "phase": "execution"},
                    "permissions": {},
                    "thinking_loop_config": {},
                },
            }

            await self._send(response_envelope)

    async def _send_chunk(
        self,
        request_envelope: dict[str, Any],
        result: Any,
        is_final: bool = False,
    ) -> None:
        """Send a chunk envelope for streaming.

        Args:
            request_envelope: The original request.
            result: The loop result.
            is_final: Whether this is the final chunk.
        """
        content = result.output if result else ""

        chunk_envelope = {
            "envelope_id": str(uuid.uuid4()),
            "created_at": datetime.now(timezone.utc).isoformat(),
            "envelope_type": "chunk",
            "reply_to": request_envelope.get("envelope_id"),
            "sender": {
                "agent_id": self.agent_id,
                "role": self.manifest.get("role", "executor"),
            },
            "receiver": request_envelope.get("sender"),
            "payload": {
                "content_type": "text",
                "content": content,
                "format": "plain",
            },
            "preamble": {
                "intent": {"goal": "stream", "phase": "execution"},
                "permissions": {},
                "thinking_loop_config": {},
            },
            "metadata": {"is_final": is_final},
        }

        await self._send(chunk_envelope)

    async def _handle_spawn_request(self, envelope: dict[str, Any]) -> None:
        """Handle a spawn request (for spawning child agents).

        Args:
            envelope: The spawn request envelope.
        """
        logger.info("Spawn request received (forwarding to kernel)")

        # Forward spawn request to kernel
        response_envelope = {
            "envelope_id": str(uuid.uuid4()),
            "created_at": datetime.now(timezone.utc).isoformat(),
            "envelope_type": "response",
            "reply_to": envelope.get("envelope_id"),
            "sender": {
                "agent_id": self.agent_id,
                "role": self.manifest.get("role", "executor"),
            },
            "receiver": envelope.get("sender"),
            "payload": {
                "content_type": "data",
                "data": {
                    "status": "spawn_not_implemented",
                    "message": "Spawn requests should go through kernel",
                },
            },
            "preamble": {
                "intent": {"goal": "respond", "phase": "execution"},
                "permissions": {},
                "thinking_loop_config": {},
            },
        }

        await self._send(response_envelope)

    async def _handle_heartbeat(self, envelope: dict[str, Any]) -> None:
        """Handle a heartbeat from kernel.

        Args:
            envelope: The heartbeat envelope.
        """
        logger.debug("Heartbeat received")

    async def _handle_event(self, envelope: dict[str, Any]) -> None:
        """Handle an event envelope.

        Args:
            envelope: The event envelope.
        """
        payload = envelope.get("payload", {})
        data = payload.get("data", {})
        event_type = data.get("type")

        logger.info(f"Event received: {event_type}")

        if event_type == "shutdown":
            logger.info("Shutdown event received")
            self.running = False

    async def _send(self, envelope: dict[str, Any]) -> None:
        """Send an envelope via WebSocket.

        Args:
            envelope: The envelope to send.
        """
        if self.ws:
            await self.ws.send(json.dumps(envelope))


async def main() -> None:
    """Main entry point."""
    manifest_path = os.environ.get("AGENT_MANIFEST_PATH")

    if not manifest_path:
        print("Error: AGENT_MANIFEST_PATH environment variable not set")
        sys.exit(1)

    worker = AgentWorker(manifest_path)

    try:
        await worker.start()
    except KeyboardInterrupt:
        logger.info("Shutting down...")
    except Exception as e:
        logger.error(f"Fatal error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())