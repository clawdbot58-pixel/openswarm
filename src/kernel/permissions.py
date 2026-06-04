"""Default-deny permission enforcer.

The enforcer is the kernel's security gate. It runs **before** every tool
envelope is delivered and decides whether the sender is allowed to make
the call. The rules are:

1. **Default deny.** A tool/host/path not explicitly listed in the
   sender's manifest is rejected.
2. **Deny overrides allow.** If a pattern appears in both
   ``permissions.file_system.deny`` and ``permissions.file_system.allow``,
   the deny wins.
3. **Side-effect coverage.** A tool's declared ``side_effects`` must
   correspond to a permission that the agent holds. For example, a tool
   that writes files requires a non-empty ``fs.allow`` and a non-read-only
   filesystem permission.
4. **Audit everything.** Every check (allow or deny) is appended to the
   ``audit_log`` table.

The enforcer is *read-only* with respect to the agent's manifest — it
never mutates the registry.
"""
from __future__ import annotations

import fnmatch
import logging
from datetime import datetime, timezone
from typing import Any, Iterable

from .exceptions import PermissionDenied
from .models import (
    AgentManifest,
    Envelope,
    NetworkPermission,
    ToolDefinition,
    ToolPayload,
)
from .registry import AgentRegistry

logger = logging.getLogger(__name__)


# Mapping of tool "side_effect" tokens to the manifest permission block
# they require to be authorized. This is intentionally narrow — agents
# can declare any tool they want, but if the tool's ``side_effects``
# contains one of these tokens, the corresponding manifest permission
# must be present.
SIDE_EFFECT_PERMISSION: dict[str, str] = {
    "fs:read": "fs.read",
    "fs:write": "fs.write",
    "fs:delete": "fs.write",
    "shell:exec": "process.exec",
    "process:spawn": "process.spawn",
    "process:kill": "process.kill",
    "network:egress": "network.egress",
    "network:listen": "network.listen",
    "harness:execute": "harness.execute",
    "harness:workspace": "harness.workspace",
}


