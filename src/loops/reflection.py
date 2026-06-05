"""Reflection loop - draft, critique, revise."""

import time
from typing import Any

from .base_loop import BaseLoop, LoopResult
from .model_router import LLMClient


class ReflectionLoop(BaseLoop):
    """Self-correction loop - generate, critique, revise."""

    def __init__(self, model_client: LLMClient):
        """Initialize the reflection loop.

        Args:
            model_client: The LLM client for inference.
        """
        self.model_client = model_client

    @property
    def cost_multiplier(self) -> float:
        """Cost multiplier is 3.0 for reflection loop (3 LLM calls)."""
        return 3.0

    async def run(
        self,
        task: str,
        preamble: dict[str, Any],
        model_client: LLMClient,
    ) -> LoopResult:
        """Execute the reflection loop.

        Draft -> Critique -> Revise

        Args:
            task: The task content to process.
            preamble: The preamble context (intent, permissions, etc.).
            model_client: The LLM client for inference.

        Returns:
            LoopResult with output and metadata.
        """
        start_time = time.perf_counter()

        system_prompt = self._build_system_prompt(preamble)

        # Step 1: Draft
        draft_response = await model_client.generate(
            system=system_prompt,
            user=task,
            json_mode=False,
            temperature=0.7,
        )
        draft = draft_response.content
        total_cost = draft_response.cost_usd
        total_tokens = draft_response.tokens_in + draft_response.tokens_out

        intermediate = [
            {"stage": "draft", "content": draft, "model": draft_response.model_used}
        ]

        # Step 2: Critique
        critique_prompt = f"Critique this output:\n\n{draft}"
        critique_response = await model_client.generate(
            system=system_prompt,
            user=critique_prompt,
            json_mode=False,
            temperature=0.3,
        )
        critique = critique_response.content
        total_cost += critique_response.cost_usd
        total_tokens += critique_response.tokens_in + critique_response.tokens_out

        intermediate.append(
            {
                "stage": "critique",
                "content": critique,
                "model": critique_response.model_used,
            }
        )

        # Step 3: Revise
        revise_prompt = f"Revise based on the critique.\n\nOriginal: {draft}\n\nCritique: {critique}"
        revise_response = await model_client.generate(
            system=system_prompt,
            user=revise_prompt,
            json_mode=False,
            temperature=0.5,
        )
        revision = revise_response.content
        total_cost += revise_response.cost_usd
        total_tokens += revise_response.tokens_in + revise_response.tokens_out

        intermediate.append(
            {
                "stage": "revision",
                "content": revision,
                "model": revise_response.model_used,
            }
        )

        latency_ms = (time.perf_counter() - start_time) * 1000

        return LoopResult(
            output=revision,
            confidence=0.9,
            tokens_used=total_tokens,
            cost_usd=total_cost,
            latency_ms=latency_ms,
            iterations=3,
            intermediate_outputs=intermediate,
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