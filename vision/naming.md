# Naming Conventions

## Files & Directories

| Pattern | Example | Purpose |
|---------|---------|---------|
| `src/contracts/*.json` | `envelope.json` | Immutable schemas |
| `src/kernel/*.py` | `bus.py`, `registry.py` | Gateway infrastructure |
| `src/agents/*.py` | `main_agent.py` | Orchestrator logic |
| `src/agent_worker.py` | — | Single generic worker executable |
| `src/loops/*.py` | `direct.py`, `reflection.py` | Thinking loop implementations |
| `src/loops/primitives.py` | — | Reusable reasoning primitives |
| `src/harness/*.py` | `workspace.py`, `executor.py` | Sandboxed execution |
| `src/memory/*.py` | `temporary.py`, `persistent.py` | Context stores |
| `src/dashboard/backend/*.py` | `main.py` | FastAPI server |
| `src/dashboard/frontend/src/` | `App.tsx` | React app |
| `prompts/*.md` | `main_system.md`, `coder_system.md` | Agent system prompts |
| `manifests/*.json` | `main-agent.json`, `coder-fast.json` | Agent configurations |
| `skills/{skill_id}/SKILL.md` | `skills/python/SKILL.md` | Loadable expertise |
| `workspaces/{workflow_id}/` | — | Isolated execution directories |
| `heartbeats/{agent_id}.json` | — | Liveness files |
| `plans/{plan_id}.md` | `plan-7c9a.md` | Active and archived plans (YAML frontmatter + body) |
| `plans/active_plan.md` | — | Symlink or pointer to currently approved plan |
| `plans/phase_{N}_plan.md` | `phase_3_plan.md` | Phase implementation plans, human-reviewed |
| `demos/phase_{N}_demo.md` | `phase_3_demo.md` | Per-phase demo scripts (command + expected output) |
| `scratchpads/{agent_id}/{task_id}.json` | — | Persistent scratchpads for meta/specialist agents |
| `integrations/{integration_id}.json` | `supabase-main.json` | Integration manifests (type, capabilities, scope) |
| `integrations/registry.json` | — | Kernel-loaded integration index |

## Agent IDs

- `main-agent` (orchestrator, singleton)
- `{role}-{descriptor}-{tier}` e.g. `coder-python-fast`, `reviewer-security-standard`
- Temporary agents: `{role}-{descriptor}-{uuid4}`

## Envelope Types

- `request` / `response` — agent↔agent task delegation
- `event` — fire-and-forget notifications
- `error` — failure reports
- `heartbeat` — liveness ping
- `chunk` — streaming partial output
- `intent` — main agent directive to kernel
