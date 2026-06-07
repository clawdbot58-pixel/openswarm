# Interface Design - Natural Language

Unlike traditional CLIs with `/commands`, this interface uses natural language.

## How It Works

### User Types Naturally

Instead of `/run build me something`, just say what you want:

```
Build me a login form

→ Queued as task
```

### Keywords Trigger Special Actions

Certain words trigger responses without being queued as tasks:

| Input             | Action             |
| ----------------- | ------------------ |
| `status`          | Show swarm health  |
| `help`            | Show what I can do |
| `tasks` / `queue` | Show taskboard     |
| `clear`           | Clear task queue   |

### Everything Else = Task

Any other input is treated as a goal and queued to the taskboard.

## Example Conversation

```
▸ Hey, how's it going?

🟢 Swarm is running

Agents: 3/3 ready
  • main: ready (that's me!)
  • orchestrator: ready
  • worker: ready

Queue depth: 1 task(s)

---

▸ Build me a REST API for users

🎯 Got it! I've queued that as a task.

Task: REST API for users
ID: abc123

I'll delegate to my team and work on this. Want me to start now?

---

▸ Show me what tasks I have

📋 Taskboard

## task-abc123: REST API for users
- **Status:** pending
- **Priority:** medium
- ...

---

▸ What can you do?

What I can do:

Just tell me what you want! Examples:

"Build me a login form" — I'll queue that as a task
"Check the status" — See swarm health
"Show me tasks" — See queued tasks

I have a team of agents:
- main — That's me! I talk to you
- orchestrator — Plans and coordinates
- worker — Does the actual work
```

## Design Philosophy

1. **No commands to remember** - Just talk naturally
2. **Goals first** - Anything you type is a potential task
3. **Delegate naturally** - I handle the coordination
4. **Transparent** - You see the taskboard, I do the work
