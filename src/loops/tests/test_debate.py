"""Tests for DebateLoop."""

import pytest

from loops import DebateLoop, LLMClient, LoopResult


@pytest.fixture
def model_client():
    """Create a test model client."""
    return LLMClient(["gpt-4o-mini"], "openai")


@pytest.fixture
def preamble():
    """Create a test preamble."""
    return {
        "intent": {"goal": "Evaluate options", "phase": "execution"},
        "permissions": {"can_read": ["/workspace"], "can_write": ["/workspace"]},
        "thinking_loop_config": {"mode": "debate"},
    }


@pytest.mark.asyncio
async def test_debate_loop_runs(model_client, preamble):
    """Test that debate loop executes and returns result."""
    loop = DebateLoop(model_client)
    result = await loop.run(
        "Should AI be regulated?", preamble, model_client
    )

    assert isinstance(result, LoopResult)
    assert result.output
    assert result.confidence > 0
    assert result.iterations == 3


@pytest.mark.asyncio
async def test_debate_loop_cost_multiplier(model_client, preamble):
    """Test that debate loop cost multiplier is 3.0."""
    loop = DebateLoop(model_client)
    assert loop.cost_multiplier == 3.0


@pytest.mark.asyncio
async def test_debate_loop_intermediate_outputs(model_client, preamble):
    """Test that debate loop has FOR, AGAINST, and verdict outputs."""
    loop = DebateLoop(model_client)
    result = await loop.run("Is remote work better?", preamble, model_client)

    assert len(result.intermediate_outputs) == 3
    sides = [io.get("side") for io in result.intermediate_outputs]
    assert "A (FOR)" in sides
    assert "B (AGAINST)" in sides
    assert "verdict" in sides


@pytest.mark.asyncio
async def test_debate_loop_extracts_output(model_client, preamble):
    """Test that debate loop extracts proper output based on verdict."""
    loop = DebateLoop(model_client)
    result = await loop.run("Test debate", preamble, model_client)

    # Output should indicate agreement or disagreement
    assert isinstance(result.output, str)
    assert len(result.output) > 0