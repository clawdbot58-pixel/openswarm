"""Pydantic v2 models mirroring ``contracts/envelope.json`` and
``contracts/manifest.json``.

These models are the **only** Python representation of the contracts. The
kernel validates every incoming envelope and manifest against the
corresponding model; nothing else in the system should parse JSON into a
hand-rolled dict.

Design notes
------------
* **Discriminated unions** model the ``payload`` ``allOf``/``if/then`` blocks
  in ``envelope.json``. The discriminator is ``content_type``.
* **Pattern-constrained strings** (UUIDs, kebab-case agent IDs, semver
  versions) use ``Annotated[str, Field(pattern=...)]`` so validation
  errors carry useful context.
* The models round-trip cleanly to and from JSON, so the bus can
  serialize via ``model_dump_json`` and deserialize via
  ``Envelope.model_validate_json``.
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator


# ---------------------------------------------------------------------------
# Shared primitives
# ---------------------------------------------------------------------------

UUID4_PATTERN = r"^[a-f0-9-]{36}$"
KEBAB_PATTERN = r"^[a-z][a-z0-9_-]*$"
BROADCAST_AGENT_ID = "*"
"""Sentinel receiver.agent_id that means "every registered agent"."""
SEMVER_PATTERN = r"^\d+\.\d+\.\d+$"

UUID4Str = Annotated[str, Field(pattern=UUID4_PATTERN, description="UUID v4 string")]
AgentIdStr = Annotated[str, Field(pattern=KEBAB_PATTERN, description="kebab-case agent id")]
SemverStr = Annotated[str, Field(pattern=SEMVER_PATTERN, description="semver string")]


# ---------------------------------------------------------------------------
# Envelope — endpoint / preamble / payload / metadata
# ---------------------------------------------------------------------------

RoleLiteral = Literal[
    "kernel",
    "orchestrator",
    "executor",
    "specialist",
    "harness",
    "dashboard",
    "tool",
    "external",
]


class Endpoint(BaseModel):
    """A logical sender or receiver inside the swarm.

    ``agent_id`` is normally a kebab-case identifier (see
    :data:`AgentIdStr`).  The single exception is the broadcast sentinel
    :data:`BROADCAST_AGENT_ID` (``"*"``), which the bus expands to every
    registered agent at routing time.  The pattern is widened in the
    ``_validate_agent_id`` field-validator to permit it.
    """

    model_config = ConfigDict(extra="forbid")

    agent_id: str
    role: RoleLiteral
    instance_id: str | None = None

    @field_validator("agent_id", mode="before")
    @classmethod
    def _validate_agent_id(cls, value: Any) -> Any:
        if isinstance(value, str):
            if value == BROADCAST_AGENT_ID:
                return value
            if re.match(KEBAB_PATTERN + "$", value):
                return value
        raise ValueError(
            f"agent_id must match {KEBAB_PATTERN!r} or be the broadcast sentinel '*'"
        )


PhaseLiteral = Literal[
    "discovery",
    "planning",
    "execution",
    "reflection",
    "handoff",
    "recovery",
]


class IntentBlock(BaseModel):
    """The current high-level goal and workflow phase for the receiver."""

    model_config = ConfigDict(extra="forbid")

    goal: str
    phase: PhaseLiteral
    constraints: list[str] = Field(default_factory=list)


class PermissionsBlock(BaseModel):
    """Runtime permissions granted for this particular envelope's task."""

    model_config = ConfigDict(extra="forbid")

    can_read: list[str] = Field(default_factory=list)
    can_write: list[str] = Field(default_factory=list)
    can_execute: list[str] = Field(default_factory=list)
    can_delegate: bool = False
    max_tokens: int | None = Field(default=None, ge=1)


class ThinkingLoopConfig(BaseModel):
    """Configuration for the receiver's reasoning loop."""

    model_config = ConfigDict(extra="forbid")

    mode: Literal["fast", "thorough", "memo", "custom"] = "thorough"
    loop_id: str | None = None
    custom_graph: dict[str, Any] | None = None
    max_iterations: int = Field(default=10, ge=1, le=100)
    stop_conditions: list[str] = Field(default_factory=list)
    confidence_threshold: float = Field(default=0.8, ge=0.0, le=1.0)


