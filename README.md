# OpenSwarm

> A contract-first, observable, self-healing agent swarm with a sandboxed coding harness.

OpenSwarm is a multi-agent runtime built around three hard rules:

1. **Contracts before code.** Every message, every agent, every workflow is
   schema-validated. If it doesn't conform to the contract, it doesn't exist.
2. **The kernel is the only bus.** Agents do not know each other's addresses.
3. **Harness is the only executor.** No agent runs unsandboxed code.

The system is being built bottom-up, one phase at a time. Each phase is
tested before the next begins, and a working demo on real hardware is the
gate to moving on.

---

## Why

Current AI agent systems tend to fall into one of three failure modes:

- **Monolithic** — one brain does everything, context explodes, quality drops.
- **Chaotic** — multi-agent with no contracts, agents talk past each other.
- **Black-box** — you can't see what happened, can't debug, can't improve.

OpenSwarm is the alternative: a swarm you can **observe**, **debug**, and
**replace piece by piece**.

See [`vision/manifesto.md`](vision/manifesto.md) for the full position.

---

## Architecture

```
                 USER / DASHBOARD  (Phase 8: React + WebSocket)
                            │
                            │ REST / WS
                            ▼
                 MAIN AGENT  (orchestrator, singleton)
                 - never executes tools
                 - never writes files
                 - emits workflow JSON + natural language
                            │
                            │ Envelope protocol (WebSocket)
                            ▼
                       KERNEL  (gateway)
                 - message router (priority queue)
                 - agent registry (SQLite)
                 - permission enforcer (default deny)
                 - heartbeat monitor (zombie detection)
                 - workflow executor / DAG scheduler
                            │
        ┌───────────────────┼───────────────────┐
        ▼                   ▼                   ▼
   AGENT POOL          HARNESS             MEMORY
   (generic            (isolated           (context
    worker × N)         execution)          assembly)
   - planner           - workspace         - temp
   - coder             - git track         - persistent
   - reviewer          - diff gen          - semantic
   - critic            - Docker sandbox
   - meta
```

Communication rules (absolute, see `vision/architecture.md`):

1. All messages use [`envelope.json`](src/contracts/envelope.json).
2. The kernel is the only router.
3. The main agent is the only entity that talks to the user.
4. The harness is the only code executor.
5. The dashboard is read-only until Phase 8.

---

## Project Status

Phases 0–5 are complete and tested. Phases 6–11 are planned.

| Phase | Name | Status |
|------:|------|:------:|
| 0 | Contracts (`envelope.json`, `manifest.json`, `workflow.json`) | ✅ |
| 1 | Kernel — message router, registry, permission enforcer, heartbeat monitor | ✅ |
| 2 | Main Agent — user-facing orchestrator, objective parsing, status query | ✅ |
| 3 | Generic Agent Worker + Model Router with fallback chain | ✅ |
| 4 | Thinking Loops — primitives, premade loops, dynamic assembly, scoring | ✅ |
| 5 | Coding Harness — workspace, sandboxed executor, git tracker, diff streaming | ✅ |
| 6 | Memory & Context Assembly | ⏳ |
| 7 | Dashboard Backend (event stream) | ⏳ |
| 8 | Dashboard Frontend (React, workflow DAG) | ⏳ |
| 9 | Self-Healing & Workflow Recovery | ⏳ |
| 10 | Dynamic Loop Assembly & Trial/Error | ⏳ |
| 11 | Polish & Scale (Redis, auth, marketplace) | ⏳ |

**Test status:** 467/467 passing on `pytest src/`.

---

## Repo Layout

