"""Debate loop - two opposing views, then verdict."""

import time
from typing import Any

from .base_loop import BaseLoop, LoopResult
from .model_router import LLMClient


class DebateLoop(BaseLoop):
    """Debate loop - argue FOR and AGAINST, then vote."""

    def __init__(self, model_client: LLMClient):
        """Initialize the debate loop.

        Args:
            model_client: The LLM client for inference.
        """
        self.model_client = model_client

    @property
    def cost_multiplier(self) -> float:
        """Cost multiplier is 3.0 for debate loop (2 sides + 1 verdict)."""
        return 3.0

    async def run(
        self,
        task: str,
        preamble: dict[str, Any],
        model_client: LLMClient,
    ) -> LoopResult:
        """Execute the debate loop.

        Side A (FOR) -> Side B (AGAINST) -> Verdict

        Args:
            task: The task content to process.
            preamble: The preamble context (intent, permissions, etc.).
            model_client: The LLM client for inference.

        Returns:
            LoopResult with output and metadata.
        """
        start_time = time.perf_counter()

        system_prompt = self._build_system_prompt(preamble)

        # Step 1: Side A - Argue FOR
        side_a_prompt = f"Argue FOR the following:\n\n{task}"
        side_a_response = await model_client.generate(
            system=system_prompt,
            user=side_a_prompt,
            json_mode=False,
            temperature=0.7,
        )
        side_a = side_a_response.content
        total_cost = side_a_response.cost_usd
        total_tokens = side_a_response.tokens_in + side_a_response.tokens_out

        intermediate = [
            {"side": "A (FOR)", "content": side_a, "model": side_a_response.model_used}
        ]

        # Step 2: Side B - Argue AGAINST
        side_b_prompt = f"Argue AGAINST the following:\n\n{task}"
        side_b_response = await model_client.generate(
            system=system_prompt,
            user=side_b_prompt,
            json_mode=False,
            temperature=0.7,
        )
        side_b = side_b_response.content
        total_cost += side_b_response.cost_usd
        total_tokens += side_b_response.tokens_in + side_b_response.tokens_out

        intermediate.append(
            {
                "side": "B (AGAINST)",
                "content": side_b,
                "model": side_b_response.model_used,
            }
        )

        # Step 3: Verdict
        verdict_prompt = f"""Which is better? A or B?

A (FOR): {side_a}

B (AGAINST): {side_b}

Respond with 'A' or 'B' and a brief explanation."""
        verdict_response = await model_client.generate(
            system=system_prompt,
            user=verdict_prompt,
            json_mode=False,
            temperature=0.3,
        )
        verdict = verdict_response.content
        total_cost += verdict_response.cost_usd
        total_tokens += verdict_response.tokens_in + verdict_response.tokens_out

        intermediate.append(
            {
                "side": "verdict",
                "content": verdict,
                "model": verdict_response.model_used,
            }
        )

        latency_ms = (time.perf_counter() - start_time) * 1000

        # Extract the actual output based on verdict
        output = self._extract_output(verdict, side_a, side_b)

        return LoopResult(
            output=output,
            confidence=0.85,
            tokens_used=total_tokens,
            cost_usd=total_cost,
            latency_ms=latency_ms,
            iterations=3,
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

    def _extract_output(self, verdict: str, side_a: str, side_b: str) -> str:
        """Extract the final output based on the verdict."""
        verdict_lower = verdict.lower().strip()

        # Determine which side won
        if verdict_lower.startswith("a"):
            return f"AGREED: {side_a}"
        elif verdict_lower.startswith("b"):
            return f"DISAGREED: {side_b}"
        else:
            # Default: combine both perspectives
            return f"CONSENSUS: {verdict}"