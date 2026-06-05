"""Tests for PreambleAssembler."""


import pytest

from loops import assemble, assemble_minimal


def test_assemble_contains_role():
    """Test that assemble includes role section."""
    preamble = {"intent": {}, "permissions": {}}
    manifest = {"agent_id": "test-agent", "role": "executor", "intent": "Test intent"}

    result = assemble(preamble, manifest)

    assert "ROLE" in result
    assert "test-agent" in result
    assert "executor" in result


def test_assemble_contains_permissions():
    """Test that assemble includes permissions section."""
    preamble = {
        "intent": {},
        "permissions": {
            "can_read": ["/workspace"],
            "can_write": ["/workspace/output"],
        },
    }
    manifest = {"agent_id": "test-agent", "role": "executor", "intent": ""}

    result = assemble(preamble, manifest)

    assert "PERMISSIONS" in result
    assert "/workspace" in result


def test_assemble_contains_thinking_loop():
    """Test that assemble includes thinking loop config."""
    preamble = {
        "intent": {},
        "permissions": {},
        "thinking_loop_config": {"mode": "thorough", "max_iterations": 5},
    }
    manifest = {"agent_id": "test-agent", "role": "executor", "intent": ""}

    result = assemble(preamble, manifest)

    assert "THINKING LOOP" in result
    assert "thorough" in result
    assert "5" in result


def test_assemble_contains_memory():
    """Test that assemble includes memory context."""
    preamble = {
        "intent": {},
        "permissions": {},
        "memory_context": {
            "recent_events": [
                {"timestamp": "2026-01-01T00:00:00Z", "type": "action", "content": "test"}
            ],
            "relevant_history": [],
            "session_state": {"workflow_id": "test-workflow"},
        },
    }
    manifest = {"agent_id": "test-agent", "role": "executor", "intent": ""}

    result = assemble(preamble, manifest)

    assert "MEMORY" in result
    assert "recent_events" in result.lower() or "test" in result


def test_assemble_contains_intent():
    """Test that assemble includes task intent."""
    preamble = {
        "intent": {"goal": "Complete task", "phase": "execution", "constraints": []},
        "permissions": {},
    }
    manifest = {"agent_id": "test-agent", "role": "executor", "intent": ""}

    result = assemble(preamble, manifest)

    assert "TASK" in result
    assert "Complete task" in result


def test_assemble_minimal():
    """Test that assemble_minimal creates minimal context."""
    result = assemble_minimal("Do something", "My intent")

    assert "TASK" in result
    assert "Do something" in result
    assert "My intent" in result


def test_assemble_minimal_without_intent():
    """Test assemble_minimal without intent."""
    result = assemble_minimal("Do something")

    assert "TASK" in result
    assert "Do something" in result


def test_assemble_handles_empty_preamble():
    """Test that assemble handles empty preamble gracefully."""
    preamble = {"intent": {}, "permissions": {}}
    manifest = {"agent_id": "test-agent", "role": "executor", "intent": "Test"}

    result = assemble(preamble, manifest)

    assert "ROLE" in result
    assert "test-agent" in result


def test_assemble_handles_missing_optional_fields():
    """Test that assemble handles missing optional fields."""
    preamble = {}
    manifest = {"agent_id": "test-agent", "role": "executor", "intent": "Test"}

    result = assemble(preamble, manifest)

    assert "ROLE" in result
    assert "test-agent" in result