```
openswarm/
├── conftest.py              # src/ import shim for pytest
├── pyproject.toml           # pytest config (asyncio_mode = "auto")
├── requirements.txt
├── contracts/               # JSON schemas (the "prison walls")
├── manifests/               # agent configurations
│   ├── main-agent.json
│   ├── conductor.json
│   ├── coder-python-fast.json
│   ├── reviewer-security-powerful.json
│   ├── researcher-web-standard.json
│   └── sector-manager-template.json
├── prompts/                 # agent system prompts
│   ├── main_agent_system.md
│   ├── conductor_system.md
│   └── sector_manager_system.md
├── src/
│   ├── agent_worker.py      # ONE executable for all non-orchestrator agents
│   ├── kernel/              # gateway control plane (Phase 1)
│   │   ├── bus.py           # priority queue + router
│   │   ├── registry.py      # SQLite agent registry
│   │   ├── permissions.py   # default-deny enforcer
│   │   ├── heartbeat.py     # zombie detection
│   │   ├── api.py           # FastAPI REST surface
│   │   ├── websocket.py     # WS surface
│   │   ├── config.py        # pydantic-settings
│   │   └── models.py        # Envelope, Manifest, Permissions
│   ├── agents/              # Phase 2: orchestrator + specialists
│   │   ├── main_agent.py
│   │   ├── conductor.py
│   │   ├── sector_manager.py
│   │   ├── base_agent.py
│   │   ├── llm_client.py
│   │   └── objective_parser.py
│   ├── loops/               # Phase 3 + 4: thinking loops
│   │   ├── primitives.py    # generate, critique, vote, revise, branch, merge
│   │   ├── assembler.py     # executes a LoopGraph (DAG)
│   │   ├── graph.py         # LoopGraph / LoopNode / LoopEdge
│   │   ├── model_router.py  # fallback chain
│   │   ├── router.py        # mode → loop selector
│   │   ├── direct.py        # single LLM call
│   │   ├── cot.py           # chain-of-thought
│   │   ├── reflection.py    # generate → critique → revise
│   │   ├── tree.py          # tree of thoughts
│   │   ├── debate.py        # two opposing views + vote
│   │   ├── ensemble.py      # N models, vote
│   │   ├── tool_executor.py # dispatch harness.* tools via HarnessClient
│   │   ├── preamble_assembler.py
│   │   ├── registry.py      # loop template registry
│   │   ├── optimizer.py     # loop ranking
│   │   └── meta_stub.py     # placeholder for Phase 10 meta-agent
│   └── harness/             # Phase 5: sandboxed execution
│       ├── workspace.py     # WorkspaceManager + Workspace
│       ├── executor.py      # CodeExecutor (SubprocessBackend + DockerBackend)
│       ├── git_tracker.py   # auto-commit per workflow
│       ├── diff_generator.py # KernelEventSink + RecordingSink
│       ├── server.py        # FastAPI app for harness tools
│       ├── client.py        # HarnessClient + InProcessHarnessClient
│       └── Dockerfile.harness
└── vision/                  # design docs (read these first)
    ├── manifesto.md
    ├── architecture.md
    ├── agent-model.md
    ├── environment.md
    ├── prompt-engineering.md
    ├── thinking-loops.md
    ├── security.md
    ├── self-healing.md
    ├── dashboard-vision.md
    ├── testing.md
    ├── naming.md
    └── phases.md
```

---

## Quick Start

```bash
# 1. Install
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 2. Run the test suite
pytest src/

# 3. Start the kernel (in one terminal)
python -m kernel.main

# 4. Start a generic worker (in another terminal)
AGENT_MANIFEST_PATH=manifests/coder-python-fast.json \
  KERNEL_WS=ws://127.0.0.1:8765/ws \
  python src/agent_worker.py

# 5. (Phase 2) Start the main agent (in a third terminal)
python -m agents.main_agent
```

By Phase 8 the dashboard takes the place of the manual `python -m` calls.

---

## The Kernel in 60 Seconds

The kernel is a Python process that owns:

- a **WebSocket** server at `/ws` for agent connections
- a **REST** API at `/registry`, `/plans`, etc.
- a **SQLite** agent registry
- a **priority queue** per agent with a default-deny **permission enforcer**
- a **heartbeat monitor** that marks agents `zombie` after a configurable timeout

Every message crossing the kernel is an [`Envelope`](src/contracts/envelope.json)
with `envelope_type` ∈ `{request, response, event, error, heartbeat, chunk, intent}`.
The kernel rejects envelopes that don't validate; agents that go silent are
reaped and a `loop_detected`/`agent_zombie` event is emitted to the main agent.

