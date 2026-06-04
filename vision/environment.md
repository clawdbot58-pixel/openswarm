# Environment Injection Contract

## Purpose

Every agent preamble carries a deterministic `ENVIRONMENT` block assembled by the kernel. This block is the agent's only authoritative source for: where it runs, what time it is, what it can touch, what integrations are wired, and what plan (if any) it is executing under.

The agent must **not** infer these from context. It reads them. This eliminates an entire class of "I assumed the working dir was X" failures.

---

## Schema

```yaml
environment:
  # Identity
  agent_id: "string — kebab-case, unique within swarm"
  agent_role: "orchestrator | executor | specialist | critic | meta"
  instance_id: "uuid — unique per running instance"
  
  # Filesystem & sandbox
  working_directory: "string — absolute path, agent's writable root"
  read_only_roots: ["string — paths readable but not writable"]
  sandbox_type: "none | docker | firejail | gvisor"
  sandbox_id: "string — container or jail ID, if applicable"
  filesystem_writable: bool
  
  # Network
  network:
    enabled: bool
    allow: ["hostname or CIDR"]   # explicit allowlist; empty if disabled
    deny: ["hostname or CIDR"]    # explicit denylist; applied first
  
  # Time & locale
  kernel_time: "ISO-8601 UTC, e.g. 2026-06-04T13:26:00Z"
  user_locale: "BCP-47, e.g. en-US, fr-FR, ja-JP"
  user_timezone: "IANA, e.g. America/Los_Angeles"
  user_language: "BCP-47 — working language for user-facing output"
  
  # Models
  model_tier_chain: ["primary", "fallback1", "fallback2"]   # ordered
  cost_budget_remaining_usd: number
  cost_budget_per_step_usd: number
  
  # Capabilities (positive list, see prompt-engineering.md §18)
  capabilities:
    fs:
      read: ["glob patterns"]
      write: ["glob patterns"]
    shell:
      allow: ["command patterns"]
      deny: ["command patterns — applied first, wins ties"]
    tools: ["tool_name", ...]
    integrations: ["integration_id", ...]
    skills: ["skill_id", ...]
  
  # Integrations (resolved credentials are NOT exposed)
  integrations:
    - id: "string"
      type: "database | vcs | messaging | payment | custom"
      capabilities: ["read", "write", ...]
      status: "ready | degraded | unavailable"
  
  # Git / branch context (if working_directory is a repo)
  branch_info:
    branch: "string"
    last_commit: "sha"
    dirty: bool
    base_branch: "string — what to diff/merge against"
  
  # Plan context (null if not under a plan)
  plan_context:
    plan_id: "string"
    goal: "string"
    current_step_id: "string | null"
    steps_remaining: number
    approved_at: "ISO-8601"
  
  # Heartbeat & lifecycle
  heartbeat_interval_s: number
  heartbeat_deadline: "ISO-8601 — miss this and you're a zombie"
  lifecycle: "ephemeral | session | persistent"
  
  # Kernel connection
  kernel_uri: "ws://... or wss://..."
  envelope_inbox: "channel name for inbound messages"
```

---

## Assembly Rules

The kernel assembles the block at agent boot, then re-emits it whenever any field changes (clock tick for `kernel_time`, budget updates, sandbox state, plan transitions). The agent does not need to poll; the block is in every preamble.

**Source of truth priority** (highest wins):
1. Manifest (capabilities, integrations, model_tier_chain)
2. Active plan (plan_context)
3. Live runtime state (sandbox_id, branch_info, kernel_time)
4. User preferences (user_locale, user_language)
5. Defaults (heartbeat_interval_s = 10, cost_budget = ∞)

**What the kernel NEVER puts in the environment block**:
- Raw credentials, tokens, API keys. The agent receives a scoped, expiring access token at tool-call time, not in the preamble.
- Other agents' preambles. Agents cannot read each other's state directly.
- The user's full history. Only the slice relevant to the current workflow.
- Secrets the user has marked private. Kernel pre-filters.

---

## Agent Responsibilities

When the agent reads its environment block, it must:

1. **Verify capabilities match intent.** If the task requires `shell.exec` and it's not in `capabilities.shell.allow`, emit `request_capability` — do not attempt the call.
2. **Check `sandbox_type` before any I/O.** If `sandbox_type != "none"`, assume filesystem and network may be restricted even if not explicitly listed.
3. **Use `working_directory` as the root for relative paths.** Never `cd ..` out of it.
4. **Use `kernel_time` for any timestamp.** Do not infer the time from the model's training data.
5. **Honor `cost_budget_remaining_usd`.** If a tool call would exceed it, stop and emit `budget_exhausted` rather than overrunning.
6. **Respect `plan_context`.** If `current_step_id` is set, the agent's job is that step. Do not freelance outside the plan.
7. **Treat `user_language` as authoritative** for user-facing text. The system prompt's English template is a default, not a requirement.

