# System Architecture

## High-Level Diagram

```
┌─────────────────────────────────────────────────────────────┐
│                        USER / DASHBOARD                      │
│              (React frontend, WebSocket stream)              │
└─────────────────────────────┬───────────────────────────────┘
                              │ REST / WS
┌─────────────────────────────▼───────────────────────────────┐
│                      MAIN AGENT (Orchestrator)               │
│  - Receives user goals                                     │
│  - Decomposes into workflow DAGs                            │
│  - Spawns/replaces agents                                   │
│  - Handles failure events (retry, mutate, escalate)         │
│  - Reports status in natural language                       │
│  - NEVER executes tools, NEVER writes files                 │
└─────────────────────────────┬───────────────────────────────┘
                              │ Envelope protocol (WebSocket)
┌─────────────────────────────▼───────────────────────────────┐
│                        KERNEL (Gateway)                      │
│  - Message router (priority queue)                           │
│  - Agent registry (SQLite)                                   │
│  - Permission enforcer (drop unauthorized messages)          │
│  - Heartbeat monitor (mark zombie, notify main agent)        │
│  - Workflow executor (DAG scheduler, checkpointing)           │
│  - The ONLY communication bus. No agent talks to another.    │
└────────────┬────────────────┬────────────────┬───────────────┘
             │                │                │
    ┌────────▼────────┐ ┌──────▼──────┐ ┌───────▼────────┐
    │  AGENT POOL     │ │  HARNESS    │ │   MEMORY      │
    │  (generic       │ │  (isolated  │ │   SYSTEM      │
    │   worker x N)   │ │   execution)│ │  (context     │
    │                 │ │             │ │   assembly)   │
    │ - planner       │ │ - workspace │ │               │
    │ - coder         │ │ - git track │ │ - temp        │
    │ - reviewer      │ │ - diff gen  │ │ - persistent  │
    │ - critic        │ │ - sandbox   │ │ - semantic    │
    │ - meta          │ │             │ │               │
    └─────────────────┘ └─────────────┘ └───────────────┘
```

## Communication Rules (ABSOLUTE)

1. **All messages use envelope.json.** No exceptions.
2. **Kernel is the only router.** Agents do not know each other's addresses.
3. **Main agent is the only user-facing entity.** No other agent speaks to the user.
4. **Harness is the only code executor.** No agent runs unsandboxed code.
5. **Dashboard is read-only until Phase 8.** It observes, does not control.

## Data Flow

```
User Goal
    ↓
Main Agent → LLM call → Workflow JSON
    ↓
Kernel validates → Stores in registry → Begins DAG execution
    ↓
For each step:
    - Resolve dependencies
    - Spawn/select agent (or reuse persistent)
    - Assemble preamble (identity + environment + capabilities + constraints + memory + task)
    - Send envelope to agent
    - Agent executes thinking loop → returns result envelope
    - Kernel checkpoints → proceeds to next step
    ↓
On failure:
    - Kernel emits event to Main Agent
    - Main Agent decides: retry / mutate config / spawn replacement / escalate
    - If mutate: kernel updates agent manifest, re-runs step
    ↓
On completion:
    - Kernel emits event to Main Agent
    - Main Agent summarizes for user
    - Dashboard updates workflow graph
```

## Environment Injection (Preamble Assembly)

Every envelope the kernel sends to an agent carries a preamble assembled from these layers, in order:

1. **System prompt** (from `prompts/{role}_system_v{ver}.md`)
2. **Identity block** — agent_id, role, instance_id
3. **Environment block** — working_directory, time, locale, sandbox, integrations, branch_info, plan_context, heartbeat. See `environment.md` for the full schema.
4. **Capabilities block** — positive list of allowed actions, generated from manifest
5. **Constraints block** — hard rules, generated from manifest
6. **Memory** — scratchpad (rw), session events (ro), semantic hits (ro)
7. **Loaded skills** — pre-rendered summaries, full text available via `skill.read`
8. **Current task** — the envelope payload
9. **Output contract** — which JSON shape the agent must respond with

The agent sees all of this as a single message. It does not know the boundaries. The kernel may re-emit any layer as it changes (clock tick, plan transition, budget update).

## Plan-Approval Flow (Kernel-Mediated)

The Main Agent's transition from `planning` to `standard` mode is **kernel-mediated**, not agent-decided. The flow:

```
User sends goal
    ↓
Kernel: parse goal, set Main Agent mode = "planning", emit goal envelope
    ↓
Main Agent: explore, ask, draft plan
    ↓
Main Agent: emit {"type": "plan_ready", "plan": {...}, "questions": [...]}
    ↓
Kernel: validate plan schema, persist to plans/{plan_id}.md
Kernel: emit event "plan_pending_approval" to dashboard
Kernel: block all workflow execution until approval
    ↓
User: reviews plan in dashboard → approve | edit | reject
    ↓
On approve: Kernel flips mode to "standard", injects plan into Main Agent preamble, begins execution
On edit:    Kernel re-emits user feedback to Main Agent (mode stays "planning")
On reject:  Kernel archives plan with status="rejected", Main Agent may re-plan
```

The Main Agent **cannot** override this. Even if it tries to emit a workflow JSON while in `planning` mode, the kernel rejects it. The only way to execute is to pass through user approval.

## Integration Registry

Beyond the built-in tools (fs, shell, git, memory, etc.), the kernel maintains an **integration registry**: named, capability-scoped bundles that agents can declare in their manifest.

```
┌──────────────────────────────────────────────────────┐
│                Integration Registry                   │
├──────────────────────────────────────────────────────┤
│ ID              TYPE        CAPABILITIES     STATUS   │
│ github-prod     vcs         r/w/pr          ready    │
│ supabase-main   database    r/w/migrate     ready    │
│ stripe-live     payment     charge/refund   ready    │
│ sentry-prod     observability  read         degraded │
└──────────────────────────────────────────────────────┘
```

- Credentials live in the **kernel vault**. Agents receive scoped, expiring tokens at tool-call time, never raw secrets.
- Adding an integration: register in vault, add entry to integration manifest, restart kernel.
- Removing an integration: any in-flight agent call fails with `integration_unavailable` event; the workflow pauses; user decides.
- An agent without a needed integration may `request_capability`. Main Agent can grant by spawning a worker with the integration in its manifest.

## Branch-Aware Workflows

If `working_directory` is a git repo (typical for coding workflows), the kernel injects `branch_info` into the environment block. Rules:

- Worker agents operate on the branch declared in their manifest. They may commit but not push, not merge, not delete branches.
- Creating a PR is a kernel-mediated action: agent emits `{"type": "request_pr", "branch": "...", "title": "...", "body": "..."}`, kernel handles the API call and emits the result.
- Merging requires explicit user action via the dashboard (no agent can auto-merge to `main`).
- The Main Agent can never `git push` or `git merge` directly. All VCS side effects go through the integration's `create_pr` capability.
