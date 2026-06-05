"""Loop primitives - atomic building blocks of reasoning."""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from .model_router import LLMClient


@dataclass
class PrimitiveContext:
    """Context passed to primitive execution.

    Attributes:
        task: Original user task.
        model_client: For LLM calls.
        system_prompt: Assembled preamble.
        inputs: Named inputs from upstream nodes.
        config: Node-specific config (temperature, model override, etc.).
        metadata: Execution metadata (node_id, graph_id, etc.).
    """
    task: str
    model_client: LLMClient
    system_prompt: str
    inputs: dict[str, Any] = field(default_factory=dict)
    config: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class PrimitiveResult:
    """Result from a primitive execution.

    Attributes:
        output: The text output from the primitive.
        score: Optional score (for critique/vote primitives).
        tokens_used: Total tokens consumed.
        cost_usd: Cost in USD.
        latency_ms: Execution latency in milliseconds.
        metadata: Additional execution metadata.
    """
    output: str
    score: float | None = None
    tokens_used: int = 0
    cost_usd: float = 0.0
    latency_ms: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)


class Primitive(ABC):
    """Abstract base class for reasoning primitives."""

    name: str
    cost_weight: float

    @abstractmethod
    async def execute(self, context: PrimitiveContext) -> PrimitiveResult:
        """Execute the primitive.

        Args:
            context: The execution context with inputs and config.

        Returns:
            PrimitiveResult with output and metrics.
        """
        pass


class GeneratePrimitive(Primitive):
    """Single LLM call primitive.

    Input: prompt.
    Output: generated text.
    """

    name = "generate"
    cost_weight = 1.0

    async def execute(self, context: PrimitiveContext) -> PrimitiveResult:
        """Generate text using LLM.

        Args:
            context: Must contain 'prompt' in inputs or config.

        Returns:
            PrimitiveResult with generated text.
        """
        import time

        prompt = context.inputs.get("prompt", context.config.get("prompt", ""))
        temperature = context.config.get("temperature", 0.7)
        model_override = context.config.get("model")

        start_time = time.perf_counter()

        if model_override:
            from .model_router import ModelRouter
            router = ModelRouter([model_override], context.model_client.router.provider)
            client = LLMClient([model_override], context.model_client.router.provider)
            response = await client.generate(
                system=context.system_prompt,
                user=prompt,
                json_mode=False,
                temperature=temperature,
            )
        else:
            response = await context.model_client.generate(
                system=context.system_prompt,
                user=prompt,
                json_mode=False,
                temperature=temperature,
            )

        latency_ms = (time.perf_counter() - start_time) * 1000
        tokens_used = response.tokens_in + response.tokens_out

        return PrimitiveResult(
            output=response.content,
            tokens_used=tokens_used,
            cost_usd=response.cost_usd,
            latency_ms=latency_ms,
            metadata={
                "model_used": response.model_used,
                "prompt_tokens": response.tokens_in,
                "completion_tokens": response.tokens_out,
            },
        )