class MemoryItem(BaseModel):
    """A single memory entry surfaced into the preamble."""

    model_config = ConfigDict(extra="forbid")

    timestamp: datetime
    type: Literal["action", "result", "decision", "error", "context"]
    content: Any
    relevance_score: float | None = Field(default=None, ge=0.0, le=1.0)


class MemoryContext(BaseModel):
    """The three memory channels an agent may consult."""

    model_config = ConfigDict(extra="forbid")

    recent_events: list[MemoryItem] = Field(default_factory=list)
    relevant_history: list[MemoryItem] = Field(default_factory=list)
    session_state: dict[str, Any] = Field(default_factory=dict)


class Preamble(BaseModel):
    """Context the kernel prepends to every inference call."""

    model_config = ConfigDict(extra="forbid")

    intent: IntentBlock
    permissions: PermissionsBlock = Field(default_factory=PermissionsBlock)
    thinking_loop_config: ThinkingLoopConfig = Field(default_factory=ThinkingLoopConfig)
    memory_context: MemoryContext | None = None


# --- Payload variants --------------------------------------------------------

TextFormat = Literal["plain", "markdown", "json", "xml"]


class TextPayload(BaseModel):
    """Free-form textual content."""

    model_config = ConfigDict(extra="forbid")

    content_type: Literal["text"] = "text"
    content: str
    format: TextFormat = "plain"


class ToolPayload(BaseModel):
    """A request to invoke a tool. Permission-checked by the kernel."""

    model_config = ConfigDict(extra="forbid")

    content_type: Literal["tool"] = "tool"
    tool_name: str
    action: Literal["invoke", "stream", "cancel", "status"]
    parameters: dict[str, Any] = Field(default_factory=dict)
    streaming: bool = False


class DataPayload(BaseModel):
    """Arbitrary structured data with an optional schema reference.

    The contract uses ``schema`` as the field name, but that collides with
    the legacy :meth:`BaseModel.schema` method. We expose the field as
    ``schema_ref`` in Python and serialize/deserialize as ``schema``.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    content_type: Literal["data"] = "data"
    data: Any
    schema_ref: str | None = Field(default=None, alias="schema")


class WorkflowPayload(BaseModel):
    """An inline workflow submission. Validated against ``workflow.json``."""

    model_config = ConfigDict(extra="forbid")

    content_type: Literal["workflow"] = "workflow"
    workflow: dict[str, Any]
    workflow_id: UUID4Str | None = None


class CheckpointPayload(BaseModel):
    """A workflow checkpoint the agent is persisting."""

    model_config = ConfigDict(extra="forbid")

    content_type: Literal["checkpoint"] = "checkpoint"
    workflow_id: str
    last_step_id: str | None = None
    state_blob: dict[str, Any] = Field(default_factory=dict)
    agent_outputs: dict[str, Any] = Field(default_factory=dict)


class SpawnRequestPayload(BaseModel):
    """A request from the main agent to spawn or clone another agent."""

    model_config = ConfigDict(extra="forbid")

    content_type: Literal["spawn_request"] = "spawn_request"
    reason: str | None = None
    manifest_delta: dict[str, Any]
    base_manifest_id: str | None = None


Payload = Annotated[
    Union[
        TextPayload,
        ToolPayload,
        DataPayload,
        WorkflowPayload,
        CheckpointPayload,
        SpawnRequestPayload,
    ],
    Field(discriminator="content_type"),
]
"""Discriminated union of all valid envelope payloads."""


class ModelRouting(BaseModel):
    """Hints for the model router (used by agents, not the kernel)."""

    model_config = ConfigDict(extra="forbid")

    tier: Literal["fast", "standard", "powerful"] | None = None
    requested_model: str | None = None
    fallback_models: list[str] = Field(default_factory=list)


class EnvelopeMetadata(BaseModel):
    """Free-form envelope metadata including kernel routing signals."""

    model_config = ConfigDict(extra="forbid")

    trace_id: str | None = None
    span_id: str | None = None
    content_hash: str | None = None
    priority: int = Field(default=5, ge=0, le=10)
    tags: list[str] = Field(default_factory=list)
    model_routing: ModelRouting | None = None


EnvelopeTypeLiteral = Literal[
    "request",
    "response",
    "event",
    "error",
    "heartbeat",
    "chunk",
    "intent",
]


class Envelope(BaseModel):
    """The universal message envelope — only shape allowed on the bus."""

    model_config = ConfigDict(extra="forbid")

    envelope_id: UUID4Str
    created_at: datetime
    expires_at: datetime | None = None
    envelope_type: EnvelopeTypeLiteral
    sender: Endpoint
    receiver: Endpoint
    reply_to: UUID4Str | None = None
    preamble: Preamble
    payload: Payload
    metadata: EnvelopeMetadata = Field(default_factory=EnvelopeMetadata)

    @field_validator("created_at", "expires_at", mode="before")
    @classmethod
    def _parse_datetimes(cls, value: Any) -> Any:
        """Accept both ISO 8601 strings and existing :class:`datetime` values."""
        if value is None or isinstance(value, datetime):
            return value
        if isinstance(value, str):
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        raise ValueError(f"unsupported datetime value: {value!r}")

    @property
    def priority(self) -> int:
        """Convenience accessor for the routing priority (0-10)."""
        return self.metadata.priority

    @property
    def is_expired(self) -> bool:
        """True if the envelope's ``expires_at`` is in the past."""
        if self.expires_at is None:
            return False
        now = datetime.now(self.expires_at.tzinfo)
        return now >= self.expires_at

    def is_for(self, agent_id: str) -> bool:
        """True if this envelope targets ``agent_id`` directly."""
        return self.receiver.agent_id == agent_id

    def is_broadcast(self) -> bool:
        """True if this envelope targets every registered agent.

        The envelope schema encodes the receiver as a normal endpoint, so we
        use a sentinel agent_id :data:`BROADCAST_AGENT_ID` (``"*"``) for
        broadcast — the bus expands it to every registered agent at
        routing time.
        """
        return self.receiver.agent_id == BROADCAST_AGENT_ID


# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------

StatusLiteral = Literal[
    "initializing",
    "ready",
    "busy",
    "idle",
    "draining",
    "offline",
    "error",
    "zombie",
]

RoleManifestLiteral = Literal[
    "kernel",
    "orchestrator",
    "executor",
    "specialist",
    "harness",
    "dashboard",
    "critic",
    "meta",
]

CategoryLiteral = Literal[
    "coding",
    "planning",
    "review",
    "research",
    "testing",
    "deployment",
    "analysis",
    "custom",
]


class ToolDefinition(BaseModel):
    """A single tool the agent can invoke."""

    model_config = ConfigDict(extra="forbid")

    name: str
    description: str
    parameters: dict[str, dict[str, Any]] = Field(default_factory=dict)
    returns: dict[str, Any] | None = None
    side_effects: list[str] = Field(default_factory=list)


class InferenceSpec(BaseModel):
    """The LLM provider + model the agent is wired to."""

    model_config = ConfigDict(extra="forbid")

    provider: Literal["openai", "anthropic", "ollama", "lmstudio", "custom"]
    models: list[str] = Field(default_factory=list)
    default_model: str | None = None
    max_context_tokens: int | None = None
    supports_streaming: bool = True


class Capabilities(BaseModel):
    """Inference + tool + skill surface for the agent."""

    model_config = ConfigDict(extra="forbid")

    inference: InferenceSpec
    tools: list[ToolDefinition] = Field(default_factory=list)
    skills: list[str] = Field(default_factory=list)
    protocols: list[Literal["envelope", "stream", "event", "batch"]] = Field(
        default_factory=lambda: ["envelope"]
    )


class FsPermission(BaseModel):
    """Filesystem allow/deny globs."""

    model_config = ConfigDict(extra="forbid")

    allow: list[str] = Field(default_factory=list)
    deny: list[str] = Field(default_factory=list)
    read_only: bool = False


class NetworkPermission(BaseModel):
    """Network egress rules."""

    model_config = ConfigDict(extra="forbid")

    allow: list[str] = Field(default_factory=list)
    deny: list[str] = Field(default_factory=list)


