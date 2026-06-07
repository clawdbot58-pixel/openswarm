# OpenSwarm Dashboard Frontend (Phase 8)

The visual face of the swarm. Renders real-time agent state, animated workflow
graphs, live logs, and the workspace — all driven by the Phase 7 backend at
`http://localhost:8765`.

## Stack

- **React 18 + Vite + TypeScript** (strict)
- **Tailwind CSS** for styling
- **@xyflow/react** for the workflow DAG
- **@monaco-editor/react** for code viewing
- **react-grid-layout** for panel arrangement
- **framer-motion** for spring-driven micro-interactions
- **@phosphor-icons/react** for iconography
- **dagre** for DAG layout
- **diff2html**-style unified-diff parser for diffs

## Run

```bash
npm install
npm run dev         # http://localhost:5173
npm run typecheck   # strict TS, zero errors
npm test            # vitest + react-testing-library
npm run build       # production bundle
```

Vite's dev server proxies `/api` and `/stream` to the Phase 7 backend on
port 8765 — start that first (or override with `VITE_API_BASE` / `VITE_WS_BASE`).

## What it does

| View | What it shows | Data |
| --- | --- | --- |
| `swarm_overview` | Grid of agent cards with status, tier, task | `GET /api/agents` |
| `workflow_dag` | Interactive DAG with animated edges | `GET /api/workflows/{id}` |
| `log_stream` | Filterable, auto-scrolling log feed | `GET /api/logs` + WS `/stream` |
| `workspace_explorer` | File tree + Monaco editor + diff | `/api/workspaces/{id}/{files,file,diff,history}` |
| `agent_detail` | Single-agent manifest, memory, errors | `GET /api/agents/{id}` |
| `metrics` | System vitals, throughput, cost | `GET /api/metrics` |
| `custom` | Generic data tables for any `data_sources` | Whatever the ConfigAPI gives us |

The `LayoutRenderer` is a `react-grid-layout` grid of panels; each panel
references a `view_id` from the ConfigAPI. The `LayoutBuilder` composes
new layouts by dragging views from the palette onto the canvas, then saves
via `POST /api/layouts`.

## File map

```
src/
├── App.tsx               # Shell, header, mode switch
├── main.tsx              # Vite mount
├── types.ts              # Wire-format types
├── api.ts                # Typed REST client
├── websocket.ts          # StreamClient (auto-reconnect)
├── theme.ts              # Status tokens + motion tokens
├── env.ts                # Vite env reader
├── components/           # Atomic components
├── views/                # One file per view_type
├── hooks/                # Data hooks (useAgents, useLogs, …)
├── utils/                # format, language, dagre layout, cn
└── styles/index.css      # Tailwind + theming
```

## Design philosophy

- Dark-first, warm-zinc surface with a single committed amber accent.
- Animations use cubic-bezier ease-out and Framer springs; nothing bounces.
- No emojis, no purple-glow, no oversized H1s.
- 3-equal-card grids are replaced with asymmetric 2/3 + 1/3 splits and
  numbered list rows.
- Status badges pulse when an agent is busy, so the dashboard feels alive
  even with zero interaction.
