"""Tests for ModelRouter."""

import pytest

from loops import LLMClient, ModelExhaustedError, ModelRouter


@pytest.fixture
def models():
    """Create test model list."""
    return ["gpt-4o-mini", "gpt-4o", "claude-sonnet"]


@pytest.mark.asyncio
async def test_model_router_primary_succeeds(models):
    """Test that model router uses primary model when successful."""
    router = ModelRouter(models, "openai")
    response = await router.call("System", "User message")

    assert response.model_used == "gpt-4o-mini"
    assert response.content
    assert response.tokens_in > 0
    assert response.tokens_out > 0
    assert response.cost_usd >= 0
    assert response.latency_ms > 0


@pytest.mark.asyncio
async def test_model_router_fallback(models):
    """Test that model router falls back to next model on failure."""
    models_failing = ["failing-model", "gpt-4o-mini"]
    router = ModelRouter(models_failing, "openai")

    # First model fails, should use second
    response = await router.call("System", "User message")

    assert response.model_used == "gpt-4o-mini"


@pytest.mark.asyncio
async def test_model_router_all_fail():
    """Test that model router raises error when all models fail."""
    router = ModelRouter(["failing-1", "failing-2"], "openai")

    with pytest.raises(ModelExhaustedError):
        await router.call("System", "User message")


@pytest.mark.asyncio
async def test_model_response_fields():
    """Test that ModelResponse has all required fields."""
    router = ModelRouter(["gpt-4o-mini"], "openai")
    response = await router.call("System", "User")

    assert hasattr(response, "content")
    assert hasattr(response, "model_used")
    assert hasattr(response, "tokens_in")
    assert hasattr(response, "tokens_out")
    assert hasattr(response, "cost_usd")
    assert hasattr(response, "latency_ms")


@pytest.mark.asyncio
async def test_llm_client_uses_router():
    """Test that LLMClient properly uses the router."""
    client = LLMClient(["gpt-4o-mini", "gpt-4o"], "openai")
    response = await client.generate("System", "User")

    assert response.content
    assert response.model_used in ["gpt-4o-mini", "gpt-4o"]


@pytest.mark.asyncio
async def test_model_router_json_mode():
    """Test that model router handles JSON mode."""
    router = ModelRouter(["gpt-4o-mini"], "openai")
    response = await router.call("System", "User", json_mode=True)

    assert response.content


@pytest.mark.asyncio
async def test_model_router_temperature():
    """Test that model router respects temperature parameter."""
    router = ModelRouter(["gpt-4o-mini"], "openai")
    response = await router.call("System", "User", temperature=0.1)

    assert response.content