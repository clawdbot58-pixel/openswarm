"""Tests for ReflectionLoop."""

import pytest

from loops import LLMClient, LoopResult, ReflectionLoop


@pytest.fixture
def model_client():
    """Create a test model client."""
    return LLMClient(["gpt-4o-mini"], "openai")


@pytest.fixture
def preamble():
    """Create a test preamble."""
    return {
        "intent": {"goal": "Refine text", "phase": "execution"},
        "permissions": {"can_read": ["/workspace"], "can_write": ["/workspace"]},
        "thinking_loop_config": {"mode": "reflection"},
    }


@pytest.mark.asyncio
async def test_reflection_loop_runs(model_client, preamble):
    """Test that reflection loop executes and returns result."""
    loop = ReflectionLoop(model_client)
    result = await loop.run("Improve this text: The cat sat.", preamble, model_client)

    assert isinstance(result, LoopResult)
    assert result.output
    assert result.confidence > 0
    assert result.iterations == 3


@pytest.mark.asyncio
async def test_reflection_loop_cost_multiplier(model_client, preamble):
    """Test that reflection loop cost multiplier is 3.0."""
    loop = ReflectionLoop(model_client)
    assert loop.cost_multiplier == 3.0


@pytest.mark.asyncio
async def test_reflection_loop_intermediate_outputs(model_client, preamble):
    """Test that reflection loop has 3 intermediate outputs."""
    loop = ReflectionLoop(model_client)
    result = await loop.run("Write a haiku", preamble, model_client)

    assert len(result.intermediate_outputs) == 3
    stages = [io.get("stage") for io in result.intermediate_outputs]
    assert "draft" in stages
    assert "critique" in stages
    assert "revision" in stages


@pytest.mark.asyncio
async def test_reflection_loop_cost_accumulation(model_client, preamble):
    """Test that reflection loop accumulates costs from all 3 calls."""
    loop = ReflectionLoop(model_client)
    result = await loop.run("Write test", preamble, model_client)

    # Each call has a cost, total should be sum of all
    assert result.cost_usd > 0
    assert result.tokens_used > 0