See `src/kernel/` for the implementation and `vision/architecture.md` for the
full data flow.

---

## The Main Agent in 60 Seconds

The main agent is a singleton, user-facing orchestrator. It:

1. accepts a user message (CLI or WebSocket),
2. parses it into a `StructuredObjective`,
3. dispatches a `spawn_initial_swarm` envelope to the **Conductor**,
4. subscribes to kernel events (`agent_zombie`, `permission_denied`, etc.)
   and surfaces them in natural language.

It **never** executes tools, **never** writes files, **never** runs code.
All real work is delegated to the conductor → sector managers → workers.

See `src/agents/main_agent.py`.

---

## The Harness in 60 Seconds

The harness is the only place code actually runs. Each workflow gets:

- a **`Workspace`** directory under `data/workspaces/{workflow_id}/`
  (a real git repo so every change is a commit),
- a **`CodeExecutor`** with one of two backends:
  - **`SubprocessBackend`** for tests and local dev,
  - **`DockerBackend`** for production, with `--network=none`, `--read-only`,
    `--cap-drop=ALL`, `--security-opt=no-new-privileges`, and a 512MB / 30s
    timeout (OOM detection via exit code 137).
- a **`GitTracker`** that auto-commits every change with a deterministic
  `|||BODY|||` separator so the log parser never collides with NULs,
- a **`DiffGenerator`** that streams each step's diff through a
  `KernelEventSink` (production) or a `RecordingSink` (tests).

The seven harness tools are exposed both as a **FastAPI** server
(`harness.server.create_app`) and an in-process client
(`harness.client.InProcessHarnessClient` for tests).

The agent-side `ToolExecutor` (`src/loops/tool_executor.py`) dispatches
`harness_exec`, `harness_write_file`, `harness_read_file`,
`harness_list_files`, `harness_reset`, `harness_get_history`,
`harness_get_diff` and validates the per-task `permissions` dict
(filesystem globs, network allowlist, harness runtime allowlist).

See `src/harness/`.

---

## Thinking Loops in 60 Seconds

Reasoning is a **composable pipeline of primitives**:

| Primitive | Description | Cost |
|-----------|-------------|:----:|
| `generate` | Single LLM call, returns output | 1× |
| `critique` | LLM evaluates another output | 1× |
| `vote` | LLM selects best from N candidates | 1× |
| `revise` | LLM rewrites output based on critique | 1× |
| `branch` | Generate N parallel candidates | N× |
| `merge` | Combine multiple outputs into one | 1× |

Premade loops:

- **direct** — `generate → output`
- **cot** — `generate("Think step by step…") → output`
- **reflection** — `generate → critique → revise → output`
- **tree** — `branch(3) → vote → merge(best) → output`
- **debate** — `branch(2 opposing) → critique(each) → vote → merge`
- **ensemble** — N models in parallel, vote

Dynamic assembly is JSON-driven (`vision/thinking-loops.md`):

```python
from loops import LoopGraph, LoopAssembler, LLMClient

graph = LoopGraph.from_dict({
    "id": "draft-check-fix",
    "name": "Dynamic Reflection",
    "description": "Generate → critique → revise",
    "nodes": [
        {"id": "draft", "primitive": "generate", "model": "gpt-4o-mini"},
        {"id": "check", "primitive": "critique", "model": "claude-sonnet"},
        {"id": "fix",   "primitive": "revise",   "model": "gpt-4o"},
    ],
    "edges": [
        {"from": "draft", "to": "check"},
        {"from": "check", "to": "fix"},
    ],
})

assembler = LoopAssembler()
result = await assembler.execute(graph, task, preamble, LLMClient(["gpt-4o-mini"]))
```

`LoopGraph.from_dict` accepts the `from`/`to` shorthand on edges and
auto-derives `entry_node` and `terminal_nodes` when they're omitted.

See `src/loops/`.

---

## Security Model

The kernel is a **default-deny** permission enforcer:

1. If a tool/path/host is not in the sender's manifest, it's denied.
2. If a pattern appears in both `permissions.file_system.deny` and
   `permissions.file_system.allow`, **deny wins**.
