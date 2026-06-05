"""Configurable LLM client used by every user-facing agent.

The swarm needs to talk to LLM providers, but it must not be locked to
any one of them. This module is the abstraction layer. It exposes a
single :class:`LLMClient` with a small, deliberately narrow surface:

* :meth:`LLMClient.complete` — synchronous-style chat completion
* :meth:`LLMClient.stream`   — async generator of text chunks

Provider implementations live as small private classes. New providers
are added by implementing two methods and registering in
:data:`_PROVIDERS`. The public API never leaks provider-specific
types; every call normalises into a :class:`CompletionResult`.

Model fallback
--------------
The :class:`ModelRouter` wraps an ordered list of
``(provider_name, model_name)`` pairs and falls through them on
transport errors, timeouts, and rate-limit responses. It is the only
piece that talks to multiple providers; the public client always
returns a single result.

Mocking
-------
Tests can build a client with ``provider="mock"`` to get canned
responses. This is the recommended way to run the full agent
integration suite without burning real tokens.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field, replace
from typing import Any, AsyncIterator, Iterable, Protocol, runtime_checkable

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Result / request shapes
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class CompletionRequest:
    """A single completion request.

    Mirrors the minimum surface area every provider needs. The agent
    builds one of these per call, then hands it to the client.
    """

    messages: list[dict[str, str]]
    """OpenAI-style chat messages. Each item has ``role`` and ``content``."""

    model: str
    """Specific model id (e.g. ``"gpt-4o-mini"``)."""

    temperature: float = 0.2
    max_tokens: int | None = None
    stop: list[str] | None = None
    response_format_json: bool = False
    """If True, the provider is asked to return valid JSON (best-effort)."""

    extra: dict[str, Any] = field(default_factory=dict)
    """Provider-specific knobs that did not fit the common surface."""


@dataclass(slots=True)
class CompletionResult:
    """Normalised completion output."""

    text: str
    model: str
    provider: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_ms: float = 0.0
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class StreamChunk:
    """A single streaming chunk."""

    text: str
    is_final: bool = False


# ---------------------------------------------------------------------------
# Provider protocol
# ---------------------------------------------------------------------------

@runtime_checkable
class _ProviderProtocol(Protocol):
    """Internal contract every provider implementation must satisfy."""

    name: str

    async def complete(self, request: CompletionRequest) -> CompletionResult: ...

    async def stream(self, request: CompletionRequest) -> AsyncIterator[StreamChunk]: ...


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class LLMError(RuntimeError):
    """Base class for provider errors raised by :class:`LLMClient`."""


class ProviderNotFound(LLMError):
    """Raised when an unknown provider name is requested."""


class AllModelsFailed(LLMError):
    """Raised by :class:`ModelRouter` when every fallback model failed."""

    def __init__(self, message: str, errors: list[Exception]) -> None:
        super().__init__(message)
        self.errors = list(errors)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _env(name: str, default: str | None = None) -> str | None:
    """Read an environment variable, returning ``default`` if absent."""
    return os.environ.get(name, default)


def _coerce_temperature(value: float | int | None) -> float:
    """Clamp temperature to a sane range. ``None`` becomes 0.2."""
    if value is None:
        return 0.2
    try:
        v = float(value)
    except (TypeError, ValueError):
        return 0.2
    return max(0.0, min(2.0, v))


# ---------------------------------------------------------------------------
# OpenAI-compatible provider (OpenAI, OpenRouter, Together, etc.)
# ---------------------------------------------------------------------------

class _OpenAICompatibleProvider:
    """Generic OpenAI-style ``/chat/completions`` provider.

    Works against any service that implements the OpenAI HTTP contract:
    OpenAI, OpenRouter, Together AI, Groq, etc. Provider-specific
    behaviour is reduced to (a) the base URL and (b) the auth header.
    """

    name: str = "openai"

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str = "https://api.openai.com/v1",
        provider_label: str = "openai",
    ) -> None:
        self._api_key = api_key or _env("OPENAI_API_KEY")
        self._base_url = base_url.rstrip("/")
        self.name = provider_label
        # Lazy-imported to keep the module importable in environments
        # where ``httpx`` is missing (it ships in requirements.txt but
        # we want a clear error if someone forgot to install it).
        try:
            import httpx  # noqa: F401
        except ImportError as exc:  # pragma: no cover
            raise LLMError(
                "httpx is required for the OpenAI-compatible provider; "
                "install it via 'pip install httpx'."
            ) from exc

    def _headers(self) -> dict[str, str]:
        if not self._api_key:
            raise LLMError(
                f"provider {self.name!r} requires an API key "
                f"(set OPENAI_API_KEY or pass api_key=...)"
            )
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

    def _payload(self, request: CompletionRequest) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": request.model,
            "messages": list(request.messages),
            "temperature": _coerce_temperature(request.temperature),
        }
        if request.max_tokens is not None:
            payload["max_tokens"] = int(request.max_tokens)
        if request.stop:
            payload["stop"] = list(request.stop)
        if request.response_format_json:
            payload["response_format"] = {"type": "json_object"}
        payload.update(request.extra)
        return payload

    async def complete(self, request: CompletionRequest) -> CompletionResult:
        import httpx

        url = f"{self._base_url}/chat/completions"
        body = self._payload(request)
        started = time.perf_counter()
        try:
            async with httpx.AsyncClient(timeout=120.0) as client:
                resp = await client.post(url, headers=self._headers(), json=body)
        except httpx.HTTPError as exc:
            raise LLMError(f"{self.name} transport error: {exc}") from exc
        latency_ms = (time.perf_counter() - started) * 1000.0
        if resp.status_code >= 400:
            raise LLMError(
                f"{self.name} HTTP {resp.status_code}: {resp.text[:500]}"
            )
        data = resp.json()
        try:
            text = data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError(
                f"{self.name} returned an unexpected payload: {exc}"
            ) from exc
        usage = data.get("usage", {}) or {}
        return CompletionResult(
            text=str(text),
            model=str(data.get("model", request.model)),
            provider=self.name,
            prompt_tokens=int(usage.get("prompt_tokens", 0) or 0),
            completion_tokens=int(usage.get("completion_tokens", 0) or 0),
            latency_ms=latency_ms,
            raw=data,
        )

    async def stream(self, request: CompletionRequest) -> AsyncIterator[StreamChunk]:
        import httpx

        url = f"{self._base_url}/chat/completions"
        body = self._payload(request)
        body["stream"] = True
        started = time.perf_counter()
        try:
            async with httpx.AsyncClient(timeout=120.0) as client:
                async with client.stream(
                    "POST", url, headers=self._headers(), json=body
                ) as resp:
                    if resp.status_code >= 400:
                        text = await resp.aread()
                        raise LLMError(
                            f"{self.name} HTTP {resp.status_code}: "
                            f"{text.decode('utf-8', errors='replace')[:500]}"
                        )
                    async for line in resp.aiter_lines():
                        if not line or not line.startswith("data:"):
                            continue
                        payload = line[len("data:"):].strip()
                        if payload == "[DONE]":
                            yield StreamChunk(text="", is_final=True)
                            return
                        try:
                            evt = json.loads(payload)
                        except json.JSONDecodeError:
                            continue
                        try:
                            delta = (
                                evt["choices"][0]["delta"].get("content") or ""
                            )
                        except (KeyError, IndexError, TypeError):
                            delta = ""
                        if delta:
                            yield StreamChunk(text=str(delta))
        except httpx.HTTPError as exc:
            raise LLMError(f"{self.name} transport error: {exc}") from exc
        finally:
            _ = started  # latency tracked at complete() level


# ---------------------------------------------------------------------------
# OpenRouter provider
# ---------------------------------------------------------------------------

class _OpenRouterProvider(_OpenAICompatibleProvider):
    """OpenRouter — OpenAI-compatible API at ``openrouter.ai``."""

    def __init__(self, *, api_key: str | None = None) -> None:
        super().__init__(
            api_key=api_key or _env("OPENROUTER_API_KEY"),
            base_url=_env(
                "OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1"
            ),
            provider_label="openrouter",
        )


# ---------------------------------------------------------------------------
# Anthropic provider
# ---------------------------------------------------------------------------

class _AnthropicProvider:
    """Anthropic Messages API.

    Implements the same interface as the OpenAI provider but converts
    chat messages to Anthropic's ``system``/``messages`` shape.
    """

    name: str = "anthropic"

    def __init__(self, *, api_key: str | None = None) -> None:
        self._api_key = api_key or _env("ANTHROPIC_API_KEY")
        self._base_url = _env("ANTHROPIC_BASE_URL", "https://api.anthropic.com")
        self._version = _env("ANTHROPIC_VERSION", "2023-06-01")

    def _headers(self) -> dict[str, str]:
        if not self._api_key:
            raise LLMError("anthropic provider requires ANTHROPIC_API_KEY")
        return {
            "x-api-key": self._api_key,
            "anthropic-version": self._version,
            "content-type": "application/json",
        }

    def _payload(self, request: CompletionRequest) -> dict[str, Any]:
        system_parts: list[str] = []
        anthropic_messages: list[dict[str, Any]] = []
        for msg in request.messages:
            role = msg.get("role")
            content = msg.get("content", "") or ""
            if role == "system":
                system_parts.append(content)
            elif role in ("user", "assistant"):
                anthropic_messages.append(
                    {"role": role, "content": content}
                )
            else:
                # Treat unknown roles as user content rather than dropping
                # them silently.
                anthropic_messages.append({"role": "user", "content": content})
        if not anthropic_messages:
            anthropic_messages.append({"role": "user", "content": ""})
        payload: dict[str, Any] = {
            "model": request.model,
            "messages": anthropic_messages,
            "max_tokens": int(request.max_tokens or 1024),
            "temperature": _coerce_temperature(request.temperature),
        }
        if system_parts:
            payload["system"] = "\n\n".join(system_parts)
        if request.stop:
            payload["stop_sequences"] = list(request.stop)
        payload.update(request.extra)
        return payload

    async def complete(self, request: CompletionRequest) -> CompletionResult:
        import httpx

        url = f"{self._base_url}/v1/messages"
        body = self._payload(request)
        started = time.perf_counter()
        try:
            async with httpx.AsyncClient(timeout=120.0) as client:
                resp = await client.post(
                    url, headers=self._headers(), json=body
                )
        except httpx.HTTPError as exc:
            raise LLMError(f"anthropic transport error: {exc}") from exc
        latency_ms = (time.perf_counter() - started) * 1000.0
        if resp.status_code >= 400:
            raise LLMError(
                f"anthropic HTTP {resp.status_code}: {resp.text[:500]}"
            )
        data = resp.json()
        # Anthropic returns a list of content blocks; concatenate the text.
        parts: list[str] = []
        for block in data.get("content", []) or []:
            if block.get("type") == "text":
                parts.append(str(block.get("text", "")))
        text = "".join(parts)
        usage = data.get("usage", {}) or {}
        return CompletionResult(
            text=text,
            model=str(data.get("model", request.model)),
            provider=self.name,
            prompt_tokens=int(usage.get("input_tokens", 0) or 0),
            completion_tokens=int(usage.get("output_tokens", 0) or 0),
            latency_ms=latency_ms,
            raw=data,
        )

    async def stream(self, request: CompletionRequest) -> AsyncIterator[StreamChunk]:
        # Anthropic streaming uses Server-Sent Events; implementing a full
        # parser here is out of scope for Phase 2. Fall back to a single
        # completion chunk instead — better than lying about streaming.
        result = await self.complete(request)
        yield StreamChunk(text=result.text, is_final=True)


# ---------------------------------------------------------------------------
# Mock provider
# ---------------------------------------------------------------------------

class _MockProvider:
    """Canned-response provider used in tests and offline runs.

    The mock returns either:

    * a fixed string (``text=``) for every call, or
    * a function of the request that returns a string, or
    * a JSON object that is auto-serialised when the request asks for
      ``response_format_json=True``.

    Tests pass a callable to drive branchy behaviour (e.g. different
    responses per call index).
    """

    name: str = "mock"

    def __init__(
        self,
        *,
        text: str | Iterable[str] | callable = "ok",
    ) -> None:
        self._text = text
        self.calls: list[CompletionRequest] = []

    def _resolve(self, request: CompletionRequest) -> str:
        idx = len(self.calls)
        if callable(self._text):
            value = self._text(idx, request)
        elif isinstance(self._text, str):
            value = self._text
        elif isinstance(self._text, dict):
            value = self._text
        else:
            iterator = iter(self._text)
            try:
                value = next(iterator)
            except StopIteration:
                value = ""
        # JSON mode: serialise non-string values as JSON.
        if request.response_format_json and not isinstance(value, str):
            return json.dumps(value)
        return str(value)

    async def complete(self, request: CompletionRequest) -> CompletionResult:
        # Note: append the request to ``calls`` AFTER resolving the
        # text so that callable mocks indexed by ``len(self.calls)``
        # see the call they are about to produce, not the previous one.
        text = self._resolve(request)
        self.calls.append(request)
        return CompletionResult(
            text=text,
            model=request.model,
            provider=self.name,
            prompt_tokens=max(1, len(request.messages) * 8),
            completion_tokens=max(1, len(text) // 4),
            latency_ms=0.5,
        )

    async def stream(self, request: CompletionRequest) -> AsyncIterator[StreamChunk]:
        text = self._resolve(request)
        self.calls.append(request)
        yield StreamChunk(text=text, is_final=True)


# ---------------------------------------------------------------------------
# Provider registry
# ---------------------------------------------------------------------------

_PROVIDER_FACTORIES: dict[str, callable] = {
    "openai": lambda: _OpenAICompatibleProvider(
        api_key=_env("OPENAI_API_KEY"),
        base_url=_env("OPENAI_BASE_URL", "https://api.openai.com/v1"),
        provider_label="openai",
    ),
    "openrouter": lambda: _OpenRouterProvider(),
    "anthropic": lambda: _AnthropicProvider(),
    "mock": lambda: _MockProvider(),
}


def register_provider(name: str, factory: callable) -> None:
    """Register a custom provider factory (used by tests and plugins)."""
    _PROVIDER_FACTORIES[name] = factory


# ---------------------------------------------------------------------------
# Model router (fallback chain)
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class ModelRoute:
    """One hop in a fallback chain."""

    provider: str
    model: str


class ModelRouter:
    """Try an ordered list of ``(provider, model)`` pairs in sequence.

    A failure of any kind on hop *n* triggers hop *n+1*. The router
    exposes a :meth:`complete` that returns the first successful
    result, plus :meth:`stream` for streaming callers.

    The router does **not** retry the same hop on failure — the
    provider client is expected to handle its own rate-limit backoff
    if it wants. The router's job is only cross-provider failover.
    """

    def __init__(
        self,
        routes: list[ModelRoute],
        *,
        providers: dict[str, _ProviderProtocol] | None = None,
    ) -> None:
        if not routes:
            raise ValueError("ModelRouter requires at least one route")
        self._routes = list(routes)
        # If the caller supplied pre-built provider instances, use them;
        # otherwise construct defaults from the registry.
        self._providers: dict[str, _ProviderProtocol] = (
            providers
            if providers is not None
            else self._default_providers([r.provider for r in routes])
        )

    @staticmethod
    def _default_providers(names: Iterable[str]) -> dict[str, _ProviderProtocol]:
        providers: dict[str, _ProviderProtocol] = {}
        for name in set(names):
            factory = _PROVIDER_FACTORIES.get(name)
            if factory is None:
                raise ProviderNotFound(f"unknown provider: {name!r}")
            providers[name] = factory()
        return providers

    @property
    def routes(self) -> list[ModelRoute]:
        """The fallback chain in order. Read-only view."""
        return list(self._routes)

    async def complete(self, request: CompletionRequest) -> CompletionResult:
        """Try each route in order. Raise :class:`AllModelsFailed` if all fail."""
        errors: list[Exception] = []
        for idx, route in enumerate(self._routes):
            provider = self._providers.get(route.provider)
            if provider is None:
                errors.append(ProviderNotFound(f"missing provider {route.provider!r}"))
                continue
            attempt = replace(request, model=route.model)
            try:
                return await provider.complete(attempt)
            except LLMError as exc:
                errors.append(exc)
                logger.warning(
                    "model route %d failed provider=%s model=%s: %s",
                    idx, route.provider, route.model, exc,
                )
        raise AllModelsFailed(
            f"all {len(self._routes)} model routes failed", errors
        )

    async def stream(
        self, request: CompletionRequest
    ) -> AsyncIterator[StreamChunk]:
        """Stream from the first route that succeeds the initial handshake."""
        for idx, route in enumerate(self._routes):
            provider = self._providers.get(route.provider)
            if provider is None:
                continue
            attempt = replace(request, model=route.model)
            try:
                async for chunk in provider.stream(attempt):
                    yield chunk
                return
            except LLMError as exc:
                logger.warning(
                    "stream route %d failed provider=%s model=%s: %s",
                    idx, route.provider, route.model, exc,
                )
                if idx == len(self._routes) - 1:
                    raise
        # Empty chain — surface as error.
        raise AllModelsFailed("no routes configured", [])


# ---------------------------------------------------------------------------
# Public client
# ---------------------------------------------------------------------------

class LLMClient:
    """The one object agents use to talk to LLMs.

    The client wraps a :class:`ModelRouter` and adds a thin layer of
    convenience helpers used by the agent classes: JSON-mode
    completion, prompt construction, and an opt-in retry loop with
    exponential backoff for transient errors.
    """

    def __init__(
        self,
        router: ModelRouter,
        *,
        max_retries: int = 2,
        retry_backoff_seconds: float = 0.5,
    ) -> None:
        self._router = router
        self._max_retries = max(0, int(max_retries))
        self._retry_backoff = max(0.0, float(retry_backoff_seconds))

    @property
    def router(self) -> ModelRouter:
        """The underlying :class:`ModelRouter` (read-only access)."""
        return self._router

    @classmethod
    def from_config(
        cls,
        config: dict[str, Any] | None = None,
        *,
        providers: dict[str, _ProviderProtocol] | None = None,
    ) -> "LLMClient":
        """Build a client from a small config dict.

        ``config`` shape::

            {
                "routes": [
                    {"provider": "openrouter", "model": "anthropic/claude-3.5-sonnet"},
                    {"provider": "openai",     "model": "gpt-4o-mini"},
                ],
                "max_retries": 2,
                "retry_backoff_seconds": 0.5,
            }

        If ``config`` is ``None`` or routes is empty, a single ``mock``
        route is used — convenient for tests.
        """
        config = config or {}
        raw_routes = config.get("routes") or []
        if not raw_routes:
            raw_routes = [{"provider": "mock", "model": "mock-model"}]
        routes = [
            ModelRoute(provider=r["provider"], model=r["model"])
            for r in raw_routes
        ]
        if providers is not None:
            router = ModelRouter(routes, providers=providers)
        else:
            router = ModelRouter(routes)
        return cls(
            router,
            max_retries=int(config.get("max_retries", 2)),
            retry_backoff_seconds=float(
                config.get("retry_backoff_seconds", 0.5)
            ),
        )

    # -- core API ----------------------------------------------------------

    async def complete(self, request: CompletionRequest) -> CompletionResult:
        """Complete a request with retry/backoff across the chain."""
        attempt = 0
        last_error: Exception | None = None
        while attempt <= self._max_retries:
            try:
                return await self._router.complete(request)
            except AllModelsFailed as exc:
                last_error = exc
            attempt += 1
            if attempt <= self._max_retries and self._retry_backoff > 0:
                await asyncio.sleep(self._retry_backoff * (2 ** (attempt - 1)))
        assert last_error is not None  # always set if we exit the loop
        raise last_error

    async def stream(self, request: CompletionRequest) -> AsyncIterator[StreamChunk]:
        """Stream chunks from the first working route."""
        async for chunk in self._router.stream(request):
            yield chunk

    # -- convenience -------------------------------------------------------

    async def complete_text(
        self,
        *,
        system: str,
        user: str,
        model: str | None = None,
        temperature: float = 0.2,
        max_tokens: int | None = None,
    ) -> CompletionResult:
        """Single-turn completion: ``system`` + ``user`` → text."""
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        primary = model or self._router.routes[0].model
        return await self.complete(
            CompletionRequest(
                messages=messages,
                model=primary,
                temperature=temperature,
                max_tokens=max_tokens,
            )
        )

    async def complete_json(
        self,
        *,
        system: str,
        user: str,
        model: str | None = None,
        temperature: float = 0.0,
        max_tokens: int | None = None,
    ) -> dict[str, Any]:
        """Request a JSON object and parse it.

        The provider is asked to return JSON. Some providers honour
        that natively (OpenAI JSON mode, OpenRouter, Anthropic with
        guidance), but we never trust them blindly — if the result
        is not valid JSON we raise :class:`LLMError`.
        """
        primary = model or self._router.routes[0].model
        result = await self.complete(
            CompletionRequest(
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                model=primary,
                temperature=temperature,
                max_tokens=max_tokens,
                response_format_json=True,
            )
        )
        text = result.text.strip()
        # Strip code fences if a stubborn model adds them.
        if text.startswith("```"):
            text = text.strip("`")
            if text.startswith("json"):
                text = text[4:]
            text = text.strip()
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise LLMError(
                f"model did not return valid JSON: {exc}; raw={text[:200]!r}"
            ) from exc


__all__ = [
    "AllModelsFailed",
    "CompletionRequest",
    "CompletionResult",
    "LLMClient",
    "LLMError",
    "ModelRoute",
    "ModelRouter",
    "ProviderNotFound",
    "StreamChunk",
    "register_provider",
]
