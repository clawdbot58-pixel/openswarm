"""Wire LLM providers (Ollama, NVIDIA NIM, OpenAI, mock) from config/env."""
from __future__ import annotations

import os
from typing import Any

from agents.llm_client import (
    LLMClient,
    ModelRoute,
    _OpenAICompatibleProvider,
    register_provider,
)

_PROVIDERS_REGISTERED = False


def ensure_llm_providers() -> None:
    """Register OpenAI-compatible providers used by OpenSwarm."""
    global _PROVIDERS_REGISTERED
    if _PROVIDERS_REGISTERED:
        return

    register_provider(
        "ollama",
        lambda: _OpenAICompatibleProvider(
            api_key=os.environ.get("OLLAMA_API_KEY", "ollama"),
            base_url=os.environ.get("OLLAMA_BASE_URL", "http://127.0.0.1:11434/v1"),
            provider_label="ollama",
        ),
    )
    register_provider(
        "nim",
        lambda: _OpenAICompatibleProvider(
            api_key=os.environ.get("NVIDIA_API_KEY", os.environ.get("NIM_API_KEY", "")),
            base_url=os.environ.get(
                "NIM_BASE_URL",
                "https://integrate.api.nvidia.com/v1",
            ),
            provider_label="nim",
        ),
    )
    _PROVIDERS_REGISTERED = True


def build_llm_client_from_env(profile: str | None = None) -> LLMClient:
    """Build an :class:`LLMClient` from env vars and optional profile name.

    Profiles:
    * ``nim`` — NVIDIA NIM (24/7 cloud inference)
    * ``ollama`` — local Ollama (fast iteration)
    * ``mock`` — no external calls (tests / offline)
    * ``auto`` — nim if ``NVIDIA_API_KEY`` set, else ollama, else mock
    """
    ensure_llm_providers()
    profile = (profile or os.environ.get("OPENSWARM_LLM_PROFILE", "auto")).lower()

    if profile == "auto":
        if os.environ.get("NVIDIA_API_KEY") or os.environ.get("NIM_API_KEY"):
            profile = "nim"
        elif os.environ.get("OLLAMA_BASE_URL") or _ollama_reachable():
            profile = "ollama"
        else:
            profile = "mock"

    routes: list[dict[str, str]]
    if profile == "nim":
        model = os.environ.get("NIM_MODEL", "meta/llama-3.1-8b-instruct")
        routes = [{"provider": "nim", "model": model}]
    elif profile == "ollama":
        model = os.environ.get("OLLAMA_MODEL", "llama3.2")
        routes = [{"provider": "ollama", "model": model}]
    elif profile == "mock":
        routes = [{"provider": "mock", "model": "mock-model"}]
    else:
        routes = [{"provider": profile, "model": os.environ.get("OPENSWARM_LLM_MODEL", "gpt-4o-mini")}]

    return LLMClient.from_config({"routes": routes})


def build_llm_client_from_section(section: Any | None) -> LLMClient:
    """Build a client from :class:`config.LLMSection` if present."""
    if section is None:
        return build_llm_client_from_env()
    profile = getattr(section, "profile", "auto")
    routes_cfg = getattr(section, "routes", None) or []
    if routes_cfg:
        ensure_llm_providers()
        routes = [{"provider": r.provider, "model": r.model} for r in routes_cfg]
        return LLMClient.from_config(
            {
                "routes": routes,
                "max_retries": int(getattr(section, "max_retries", 2)),
            }
        )
    return build_llm_client_from_env(profile)


def _ollama_reachable() -> bool:
    import urllib.error
    import urllib.request

    base = os.environ.get("OLLAMA_BASE_URL", "http://127.0.0.1:11434").rstrip("/")
    try:
        with urllib.request.urlopen(f"{base}/api/tags", timeout=0.5) as resp:  # noqa: S310
            return 200 <= resp.status < 300
    except (urllib.error.URLError, OSError, ValueError):
        return False


__all__ = ["build_llm_client_from_env", "build_llm_client_from_section", "ensure_llm_providers"]