---

## Examples

### Minimal (test agent)

```yaml
environment:
  agent_id: "test-1"
  agent_role: "executor"
  instance_id: "uuid-1"
  working_directory: "/tmp/swarm-test"
  read_only_roots: []
  sandbox_type: "none"
  filesystem_writable: true
  network: { enabled: true, allow: ["*"], deny: [] }
  kernel_time: "2026-06-04T13:26:00Z"
  user_locale: "en-US"
  user_timezone: "America/Los_Angeles"
  user_language: "en"
  model_tier_chain: ["gpt-4o-mini"]
  cost_budget_remaining_usd: 1.00
  cost_budget_per_step_usd: 0.10
  capabilities:
    fs: { read: ["/tmp/swarm-test/**"], write: ["/tmp/swarm-test/**"] }
    shell: { allow: ["echo", "ls"], deny: [] }
    tools: []
    integrations: []
    skills: []
  integrations: []
  branch_info: null
  plan_context: null
  heartbeat_interval_s: 30
  heartbeat_deadline: "2026-06-04T13:26:30Z"
  lifecycle: "ephemeral"
  kernel_uri: "ws://localhost:7878/agent/test-1"
  envelope_inbox: "in.test-1"
```

### Full (production coder agent with integrations)

```yaml
environment:
  agent_id: "coder-python-standard"
  agent_role: "executor"
  instance_id: "uuid-2"
  working_directory: "/workspaces/feature-42"
  read_only_roots: ["/libs", "/shared"]
  sandbox_type: "docker"
  sandbox_id: "docker-abc123"
  filesystem_writable: true
  network:
    enabled: true
    allow: ["pypi.org", "github.com", "api.openai.com"]
    deny: []
  kernel_time: "2026-06-04T13:26:00Z"
  user_locale: "fr-FR"
  user_timezone: "Europe/Paris"
  user_language: "fr"
  model_tier_chain: ["claude-sonnet-4", "gpt-4o", "gpt-4o-mini"]
  cost_budget_remaining_usd: 4.50
  cost_budget_per_step_usd: 0.50
  capabilities:
    fs:
      read: ["/workspaces/feature-42/**", "/libs/**", "/shared/**"]
      write: ["/workspaces/feature-42/**"]
    shell:
      allow: ["python", "pytest", "git", "pip", "ls", "cat"]
      deny: ["rm -rf /", "curl | sh", "wget | bash", "eval *"]
    tools: ["fs.read", "fs.write", "shell.exec", "git.diff", "git.commit", "memory.query"]
    integrations:
      - id: "github-prod"
      - id: "supabase-main"
    skills: ["python", "pytest", "django"]
  integrations:
    - { id: "github-prod", type: "vcs", capabilities: ["read", "write", "create_pr"], status: "ready" }
    - { id: "supabase-main", type: "database", capabilities: ["read", "write", "schema-migrate"], status: "ready" }
  branch_info:
    branch: "feature/42-login-page"
    last_commit: "a1b2c3d"
    dirty: true
    base_branch: "main"
  plan_context:
    plan_id: "plan-7c9a"
    goal: "Build login page with email + OAuth"
    current_step_id: "step_3_implement"
    steps_remaining: 2
    approved_at: "2026-06-04T13:00:00Z"
  heartbeat_interval_s: 10
  heartbeat_deadline: "2026-06-04T13:26:10Z"
  lifecycle: "session"
  kernel_uri: "wss://kernel.openswarm.local/agent/coder-python-standard/instance-uuid-2"
  envelope_inbox: "in.coder-python-standard.uuid-2"
```

---

## What This Enables

- **Deterministic grounding.** The agent cannot hallucinate its filesystem, time, or capabilities. The block is the contract.
- **Audit trail.** Every decision an agent makes is logged with the exact environment block it had. Replay is faithful.
- **Migration.** Moving a workflow to a new machine is a matter of re-assembling the block, not re-prompting the agent.
- **Safety.** A misbehaving agent cannot escalate by guessing — capabilities are explicit, and the kernel rejects out-of-scope calls before they execute.
- **Localization.** The same prompt template serves every user because the environment carries their locale, not the prompt.
