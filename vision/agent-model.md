# Agent Model

## Every Agent Has

| Field | Purpose |
|-------|---------|
| `agent_id` | Kebab-case unique identifier |
| `intent` | Single-sentence purpose. Main agent uses this to select agents for workflow steps. |
| `role` | `orchestrator` (main only), `executor` (does work), `specialist` (domain expert), `critic` (judges output), `meta` (assembles loops) |
| `category` | UI grouping: coding, planning, review, research, testing, deployment, analysis, custom |
| `tags` | Searchable labels for agent selection and dashboard filtering |
| `capabilities` | What models, tools, skills, protocols it supports |
| `permissions` | What it can read, write, execute, access |
| `lifecycle` | How long it lives, what happens when it dies |
| `thinking_profile` | Available reasoning loops, whether dynamic assembly is allowed |
| `model_tier` | fast / standard / powerful, with optional cost budget |
| `integrations` | External capabilities (databases, VCS, payments) the agent can call. Kernel-resolved credentials. |
| `plan_role` | For main agent only: `planner | executor | both`. Defaults to `both`. |
| `scratchpad` | Storage location for the agent's internal reflections (per §11 of prompt-engineering.md) |

## Agent Lifecycle States

```
initializing → ready → busy → idle → draining → offline
     ↑___________|     |      |      |            |
     |__________________|      |      |            |
     error ←──────────────────┘      |            |
     zombie ←────────────────────────┘            |
     (auto-restart if configured) ←──────────────┘
```

## Agent Types

### 1. Orchestrator (Main Agent)
- **Singleton.** Only one exists per swarm.
- **No tools.** No file access. No code execution.
- **Input:** User goals, kernel events (step_complete, agent_zombie, permission_denied, workflow_resume).
- **Output:** Workflow JSON, spawn_request envelopes, natural language to user.
- **Decision engine:** LLM call with strict system prompt. Must output valid JSON decisions.

### 2. Executor (Generic Worker)
- **Many instances.** One executable, many manifests.
- **Boot sequence:** Load manifest → Register with kernel → Start heartbeat → Enter message loop.
- **Message loop:** Pop envelope → Validate permissions → Route to thinking loop → Execute → Return result.
- **No hardcoded logic.** Behavior is 100% determined by manifest + preamble.

### 3. Specialist
- **Domain-tuned executors.** Same code as executor, different manifest (e.g. `python-coder`, `frontend-reviewer`).
- **May have custom skills.** Loaded from `skills/{skill_id}/SKILL.md` into preamble.

### 4. Critic
- **No tools.** Pure LLM judge.
- **Input:** Agent output + rubric.
- **Output:** Score 1-10 + critique text.
- **Used by:** Loop optimizer to score thinking loop effectiveness.

### 5. Meta-Agent
- **Assembles thinking loops.** Receives task descriptions, outputs loop graph JSON.
- **Does not execute loops.** Only designs them.
- **Output stored in** `loop_templates` table for main agent approval.

### 6. Planner (subtype of Main Agent)
- **Operates in `planning` mode only.** See `prompt-engineering.md` §10.
- **No tools, no file writes, no execution.** Reads, searches, asks, plans.
- **Output is a plan document** conforming to the schema in `prompt-engineering.md` §12.
- **Always paired with a user approval step** before any execution begins.
- The Main Agent's `plan_role: "planner" | "executor" | "both"` field declares whether it can plan, execute, or both. Default: `both` (plan once, then execute the approved plan).

## Integrations

An agent may declare external integrations in its manifest. An integration is a named, capability-scoped bundle (e.g. `supabase-main`, `github-prod`, `stripe-live`). The kernel resolves credentials from a vault; the agent never sees them.

```json
{
  "integrations": [
    {
      "id": "supabase-main",
      "type": "database",
      "capabilities": ["read", "write", "schema-migrate"],
      "credential_source": "kernel_vault",
      "scope": "project:openswarm"
    }
  ]
}
```

- Integrations are **per-agent** (declared in manifest) and **per-workflow** (kernel re-validates at spawn).
- Adding/removing an integration is a **manifest edit**, not a runtime prompt action. No agent can enable integrations dynamically.
- The kernel maintains an integration registry; tool calls against integrations are schema-validated like any other tool.
- An agent without a needed integration may `request_capability`. Main Agent decides whether to spawn a worker with the integration or refuse.

## Scratchpad Storage

Every agent has a `scratchpad` field in its manifest pointing to a storage backend. The scratchpad holds the agent's internal reflections (see `prompt-engineering.md` §11).

| Backend | When to use | Lifetime | Kernel guarantees |
|---------|-------------|----------|-------------------|
| `memory` (in-process) | Default for short tasks | Per task | Wiped on task end |
| `sqlite` (kernel DB) | Multi-step tasks, audit-required | Per workflow | Persists in `scratchpads` table, queryable for replay |
| `file` (per-agent JSON) | Long-running specialists | Persistent | Append-only, git-tracked |

Worker agents default to `memory`. Main Agent uses `sqlite`. Meta-agents use `file` (their scratchpads become loop template history).

## Plan Approval as Agent Capability

The plan-approval workflow is a **first-class Main Agent capability**, not a UI feature. The Main Agent's `plan_role: "both"` (default) means it can plan, hand off to user, and then execute.

A separate **Planner-only** agent (`plan_role: "planner"`) can be deployed when you want a hard separation: a planning LLM that cannot execute, and an executing Main Agent that cannot plan. Use this for high-stakes or regulated workflows.

## Agent Selection (Main Agent Logic)

When main agent builds a workflow, it selects agents by:

1. **Intent matching:** Which agents have `intent` semantically closest to the step goal?
2. **Category filtering:** Step type → agent category.
3. **Model tier:** Budget-conscious steps → `fast` tier. Complex steps → `powerful` tier.
4. **Availability:** Skip `busy` or `zombie` agents unless `spawn_fresh` is set.
5. **Tag matching:** Specific requirements (e.g. `python`, `react`, `legacy`) filter candidates.
