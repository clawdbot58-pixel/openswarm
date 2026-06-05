"""Tests for TreeOfThoughtsLoop."""

import pytest

from loops import LLMClient, LoopResult, TreeOfThoughtsLoop


@pytest.fixture
def model_client():
    """Create a test model client."""
    return LLMClient(["gpt-4o-mini"], "openai")


@pytest.fixture
def preamble():
    """Create a test preamble."""
    return {
        "intent": {"goal": "Select best approach", "phase": "execution"},
        "permissions": {"can_read": ["/workspace"], "can_write": ["/workspace"]},
        "thinking_loop_config": {"mode": "tree"},
    }


@pytest.mark.asyncio
async def test_tree_loop_runs(model_client, preamble):
    """Test that tree loop executes and returns result."""
    loop = TreeOfThoughtsLoop(model_client, branch_count=3)
    result = await loop.run(
        "What is the best way to sort a list?", preamble, model_client
    )

    assert isinstance(result, LoopResult)
    assert result.output
    assert result.confidence > 0


@pytest.mark.asyncio
async def test_tree_loop_cost_multiplier(model_client, preamble):
    """Test that tree loop cost multiplier is 4.0 (3 branches + 1 vote)."""
    loop = TreeOfThoughtsLoop(model_client, branch_count=3)
    assert loop.cost_multiplier == 4.0


@pytest.mark.asyncio
async def test_tree_loop_custom_branch_count(model_client, preamble):
    """Test tree loop with custom branch count."""
    loop = TreeOfThoughtsLoop(model_client, branch_count=5)
    assert loop.cost_multiplier == 6.0  # 5 branches + 1 vote


@pytest.mark.asyncio
async def test_tree_loop_intermediate_outputs(model_client, preamble):
    """Test that tree loop has branch and vote outputs."""
    loop = TreeOfThoughtsLoop(model_client, branch_count=3)
    result = await loop.run("Best programming language?", preamble, model_client)

    # Should have 3 branches + 1 vote
    assert len(result.intermediate_outputs) >= 3
    # Check for branches
    branches = [io for io in result.intermediate_outputs if io.get("stage") == "branch"]
    assert len(branches) == 3
    # Check for vote
    votes = [io for io in result.intermediate_outputs if io.get("stage") == "vote"]
    assert len(votes) == 1