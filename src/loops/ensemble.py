"""Ensemble loop - multiple models vote."""

import time
from typing import Any

from .base_loop import BaseLoop, LoopResult
from .model_router import LLMClient, ModelRouter


class EnsembleLoop(BaseLoop):
    """Ensemble loop - generate with multiple models, vote on best."""

    def __init__(self, models: list[str], provider: str = "openai"):
        """Initialize the ensemble loop.

        Args:
            models: List of models to use for ensemble.
            provider: The LLM provider.
        """
        self.models = models
        self.provider = provider

    @property
    def cost_multiplier(self) -> float:
        """Cost multiplier equals number of models."""
        return float(len(self.models))

    async def run(
        self,
        task: str,
        preamble: dict[str, Any],
        model_client: LLMClient,
    ) -> LoopResult:
        """Execute the ensemble loop.

        Generate (N models) -> Vote -> Best

        Args:
            task: The task content to process.
            preamble: The preamble context (intent, permissions, etc.).
            model_client: The LLM client for inference (used for voting only).

        Returns:
            LoopResult with output and metadata.
        """
        start_time = time.perf_counter()

        system_prompt = self._build_system_prompt(preamble)

        # Step 1: Generate with each model
        outputs = []
        total_cost = 0.0
        total_tokens = 0

        for model in self.models:
            router = ModelRouter([model], self.provider)
            client = LLMClient([model], self.provider)

            response = await client.generate(
                system=system_prompt,
                user=task,
                json_mode=False,
                temperature=0.7,
            )
            outputs.append(response.content)
            total_cost += response.cost_usd
            total_tokens += response.tokens_in + response.tokens_out

        intermediate = [
            {"model": model, "content": output}
            for model, output in zip(self.models, outputs)
        ]

        # Step 2: Vote on outputs
        vote_prompt = self._build_vote_prompt(task, outputs)
        vote_response = await model_client.generate(
            system=system_prompt,
            user=vote_prompt,
            json_mode=False,
            temperature=0.3,
        )
        total_cost += vote_response.cost_usd
        total_tokens += vote_response.tokens_in + vote_response.tokens_out

        # Parse the vote to find the best output
        best_index = self._parse_vote(vote_response.content, outputs)
        best = outputs[best_index]

        intermediate.append(
            {
                "stage": "vote",
                "content": vote_response.content,
                "best_index": best_index,
                "model": vote_response.model_used,
            }
        )

        latency_ms = (time.perf_counter() - start_time) * 1000

        return LoopResult(
            output=best,
            confidence=0.9,
            tokens_used=total_tokens,
            cost_usd=total_cost,
            latency_ms=latency_ms,
            iterations=len(self.models) + 1,
            intermediate_outputs=intermediate,
        )

    def _build_system_prompt(self, preamble: dict[str, Any]) -> str:
        """Build system prompt from preamble."""
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

    def _build_vote_prompt(self, task: str, outputs: list[str]) -> str:
        """Build the voting prompt."""
        prompt = f"Task: {task}\n\n"
        prompt += "Select the best output:\n\n"
        for i, output in enumerate(outputs):
            prompt += f"Output {i + 1}:\n{output}\n\n"
        prompt += "Respond with just the number (1, 2, etc.) of the best output."
        return prompt

    def _parse_vote(self, vote_content: str, outputs: list[str]) -> int:
        """Parse the vote content to find the best output index."""
        vote_lower = vote_content.lower().strip()

        # Try to find a number in the response
        for i in range(len(outputs)):
            if str(i + 1) in vote_lower:
                return i

        # Default to first output if parse fails
        return 0