class CritiquePrimitive(Primitive):
    """Evaluate output against a rubric.

    Input: target_output + rubric.
    Output: critique text + score.
    """

    name = "critique"
    cost_weight = 1.0

    async def execute(self, context: PrimitiveContext) -> PrimitiveResult:
        """Critique an output.

        Args:
            context: Must contain 'target' and 'rubric' in inputs or config.

        Returns:
            PrimitiveResult with critique and score (1-10).
        """
        import time

        target = context.inputs.get("target", context.config.get("target", ""))
        rubric = context.inputs.get(
            "rubric",
            context.config.get("rubric", "Evaluate quality, correctness, and completeness."),
        )

        critique_prompt = f"""Critique the following output:

--- OUTPUT ---
{target}
--- END OUTPUT ---

Rubric: {rubric}

Provide a critique and score from 1-10, where 10 is perfect."""

        temperature = context.config.get("temperature", 0.3)
        model_override = context.config.get("model")

        start_time = time.perf_counter()

        if model_override:
            client = LLMClient([model_override], context.model_client.router.provider)
            response = await client.generate(
                system=context.system_prompt,
                user=critique_prompt,
                json_mode=False,
                temperature=temperature,
            )
        else:
            response = await context.model_client.generate(
                system=context.system_prompt,
                user=critique_prompt,
                json_mode=False,
                temperature=temperature,
            )

        latency_ms = (time.perf_counter() - start_time) * 1000
        tokens_used = response.tokens_in + response.tokens_out

        score = self._parse_score(response.content)

        return PrimitiveResult(
            output=response.content,
            score=score,
            tokens_used=tokens_used,
            cost_usd=response.cost_usd,
            latency_ms=latency_ms,
            metadata={"model_used": response.model_used},
        )

    def _parse_score(self, content: str) -> float:
        """Parse score from critique content.

        Args:
            content: The critique text.

        Returns:
            Score from 1-10, defaulting to 5.0 if not found.
        """
        import re

        patterns = [
            r"score[:\s]+(\d+(?:\.\d+)?)",
            r"(\d+(?:\.\d+)?)\s*(?:/|out of)\s*10",
            r"rating[:\s]+(\d+(?:\.\d+)?)",
        ]

        for pattern in patterns:
            match = re.search(pattern, content, re.IGNORECASE)
            if match:
                score = float(match.group(1))
                return max(1.0, min(10.0, score))

        return 5.0


class VotePrimitive(Primitive):
    """Select best from candidates.

    Input: candidates[] + criteria.
    Output: winner_index + reasoning.
    """

    name = "vote"
    cost_weight = 1.0

    async def execute(self, context: PrimitiveContext) -> PrimitiveResult:
        """Vote for the best candidate.

        Args:
            context: Must contain 'candidates' list in inputs or config.

        Returns:
            PrimitiveResult with winner_index and reasoning.
        """
        import time

        candidates = context.inputs.get(
            "candidates",
            context.config.get("candidates", []),
        )
        criteria = context.inputs.get(
            "criteria",
            context.config.get("criteria", "Which is best overall?"),
        )

        if not candidates:
            return PrimitiveResult(
                output="No candidates provided",
                score=0.0,
                tokens_used=0,
                cost_usd=0.0,
                latency_ms=0.0,
                metadata={"error": "no_candidates"},
            )

        if len(candidates) == 1:
            return PrimitiveResult(
                output="Only one candidate, auto-selected.",
                score=1.0,
                tokens_used=0,
                cost_usd=0.0,
                latency_ms=0.0,
                metadata={"winner_index": 0},
            )

        vote_prompt = self._build_vote_prompt(candidates, criteria)

        temperature = context.config.get("temperature", 0.3)
        model_override = context.config.get("model")

        start_time = time.perf_counter()

        if model_override:
            client = LLMClient([model_override], context.model_client.router.provider)
            response = await client.generate(
                system=context.system_prompt,
                user=vote_prompt,
                json_mode=False,
                temperature=temperature,
            )
        else:
            response = await context.model_client.generate(
                system=context.system_prompt,
                user=vote_prompt,
                json_mode=False,
                temperature=temperature,
            )

        latency_ms = (time.perf_counter() - start_time) * 1000
        tokens_used = response.tokens_in + response.tokens_out

        winner_index = self._parse_winner(response.content, len(candidates))

        return PrimitiveResult(
            output=response.content,
            score=float(winner_index) / len(candidates) if len(candidates) > 0 else 0.0,
            tokens_used=tokens_used,
            cost_usd=response.cost_usd,
            latency_ms=latency_ms,
            metadata={
                "winner_index": winner_index,
                "num_candidates": len(candidates),
            },
        )

    def _build_vote_prompt(self, candidates: list[str], criteria: str) -> str:
        """Build the voting prompt.

        Args:
            candidates: List of candidate strings.
            criteria: Selection criteria.

        Returns:
            Formatted vote prompt.
        """
        prompt = f"Task: {criteria}\n\nCandidates:\n\n"
        for i, candidate in enumerate(candidates):
            prompt += f"[{i + 1}] {candidate}\n\n"
        prompt += "Select the best candidate. Reply with just the number (1, 2, etc.) and brief reasoning."
        return prompt

    def _parse_winner(self, content: str, num_candidates: int) -> int:
        """Parse winner index from vote content.

        Args:
            content: The vote response.
            num_candidates: Total number of candidates.

        Returns:
            Winner index (0-based).
        """
        import re

        content_lower = content.lower().strip()

        patterns = [
            r"(?:best|winner|selected?)[:\s]*\[?(\d+)\]?",
            r"^(\d+)",
            r"\b(\d+)\s*(?:is best|winner)",
        ]

        for pattern in patterns:
            match = re.search(pattern, content_lower)
            if match:
                idx = int(match.group(1)) - 1
                if 0 <= idx < num_candidates:
                    return idx

        return 0


