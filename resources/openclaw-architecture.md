# OpenClaw Architecture Reference

## Source: github.com/anthropics/openclaw (as of 2026)

## Core Components

### 1. Gateway (Control Plane)
- Persistent WebSocket server (Node.js)
- Single source of truth for all agent state
- Decouples I/O from execution
- Handles 25+ channel adapters (Telegram, Discord, Slack, etc.)
- Key insight: "The Gateway is the central nervous system."

### 2. Agent Loop
```
Input → Context Assembly → LLM Inference → Tool Execution → Output
```
- Context assembly = CLAUDE.md + SOUL.md + SKILL.md + conversation history
- Tool execution = sandboxed shell, browser, file I/O
- Error recovery = exponential backoff, model fallback

### 3. Node Host (Execution Environment)
- Privileged local execution
- Three-phase policy:
  1. Allowlist check (is this command pre-approved?)
  2. Approval gate (user confirmation for sensitive ops)
  3. Execution (Docker sandbox)
- File system: workspace per conversation
- Git tracking: auto-commit after every file change

### 4. Memory System
- Temporary: session scratchpad
- Persistent: cross-session project knowledge
- Semantic: vector search over past conversations
- Loaded into context window before every LLM call

### 5. Heartbeat Daemon
- Wakes every ~30 minutes
- Reads HEARTBEAT.md (user-defined periodic tasks)
- Can trigger workflows autonomously
- "The agent works while you sleep."

### 6. Skills System
- Directory: `skills/{skill_id}/SKILL.md`
- Loaded into context when agent needs capability
- Community-contributed
- Versioned

## What We Steal

| OpenClaw Feature | OpenSwarm Equivalent |
|------------------|----------------------|
| Gateway | Kernel (Phase 1) |
| Context assembly (CLAUDE.md + skills) | Preamble assembly (Phase 6) |
| Node host sandbox | Harness (Phase 5) |
| Heartbeat daemon | Heartbeat monitor + auto-restart (Phase 1, 9) |
| Skills system | Skills directory + loadable expertise (Phase 3) |
| Agent loop | Agent worker + thinking loops (Phase 3, 4) |
| Error recovery | Self-healing hierarchy (Phase 9) |

## What We Differ

| OpenClaw | OpenSwarm |
|----------|-----------|
| Single agent per conversation | Multi-agent swarm per workflow |
| Human chats with agent directly | Human chats with orchestrator only |
| Skills are static | Thinking loops are dynamic and optimizable |
| Error recovery is hardcoded | Error recovery is orchestrator-decided |
| No visual dashboard | Real-time dashboard is first-class |
| No checkpointing | Checkpointing is core to resilience |
