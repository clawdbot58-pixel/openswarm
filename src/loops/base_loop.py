"""Base class for all thinking loops."""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any


@dataclass
class LoopResult:
    """Result of a thinking loop execution."""
    output: str
    confidence: float
    tokens_used: int
    cost_usd: float
    latency_ms: float
    iterations: int
    intermediate_outputs: list[dict[str, Any]]


class BaseLoop(ABC):
    """Abstract base class for thinking loops."""

    @abstractmethod
    async def run(
        self,
        task: str,
        preamble: dict[str, Any],
        model_client: Any,
    ) -> LoopResult:
        """Execute the thinking loop.

        Args:
            task: The task content to process.
            preamble: The preamble context (intent, permissions, etc.).
            model_client: The LLM client for inference.

        Returns:
            LoopResult with output and metadata.
        """
        pass

    @property
    @abstractmethod
    def cost_multiplier(self) -> float:
        """Cost multiplier for budget tracking.

        Returns:
            Multiplier factor (e.g., 1.0 for direct, 3.0 for reflection).
        """
        pass