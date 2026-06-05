"""Tool executor — Phase 5 wiring.

Replaces the Phase 3 stub with a real executor that:

* validates the tool is declared in the manifest;
* checks the per-task permission block (``can_execute``,
  ``can_read``, ``can_write``);
* dispatches :class:`HarnessClient`-backed tool calls to the harness
  server when the tool is a harness tool (the ``harness_*`` family).

The class keeps the same public surface as the Phase 3 stub —
:func:`execute` and :func:`list_tools` — so existing agent code that
imports it does not need to change.
"""
from __future__ import annotations

import fnmatch
import logging
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

# Make the harness package importable from inside src/loops/.
_SRC = Path(__file__).resolve().parent.parent
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from harness.client import HarnessClient, HarnessError, InProcessHarnessClient  # noqa: E402
from harness.server import HarnessServer  # noqa: E402

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------

@dataclass
class ToolResult:
    """Result of a tool execution."""

    status: str
    message: str
    output: Any = None
    error: str | None = None


# ---------------------------------------------------------------------------
# Tool taxonomy
# ---------------------------------------------------------------------------

# Tools whose implementation lives in the harness.  Anything not in
# this set is treated as a "local" tool — we still validate the call
# and return a sensible default, but the agent is expected to use a
# harness tool for any code execution or file mutation.
HARNESS_TOOLS: frozenset[str] = frozenset(
    {
        "harness_exec",
        "harness_write_file",
        "harness_read_file",
        "harness_list_files",
        "harness_reset",
        "harness_get_history",
        "harness_get_diff",
    }
)

# Required parameters for each harness tool.  Mirrors the request
# models in :mod:`harness.server` so the executor can reject malformed
# calls before they hit the network.
HARNESS_TOOL_REQUIRED: dict[str, tuple[str, ...]] = {
    "harness_exec": ("workflow_id", "runtime", "code"),
    "harness_write_file": ("workflow_id", "path", "content"),
    "harness_read_file": ("workflow_id", "path"),
    "harness_list_files": ("workflow_id",),
    "harness_reset": ("workflow_id", "commit_hash"),
    "harness_get_history": ("workflow_id",),
    "harness_get_diff": ("workflow_id", "commit"),
}


# ---------------------------------------------------------------------------
# ToolExecutor
# ---------------------------------------------------------------------------

