# Phase 7 Demo: Dashboard Backend

## Running the Server

```bash
cd /Users/thomas/Desktop/openswarm
uvicorn src.dashboard.backend.main:app --host 0.0.0.0 --port 8765 --reload
```

## API Documentation

Once running, OpenAPI docs are available at:
- http://localhost:8765/docs (Swagger UI)
- http://localhost:8765/redoc (ReDoc)
- http://localhost:8765/openapi.json (OpenAPI JSON)

## Endpoints

### Introspection — Agents

**Get all agents:**
```bash
curl http://localhost:8765/api/agents
```

**Get agents filtered by status:**
```bash
curl "http://localhost:8765/api/agents?status=ready"
```

**Get agent detail:**
```bash
curl http://localhost:8765/api/agents/main-agent
```

### Introspection — Workflows

**Get all workflows:**
```bash
curl http://localhost:8765/api/workflows
```

**Get workflows filtered by status:**
```bash
curl "http://localhost:8765/api/workflows?status=running"
```

**Get workflow detail:**
```bash
curl http://localhost:8765/api/workflows/<workflow_id>
```

### Introspection — Logs

**Get logs:**
```bash
curl "http://localhost:8765/api/logs?limit=10"
```

**Get logs filtered by agent:**
```bash
curl "http://localhost:8765/api/logs?agent_id=main-agent"
```

**Get logs filtered by severity:**
```bash
curl "http://localhost:8765/api/logs?severity=error"
```

### Introspection — Workspaces

**Get workspaces:**
```bash
curl http://localhost:8765/api/workspaces
```

**Get workspace files:**
```bash
curl "http://localhost:8765/api/workspaces/<workflow_id>/files?path=/"
```

**Get workspace file:**
```bash
curl "http://localhost:8765/api/workspaces/<workflow_id>/file?path=/src/main.py"
```

**Get workspace diff:**
```bash
curl "http://localhost:8765/api/workspaces/<workflow_id>/diff?commit_hash=abc123"
```

**Get workspace history:**
```bash
curl http://localhost:8765/api/workspaces/<workflow_id>/history
```

### Introspection — Loops

**Get loop templates:**
```bash
curl http://localhost:8765/api/loops
```

**Get loop performance:**
```bash
curl http://localhost:8765/api/loops/<template_id>/performance
```

### Introspection — Memory

**Get agent memory:**
```bash
curl http://localhost:8765/api/memory/main-agent
```

**Get memory filtered by type:**
```bash
curl "http://localhost:8765/api/memory/main-agent?type=observation"
```

### Introspection — Metrics

**Get system metrics:**
```bash
curl http://localhost:8765/api/metrics
```

Response:
```json
{
  "total_agents": 5,
  "active_agents": 3,
  "zombie_agents": 0,
  "busy_agents": 2,
  "idle_agents": 1,
  "total_workflows": 12,
  "running_workflows": 4,
  "completed_workflows": 7,
  "failed_workflows": 1,
  "messages_per_minute": 47.3,
  "avg_loop_latency_ms": 123.4,
  "total_cost_today_usd": 2.34,
  "upqueue_seconds": 5.2,
  "queue_total": 3,
  "started_at": "2026-06-05T10:00:00Z"
}
```

## WebSocket Stream

Connect to `/stream` for real-time events.

### JavaScript Client Example

```javascript
const ws = new WebSocket("ws://localhost:8765/stream");

ws.onopen = () => {
  console.log("Connected to dashboard stream");

  // Subscribe to specific event types (optional)
  ws.send(JSON.stringify({ type: "subscribe", events: ["agent_status_changed"] }));
};

ws.onmessage = (event) => {
  const msg = JSON.parse(event.data);

  if (msg.type === "snapshot") {
    console.log("Initial snapshot:", msg.data);
    // msg.data.metrics — current system metrics
    // msg.data.agents — current agent list
  } else if (msg.type === "event") {
    console.log("Event:", msg.event_name, msg.data);
    // msg.event_name — e.g., "agent_zombie", "queue_overflow"
    // msg.data — event details
  } else if (msg.type === "heartbeat") {
    console.log("Heartbeat");
  }
};

ws.onclose = () => {
  console.log("Disconnected from dashboard stream");
};
```

### Event Types Received

| Event | Description |
|-------|-------------|
| `agent_zombie` | An agent stopped responding |
| `permission_denied` | Permission check failed |
| `queue_overflow` | Message queue exceeded limit |
| `auto_restart_triggered` | Agent auto-restarted |
| `step_complete` | Workflow step completed |
| `file_changed` | Harness file changed |
| `execution_complete` | Workflow execution finished |
| `agent_status_changed` | Agent went ready/busy/idle/zombie |
| `workflow_status_changed` | Workflow status changed |

## Configuration API

### Views

**Save a view:**
```bash
curl -X POST http://localhost:8765/api/views \
  -H "Content-Type: application/json" \
  -d '{
    "view_id": "research-panel",
    "name": "Research Assistant",
    "description": "Shows active research agents",
    "view_type": "custom",
    "data_sources": ["/api/agents", "/api/workflows"],
    "filters": {"category": "research"},
    "refresh_interval_ms": 5000
  }'
```

**List views:**
```bash
curl http://localhost:8765/api/views
```

**Get view:**
```bash
curl http://localhost:8765/api/views/research-panel
```

**Delete view:**
```bash
curl -X DELETE http://localhost:8765/api/views/research-panel
```

### Layouts

