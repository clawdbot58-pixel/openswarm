"""Tests for the LLM client abstraction."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

# Make ``src`` importable.
_SRC = Path(__file__).resolve().parents[3] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from agents.llm_client import (  # noqa: E402
    AllModelsFailed,
    CompletionRequest,
    LLMClient,
    LLMError,
    ModelRoute,
    ModelRouter,
    _MockProvider,
    register_provider,
)


# ---------------------------------------------------------------------------
# Mock provider
# ---------------------------------------------------------------------------

def test_mock_provider_returns_constant_text() -> None:
    """The mock returns the configured text for every call."""
    provider = _MockProvider(text="hello world")
    req = CompletionRequest(
        messages=[{"role": "user", "content": "hi"}], model="m"
    )
    import asyncio

    result = asyncio.run(provider.complete(req))
    assert result.text == "hello world"
    assert result.provider == "mock"
    assert provider.calls == [req]


def test_mock_provider_callable_drives_branches() -> None:
    """A callable mock can return different text per call index."""
    responses = ["first", "second", "third"]
    provider = _MockProvider(text=lambda idx, req: responses[idx])
    import asyncio

    texts = []
    for _ in range(3):
        result = asyncio.run(
            provider.complete(
                CompletionRequest(
                    messages=[{"role": "user", "content": "x"}], model="m"
                )
            )
        )
        texts.append(result.text)
    assert texts == ["first", "second", "third"]


def test_mock_provider_serialises_dict_when_json_mode() -> None:
    """When ``response_format_json=True`` a dict becomes JSON text."""
    provider = _MockProvider(text={"answer": 42})
    import asyncio

    result = asyncio.run(
        provider.complete(
            CompletionRequest(
                messages=[{"role": "user", "content": "?"}],
                model="m",
                response_format_json=True,
            )
        )
    )
    parsed = json.loads(result.text)
    assert parsed == {"answer": 42}


# ---------------------------------------------------------------------------
# LLMClient.from_config
# ---------------------------------------------------------------------------

def test_from_config_default_uses_mock() -> None:
    """An empty config falls back to a single mock route."""
    client = LLMClient.from_config({})
    assert len(client.router.routes) == 1
    assert client.router.routes[0].provider == "mock"


def test_from_config_with_explicit_routes() -> None:
    """Routes in the config are honoured."""
    client = LLMClient.from_config(
        {
            "routes": [
                {"provider": "mock", "model": "a"},
                {"provider": "mock", "model": "b"},
            ],
            "max_retries": 4,
        }
    )
    assert [r.model for r in client.router.routes] == ["a", "b"]
    assert client._max_retries == 4


def test_from_config_with_providers_dict() -> None:
    """A caller may inject pre-built provider instances."""
    provider = _MockProvider(text="custom")
    client = LLMClient.from_config(
        {"routes": [{"provider": "mock", "model": "x"}]},
        providers={"mock": provider},
    )
    assert client.router._providers["mock"] is provider


# ---------------------------------------------------------------------------
# ModelRouter
# ---------------------------------------------------------------------------

def test_router_requires_at_least_one_route() -> None:
    with pytest.raises(ValueError):
        ModelRouter([])


def test_router_complete_returns_first_success() -> None:
    """A working primary route is used; fallbacks are not touched."""
    a = _MockProvider(text="a-ok")
    b = _MockProvider(text="b-ok")
    router = ModelRouter(
        [
            ModelRoute(provider="a", model="ma"),
            ModelRoute(provider="b", model="mb"),
        ],
        providers={"a": a, "b": b},
    )
    import asyncio

    result = asyncio.run(
        router.complete(
            CompletionRequest(
                messages=[{"role": "user", "content": "?"}], model="ignored"
            )
        )
    )
    assert result.text == "a-ok"
    assert len(a.calls) == 1
    assert b.calls == []


def test_router_falls_back_on_error() -> None:
    """When the primary raises ``LLMError`` the next route is tried."""
    class _Boom(_MockProvider):
        async def complete(self, request):  # type: ignore[override]
            raise LLMError("primary down")

    a = _Boom()
    b = _MockProvider(text="fallback-ok")
    router = ModelRouter(
        [ModelRoute("a", "ma"), ModelRoute("b", "mb")],
        providers={"a": a, "b": b},
    )
    import asyncio

    result = asyncio.run(
        router.complete(
            CompletionRequest(
                messages=[{"role": "user", "content": "?"}], model="x"
            )
        )
    )
    assert result.text == "fallback-ok"
    assert b.calls and b.calls[0].model == "mb"


def test_router_raises_all_models_failed_when_every_route_fails() -> None:
    class _Boom(_MockProvider):
        async def complete(self, request):  # type: ignore[override]
            raise LLMError("nope")

    router = ModelRouter(
        [ModelRoute("a", "ma"), ModelRoute("b", "mb")],
        providers={"a": _Boom(), "b": _Boom()},
    )
    import asyncio

    with pytest.raises(AllModelsFailed) as exc:
        asyncio.run(
            router.complete(
                CompletionRequest(
                    messages=[{"role": "user", "content": "?"}], model="x"
                )
            )
        )
    assert len(exc.value.errors) == 2


# ---------------------------------------------------------------------------
# LLMClient retries
# ---------------------------------------------------------------------------

def test_client_retries_then_succeeds() -> None:
    """``max_retries`` lets us burn through ``AllModelsFailed`` attempts."""
    calls = {"n": 0}

    class _Sometimes(_MockProvider):
        async def complete(self, request):  # type: ignore[override]
            calls["n"] += 1
            if calls["n"] < 2:
                raise LLMError("transient")
            return await super().complete(request)

    client = LLMClient(
        ModelRouter(
            [ModelRoute("mock", "m")], providers={"mock": _Sometimes(text="ok")}
        ),
        max_retries=3,
        retry_backoff_seconds=0.0,
    )
    import asyncio

    result = asyncio.run(
        client.complete(
            CompletionRequest(
                messages=[{"role": "user", "content": "?"}], model="m"
            )
        )
    )
    assert result.text == "ok"
    assert calls["n"] == 2


def test_client_raises_after_exhausting_retries() -> None:
    class _Boom(_MockProvider):
        async def complete(self, request):  # type: ignore[override]
            raise LLMError("always")

    client = LLMClient(
        ModelRouter(
            [ModelRoute("mock", "m")], providers={"mock": _Boom()}
        ),
        max_retries=2,
        retry_backoff_seconds=0.0,
    )
    import asyncio

    with pytest.raises(AllModelsFailed):
        asyncio.run(
            client.complete(
                CompletionRequest(
                    messages=[{"role": "user", "content": "?"}], model="m"
                )
            )
        )


# ---------------------------------------------------------------------------
# Convenience helpers
# ---------------------------------------------------------------------------

def test_complete_text_routes_first_model() -> None:
    provider = _MockProvider(text="text-ok")
    client = LLMClient(
        ModelRouter(
            [ModelRoute("mock", "primary"), ModelRoute("mock", "fb")],
            providers={"mock": provider},
        )
    )
    import asyncio

    result = asyncio.run(
        client.complete_text(system="s", user="u", temperature=0.1)
    )
    assert result.text == "text-ok"
    assert provider.calls[0].messages[0]["role"] == "system"
    assert provider.calls[0].model == "primary"


def test_complete_json_parses_response() -> None:
    provider = _MockProvider(text='{"foo": 1}')
    client = LLMClient(
        ModelRouter([ModelRoute("mock", "m")], providers={"mock": provider})
    )
    import asyncio

    data = asyncio.run(client.complete_json(system="s", user="u"))
    assert data == {"foo": 1}


def test_complete_json_strips_code_fences() -> None:
    provider = _MockProvider(text="```json\n{\"x\": 2}\n```")
    client = LLMClient(
        ModelRouter([ModelRoute("mock", "m")], providers={"mock": provider})
    )
    import asyncio

    data = asyncio.run(client.complete_json(system="s", user="u"))
    assert data == {"x": 2}


def test_complete_json_raises_on_invalid_json() -> None:
    provider = _MockProvider(text="not json at all")
    client = LLMClient(
        ModelRouter([ModelRoute("mock", "m")], providers={"mock": provider})
    )
    import asyncio

    with pytest.raises(LLMError):
        asyncio.run(client.complete_json(system="s", user="u"))


# ---------------------------------------------------------------------------
# register_provider
# ---------------------------------------------------------------------------

def test_register_provider_adds_factory() -> None:
    """``register_provider`` makes a new name resolvable."""
    provider = _MockProvider(text="custom-text")
    register_provider("test-custom", lambda: provider)
    client = LLMClient.from_config(
        {"routes": [{"provider": "test-custom", "model": "x"}]}
    )
    import asyncio

    result = asyncio.run(
        client.complete(
            CompletionRequest(
                messages=[{"role": "user", "content": "?"}], model="x"
            )
        )
    )
    assert result.text == "custom-text"
