# Swarm Orchestrator

The orchestrator coordinates agents to accomplish user goals. It's the "kernel" that routes tasks to the right agents.

## Flow

```
User Message → Orchestrator → Planner → Coder → Reviewer → User
```

## Commands

- `/start` - Show welcome message
- `/status` - Show swarm health
- `/run <goal>` - Execute a goal
- `/logs` - Show recent activity
- `/help` - Show commands

## State

The orchestrator maintains:
- **Queue**: Pending goals
- **Agents**: Available agents and their status
- **Memory**: Shared context between agents
- **Workflows**: Active task executions

## Agent Routing

| Goal Type | Primary Agent | Secondary |
|-----------|---------------|-----------|
| Build feature | coder | planner → reviewer |
| Fix bug | coder | reviewer |
| Review code | reviewer | - |
| Plan project | planner | - |
| Research | researcher | planner |

## Integration

This orchestrator is designed to work with:
- Telegram bot interface
- CLI interface
- API endpoints