class PermissionEnforcer:
    """Stateless policy engine, driven by the registry at call time."""

    def __init__(self, registry: AgentRegistry) -> None:
        self._registry = registry

    # -- public API --------------------------------------------------------

    async def check(
        self,
        envelope: Envelope,
        registry: AgentRegistry | None = None,
    ) -> bool:
        """Decide whether ``envelope`` may be delivered.

        Returns ``True`` when allowed, ``False`` when denied. Denial side
        effects (audit row + events) are emitted inline so the caller does
        not have to remember to do them.
        """
        reg = registry or self._registry
        if envelope.payload.content_type != "tool":
            # Non-tool payloads are not permission-checked at the kernel
            # layer. The harness (Phase 5) and the agent itself are
            # responsible for finer-grained checks.
            await self._audit(
                reg,
                envelope_id=envelope.envelope_id,
                agent_id=envelope.sender.agent_id,
                action="permission_check",
                result="allow",
                reason="non_tool_payload",
            )
            return True

        tool_payload = envelope.payload  # type: ignore[assignment]
        assert isinstance(tool_payload, ToolPayload)
        sender_id = envelope.sender.agent_id

        try:
            manifest = await reg.get(sender_id)
        except Exception as exc:
            # Sender not registered → default deny.
            await self._deny(
                reg,
                envelope,
                reason=f"sender_not_registered:{exc}",
            )
            return False

        # Rule 1: tool_name must appear in capabilities.
        tool = manifest.get_tool(tool_payload.tool_name)
        if tool is None:
            await self._deny(
                reg,
                envelope,
                reason=(
                    f"tool_not_in_capabilities:{tool_payload.tool_name}"
                ),
            )
            return False

        # Rule 2: side-effects coverage.
        side_effect_check = self._check_side_effects(manifest, tool, tool_payload)
        if side_effect_check is not None:
            await self._deny(reg, envelope, reason=side_effect_check)
            return False

        # Rule 3: filesystem path scoping (best-effort, only when params
        # look like a path).
        path_check = self._check_fs_params(manifest, tool, tool_payload.parameters)
        if path_check is not None:
            await self._deny(reg, envelope, reason=path_check)
            return False

        # Rule 4: network host scoping (best-effort).
        net_check = self._check_network_params(manifest, tool, tool_payload.parameters)
        if net_check is not None:
            await self._deny(reg, envelope, reason=net_check)
            return False

        await self._audit(
            reg,
            envelope_id=envelope.envelope_id,
            agent_id=sender_id,
            action="permission_check",
            result="allow",
            reason=f"tool:{tool_payload.tool_name}",
        )
        return True

    # -- helpers -----------------------------------------------------------

    def _check_side_effects(
        self,
        manifest: AgentManifest,
        tool: ToolDefinition,
        payload: ToolPayload,
    ) -> str | None:
        """Return a denial reason string or ``None`` if side-effects are allowed."""
        if not tool.side_effects:
            return None
        for effect in tool.side_effects:
            required = SIDE_EFFECT_PERMISSION.get(effect)
            if required is None:
                # Unknown side effect token → conservative deny.
                return f"unknown_side_effect:{effect}"
            permission = self._resolve_permission(manifest, required)
            if not permission:
                return f"missing_permission:{required} for {effect}"
        return None

    @staticmethod
    def _resolve_permission(manifest: AgentManifest, name: str) -> bool:
        """Check whether ``manifest`` holds the given high-level permission.

        ``name`` is one of:
            ``fs.read``  — manifest may have a non-empty ``fs.allow`` or
                           ``read_only`` set (read access).
            ``fs.write`` — manifest must have a writable ``fs.allow`` set
                           (no ``read_only``).
            ``process.exec``  — manifest.permissions.process present.
            ``process.spawn`` — manifest.permissions.process.can_spawn.
            ``process.kill``  — manifest.permissions.process.can_kill.
            ``network.egress`` — manifest.permissions.network present.
            ``network.listen`` — manifest.permissions.network present.
            ``harness.execute`` — manifest.permissions.harness.can_execute_code.
            ``harness.workspace``— manifest.permissions.harness.can_access_workspace.
        """
        perms = manifest.permissions
        if perms is None:
            return False
        if name == "fs.read":
            return perms.file_system is not None and bool(perms.file_system.allow)
        if name == "fs.write":
            fs = perms.file_system
            return fs is not None and bool(fs.allow) and not fs.read_only
        if name in {"process.exec", "process.spawn", "process.kill"}:
            return perms.process is not None
        if name in {"network.egress", "network.listen"}:
            return perms.network is not None
        if name == "harness.execute":
            return perms.harness is not None and perms.harness.can_execute_code
        if name == "harness.workspace":
            return perms.harness is not None and perms.harness.can_access_workspace
        return False

    def _check_fs_params(
        self,
        manifest: AgentManifest,
        tool: ToolDefinition,
        params: dict[str, Any],
    ) -> str | None:
        """Enforce ``permissions.file_system.allow/deny`` on path-shaped params."""
        if "fs:read" not in tool.side_effects and "fs:write" not in tool.side_effects:
            return None
        perms = manifest.permissions
        if perms is None or perms.file_system is None:
            return "missing_fs_permission"
        fs = perms.file_system
        candidate_paths = self._extract_path_params(params)
        for path in candidate_paths:
            if not self._path_allowed(path, fs.allow, fs.deny):
                return f"path_not_allowed:{path}"
        return None

    @staticmethod
    def _extract_path_params(params: dict[str, Any]) -> Iterable[str]:
        """Yield string params that look like filesystem paths."""
        keys = {"path", "file", "filepath", "file_path", "src", "dst", "dir"}
        for k, v in params.items():
            if not isinstance(v, str):
                continue
            kl = k.lower()
            if kl in keys or kl.endswith("_path") or "/" in v or v.startswith("~"):
                yield v

    @staticmethod
    def _path_allowed(path: str, allow: list[str], deny: list[str]) -> bool:
        # Default deny: nothing allowed if allow is empty.
        if not allow:
            return False
        # Deny wins.
        for pat in deny:
            if fnmatch.fnmatchcase(path, pat):
                return False
        # Allow must match.
        for pat in allow:
            if fnmatch.fnmatchcase(path, pat):
                return True
        return False

    def _check_network_params(
        self,
        manifest: AgentManifest,
        tool: ToolDefinition,
        params: dict[str, Any],
    ) -> str | None:
        if "network:egress" not in tool.side_effects and "network:listen" not in tool.side_effects:
            return None
        perms = manifest.permissions
        if perms is None or perms.network is None:
            return "missing_network_permission"
        net = perms.network
        hosts = self._extract_host_params(params)
        for host in hosts:
            if not self._host_allowed(host, net.allow, net.deny):
                return f"host_not_allowed:{host}"
        return None

    @staticmethod
    def _extract_host_params(params: dict[str, Any]) -> Iterable[str]:
        keys = {"host", "url", "endpoint", "address", "domain"}
        for k, v in params.items():
            if not isinstance(v, str):
                continue
            kl = k.lower()
            if kl in keys or "://" in v:
                yield v

    @staticmethod
    def _host_allowed(host: str, allow: list[str], deny: list[str]) -> bool:
        if not allow:
            return False
        for pat in deny:
            if fnmatch.fnmatchcase(host, pat):
                return False
        for pat in allow:
            if fnmatch.fnmatchcase(host, pat):
                return True
        return False

    # -- audit + emit ------------------------------------------------------

    async def _deny(
        self,
        registry: AgentRegistry,
        envelope: Envelope,
        *,
        reason: str,
    ) -> None:
        """Record the deny, raise :class:`PermissionDenied`, emit events."""
        await self._audit(
            registry,
            envelope_id=envelope.envelope_id,
            agent_id=envelope.sender.agent_id,
            action="permission_check",
            result="deny",
            reason=reason,
        )
        # Surface the denial to both sender and main-agent via the bus
        # event channel. We import lazily to avoid a circular import at
        # module load (bus.py imports us).
        from .bus import MessageBus  # local import

        bus: MessageBus | None = getattr(registry, "_bus", None)
        if bus is not None:
            await bus._emit_to_main(  # type: ignore[attr-defined]
                "permission_denied",
                {
                    "envelope_id": envelope.envelope_id,
                    "sender": envelope.sender.agent_id,
                    "receiver": envelope.receiver.agent_id,
                    "reason": reason,
                },
            )
        # Note: we do NOT raise here. The caller (bus.send) translates a
        # ``False`` return into a PermissionDenied raise. The events have
        # already been emitted by the time the raise happens.

    async def _audit(
        self,
        registry: AgentRegistry,
        *,
        envelope_id: str,
        agent_id: str,
        action: str,
        result: str,
        reason: str,
    ) -> None:
        await registry.audit(
            action=action,
            result=result,
            agent_id=agent_id,
            envelope_id=envelope_id,
            details={
                "reason": reason,
                "checked_at": datetime.now(timezone.utc).isoformat().replace(
                    "+00:00", "Z"
                ),
            },
        )


__all__ = ["PermissionEnforcer", "SIDE_EFFECT_PERMISSION"]
