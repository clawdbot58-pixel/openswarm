"""Async HTTP client for the harness server.

The :class:`ToolExecutor` in :mod:`loops.tool_executor` uses a
:class:`HarnessClient` to forward harness tool calls.  The client is a
thin wrapper around :mod:`httpx`; the URL points at the harness
FastAPI server (the default ``http://localhost:8770``).

For local development and tests, a :class:`HarnessClient` can also be
created with ``InProcessHarnessClient`` which calls a server instance
directly.  The two clients expose the same surface.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping, Optional

import httpx

logger = logging.getLogger(__name__)


DEFAULT_BASE_URL: str = os.environ.get(
    "OPENSWARM_HARNESS_URL", "http://localhost:8770"
)
DEFAULT_TIMEOUT: float = 60.0


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class HarnessError(RuntimeError):
    """Base error for harness client failures."""

    def __init__(self, message: str, *, status_code: int | None = None,
                 detail: Any = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.detail = detail


class HarnessPermissionDenied(HarnessError):
    """The kernel-side permission enforcer rejected the request."""


class HarnessBadRequest(HarnessError):
    """The harness server rejected the call as malformed."""


# ---------------------------------------------------------------------------
# HarnessClient
# ---------------------------------------------------------------------------

class HarnessClient:
    """Async HTTP client targeting the harness server.

    The client is intentionally narrow: one method per public harness
    tool.  It does not invent parameters the server does not accept.
    """

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        """Initialize the client.

        Args:
            base_url: Root URL of the harness server.
            timeout: Default per-request timeout in seconds.
            client: Pre-built :class:`httpx.AsyncClient`.  When ``None``
                the client creates its own.
        """
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._client = client
        self._owns_client = client is None

    # -- lifecycle --------------------------------------------------------

    async def aclose(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    async def __aenter__(self) -> "HarnessClient":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                timeout=self.timeout,
            )
        return self._client

    # -- public API -------------------------------------------------------

    async def health(self) -> dict[str, Any]:
        """Return the harness's liveness payload."""
        return await self._request("GET", "/health")

    async def exec(
        self,
        workflow_id: str,
        runtime: str,
        code: str,
        *,
        timeout: int | None = None,
        memory: str | None = None,
        cpu: float | None = None,
        env_vars: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        """Invoke ``harness_exec`` over the wire."""
        params: dict[str, Any] = {
            "workflow_id": workflow_id,
            "runtime": runtime,
            "code": code,
        }
        if timeout is not None:
            params["timeout"] = timeout
        if memory is not None:
            params["memory"] = memory
        if cpu is not None:
            params["cpu"] = cpu
        if env_vars:
            params["env_vars"] = dict(env_vars)
        return await self._request("POST", "/tools/exec", json=params)

    async def write_file(
        self,
        workflow_id: str,
        path: str,
        content: str,
        *,
        agent_id: str,
    ) -> dict[str, Any]:
        """Invoke ``harness_write_file`` over the wire."""
        return await self._request(
            "POST",
            "/tools/write",
            json={
                "workflow_id": workflow_id,
                "path": path,
                "content": content,
                "agent_id": agent_id,
            },
        )

    async def read_file(
        self,
        workflow_id: str,
        path: str,
    ) -> dict[str, Any]:
        """Invoke ``harness_read_file`` over the wire."""
        return await self._request(
            "GET",
            "/tools/read",
            params={"workflow_id": workflow_id, "path": path},
        )

    async def list_files(
        self,
        workflow_id: str,
        path: str = ".",
    ) -> dict[str, Any]:
        """Invoke ``harness_list_files`` over the wire."""
        return await self._request(
            "GET",
            "/tools/list",
            params={"workflow_id": workflow_id, "path": path},
        )

    async def reset(
        self,
        workflow_id: str,
        commit_hash: str,
        *,
        agent_id: str,
    ) -> dict[str, Any]:
        """Invoke ``harness_reset`` over the wire."""
        return await self._request(
            "POST",
            "/tools/reset",
            json={
                "workflow_id": workflow_id,
                "commit_hash": commit_hash,
                "agent_id": agent_id,
            },
        )

    async def get_history(self, workflow_id: str) -> dict[str, Any]:
        """Return the git history for ``workflow_id``."""
        return await self._request(
            "GET",
            "/tools/history",
            params={"workflow_id": workflow_id},
        )

    async def get_diff(
        self,
        workflow_id: str,
        commit_hash: str,
    ) -> dict[str, Any]:
        """Return the diff for ``commit_hash``."""
        return await self._request(
            "GET",
            "/tools/diff",
            params={"workflow_id": workflow_id, "commit": commit_hash},
        )

    # -- internals --------------------------------------------------------

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        try:
            response = await self.client.request(
                method,
                path,
                params=params,
                json=json,
                timeout=timeout or self.timeout,
            )
        except httpx.HTTPError as exc:
            raise HarnessError(f"harness transport error: {exc}") from exc

        if response.status_code == 200:
            try:
                return response.json()
            except ValueError as exc:
                # httpx's ``Response.json`` raises ``ValueError`` (the stdlib
                # ``JSONDecodeError``) on malformed bodies.
                raise HarnessError(
                    f"harness returned invalid JSON: {exc}",
                    status_code=response.status_code,
                ) from exc
        if response.status_code == 400:
            raise HarnessBadRequest(
                f"harness rejected request: {response.text}",
                status_code=response.status_code,
                detail=self._safe_json(response),
            )
        if response.status_code == 403:
            raise HarnessPermissionDenied(
                f"harness permission denied: {response.text}",
                status_code=response.status_code,
                detail=self._safe_json(response),
            )
        raise HarnessError(
            f"harness returned {response.status_code}: {response.text}",
            status_code=response.status_code,
            detail=self._safe_json(response),
        )

    @staticmethod
    def _safe_json(response: httpx.Response) -> Any:
        try:
            return response.json()
        except json.JSONDecodeError:
            return response.text


# ---------------------------------------------------------------------------
# In-process client (used by tests and the agent worker)
# ---------------------------------------------------------------------------

class InProcessHarnessClient(HarnessClient):
    """Client that dispatches to a server object directly.

    The ``server`` argument is any object exposing the same async
    methods as :class:`~harness.server.HarnessServer`.  This is what
    the agent worker uses when it runs alongside the harness (avoids
    the HTTP round-trip in dev).
    """

    def __init__(self, server: Any) -> None:
        super().__init__(base_url="in-process://harness")
        self._server = server
        # Mark as a synthetic client; never tries to use httpx.
        self._client = None  # type: ignore[assignment]
        self._owns_client = False

    async def aclose(self) -> None:  # noqa: D401
        return None

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        from .server import HarnessServer  # local import for type checks

        if not isinstance(self._server, HarnessServer):
            raise HarnessError(
                f"in-process client requires a HarnessServer instance, "
                f"got {type(self._server).__name__}"
            )
        handler = self._route(path, method)
        if handler is None:
            raise HarnessError(f"no in-process route for {method} {path}")
        return await handler(json or params or {})

    def _route(self, path: str, method: str) -> Optional[Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]]:
        server = self._server
        if method == "GET" and path == "/health":
            return lambda _payload: server.health()
        if method == "POST" and path == "/tools/exec":
            return lambda payload: server.handle_tool_exec(payload)
        if method == "POST" and path == "/tools/write":
            return lambda payload: server.handle_tool_write(payload)
        if method == "GET" and path == "/tools/read":
            return lambda payload: server.handle_tool_read(payload)
        if method == "GET" and path == "/tools/list":
            return lambda payload: server.handle_tool_list(payload)
        if method == "POST" and path == "/tools/reset":
            return lambda payload: server.handle_tool_reset(payload)
        if method == "GET" and path == "/tools/history":
            return lambda payload: server.handle_tool_history(payload)
        if method == "GET" and path == "/tools/diff":
            return lambda payload: server.handle_tool_diff(payload)
        return None


__all__ = [
    "DEFAULT_BASE_URL",
    "DEFAULT_TIMEOUT",
    "HarnessBadRequest",
    "HarnessClient",
    "HarnessError",
    "HarnessPermissionDenied",
    "InProcessHarnessClient",
]
