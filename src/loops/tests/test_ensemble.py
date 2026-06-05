"""Tests for EnsembleLoop."""

import pytest

from loops import EnsembleLoop, LLMClient, LoopResult


@pytest.fixture
def model_client():
    """Create a test model client."""
    return LLMClient(["gpt-4o-mini"], "openai")


@pytest.fixture
def preamble():
    """Create a test preamble."""
    return {
        "intent": {"goal": "Select best output", "phase": "execution"},
        "permissions": {"can_read": ["/workspace"], "can_write": ["/workspace"]},
        "thinking_loop_config": {"mode": "ensemble"},
    }


@pytest.mark.asyncio
async def test_ensemble_loop_runs(model_client, preamble):
    """Test that ensemble loop executes and returns result."""
    loop = EnsembleLoop(["gpt-4o-mini", "claude-sonnet"], "openai")
    result = await loop.run("What is AI?", preamble, model_client)

    assert isinstance(result, LoopResult)
    assert result.output
    assert result.confidence > 0


@pytest.mark.asyncio
async def test_ensemble_loop_cost_multiplier(model_client, preamble):
    """Test that ensemble loop cost multiplier equals number of models."""
    loop = EnsembleLoop(["gpt-4o-mini", "claude-sonnet"], "openai")
    assert loop.cost_multiplier == 2.0


@pytest.mark.asyncio
async def test_ensemble_loop_with_multiple_models(model_client, preamble):
    """Test ensemble loop with 3 models."""
    loop = EnsembleLoop(
        ["gpt-4o-mini", "claude-sonnet", "gpt-4o"],
        "openai"
    )
    assert loop.cost_multiplier == 3.0


@pytest.mark.asyncio
async def test_ensemble_loop_intermediate_outputs(model_client, preamble):
    """Test that ensemble loop has outputs from all models + vote."""
    loop = EnsembleLoop(["gpt-4o-mini", "claude-sonnet"], "openai")
    result = await loop.run("Explain machine learning", preamble, model_client)

    # Should have 2 model outputs + 1 vote
    assert len(result.intermediate_outputs) == 3
    # Check for model outputs (exclude the vote stage, which also carries a
    # ``model`` key for transparency).
    outputs = [io for io in result.intermediate_outputs if io.get("stage") != "vote"]
    assert len(outputs) == 2
    # Check for vote
    votes = [io for io in result.intermediate_outputs if io.get("stage") == "vote"]
    assert len(votes) == 1