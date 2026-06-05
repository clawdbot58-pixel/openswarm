"""Tests for DirectLoop."""

import pytest

from loops import DirectLoop, LLMClient, LoopResult


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
        "thinking_loop_config": {"mode": "direct"},
    }


@pytest.mark.asyncio
async def test_direct_loop_runs(model_client, preamble):
    """Test that direct loop executes and returns result."""
    loop = DirectLoop(model_client)
    result = await loop.run("Write hello world", preamble, model_client)

    assert isinstance(result, LoopResult)
    assert result.output
    assert result.confidence > 0
    assert result.iterations == 1


@pytest.mark.asyncio
async def test_direct_loop_cost_multiplier(model_client, preamble):
    """Test that direct loop cost multiplier is 1.0."""
    loop = DirectLoop(model_client)
    assert loop.cost_multiplier == 1.0


@pytest.mark.asyncio
async def test_direct_loop_output_content(model_client, preamble):
    """Test that direct loop returns output content."""
    loop = DirectLoop(model_client)
    task = "What is 2+2?"
    result = await loop.run(task, preamble, model_client)

    assert isinstance(result.output, str)
    assert len(result.output) > 0