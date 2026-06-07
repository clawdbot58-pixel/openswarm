"""Agent workspace taskboard — visible queue the user can open in Finder."""
from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path


def ensure_agent_workspace(root: Path) -> Path:
    """Create the user-facing agent workspace and seed files if missing."""
    ws = root / "workspaces" / "agent"
    ws.mkdir(parents=True, exist_ok=True)
    taskboard = ws / "TASKBOARD.md"
    if not taskboard.is_file():
        taskboard.write_text(
            "# Taskboard\n\n"
            "## Active Tasks\n\n"
            "_No active tasks yet_\n\n"
            "---\n\n"
            "## Completed\n\n",
            encoding="utf-8",
        )
    memory_dir = ws / "memory"
    memory_dir.mkdir(exist_ok=True)
    return ws


def queue_goal(workspace: Path, goal: str, *, source: str = "user") -> str:
    """Append a task to TASKBOARD.md and return a short task id."""
    if workspace.name != "agent":
        workspace = workspace / "agent"
    ensure_agent_workspace(workspace.parent)
    taskboard = workspace / "TASKBOARD.md"

    task_id = datetime.now(timezone.utc).strftime("%H%M%S")
    ts = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    entry = (
        f"\n## task-{task_id}: {goal.strip()}\n"
        f"- **Status:** pending\n"
        f"- **Source:** {source}\n"
        f"- **Created:** {ts}\n"
        f"- **Description:** {goal.strip()}\n"
    )
    text = taskboard.read_text(encoding="utf-8")
    if "_No active tasks yet_" in text:
        text = text.replace("_No active tasks yet_", entry.strip())
    else:
        text = re.sub(
            r"(## Active Tasks\n)",
            r"\1" + entry,
            text,
            count=1,
        )
    taskboard.write_text(text, encoding="utf-8")
    return task_id


def format_taskboard_preview(workspace: Path, *, max_chars: int = 1200) -> str:
    """Return a Telegram-friendly preview of active tasks."""
    taskboard = workspace / "TASKBOARD.md"
    if not taskboard.is_file():
        return "No tasks yet."
    content = taskboard.read_text(encoding="utf-8")
    match = re.search(r"## Active Tasks\n\n([\s\S]*?)(?:---|$)", content)
    active = (match.group(1).strip() if match else "").strip()
    if not active or active == "_No active tasks yet_":
        return "No active tasks."
    if len(active) > max_chars:
        return active[: max_chars - 3] + "..."
    return active


__all__ = ["ensure_agent_workspace", "format_taskboard_preview", "queue_goal"]
