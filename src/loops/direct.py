"""Direct loop - single LLM call."""

import time
from typing import Any

from .base_loop import BaseLoop, LoopResult
from .model_router import LLMClient


class DirectLoop(BaseLoop):
    """Single LLM call without reasoning."""

    def __init__(self, model_client: LLMClient):
        """Initialize the direct loop.

        Args:
            model_client: The LLM client for inference.
        """
        self.model_client = model_client

    @property
    def cost_multiplier(self) -> float:
        """Cost multiplier is 1.0 for direct loop."""
        return 1.0

    async def run(
        self,
        task: str,
        preamble: dict[str, Any],
        model_client: LLMClient,
    ) -> LoopResult:
        """Execute the direct loop.

        Args:
            task: The task content to process.
            preamble: The preamble context (intent, permissions, etc.).
            model_client: The LLM client for inference.

        Returns:
            LoopResult with output and metadata.
        """
        start_time = time.perf_counter()

        system_prompt = self._build_system_prompt(preamble)

        response = await model_client.generate(
            system=system_prompt,
            user=task,
            json_mode=False,
            temperature=0.7,
        )

        latency_ms = (time.perf_counter() - start_time) * 1000
        tokens_used = response.tokens_in + response.tokens_out

        return LoopResult(
            output=response.content,
            confidence=0.8,
            tokens_used=tokens_used,
            cost_usd=response.cost_usd,
            latency_ms=latency_ms,
            iterations=1,
            intermediate_outputs=[],
        )

    def _build_system_prompt(self, preamble: dict[str, Any]) -> str:
        """Build system prompt from preamble.

        Args:
            preamble: The preamble context.

        Returns:
            Formatted system prompt.
        """
        intent = preamble.get("intent", {})
        permissions = preamble.get("permissions", {})

        prompt = f"# ROLE\n"
        if "goal" in intent:
            prompt += f"Goal: {intent['goal']}\n"

        prompt += f"\n# PERMISSIONS\n"
        if "can_read" in permissions:
            prompt += f"Read: {permissions['can_read']}\n"
        if "can_write" in permissions:
            prompt += f"Write: {permissions['can_write']}\n"
        if "can_execute" in permissions:
            prompt += f"Execute: {permissions['can_execute']}\n"

        return prompt