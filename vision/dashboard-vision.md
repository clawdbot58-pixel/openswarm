# Dashboard Vision

## Philosophy

The dashboard is not a log viewer. It is the **swarm's nervous system made visible**.

You should be able to:
- See every agent's heartbeat in real-time
- Watch workflows execute as animated graphs
- Read agent "thoughts" (preambles, reasoning steps)
- Intervene (pause, kill, respawn, edit manifest) without touching code
- Replay any workflow from any checkpoint
- Compare agent performance across model/loop configurations

## Views

### 1. Swarm Overview (Default)
- Grid of agent cards: icon, name, status (color), model tier, current task
- Filter by: category, status, model, tags
- Click card → agent detail panel

### 2. Workflow DAG
- React-flow graph: nodes = steps, edges = dependencies
- Node colors: pending(gray) → running(blue) → success(green) → failed(red) → recovering(yellow)
- Click node → step detail: agent assigned, preamble, output preview, logs
- Animated: pulse on active, shake on error

### 3. Live Log Stream
- WebSocket feed of all envelope traffic
- Filter: by agent, by envelope_type, by workflow, by severity
- Search: full-text across payloads
- Color coding: request(blue), response(green), event(yellow), error(red)

### 4. Workspace Explorer
- File tree of active workflow workspace
- Click file → monaco-editor view
- Diff viewer: side-by-side before/after for every agent edit
- Git timeline: commit graph with agent attribution

### 5. Loop Laboratory (Phase 10+)
- Visual graph editor for thinking loops
- Drag primitives, connect edges
- Run A/B tests: same task, different loops, compare scores
- Leaderboard: loop templates ranked by score/cost/speed

### 6. Agent Factory (Phase 11+)
- Form to create new agent manifests
- Template gallery (clone from existing)
- Live preview of assembled preamble
- Deploy to swarm with one click

## Real-Time Requirements

- Heartbeat latency < 2s visible in UI
- Log stream: < 500ms from kernel to browser
- Workflow graph updates: per-step, not per-batch
- File changes: push via WebSocket, not poll

## Additional Views (v1+)

### 7. Plan Review (when plan_pending_approval event fires)

Modal or full-page takeover when a plan needs user approval.

```
┌──────────────────────────────────────────────────────┐
│  Plan: Build login page with email + OAuth           │
│  Status: PENDING APPROVAL                            │
│  Submitted by: main-agent @ 13:24 UTC                │
├──────────────────────────────────────────────────────┤
│  Goal: User wants a login page supporting email and  │
│        GitHub OAuth. Existing Django backend.        │
│                                                      │
│  Steps:                                              │
│   1. planner: scaffold Django OAuth app     [OK]    │
│   2. designer: produce UI mock (parallel)    [OK]    │
│   3. coder-python-standard: implement       [READY]  │
│   4. tester: unit + e2e tests               [BLOCKED]│
│   5. reviewer-security: audit               [BLOCKED]│
│   6. deployer: staging deploy               [BLOCKED]│
│                                                      │
│  Cost estimate: $0.18  |  Time estimate: 8 min       │
│  Open questions:                                     │
│    - Use django-allauth or roll-our-own?             │
│    - Which OAuth provider(s) to enable?              │
│                                                      │
│  Risks:                                              │
│    - Existing user table may need migration          │
│                                                      │
│  [Approve]  [Edit]  [Reject with feedback]           │
└──────────────────────────────────────────────────────┘
```

Backend endpoint: `GET /plans/{plan_id}` returns the plan YAML. `POST /plans/{plan_id}/decision` with `{action: "approve|edit|reject", feedback?: "..."}`. After approval, the plan becomes the workflow scaffold and execution begins.

### 8. Agent Scratchpad View (clicking a thinking bubble)

When a user clicks an agent's status update, side panel shows:

- Agent ID, role, current task
- Live scratchpad contents (updated via WebSocket, see `prompt-engineering.md` §11)
- Last 10 envelopes sent/received (collapsed by default)
- Current environment block (read-only, copyable)

The scratchpad is the agent's private reasoning. Showing it makes the agent debuggable without compromising the user-facing communication.

### 9. Integration Panel (admin only)

Lists the integration registry: each integration's ID, type, status, capabilities, last-used timestamp. Used to:

- See which workflows touched which integrations.
- Mark an integration `degraded` (which causes in-flight calls to fail safely).
- Rotate credentials (re-binds the vault entry, reissues tokens).

### 10. Loop Detection Alert (when `loop_detected` event fires)

Banner across the top of the dashboard. Click to expand: which agent, which pattern (action_repeat / clarification_spin / tool_failure_repeat / mutate_exhausted), last 3 envelopes, suggested action. The Main Agent's response is shown inline.

### 11. Budget & Cost Live View

Sidebar widget per workflow: current `cost_usd` vs `cost_budget_remaining_usd`, color-coded (green/yellow/red). Click for per-step breakdown. Lets the user intervene before budget_exhausted fires.

## Updated Log Stream Event Types

Add to the filter list (was §3): `plan_pending_approval`, `plan_approved`, `plan_rejected`, `loop_detected`, `budget_exhausted`, `integration_unavailable`, `step_timeout`, `step_recovered`.

## Tech Stack

- **Frontend:** React + Vite + TypeScript + Tailwind
- **Graph:** @xyflow/react (react-flow)
- **Editor:** Monaco Editor (VS Code core)
- **Diff:** diff2html or custom
- **Real-time:** WebSocket to dashboard backend
- **Backend:** FastAPI (Phase 7), read-only initially
