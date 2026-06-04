# Implementation Phases

## Phase 0: Contracts ✅
Write `src/contracts/envelope.json`, `manifest.json`, `workflow.json`.
These are the prison walls. Never change without version bump.

## Phase 1: Kernel (Gateway Control Plane)
**Deliverable:** `src/kernel/` — running Python process, WebSocket + REST.
**Inspiration:** OpenClaw Gateway (central nervous system, single source of truth).
**Key features:** Message router, registry, permission enforcer, heartbeat monitor.
**Constraint:** NO LLM logic. NO agents. NO dashboard. Pure infrastructure.

## Phase 2: Main Agent (Orchestrator)
**Deliverable:** `src/agents/main_agent.py` + `prompts/main_system.md`.
**Inspiration:** OpenClaw session manager (isolates contexts).
**Key features:** Receives user goals, emits workflow JSON, handles events, decides recovery.
**Constraint:** NEVER executes tools. NEVER writes files. ONLY JSON decisions + natural language.

## Phase 3: Generic Agent Worker + Model Router
**Deliverable:** `src/agent_worker.py` + `src/model_router.py` + `src/loops/`.
**Inspiration:** OpenClaw `src/agents/` (interchangeable providers).
**Key features:** ONE executable for all agents. Thinking loop router. Model fallback chain.
**Constraint:** NO hardcoded roles. NO tool execution yet.

## Phase 4: Thinking Loop / Method System
**Deliverable:** `src/loops/primitives.py`, `assembler.py`, `registry.py`.
**Inspiration:** OpenClaw skills (loadable, composable).
**Key features:** Premade loops + dynamic assembly + scoring + template registry.

## Phase 5: Coding Harness
**Deliverable:** `src/harness/` — workspace, executor, git tracker, diff generator.
**Inspiration:** OpenClaw `src/node-host/` (sandboxed execution, three-phase policy).
**Key features:** Docker isolation, auto-commit, diff streaming.

## Phase 6: Memory & Context Assembly
**Deliverable:** `src/memory/` — temporary, persistent, context assembler.
**Inspiration:** OpenClaw context bootstrap (`CLAUDE.md` + skills + history).
**Key features:** Session scratchpad, cross-session SQLite store, semantic assembly.

## Phase 7: Dashboard Backend (Event Stream)
**Deliverable:** `src/dashboard/backend/` — FastAPI, WebSocket `/stream`.
**Inspiration:** OpenClaw channel adapters (normalize to single stream).
**Key features:** Broadcast listener, REST API, real-time push.
**Constraint:** Read-only. No controls.

## Phase 8: Dashboard Frontend
**Deliverable:** `src/dashboard/frontend/` — React, workflow DAG, live logs.
**Key features:** Agent cards, graph view, log stream, workspace explorer.

## Phase 9: Self-Healing & Workflow Recovery
**Deliverable:** Patches to kernel + main_agent.
**Key features:** Checkpointing, resume on boot, mutate-on-retry, escalation.

## Phase 10: Dynamic Loop Assembly & Trial/Error
**Deliverable:** `src/meta_agent.py`, `src/loop_optimizer.py`.
**Key features:** Meta-agent proposes loops, critic scores, leaderboard.

## Phase 11: Polish & Scale
**Deliverable:** Redis queue, auth, agent marketplace, `SOUL.md` personalities.

## Phase Dependency Graph

```
0 (Contracts)
    ↓
1 (Kernel) ←────────────────────────┐
    ↓                                  │
2 (Main Agent)                         │
    ↓                                  │
3 (Agent Worker) ──→ 4 (Loops)         │
    ↓                    ↓             │
5 (Harness) ←──────────┘             │
    ↓                                │
6 (Memory)                         │
    ↓                                │
7 (Dash Backend) ←───────────────────┘
    ↓
8 (Dash Frontend)
    ↓
9 (Self-Healing)
    ↓
10 (Loop Opt)
    ↓
11 (Polish)
```

## Golden Rule

**Never build Phase N+1 until Phase N is tested and the AI can reference working code.**

## Two Additional Hard Rules

### 1. Plan Before Code (applies to Phase 2 onward)

Before writing any code in a phase, the implementing agent must produce a **phase plan** in `plans/phase_{N}_plan.md` that includes:

- Files to be created or modified (from `naming.md`)
- The full preamble assembly the new agents will receive (referencing `prompt-engineering.md` and `environment.md`)
- The contracts each new file will produce or consume (JSON shapes)
- The test strategy (per `testing.md`)
- A demo script: a real command a human can run to see the phase working

The plan is reviewed by a human before code is written. The implementing agent may use the Main Agent's `planning` mode to draft it (see `prompt-engineering.md` §10, §12).

This rule exists because the half-baked-nothing problem (see `phylosophy.md` §5) comes from jumping into code with a vague mental model. The plan is the mental model, externalized.

### 2. Demo Before Next Phase

Each phase ends with a **working demo on real hardware, not a passing test suite.** A demo is:

- A shell command the human runs.
- Real input → real output → real visible effect.
- Documented in `demos/phase_{N}_demo.md` with: command, expected output, screenshot if visual.

If the human cannot see the system work, the phase is not done. Tests catch correctness; demos catch "this is a science project."