class RevisePrimitive(Primitive):
    """Rewrite based on critique.

    Input: original + critique.
    Output: revised text.
    """

    name = "revise"
    cost_weight = 1.0

    async def execute(self, context: PrimitiveContext) -> PrimitiveResult:
        """Revise output based on critique.

        Args:
            context: Must contain 'original' and 'critique' in inputs or config.

        Returns:
            PrimitiveResult with revised text.
        """
        import time

        original = context.inputs.get("original", context.config.get("original", ""))
        critique = context.inputs.get("critique", context.config.get("critique", ""))

        revise_prompt = f"""Revise the original output based on the critique.

--- ORIGINAL ---
{original}
--- END ORIGINAL ---

--- CRITIQUE ---
{critique}
--- END CRITIQUE ---

Provide the revised output:"""

        temperature = context.config.get("temperature", 0.5)
        model_override = context.config.get("model")

        start_time = time.perf_counter()

        if model_override:
            client = LLMClient([model_override], context.model_client.router.provider)
            response = await client.generate(
                system=context.system_prompt,
                user=revise_prompt,
                json_mode=False,
                temperature=temperature,
            )
        else:
            response = await context.model_client.generate(
                system=context.system_prompt,
                user=revise_prompt,
                json_mode=False,
                temperature=temperature,
            )

        latency_ms = (time.perf_counter() - start_time) * 1000
        tokens_used = response.tokens_in + response.tokens_out

        return PrimitiveResult(
            output=response.content,
            tokens_used=tokens_used,
            cost_usd=response.cost_usd,
            latency_ms=latency_ms,
            metadata={
                "model_used": response.model_used,
                "original_length": len(original),
                "critique_length": len(critique),
            },
        )


class BranchPrimitive(Primitive):
    """Generate N parallel candidates.

    Input: prompt + n + temperature.
    Output: candidates[].
    """

    name = "branch"
    cost_weight = 1.0

    async def execute(self, context: PrimitiveContext) -> PrimitiveResult:
        """Generate multiple candidate outputs in parallel.

        Args:
            context: Must contain 'prompt' in inputs or config.
                    'n' in config sets branch count (default 3).

        Returns:
            PrimitiveResult with candidates list.
        """
        import asyncio

        prompt = context.inputs.get("prompt", context.config.get("prompt", ""))
        n = context.config.get("n", 3)
        temperature = context.config.get("temperature", 0.7)
        model_override = context.config.get("model")

        async def generate_branch(idx: int) -> tuple[int, str, float]:
            """Generate a single branch.

            Returns:
                Tuple of (index, content, cost).
            """
            branch_prompt = f"{prompt}\n\n[Branch {idx + 1} of {n}]"
            start = asyncio.get_event_loop().time()

            if model_override:
                client = LLMClient([model_override], context.model_client.router.provider)
                response = await client.generate(
                    system=context.system_prompt,
                    user=branch_prompt,
                    json_mode=False,
                    temperature=temperature,
                )
            else:
                response = await context.model_client.generate(
                    system=context.system_prompt,
                    user=branch_prompt,
                    json_mode=False,
                    temperature=temperature,
                )

            latency = (asyncio.get_event_loop().time() - start) * 1000
            return idx, response.content, response.cost_usd, latency

        start_time = asyncio.get_event_loop().time()

        results = await asyncio.gather(
            *[generate_branch(i) for i in range(n)],
            return_exceptions=True,
        )

        total_latency_ms = (asyncio.get_event_loop().time() - start_time) * 1000
        candidates = []
        total_cost = 0.0
        total_tokens = 0

        for result in results:
            if isinstance(result, Exception):
                continue
            idx, content, cost, _ = result
            candidates.append(content)
            total_cost += cost

        tokens_per = len(context.system_prompt.split()) + len(prompt.split())
        total_tokens = len(candidates) * (tokens_per + 50)

        return PrimitiveResult(
            output=str(candidates),
            tokens_used=total_tokens,
            cost_usd=total_cost,
            latency_ms=total_latency_ms,
            metadata={
                "candidates": candidates,
                "num_branches": len(candidates),
                "branch_costs": [r[2] for r in results if not isinstance(r, Exception)],
            },
        )