class ProcessPermission(BaseModel):
    """Permission to spawn / kill other processes."""

    model_config = ConfigDict(extra="forbid")

    can_spawn: bool = False
    can_kill: bool = False
    max_children: int | None = None


class HarnessPermission(BaseModel):
    """Permission to call into the sandboxed harness."""

    model_config = ConfigDict(extra="forbid")

    can_execute_code: bool = False
    can_access_workspace: bool = False
    allowed_runtimes: list[str] = Field(default_factory=list)


class Permissions(BaseModel):
    """Aggregate permission set carried by a manifest."""

    model_config = ConfigDict(extra="forbid")

    file_system: FsPermission | None = None
    network: NetworkPermission | None = None
    process: ProcessPermission | None = None
    harness: HarnessPermission | None = None
    env_vars: list[str] = Field(default_factory=list)


class Lifecycle(BaseModel):
    """How long the agent lives and what happens on death."""

    model_config = ConfigDict(extra="forbid")

    persistence: Literal["ephemeral", "session", "persistent"]
    max_age_minutes: int | None = Field(default=None, ge=1)
    max_tasks: int | None = Field(default=None, ge=1)
    auto_restart: bool = False
    restart_policy: Literal["never", "always", "exponential_backoff", "on_failure_only"] = (
        "never"
    )
    max_restarts: int = Field(default=3, ge=0)


class ThinkingProfile(BaseModel):
    """Reasoning loops the agent may run."""

    model_config = ConfigDict(extra="forbid")

    available_loops: list[str] = Field(default_factory=list)
    default_loop: str = "direct"
    allows_dynamic_assembly: bool = False
    trial_error_history: list[dict[str, Any]] = Field(default_factory=list)


class ModelTier(BaseModel):
    """Cost/quality band for the agent's inference."""

    model_config = ConfigDict(extra="forbid")

    tier: Literal["fast", "standard", "powerful"] = "standard"
    cost_budget_per_task: float | None = None


class Timeouts(BaseModel):
    """Per-agent timeout configuration."""

    model_config = ConfigDict(extra="forbid")

    default_inference_ms: int | None = Field(default=None, ge=1000)
    max_inference_ms: int | None = Field(default=None, ge=1000)
    tool_execution_ms: int | None = Field(default=None, ge=100)


class MemoryConfig(BaseModel):
    """Memory behavior configuration."""

    model_config = ConfigDict(extra="forbid")

    context_window: int = 10
    relevance_threshold: float = Field(default=0.5, ge=0.0, le=1.0)


class Configuration(BaseModel):
    """Optional runtime configuration nested under the manifest."""

    model_config = ConfigDict(extra="forbid")

    timeouts: Timeouts | None = None
    memory: MemoryConfig | None = None


class Endpoints(BaseModel):
    """Optional callback URIs the agent exposes."""

    model_config = ConfigDict(extra="forbid")

    receive: str | None = None
    stream: str | None = None
    events: str | None = None


class HealthCheck(BaseModel):
    """Health-check configuration for the heartbeat monitor."""

    model_config = ConfigDict(extra="forbid")

    endpoint: str | None = None
    interval_seconds: int = Field(default=30, ge=1)
    timeout_seconds: int = Field(default=5, ge=1)


class AgentManifest(BaseModel):
    """The single source of truth for an agent's identity and capabilities."""

    model_config = ConfigDict(extra="forbid")

    agent_id: AgentIdStr
    version: SemverStr
    role: RoleManifestLiteral
    human_readable_name: str | None = None
    description: str | None = None
    intent: str
    category: CategoryLiteral | None = None
    tags: list[str] = Field(default_factory=list)
    registration_time: datetime
    last_heartbeat: datetime | None = None
    status: StatusLiteral = "initializing"
    capabilities: Capabilities
    permissions: Permissions | None = None
    lifecycle: Lifecycle
    thinking_profile: ThinkingProfile | None = None
    model_tier: ModelTier | None = None
    configuration: Configuration | None = None
    endpoints: Endpoints | None = None
    dependencies: list[AgentIdStr] = Field(default_factory=list)
    health_check: HealthCheck | None = None

    @field_validator("registration_time", "last_heartbeat", mode="before")
    @classmethod
    def _parse_datetimes(cls, value: Any) -> Any:
        if value is None or isinstance(value, datetime):
            return value
        if isinstance(value, str):
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        raise ValueError(f"unsupported datetime value: {value!r}")

    def has_tool(self, tool_name: str) -> bool:
        """True if the agent declared ``tool_name`` in its capabilities."""
        return any(t.name == tool_name for t in self.capabilities.tools)

    def get_tool(self, tool_name: str) -> ToolDefinition | None:
        """Return the tool definition for ``tool_name`` or ``None``."""
        for tool in self.capabilities.tools:
            if tool.name == tool_name:
                return tool
        return None