**Save a layout:**
```bash
curl -X POST http://localhost:8765/api/layouts \
  -H "Content-Type: application/json" \
  -d '{
    "layout_id": "default",
    "name": "Default Dashboard",
    "views": ["research-panel", "metrics-overview"],
    "positions": {
      "research-panel": {"x": 0, "y": 0, "w": 6, "h": 4},
      "metrics-overview": {"x": 6, "y": 0, "w": 6, "h": 4}
    }
  }'
```

**Get layout:**
```bash
curl http://localhost:8765/api/layouts/default
```

## Testing

```bash
# Run all dashboard tests
python -m pytest src/dashboard/backend/tests/ -v

# Run specific test file
python -m pytest src/dashboard/backend/tests/test_introspection.py -v

# Run with coverage
python -m pytest src/dashboard/backend/tests/ --cov=src/dashboard/backend --cov-report=html
```

## Architecture Diagram

```
┌──────────────────────────────────────────────────────────────┐
│                         Browser                               │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────────────┐  │
│  │  React UI   │  │  WebSocket   │  │   REST Client        │  │
│  │  (Phase 8)   │  │  /stream     │  │   /api/*             │  │
│  └──────┬──────┘  └──────┬──────┘  └──────────┬──────────┘  │
└─────────┼────────────────┼────────────────────┼─────────────┘
          │                │                    │
          │   WebSocket    │    REST/HTTP       │
          ▼                ▼                    ▼
┌──────────────────────────────────────────────────────────────┐
│                    Dashboard Backend                          │
│  ┌──────────────────────────────────────────────────────┐   │
│  │                    FastAPI App                        │   │
│  │  ┌────────────┐  ┌────────────┐  ┌────────────────┐  │   │
│  │  │ /stream    │  │ /api/*     │  │ /api/views     │  │   │
│  │  │ WebSocket  │  │ Introspection│ │ /api/layouts  │  │   │
│  │  └─────┬──────┘  └─────┬──────┘  └───────┬────────┘  │   │
│  └────────┼───────────────┼──────────────────┼───────────┘   │
│           │               │                  │               │
│           ▼               ▼                  ▼               │
│  ┌────────────┐  ┌────────────┐  ┌────────────────────┐    │
│  │ EventStream│  │Introspection│  │   ConfigAPI         │    │
│  │            │  │    API      │  │                    │    │
│  └─────┬──────┘  └─────┬──────┘  └─────────┬──────────┘    │
│        │               │                    │                │
│        │         ┌─────┴─────┐              │                │
│        │         │           │              │                │
│        ▼         ▼           ▼              ▼                │
│  ┌────────┐  ┌────────┐  ┌────────┐  ┌────────────┐        │
│  │ Kernel │  │ Phase 1 │  │ Phase 2 │  │ dashboard  │        │
│  │  Bus   │  │ Registry│  │Workflows│  │    .db     │        │
│  └────────┘  └────────┘  └────────┘  └────────────┘        │
└──────────────────────────────────────────────────────────────┘
```

## Key Files

| File | Purpose |
|------|---------|
| `src/dashboard/backend/main.py` | FastAPI app, routes, lifespan |
| `src/dashboard/backend/introspection.py` | IntrospectionAPI — all read queries |
| `src/dashboard/backend/stream.py` | EventStream — WebSocket real-time push |
| `src/dashboard/backend/config.py` | ConfigAPI — view/layout storage |
| `src/dashboard/backend/aggregator.py` | DataAggregator — background aggregation |
| `src/dashboard/backend/models.py` | Pydantic response models |
| `src/dashboard/backend/cache.py` | In-memory cache for aggregated data |

## Demo: Real-Time Event Flow

1. Start the backend:
   ```bash
   uvicorn src.dashboard.backend.main:app --host 0.0.0.0 --port 8765
   ```

2. Connect a WebSocket client to `ws://localhost:8765/stream`

3. In another terminal, emit a test event (via kernel API or test harness):
   ```python
   await bus.emit_event("agent_zombie", {"agent_id": "test-agent"})
   ```

4. Observe the WebSocket client receives:
   ```json
   {
     "type": "event",
     "event_name": "agent_zombie",
     "data": {
       "envelope_id": "...",
       "sender": {"agent_id": "kernel", "role": "kernel"},
       "payload": {"data": {"event": "agent_zombie", "agent_id": "test-agent"}}
     }
   }
   ```

## Demo: Query System State

1. Start the backend:
   ```bash
   uvicorn src.dashboard.backend.main:app --host 0.0.0.0 --port 8765
   ```

2. Get current system metrics:
   ```bash
   curl http://localhost:8765/api/metrics | python -m json.tool
   ```

3. Get all agents:
   ```bash
   curl http://localhost:8765/api/agents | python -m json.tool
   ```

4. Get running workflows:
   ```bash
   curl "http://localhost:8765/api/workflows?status=running" | python -m json.tool
   ```

5. Get recent logs:
   ```bash
   curl "http://localhost:8765/api/logs?limit=20" | python -m json.tool
   ```

## Next Step: Phase 8

The backend is complete. Before building the frontend (Phase 8), ask the user:

> "The backend API is ready. It exposes agents, workflows, logs, workspaces, loops, memory, and metrics.
> What views do you want for YOUR dashboard? For example:
> - A taskboard with columns (todo / in-progress / done)?
> - A research panel showing active research agents and their findings?
> - A code workspace with file tree + diff viewer?
> - A minimal chat view with just Main Agent status?
> - Something else?
> Describe what you want, and I will generate the frontend (Phase 8) to match."