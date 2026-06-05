"""Tests for CoTLoop."""

import pytest

from loops import CoTLoop, LLMClient, LoopResult


@pytest.fixture
def model_client():
    """Create a test model client."""
    return LLMClient(["gpt-4o-mini"], "openai")


@pytest.fixture
def preamble():
    """Create a test preamble."""
    return {
        "intent": {"goal": "Test task", "phase": "execution"},
        "permissions": {"can_read": ["/workspace"], "can_write": ["/workspace"]},
        "thinking_loop_config": {"mode": "cot"},
    }


@pytest.mark.asyncio
async def test_cot_loop_runs(model_client, preamble):
    """Test that CoT loop executes and returns result."""
    loop = CoTLoop(model_client)
    result = await loop.run("Calculate 15 * 23", preamble, model_client)

    assert isinstance(result, LoopResult)
    assert result.output
    assert result.confidence > 0
    assert result.iterations == 1


@pytest.mark.asyncio
async def test_cot_loop_cost_multiplier(model_client, preamble):
    """Test that CoT loop cost multiplier is 1.0."""
    loop = CoTLoop(model_client)
    assert loop.cost_multiplier == 1.0


@pytest.mark.asyncio
async def test_cot_loop_includes_reasoning(model_client, preamble):
    """Test that CoT loop includes reasoning in intermediate outputs."""
    loop = CoTLoop(model_client)
    result = await loop.run("Explain why sky is blue", preamble, model_client)

    assert len(result.intermediate_outputs) > 0
    assert result.intermediate_outputs[0].get("type") == "cot_reasoning"