# OpenSwarm

> A contract-first, observable agent swarm with OpenClaw-style UX: talk on Telegram, observe on the dashboard, agents do the work.

OpenSwarm is a multi-agent runtime:

1. **Contracts before code** — every message is schema-validated.
2. **Kernel is the only bus** — agents never talk directly.
3. **Harness is the only executor** — code runs sandboxed.

Inspired by [OpenClaw](https://github.com/openclaw/openclaw) channel UX; design reference lives at `../openclaw-reference` on Desktop (not shipped in this repo).

---

## Quick start (human test)

```bash
./scripts/setup.sh
source .venv/bin/activate

# Edit .env — LLM keys + Telegram token
#   OPENSWARM_LLM_PROFILE=nim     # NVIDIA NIM, 24/7
#   OPENSWARM_LLM_PROFILE=ollama  # local fast iteration

openswarm start
```

You should get:

- **Telegram** — startup ping: *"OpenSwarm started"*. Then talk in plain English (no `/commands` required).
- **Dashboard** — http://127.0.0.1:8000/ui/ (agents, workflows, logs, workspace files)
- **Agent workspace** — `workspaces/agent/` (TASKBOARD.md, scripts, markdown — open in Finder)

```bash
openswarm status
openswarm run "Create a hello.py that prints today's date"
openswarm logs
openswarm stop
```

Smoke test everything:

```bash
./scripts/first-run.sh
```

---

## Architecture

```
  YOU (Telegram / CLI)
         │
         ▼
   MAIN AGENT ──► CONDUCTOR ──► WORKERS (coder, reviewer, …)
         │              │
         ▼              ▼
      KERNEL ◄──── HARNESS (sandboxed code in data/workspaces/)
         │
         ▼
   DASHBOARD (read-only observe at :8000/ui/)
```

| Port | Service |
|------|---------|
| 8765 | Kernel (REST + WebSocket) |
| 8000 | Dashboard (API + React UI) |

All paths in `config/openswarm.toml` are **relative** — the whole project is self-contained in one folder.

---

## Configuration

| File | Purpose |
|------|---------|
| `config/openswarm.toml` | Ports, workers, LLM profile (copy from `config/openswarm.example.toml`) |
| `.env` | Secrets: `NVIDIA_API_KEY`, `TELEGRAM_BOT_TOKEN`, `EXA_API_KEY` |
| `config/user.yaml` | Optional user preferences |
| `workspaces/agent/` | Your agent file workspace (taskboard, notes, outputs) |

### LLM profiles

| Profile | Use |
|---------|-----|
| `nim` | NVIDIA NIM — production / 24/7 |
| `ollama` | Local Ollama — fast testing |
| `auto` | NIM if key set, else Ollama if reachable, else mock |

Set in `config/openswarm.toml` under `[llm] profile = "auto"` or `OPENSWARM_LLM_PROFILE` in `.env`.

---

## Project layout

```
openswarm/
├── config/           # openswarm.toml, user.yaml
├── contracts/        # JSON schemas
├── manifests/        # Agent definitions
├── workspaces/agent/ # User-facing agent workspace (TASKBOARD, SOUL.md, …)
├── data/             # Runtime DBs, logs, harness workspaces (gitignored)
├── scripts/          # setup.sh, first-run.sh
├── src/
│   ├── kernel/       # Gateway control plane
│   ├── agents/       # Main agent, conductor, workers
│   ├── harness/      # Sandboxed execution
│   ├── dashboard/    # Backend + React frontend
│   ├── telegram_adapter/
│   └── cli/          # `openswarm` command
└── vision/           # Design docs
```

---

## Development

```bash
pip install -e ".[dev,telegram]"
pytest src/ -q
```

**Test status:** 890+ tests. Run `pytest src/ --durations=10` for slow tests.

Dashboard frontend (if you change UI):

```bash
cd src/dashboard/frontend && npm ci && npm run build
```

---

## Security

- Default-deny permissions in the kernel.
- Harness runs code in Docker/subprocess sandbox.
- Never commit `.env` or `config/openswarm.toml` with real tokens.

---

## Vision docs

See `vision/` — start with `vision/manifesto.md` and `vision/phases.md`.

## License

TBD.
