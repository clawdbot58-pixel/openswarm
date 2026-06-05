"""Tree of Thoughts loop."""

import time
from typing import Any

from .base_loop import BaseLoop, LoopResult
from .model_router import LLMClient


class TreeOfThoughtsLoop(BaseLoop):
    """Tree of thoughts - branch, vote, merge."""

    def __init__(self, model_client: LLMClient, branch_count: int = 3):
        """Initialize the tree of thoughts loop.

        Args:
            model_client: The LLM client for inference.
            branch_count: Number of branches to generate (default 3).
        """
        self.model_client = model_client
        self.branch_count = branch_count

    @property
    def cost_multiplier(self) -> float:
        """Cost multiplier is 4.0 for tree loop (branch_count + 1 critique + 1 vote)."""
        return float(self.branch_count + 1)

    async def run(
        self,
        task: str,
        preamble: dict[str, Any],
        model_client: LLMClient,
    ) -> LoopResult:
        """Execute the tree of thoughts loop.

        Branch (N) -> Vote -> Best

        Args:
            task: The task content to process.
            preamble: The preamble context (intent, permissions, etc.).
            model_client: The LLM client for inference.

        Returns:
            LoopResult with output and metadata.
        """
        start_time = time.perf_counter()

        system_prompt = self._build_system_prompt(preamble)

        # Step 1: Generate branches
        candidates = []
        total_cost = 0.0
        total_tokens = 0

        for i in range(self.branch_count):
            branch_response = await model_client.generate(
                system=system_prompt,
                user=task,
                json_mode=False,
                temperature=0.7,
            )
            candidates.append(branch_response.content)
            total_cost += branch_response.cost_usd
            total_tokens += branch_response.tokens_in + branch_response.tokens_out

        intermediate = [
            {"stage": "branch", "index": i, "content": c}
            for i, c in enumerate(candidates)
        ]

        # Step 2: Vote on candidates
        vote_prompt = self._build_vote_prompt(task, candidates)
        vote_response = await model_client.generate(
            system=system_prompt,
            user=vote_prompt,
            json_mode=False,
            temperature=0.3,
        )
        total_cost += vote_response.cost_usd
        total_tokens += vote_response.tokens_in + vote_response.tokens_out

        # Parse the vote to find the best candidate
        best_index = self._parse_vote(vote_response.content, candidates)
        best = candidates[best_index]

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
            confidence=0.85,
            tokens_used=total_tokens,
            cost_usd=total_cost,
            latency_ms=latency_ms,
            iterations=self.branch_count + 1,
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

    def _build_vote_prompt(self, task: str, candidates: list[str]) -> str:
        """Build the voting prompt."""
        prompt = f"Task: {task}\n\n"
        prompt += "Evaluate these candidates and select the best one:\n\n"
        for i, c in enumerate(candidates):
            prompt += f"Candidate {i + 1}:\n{c}\n\n"
        prompt += "Respond with just the number (1, 2, or 3) of the best candidate."
        return prompt

    def _parse_vote(self, vote_content: str, candidates: list[str]) -> int:
        """Parse the vote content to find the best candidate index."""
        vote_lower = vote_content.lower().strip()

        # Try to find a number in the response
        for i in range(len(candidates)):
            if str(i + 1) in vote_lower:
                return i

        # Default to first candidate if parse fails
        return 0