"""Tests for the introspection API.

Exercises every public method of :class:`IntrospectionAPI` against a
real kernel harness with seeded agents, audit-log rows, memory rows,
loop templates, and a workspace tree on disk.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Make ``src`` importable.
_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_SRC = _PROJECT_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from dashboard.backend.tests.conftest import make_manifest  # noqa: E402
from kernel.exceptions import AgentNotFound  # noqa: E402
from kernel.models import (  # noqa: E402
    AgentManifest,
)

# ---------------------------------------------------------------------------
# Agents
# ---------------------------------------------------------------------------


async def test_get_agents_returns_seeded(dashboard_harness):
    registry = dashboard_harness["registry"]
    intro = dashboard_harness["introspection"]

    for agent_id, role in [("coder-a", "executor"), ("coder-b", "executor")]:
        m = AgentManifest.model_validate(make_manifest(agent_id, role=role))
        await registry.register(m)

    agents = await intro.get_agents()
    ids = {a.agent_id for a in agents}
    assert {"coder-a", "coder-b"} <= ids


async def test_get_agents_filters_by_status(dashboard_harness):
    registry = dashboard_harness["registry"]
    intro = dashboard_harness["introspection"]
    m1 = AgentManifest.model_validate(make_manifest("ready-agent", status="ready"))
    m2 = AgentManifest.model_validate(make_manifest("busy-agent", status="busy"))
    await registry.register(m1)
    await registry.register(m2)
    await registry.update_status("ready-agent", "ready")
    await registry.update_status("busy-agent", "busy")

    ready = await intro.get_agents(status="ready")
    assert "ready-agent" in {a.agent_id for a in ready}
    assert "busy-agent" not in {a.agent_id for a in ready}
    busy = await intro.get_agents(status="busy")
    assert {"busy-agent"} == {a.agent_id for a in busy}


async def test_get_agents_filters_by_role_and_category(dashboard_harness):
    registry = dashboard_harness["registry"]
    intro = dashboard_harness["introspection"]
    await registry.register(
        AgentManifest.model_validate(
            make_manifest("reviewer", role="specialist", category="review")
        )
    )
    await registry.register(
        AgentManifest.model_validate(
            make_manifest("coder", role="executor", category="coding")
        )
    )
    specialists = await intro.get_agents(role="specialist")
    assert {a.agent_id for a in specialists} == {"reviewer"}
    reviewers = await intro.get_agents(category="review")
    assert {a.agent_id for a in reviewers} == {"reviewer"}


async def test_get_agents_filters_by_tags(dashboard_harness):
    registry = dashboard_harness["registry"]
    intro = dashboard_harness["introspection"]
    await registry.register(
        AgentManifest.model_validate(
            make_manifest("py", tags=["python", "backend"])
        )
    )
    await registry.register(
        AgentManifest.model_validate(make_manifest("rs", tags=["rust"]))
    )
    py = await intro.get_agents(tags=["python"])
    assert {a.agent_id for a in py} == {"py"}
    both = await intro.get_agents(tags=["python", "backend"])
    assert {a.agent_id for a in both} == {"py"}


async def test_get_agent_detail_returns_manifest_and_status(dashboard_harness):
    registry = dashboard_harness["registry"]
    intro = dashboard_harness["introspection"]
    await registry.register(
        AgentManifest.model_validate(make_manifest("detail-agent"))
    )
    detail = await intro.get_agent_detail("detail-agent")
    assert detail.agent_id == "detail-agent"
    assert detail.manifest["agent_id"] == "detail-agent"
    assert detail.status == "ready"
    assert detail.connected_ws is False
    assert detail.heartbeat_age_seconds >= 0


async def test_get_agent_detail_404_on_unknown(dashboard_harness):
    intro = dashboard_harness["introspection"]
    with pytest.raises(AgentNotFound):
        await intro.get_agent_detail("nonexistent")


async def test_get_agent_history_returns_audit_rows(dashboard_harness):
    registry = dashboard_harness["registry"]
    intro = dashboard_harness["introspection"]
    await registry.register(AgentManifest.model_validate(make_manifest("hist-agent")))
    await registry.audit(action="envelope_sent", result="ok", agent_id="hist-agent")
    await registry.audit(action="permission_denied", result="error", agent_id="hist-agent")
    history = await intro.get_agent_history("hist-agent", limit=10)
    assert len(history) == 2
    assert {h.action for h in history} == {"envelope_sent", "permission_denied"}


# ---------------------------------------------------------------------------
# Workflows
# ---------------------------------------------------------------------------


async def test_get_workflows_returns_empty_when_no_data(dashboard_harness):
    intro = dashboard_harness["introspection"]
    workflows = await intro.get_workflows()
    assert workflows == []


async def test_get_workflows_picks_up_workspaces(dashboard_harness):
    intro = dashboard_harness["introspection"]
    workspaces_dir: Path = dashboard_harness["workspaces_dir"]
    workspaces_dir.mkdir(parents=True, exist_ok=True)
    (workspaces_dir / "wf-123").mkdir()
    (workspaces_dir / "wf-123" / "src").mkdir()
    (workspaces_dir / "wf-123" / "src" / "main.py").write_text("print('hi')")
    workflows = await intro.get_workflows()
    assert {w.workflow_id for w in workflows} == {"wf-123"}


async def test_get_workflow_detail_from_workspace(dashboard_harness):
    intro = dashboard_harness["introspection"]
    workspaces_dir: Path = dashboard_harness["workspaces_dir"]
    ws = workspaces_dir / "wf-detail"
    (ws / "src").mkdir(parents=True)
    (ws / "src" / "main.py").write_text("x = 1")
    detail = await intro.get_workflow_detail("wf-detail")
    assert detail.workflow_id == "wf-detail"
    assert detail.name == "wf-detail"


async def test_get_workflow_detail_404_on_unknown(dashboard_harness):
    intro = dashboard_harness["introspection"]
    with pytest.raises(FileNotFoundError):
        await intro.get_workflow_detail("nonexistent-workflow")


async def test_get_workflow_logs_empty_when_no_audit(dashboard_harness):
    intro = dashboard_harness["introspection"]
    logs = await intro.get_workflow_logs("any-id")
    assert logs == []


# ---------------------------------------------------------------------------
# Logs
# ---------------------------------------------------------------------------


async def test_get_logs_returns_audit_rows(dashboard_harness):
    registry = dashboard_harness["registry"]
    intro = dashboard_harness["introspection"]
    await registry.register(AgentManifest.model_validate(make_manifest("log-agent")))
    await registry.audit(
        action="envelope_sent",
        result="ok",
        agent_id="log-agent",
        details={"envelope_type": "request", "priority": 5, "content_type": "text"},
    )
    await registry.audit(
        action="envelope_sent",
        result="error",
        agent_id="log-agent",
        details={"envelope_type": "error", "priority": 8, "content_type": "text"},
    )
    logs = await intro.get_logs(agent_id="log-agent")
    assert len(logs) == 2
    severities = {entry.severity for entry in logs}
    assert "error" in severities


async def test_get_logs_filter_by_severity(dashboard_harness):
    registry = dashboard_harness["registry"]
    intro = dashboard_harness["introspection"]
    await registry.register(AgentManifest.model_validate(make_manifest("sev-agent")))
    await registry.audit(
        action="envelope_sent",
        result="ok",
        agent_id="sev-agent",
        details={"envelope_type": "request", "content_type": "text"},
    )
    await registry.audit(
        action="permission_denied",
        result="error",
        agent_id="sev-agent",
        details={"envelope_type": "error"},
    )
    errors = await intro.get_logs(agent_id="sev-agent", severity="error")
    assert len(errors) == 1
    assert errors[0].result == "error"


# ---------------------------------------------------------------------------
# Workspaces
# ---------------------------------------------------------------------------


async def test_get_workspaces(dashboard_harness):
    intro = dashboard_harness["introspection"]
    workspaces_dir: Path = dashboard_harness["workspaces_dir"]
    for wf in ("alpha", "beta"):
        d = workspaces_dir / wf
        (d / "src").mkdir(parents=True)
        (d / "src" / "main.py").write_text("x = 1")
    workspaces = await intro.get_workspaces()
    assert {w.workflow_id for w in workspaces} == {"alpha", "beta"}


async def test_get_workspace_files(dashboard_harness):
    intro = dashboard_harness["introspection"]
    workspaces_dir: Path = dashboard_harness["workspaces_dir"]
    ws = workspaces_dir / "files-wf"
    src = ws / "src"
    src.mkdir(parents=True)
    (src / "a.py").write_text("a = 1")
    (src / "b.py").write_text("b = 2")
    (src / "sub").mkdir()
    (src / "sub" / "c.py").write_text("c = 3")
    files = await intro.get_workspace_files("files-wf", path="/src")
    names = {f.name for f in files}
    assert {"a.py", "b.py", "sub"} <= names


async def test_get_workspace_files_path_traversal_rejected(dashboard_harness):
    intro = dashboard_harness["introspection"]
    workspaces_dir: Path = dashboard_harness["workspaces_dir"]
    workspaces_dir.mkdir(parents=True, exist_ok=True)
    (workspaces_dir / "safe-wf").mkdir()
    files = await intro.get_workspace_files("safe-wf", path="/../")
    assert files == []


async def test_get_workspace_file(dashboard_harness):
    intro = dashboard_harness["introspection"]
    workspaces_dir: Path = dashboard_harness["workspaces_dir"]
    ws = workspaces_dir / "readme-wf"
    (ws / "src").mkdir(parents=True)
    (ws / "src" / "main.py").write_text("print('hi')")
    content = await intro.get_workspace_file("readme-wf", "/src/main.py")
    assert "print('hi')" in content.content
    assert content.size > 0


async def test_get_workspace_diff_no_git(dashboard_harness):
    intro = dashboard_harness["introspection"]
    workspaces_dir: Path = dashboard_harness["workspaces_dir"]
    workspaces_dir.mkdir(parents=True, exist_ok=True)
    (workspaces_dir / "no-git").mkdir()
    diff = await intro.get_workspace_diff("no-git", "abc123")
    assert diff == ""


async def test_get_workspace_history_no_git(dashboard_harness):
    intro = dashboard_harness["introspection"]
    workspaces_dir: Path = dashboard_harness["workspaces_dir"]
    workspaces_dir.mkdir(parents=True, exist_ok=True)
    (workspaces_dir / "no-hist").mkdir()
    history = await intro.get_workspace_history("no-hist")
    assert history == []


# ---------------------------------------------------------------------------
# Loops
# ---------------------------------------------------------------------------


async def test_get_loop_templates_sorted_by_success_rate(dashboard_harness):
    intro = dashboard_harness["introspection"]
    templates = await intro.get_loop_templates()
    # Premade templates are inserted by create_registry.
    assert len(templates) > 0
    rates = [t.success_rate for t in templates]
    assert rates == sorted(rates, reverse=True)


async def test_get_loop_templates_filter_by_task_type(dashboard_harness):
    intro = dashboard_harness["introspection"]
    templates = await intro.get_loop_templates(task_type="reasoning")
    # cot is the reasoning template.
    assert any(t.id == "cot" for t in templates)


async def test_get_loop_performance_returns_template(dashboard_harness):
    intro = dashboard_harness["introspection"]
    perf = await intro.get_loop_performance("cot")
    assert perf.template_id == "cot"
    assert perf.usage_count >= 0


async def test_get_loop_performance_404_on_unknown(dashboard_harness):
    intro = dashboard_harness["introspection"]
    with pytest.raises(FileNotFoundError):
        await intro.get_loop_performance("nonexistent-loop")


# ---------------------------------------------------------------------------
# Memory
# ---------------------------------------------------------------------------


async def test_get_agent_memory_returns_recent(dashboard_harness):
    memory = dashboard_harness["memory"]
    intro = dashboard_harness["introspection"]
    from memory.temporary import MemoryItem

    for i in range(3):
        item = MemoryItem(type="context", content={"i": i}, relevance_score=0.9)
        await memory.store(agent_id="mem-agent", item=item, tags=["t1"])

    rows = await intro.get_agent_memory("mem-agent", limit=5)
    assert len(rows) == 3
    assert {r.type for r in rows} == {"context"}


async def test_get_agent_memory_fts_search(dashboard_harness):
    memory = dashboard_harness["memory"]
    intro = dashboard_harness["introspection"]
    from memory.temporary import MemoryItem

    for content in ["alpha bravo", "charlie delta", "alpha charlie"]:
        await memory.store(
            agent_id="search-agent",
            item=MemoryItem(type="context", content=content, relevance_score=0.8),
        )

    rows = await intro.get_agent_memory("search-agent", query="alpha", limit=5)
    assert len(rows) >= 1
    # All matches should mention "alpha".
    for row in rows:
        assert "alpha" in str(row.content).lower()


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


async def test_get_system_metrics_computed(dashboard_harness):
    intro = dashboard_harness["introspection"]
    registry = dashboard_harness["registry"]
    await registry.register(AgentManifest.model_validate(make_manifest("m-agent")))
    metrics = await intro.get_system_metrics()
    assert metrics.total_agents >= 1
    assert metrics.uptime_seconds >= 0
    assert metrics.started_at is not None
    assert metrics.queue_total >= 0


async def test_get_agent_metrics_computed(dashboard_harness):
    intro = dashboard_harness["introspection"]
    registry = dashboard_harness["registry"]
    await registry.register(AgentManifest.model_validate(make_manifest("perf-agent")))
    await registry.audit(action="envelope_sent", result="ok", agent_id="perf-agent")
    await registry.audit(action="envelope_sent", result="error", agent_id="perf-agent")
    metrics = await intro.get_agent_metrics("perf-agent")
    assert metrics.agent_id == "perf-agent"
    assert metrics.tasks_failed == 1
    assert metrics.tasks_completed == 1
    assert 0.0 <= metrics.avg_confidence <= 1.0