class ToolExecutor:
    """Executes tools with permission validation and harness dispatch.

    The executor is the **last** gate before a tool call leaves the
    agent.  It performs:

    1. Manifest capability check — the tool must appear in
       ``manifest.capabilities.tools``.
    2. Required-parameter check.
    3. Per-task permission check using the ``permissions`` block
       (filesystem globs, harness runtime allowlist, …).
    4. Dispatch to either a harness tool (via :class:`HarnessClient`)
       or a local stub.
    """

    def __init__(
        self,
        manifest: dict[str, Any] | HarnessClient,
        harness_client: HarnessClient | None = None,
    ) -> None:
        """Initialize the executor.

        Args:
            manifest: Either the agent manifest dict (legacy
                ``ToolExecutor(manifest)`` signature) **or** a
                :class:`HarnessClient` whose ``manifest`` attribute
                is the manifest dict.  The two-argument form is
                preferred.
            harness_client: The client used to dispatch harness
                tools.  When ``None`` and ``manifest`` is a
                :class:`HarnessClient`, the latter is reused.
        """
        if isinstance(manifest, HarnessClient):
            harness_client = manifest
            manifest_dict = getattr(harness_client, "manifest", {}) or {}
        else:
            manifest_dict = manifest or {}
        self.manifest: dict[str, Any] = manifest_dict
        self.available_tools: dict[str, Any] = self._parse_tools(manifest_dict)
        self.harness = harness_client

    # -- public API -------------------------------------------------------

    async def execute(
        self,
        tool_name: str,
        params: dict[str, Any],
        permissions: dict[str, Any],
    ) -> ToolResult:
        """Execute ``tool_name`` with ``params`` and ``permissions``.

        Args:
            tool_name: Name of the tool to invoke.
            params: Parameters for the tool.
            permissions: Per-task permissions.  May include
                ``file_system.allow``/``deny``,
                ``harness.allowed_runtimes``, ``network.allow``.

        Returns:
            A :class:`ToolResult` describing the outcome.
        """
        # 1. Validate tool is declared in manifest capabilities.
        if tool_name not in self.available_tools:
            return ToolResult(
                status="error",
                message=f"Tool '{tool_name}' not in manifest capabilities",
                error="tool_not_found",
            )

        # 2. Validate parameter shape.
        shape_error = self._validate_params(tool_name, params)
        if shape_error is not None:
            return shape_error

        # 3. Check permissions.
        perm_error = self._check_permissions(tool_name, params, permissions)
        if perm_error is not None:
            return perm_error

        # 4. Dispatch.
        if tool_name in HARNESS_TOOLS:
            return await self._dispatch_harness(tool_name, params)

        return ToolResult(
            status="ok",
            message=f"tool {tool_name!r} accepted (no local handler)",
            output={"params": params},
        )

    def list_tools(self) -> list[str]:
        """Return the names of every tool the manifest declares."""
        return list(self.available_tools.keys())

    # -- internals --------------------------------------------------------

    @staticmethod
    def _parse_tools(manifest: dict[str, Any]) -> dict[str, Any]:
        tools: dict[str, Any] = {}
        capabilities = manifest.get("capabilities", {}) if manifest else {}
        for tool in capabilities.get("tools", []) or []:
            name = tool.get("name")
            if name:
                tools[name] = tool
        return tools

    def _validate_params(
        self,
        tool_name: str,
        params: dict[str, Any],
    ) -> ToolResult | None:
        """Reject unknown keys and missing required values."""
        tool_def = self.available_tools.get(tool_name, {})
        declared = tool_def.get("parameters", {}) or {}

        # Reject unknown parameters.
        for key in params:
            if key not in declared:
                return ToolResult(
                    status="error",
                    message=(
                        f"Invalid parameter '{key}' for tool '{tool_name}'"
                    ),
                    error="invalid_params",
                )

        # Required-value check on top of the harness schema.
        if tool_name in HARNESS_TOOL_REQUIRED:
            for key in HARNESS_TOOL_REQUIRED[tool_name]:
                if key not in params or params[key] in (None, ""):
                    return ToolResult(
                        status="error",
                        message=(
                            f"Tool '{tool_name}' requires parameter "
                            f"'{key}' (required)"
                        ),
                        error="missing_required",
                    )

        # Required-value check against the manifest schema (parameters
        # flagged with ``required: True``).
        for key, spec in declared.items():
            if isinstance(spec, dict) and spec.get("required") and (
                key not in params or params[key] in (None, "")
            ):
                return ToolResult(
                    status="error",
                    message=(
                        f"Tool '{tool_name}' requires parameter "
                        f"'{key}' (required)"
                    ),
                    error="missing_required",
                )
        return None

    def _check_permissions(
        self,
        tool_name: str,
        params: dict[str, Any],
        permissions: dict[str, Any],
    ) -> ToolResult | None:
        """Apply the per-task permission policy.

        Two rule families are enforced:

        * Filesystem globs from ``permissions.file_system``.
        * Harness runtime allowlist from ``permissions.harness``.
        """
        fs_perm = permissions.get("file_system", {}) or {}
        network_perm = permissions.get("network", {}) or {}
        harness_perm = permissions.get("harness", {}) or {}

        # Filesystem tools.
        if tool_name in {"file_read", "file_write", "file_delete"} or tool_name in HARNESS_TOOLS and self._tool_touches_filesystem(tool_name):
            read_only = bool(fs_perm.get("read_only", False))
            if read_only and tool_name in {"file_write", "file_delete"}:
                return ToolResult(
                    status="error",
                    message="File system is read-only",
                    error="permission_denied",
                )
            paths = self._collect_paths(tool_name, params)
            for path in paths:
                if not self._path_allowed(path, fs_perm.get("allow", []) or []):
                    return ToolResult(
                        status="error",
                        message=f"Path '{path}' not in allowed patterns",
                        error="permission_denied",
                    )

        # Network tools.
        if tool_name in {"web_fetch", "web_search"}:
            hosts = params.get("hosts", [])
            for host in hosts:
                if not self._host_allowed(
                    host, network_perm.get("allow", []) or []
                ):
                    return ToolResult(
                        status="error",
                        message=f"Host '{host}' not in allowed patterns",
                        error="permission_denied",
                    )

        # Harness tool runtime allowlist.
        if tool_name == "harness_exec":
            runtime = params.get("runtime")
            allowed_runtimes = harness_perm.get("allowed_runtimes", []) or []
            if allowed_runtimes and runtime not in allowed_runtimes:
                return ToolResult(
                    status="error",
                    message=(
                        f"Runtime '{runtime}' not in allowed_runtimes "
                        f"{allowed_runtimes}"
                    ),
                    error="permission_denied",
                )
            if harness_perm.get("can_execute_code") is False:
                return ToolResult(
                    status="error",
                    message="Manifest denies code execution",
                    error="permission_denied",
                )

        # Workspace access gate.
        if tool_name in HARNESS_TOOLS and tool_name != "harness_exec":
            if harness_perm.get("can_access_workspace") is False:
                return ToolResult(
                    status="error",
                    message="Manifest denies workspace access",
                    error="permission_denied",
                )

        return None

    @staticmethod
    def _tool_touches_filesystem(tool_name: str) -> bool:
        return tool_name in {
            "harness_write_file",
            "harness_read_file",
            "harness_list_files",
        }

    @staticmethod
    def _collect_paths(tool_name: str, params: dict[str, Any]) -> Iterable[str]:
        candidates: list[str] = []
        for key in ("path", "file_path", "filepath", "src", "dst"):
            if key in params and isinstance(params[key], str):
                candidates.append(params[key])
        if "paths" in params and isinstance(params["paths"], list):
            candidates.extend(str(p) for p in params["paths"] if isinstance(p, str))
        return candidates

    @staticmethod
    def _path_allowed(path: str, allow_patterns: list[str]) -> bool:
        if not allow_patterns:
            return False
        for pattern in allow_patterns:
            if fnmatch.fnmatch(path, pattern):
                return True
        return False

    @staticmethod
    def _host_allowed(host: str, allow_patterns: list[str]) -> bool:
        if not allow_patterns:
            return False
        for pattern in allow_patterns:
            if fnmatch.fnmatch(host, pattern):
                return True
        return False

    # -- harness dispatch -------------------------------------------------

    async def _dispatch_harness(
        self,
        tool_name: str,
        params: dict[str, Any],
    ) -> ToolResult:
        if self.harness is None:
            return ToolResult(
                status="error",
                message="Harness client not configured",
                error="harness_unavailable",
            )
        try:
            response = await self._call_harness(tool_name, params)
        except HarnessError as exc:
            return ToolResult(
                status="error",
                message=str(exc),
                error=exc.__class__.__name__,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("harness tool dispatch failed")
            return ToolResult(
                status="error",
                message=f"harness call failed: {exc}",
                error=exc.__class__.__name__,
            )
        return ToolResult(
            status="ok" if response.get("ok") else "error",
            message=f"harness:{tool_name}",
            output=response,
            error=None if response.get("ok") else "harness_rejected",
        )

    async def _call_harness(
        self,
        tool_name: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """Route to the right :class:`HarnessClient` method."""
        assert self.harness is not None  # checked by caller
        client = self.harness
        if tool_name == "harness_exec":
            return await client.exec(
                workflow_id=params["workflow_id"],
                runtime=params["runtime"],
                code=params["code"],
                timeout=params.get("timeout"),
                memory=params.get("memory"),
                cpu=params.get("cpu"),
                env_vars=params.get("env_vars") or {},
            )
        if tool_name == "harness_write_file":
            return await client.write_file(
                workflow_id=params["workflow_id"],
                path=params["path"],
                content=params["content"],
                agent_id=params.get("agent_id", "harness"),
            )
        if tool_name == "harness_read_file":
            return await client.read_file(
                workflow_id=params["workflow_id"],
                path=params["path"],
            )
        if tool_name == "harness_list_files":
            return await client.list_files(
                workflow_id=params["workflow_id"],
                path=params.get("path", "."),
            )
        if tool_name == "harness_reset":
            return await client.reset(
                workflow_id=params["workflow_id"],
                commit_hash=params["commit_hash"],
                agent_id=params.get("agent_id", "harness"),
            )
        if tool_name == "harness_get_history":
            return await client.get_history(workflow_id=params["workflow_id"])
        if tool_name == "harness_get_diff":
            return await client.get_diff(
                workflow_id=params["workflow_id"],
                commit_hash=params["commit"],
            )
        raise HarnessError(f"unhandled harness tool: {tool_name}")


# ---------------------------------------------------------------------------
# Convenience constructors
# ---------------------------------------------------------------------------

def make_tool_executor(
    manifest: dict[str, Any],
    *,
    harness_server: HarnessServer | None = None,
    harness_url: str | None = None,
) -> ToolExecutor:
    """Build a :class:`ToolExecutor` wired up with the right client.

    Resolution order:

    1. ``harness_server`` argument → :class:`InProcessHarnessClient`.
    2. ``harness_url`` argument or ``OPENSWARM_HARNESS_URL`` env var
       → :class:`HarnessClient` over HTTP.
    3. ``None`` → executor created without a client (will reject
       ``harness_*`` tools at call time).
    """
    if harness_server is not None:
        client: HarnessClient = InProcessHarnessClient(harness_server)
    elif harness_url is not None or os.environ.get("OPENSWARM_HARNESS_URL"):
        client = HarnessClient(
            base_url=harness_url or os.environ["OPENSWARM_HARNESS_URL"]
        )
    else:
        client = HarnessClient.__new__(HarnessClient)  # type: ignore[call-arg]
        client.base_url = ""
        client.timeout = 0.0
        client._client = None  # type: ignore[attr-defined]
        client._owns_client = False
        # Mark the client as unconfigured; _dispatch_harness will catch it.
    return ToolExecutor(manifest, harness_client=client)


__all__ = [
    "HARNESS_TOOLS",
    "HARNESS_TOOL_REQUIRED",
    "ToolExecutor",
    "ToolResult",
    "make_tool_executor",
]