# ---------------------------------------------------------------------------
# Heartbeat file
# ---------------------------------------------------------------------------

HeartbeatStatusLiteral = Literal["ready", "busy", "idle"]


class HeartbeatFile(BaseModel):
    """The JSON payload written by agents to ``heartbeats/{agent_id}.json``."""

    model_config = ConfigDict(extra="forbid")

    agent_id: AgentIdStr
    timestamp: datetime
    status: HeartbeatStatusLiteral

    @field_validator("timestamp", mode="before")
    @classmethod
    def _parse_timestamp(cls, value: Any) -> Any:
        if value is None or isinstance(value, datetime):
            return value
        if isinstance(value, str):
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        raise ValueError(f"unsupported timestamp value: {value!r}")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Recognised kernel-emitted event names. The kernel uses
# ``envelope_type="event"`` and embeds one of these names in
# ``payload.data.event`` so the contract schema is preserved.
KERNEL_EVENT_NAMES: frozenset[str] = frozenset(
    {
        "permission_denied",
        "queue_overflow",
        "agent_zombie",
        "auto_restart_triggered",
        "envelope_rejected",
        "registration_rejected",
        # Phase 9 self-healing events. The bus surfaces these to the
        # Main Agent so it can decide the recovery strategy.
        "loop_detected",
        "step_timeout",
        "budget_exhausted",
        "workflow_resume",
        "step_recovered",
        "fallback_invoked",
        "compensation_invoked",
        "respawn_requested",
        "escalation_requested",
    }
)


def is_kernel_event_name(name: str) -> bool:
    """True if ``name`` is a kernel-emitted event type."""
    return name in KERNEL_EVENT_NAMES


# Match a glob-style permission pattern. Supports ``*`` and ``?`` only.
def glob_match(pattern: str, value: str) -> bool:
    """Return True if ``value`` matches the simple ``fnmatch``-style ``pattern``.

    Uses :func:`fnmatch.fnmatchcase` semantics.  This function is re-exported
    under a kernel-local name so the permissions module does not have to
    re-import ``fnmatch`` from the standard library everywhere.
    """
    import fnmatch

    return fnmatch.fnmatchcase(value, pattern)


__all__ = [
    "AgentIdStr",
    "AgentManifest",
    "BROADCAST_AGENT_ID",
    "Capabilities",
    "CategoryLiteral",
    "CheckpointPayload",
    "Configuration",
    "DataPayload",
    "Endpoint",
    "Endpoints",
    "Envelope",
    "EnvelopeMetadata",
    "EnvelopeTypeLiteral",
    "FsPermission",
    "HarnessPermission",
    "HealthCheck",
    "HeartbeatFile",
    "HeartbeatStatusLiteral",
    "InferenceSpec",
    "IntentBlock",
    "KERNEL_EVENT_NAMES",
    "Lifecycle",
    "MemoryConfig",
    "MemoryContext",
    "MemoryItem",
    "ModelRouting",
    "ModelTier",
    "NetworkPermission",
    "Payload",
    "Permissions",
    "PermissionsBlock",
    "PhaseLiteral",
    "Preamble",
    "ProcessPermission",
    "RoleLiteral",
    "RoleManifestLiteral",
    "SemverStr",
    "SpawnRequestPayload",
    "StatusLiteral",
    "TextFormat",
    "TextPayload",
    "ThinkingLoopConfig",
    "ThinkingProfile",
    "Timeouts",
    "ToolDefinition",
    "ToolPayload",
    "UUID4Str",
    "WorkflowPayload",
    "glob_match",
    "is_kernel_event_name",
]