3. A tool's declared `side_effects` must correspond to a permission the
   agent holds. New in Phase 5: `harness:execute` and `harness:workspace`
   map to `permissions.harness.can_execute_code` and
   `permissions.harness.can_access_workspace`.
4. Every check (allow or deny) is appended to the `audit_log` table with
   envelope_id, agent_id, reason, and timestamp.

The harness runs untrusted code in Docker with:

- `--network=none` (or a tightly-scoped allowlist),
- `--read-only` root filesystem with a writable workspace overlay,
- `--cap-drop=ALL`, `--security-opt=no-new-privileges`,
- a per-step USD budget enforced kernel-side (not agent-reported),
- kernel-side **loop detection** on the envelope stream (the agent
  cannot lie its way out of a detected loop).

See `vision/security.md` and `src/kernel/permissions.py:1`.

---

## Testing

```bash
# Run the full suite
pytest src/

# Run just the kernel
pytest src/kernel/tests/

# Run just the harness
pytest src/harness/

# Run with a specific test pattern
pytest src/loops/tests/test_assembler.py -v

# Show the slowest tests
pytest src/ --durations=10
```

Conventions:

- `asyncio_mode = "auto"` — async tests are picked up automatically.
- Each test file lives next to its module under `tests/`.
- `pytest_asyncio.fixture` is used for async fixtures.
- The `conftest.py` shim at the repo root makes `src/` importable.

Test counts at the time of writing:

- 40 kernel tests (bus, heartbeat, permissions, registry, integration)
- 60+ thinking-loop tests (primitives, assembler, graph, router, loops)
- 100+ harness tests (workspace, executor, git tracker, diff generator,
  server, client, end-to-end integration)
- 8 agent worker tests (registration, message loop, spawn, heartbeat)
- 200+ agent tests (main agent, conductor, sector manager, base agent,
  LLM client, objective parser)

---

## Vision & Design Docs

Before changing anything, read the relevant vision doc. They are the
single source of truth for *why* the system is shaped the way it is.

| Doc | Topic |
|-----|-------|
| [`manifesto.md`](vision/manifesto.md) | Why this exists, what we are NOT |
| [`architecture.md`](vision/architecture.md) | High-level diagram, data flow, plan-approval flow, integration registry, branch-aware workflows |
| [`agent-model.md`](vision/agent-model.md) | Agent fields, lifecycle, agent types, scratchpad, integrations, plan_role |
| [`environment.md`](vision/environment.md) | The deterministic `ENVIRONMENT` block the kernel injects into every preamble |
| [`prompt-engineering.md`](vision/prompt-engineering.md) | Preamble structure, scratchpad protocol, planning vs standard mode, refusal format |
| [`thinking-loops.md`](vision/thinking-loops.md) | Primitives, premade loops, dynamic assembly, scoring |
| [`security.md`](vision/security.md) | Threat model, permission rules, harness sandbox, plan-approval security |
| [`self-healing.md`](vision/self-healing.md) | Failure taxonomy, recovery hierarchy, loop detection, cost ceiling |
| [`dashboard-vision.md`](vision/dashboard-vision.md) | What the dashboard shows, who controls what |
| [`testing.md`](vision/testing.md) | Unit / integration / load / chaos / refusal / loop-detection tests |
| [`naming.md`](vision/naming.md) | File, agent_id, envelope-type conventions |
| [`phases.md`](vision/phases.md) | Phase breakdown, dependencies, plan-before-code and demo-before-next rules |

---

## Contributing

Two hard rules from `vision/phases.md`:

1. **Plan Before Code.** Before writing any phase's code, produce a
   `plans/phase_{N}_plan.md` covering files to be touched, the full
   preamble assembly the new agents will receive, the JSON contracts
   consumed and produced, the test strategy, and a demo script.
2. **Demo Before Next Phase.** Every phase ends with a working demo on
   real hardware (a shell command the human runs that produces real,
   visible output). Tests catch correctness; demos catch "this is a
   science project."

Tests are not optional. The current bar is **467/467 passing** — new
work that drops a test gets fixed or reverted, not merged around.

---

## License

TBD.
