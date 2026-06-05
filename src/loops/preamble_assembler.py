"""Preamble assembler - builds full context for LLM calls."""

from typing import Any


def assemble(preamble: dict[str, Any], manifest: dict[str, Any]) -> str:
    """Assemble the full context from preamble and manifest.

    Inspired by OpenClaw's CLAUDE.md + skills + history assembly.

    Args:
        preamble: The task preamble (intent, permissions, thinking_loop_config, memory_context).
        manifest: The agent manifest.

    Returns:
        Formatted context string for LLM.
    """
    parts = []

    # Role section
    role = manifest.get("role", "executor")
    intent = manifest.get("intent", "")
    agent_id = manifest.get("agent_id", "unknown")

    parts.append(f"# ROLE\nYou are {agent_id}, a {role}. {intent}")

    # Permissions section
    permissions = preamble.get("permissions", {})
    parts.append(f"# PERMISSIONS")
    if "can_read" in permissions:
        parts.append(f"Read: {permissions['can_read']}")
    if "can_write" in permissions:
        parts.append(f"Write: {permissions['can_write']}")
    if "can_execute" in permissions:
        parts.append(f"Execute: {permissions['can_execute']}")
    if "can_delegate" in permissions:
        parts.append(f"Delegate: {permissions['can_delegate']}")

    # Thinking loop configuration
    loop_config = preamble.get("thinking_loop_config", {})
    if loop_config:
        parts.append(f"\n# THINKING LOOP")
        if "mode" in loop_config:
            parts.append(f"Mode: {loop_config['mode']}")
        if "max_iterations" in loop_config:
            parts.append(f"Max iterations: {loop_config['max_iterations']}")
        if "confidence_threshold" in loop_config:
            parts.append(f"Confidence threshold: {loop_config['confidence_threshold']}")

    # Memory context
    memory_context = preamble.get("memory_context", {})
    if memory_context:
        parts.append(f"\n# MEMORY")

        recent_events = memory_context.get("recent_events", [])
        if recent_events:
            parts.append(f"Recent events:")
            for event in recent_events[-5:]:  # Last 5 events
                event_type = event.get("type", "unknown")
                timestamp = event.get("timestamp", "")
                parts.append(f"  - [{timestamp}] {event_type}")

        relevant_history = memory_context.get("relevant_history", [])
        if relevant_history:
            parts.append(f"\nRelevant history:")
            for item in relevant_history[:3]:  # Top 3
                content = item.get("content", "")
                if isinstance(content, dict):
                    content = str(content)[:100]
                parts.append(f"  - {content[:100]}")

        session_state = memory_context.get("session_state", {})
        if session_state:
            parts.append(f"\nSession state: {session_state.get('workflow_id', 'unknown')}")

    # Intent from preamble
    intent_info = preamble.get("intent", {})
    if intent_info:
        parts.append(f"\n# TASK")
        if "goal" in intent_info:
            parts.append(f"Goal: {intent_info['goal']}")
        if "phase" in intent_info:
            parts.append(f"Phase: {intent_info['phase']}")
        if "constraints" in intent_info:
            parts.append(f"Constraints: {', '.join(intent_info['constraints'])}")

    return "\n".join(parts)


def assemble_minimal(task: str, intent: str = "") -> str:
    """Assemble a minimal context for simple tasks.

    Args:
        task: The task content.
        intent: Optional intent description.

    Returns:
        Minimal context string.
    """
    parts = []
    parts.append("# TASK")
    if intent:
        parts.append(f"Intent: {intent}")
    parts.append(f"Task: {task}")
    return "\n".join(parts)