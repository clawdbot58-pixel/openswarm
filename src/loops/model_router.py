"""Model router with fallback chain."""

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class ModelResponse:
    """Response from an LLM model call."""
    content: str
    model_used: str
    tokens_in: int
    tokens_out: int
    cost_usd: float
    latency_ms: float


class ModelExhaustedError(Exception):
    """Raised when all models in the fallback chain have failed."""
    pass


class RateLimitError(Exception):
    """Raised when rate limited."""
    pass


class TimeoutError(Exception):
    """Raised when a call times out."""
    pass


class APIError(Exception):
    """Raised when API call fails."""
    pass


class ModelRouter:
    """Routes LLM calls through a fallback chain of models."""

    def __init__(self, models: list[str], provider: str):
        """Initialize the model router.

        Args:
            models: Ordered list of models [primary, fallback1, fallback2].
            provider: The LLM provider (openai, anthropic, ollama, etc.).
        """
        self.models = models
        self.provider = provider

    async def call(
        self,
        system: str,
        user: str,
        json_mode: bool = False,
        temperature: float = 0.7,
    ) -> ModelResponse:
        """Make an LLM call with fallback chain.

        Args:
            system: System prompt.
            user: User message.
            json_mode: Whether to request JSON output.
            temperature: Sampling temperature.

        Returns:
            ModelResponse from the first successful model.

        Raises:
            ModelExhaustedError: When all models fail.
        """
        for model in self.models:
            try:
                return await self._call_single(
                    model, system, user, json_mode, temperature
                )
            except (RateLimitError, TimeoutError, APIError) as e:
                logger.warning(f"Model {model} failed: {e}, trying next...")
                continue

        raise ModelExhaustedError(f"All models exhausted: {self.models}")

    async def _call_single(
        self,
        model: str,
        system: str,
        user: str,
        json_mode: bool,
        temperature: float,
    ) -> ModelResponse:
        """Make a single model call.

        This is a stub - in production, this would call the actual provider.

        Args:
            model: Model identifier.
            system: System prompt.
            user: User message.
            json_mode: Whether to request JSON output.
            temperature: Sampling temperature.

        Returns:
            ModelResponse from the model.

        Raises:
            RateLimitError: On rate limit.
            TimeoutError: On timeout.
            APIError: On API failure.
        """
        import time
        start_time = time.perf_counter()

        # Stub fault-injection: any model name starting with ``failing-``
        # is treated as a hard failure so the router's fallback chain
        # and ``ModelExhaustedError`` paths are testable without a live
        # provider.  Real provider adapters (added in Phase 5+) will
        # raise their own typed errors on transport / rate-limit / api
        # failures; this branch only fires for the stub.
        if model.startswith("failing-"):
            raise APIError(f"stub: model {model!r} configured to fail")

        # Stub implementation - returns placeholder response
        # In Phase 5+, this would call actual LLM APIs
        await asyncio.sleep(0.01)  # Simulate network latency

        latency_ms = (time.perf_counter() - start_time) * 1000

        # Calculate mock tokens and cost
        tokens_in = len(system.split()) + len(user.split())
        tokens_out = 50  # Mock output
        cost_usd = tokens_out * 0.00001  # Rough cost estimate

        return ModelResponse(
            content=f"[Stub response from {model}] {user[:50]}",
            model_used=model,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cost_usd=cost_usd,
            latency_ms=latency_ms,
        )


class LLMClient:
    """Simple LLM client that uses ModelRouter."""

    def __init__(self, models: list[str], provider: str = "openai"):
        """Initialize the LLM client.

        Args:
            models: List of models to use.
            provider: The LLM provider.
        """
        self.router = ModelRouter(models, provider)

    async def generate(
        self,
        system: str,
        user: str,
        json_mode: bool = False,
        temperature: float = 0.7,
    ) -> ModelResponse:
        """Generate a response using the model router.

        Args:
            system: System prompt.
            user: User message.
            json_mode: Whether to request JSON output.
            temperature: Sampling temperature.

        Returns:
            ModelResponse from the model.
        """
        return await self.router.call(system, user, json_mode, temperature)