class MergePrimitive(Primitive):
    """Combine multiple outputs.

    Input: outputs[] + strategy.
    Output: merged text.
    """

    name = "merge"
    cost_weight = 1.0

    async def execute(self, context: PrimitiveContext) -> PrimitiveResult:
        """Merge multiple outputs into one.

        Args:
            context: Must contain 'outputs' list in inputs or config.
                    'strategy' in config (default 'combine').

        Returns:
            PrimitiveResult with merged output.
        """
        import time

        outputs = context.inputs.get(
            "outputs",
            context.config.get("outputs", []),
        )
        strategy = context.config.get(
            "strategy",
            context.inputs.get("strategy", "combine"),
        )

        if not outputs:
            return PrimitiveResult(
                output="No outputs to merge",
                tokens_used=0,
                cost_usd=0.0,
                latency_ms=0.0,
                metadata={"error": "no_outputs"},
            )

        if len(outputs) == 1:
            return PrimitiveResult(
                output=outputs[0],
                tokens_used=0,
                cost_usd=0.0,
                latency_ms=0.0,
                metadata={"singleton": True},
            )

        merge_prompt = self._build_merge_prompt(outputs, strategy)

        temperature = context.config.get("temperature", 0.5)
        model_override = context.config.get("model")

        start_time = time.perf_counter()

        if model_override:
            client = LLMClient([model_override], context.model_client.router.provider)
            response = await client.generate(
                system=context.system_prompt,
                user=merge_prompt,
                json_mode=False,
                temperature=temperature,
            )
        else:
            response = await context.model_client.generate(
                system=context.system_prompt,
                user=merge_prompt,
                json_mode=False,
                temperature=temperature,
            )

        latency_ms = (time.perf_counter() - start_time) * 1000
        tokens_used = response.tokens_in + response.tokens_out

        return PrimitiveResult(
            output=response.content,
            tokens_used=tokens_used,
            cost_usd=response.cost_usd,
            latency_ms=latency_ms,
            metadata={
                "num_inputs": len(outputs),
                "strategy": strategy,
                "model_used": response.model_used,
            },
        )

    def _build_merge_prompt(self, outputs: list[str], strategy: str) -> str:
        """Build the merge prompt.

        Args:
            outputs: List of outputs to merge.
            strategy: Merge strategy (combine, synthesize, choose_best).

        Returns:
            Formatted merge prompt.
        """
        if strategy == "synthesize":
            prompt = "Synthesize these outputs into a single coherent response:\n\n"
        elif strategy == "choose_best":
            prompt = "Select the best parts from these outputs and combine:\n\n"
        else:
            prompt = "Combine these outputs into a single response:\n\n"

        for i, output in enumerate(outputs):
            prompt += f"[{i + 1}] {output}\n\n"

        prompt += "Provide the merged result."
        return prompt


PRIMITIVES: dict[str, type[Primitive]] = {
    "generate": GeneratePrimitive,
    "critique": CritiquePrimitive,
    "vote": VotePrimitive,
    "revise": RevisePrimitive,
    "branch": BranchPrimitive,
    "merge": MergePrimitive,
}


def get_primitive(name: str) -> Primitive:
    """Get a primitive instance by name.

    Args:
        name: Primitive name (generate, critique, vote, revise, branch, merge).

    Returns:
        Primitive instance.

    Raises:
        ValueError: If primitive name is unknown.
    """
    if name not in PRIMITIVES:
        raise ValueError(f"Unknown primitive: {name}. Available: {list(PRIMITIVES.keys())}")
    return PRIMITIVES[name]()