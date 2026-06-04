# Testing Strategy

## Unit Tests (Every Phase)

- Schema validation: Every generated JSON must pass contract validation.
- Message routing: Send envelope A to agent B, verify receipt.
- Permission enforcement: Attempt unauthorized action, verify denial.
- Heartbeat: Kill agent process, verify zombie detection within 20s.

## Integration Tests (Per Phase)

### Phase 1
- Start kernel. Connect 3 test WebSocket clients. Send 100 messages. Verify routing.
- Kill one client mid-message. Verify zombie event emitted.

### Phase 2
- Send user goal to main agent. Verify workflow JSON output.
- Inject fake `agent_zombie` event. Verify main agent emits `spawn_request`.

### Phase 3
- Spawn agent with manifest. Send task. Verify result envelope.
- Test model fallback: block primary model, verify fallback used.

### Phase 5
- Send `tool_exec` to harness. Verify Docker container created.
- Write file in harness. Verify git commit created.
- Kill harness mid-execution. Verify timeout and error envelope.

### Phase 9
- Start workflow. Kill kernel after step 2. Restart kernel. Verify workflow resumes.
- Verify checkpoint restored. Verify agent assignments preserved.

## Load Tests (Phase 11)

- 100 concurrent workflows
- 50 agents spawning/killing per minute
- Kernel must not drop messages, must checkpoint within 100ms

## Failure Injection

Use `chaos/` scripts to randomly:
- Kill agent processes
- Drop WebSocket connections
- Corrupt envelope JSON
- Exhaust model rate limits
- Fill disk workspaces

System must degrade gracefully, never lose checkpoint data.

## Plan-Approval Workflow Tests (Phase 2+)

- Send a non-trivial user goal. Main Agent emits `plan_ready` envelope. Kernel persists plan. No workflow execution occurs.
- Verify dashboard surfaces `plan_pending_approval` event.
- POST `/plans/{plan_id}/decision` with `action: approve`. Kernel flips mode, injects plan, begins execution.
- POST `/plans/{plan_id}/decision` with `action: reject` and feedback. Main Agent receives feedback, mode stays `planning`.
- Try to bypass: Main Agent in `planning` mode emits a workflow JSON directly. Kernel rejects with `mode_violation` event.

## Environment Injection Tests (Phase 2+)

- Spawn an agent. Verify the preamble contains a valid `environment` block matching `environment.md` schema.
- Change the system clock. Verify the next preamble carries the updated `kernel_time`.
- Mark an integration `degraded` in the registry. Verify the next preamble shows `status: degraded` and any subsequent tool call to it fails with `integration_unavailable`.
- Spawn an agent without a needed integration in its manifest. Verify it can `request_capability` and Main Agent can grant by spawning a worker with the integration.

## Scratchpad Tests (Phase 2+)

- Worker agent writes to scratchpad via the `scratchpad.write` envelope. Verify the next preamble includes the entry.
- Worker agent writes a `decision` entry before a non-trivial action. Verify it appears in the audit log.
- After task end, in-memory scratchpad is wiped. SQLite scratchpad persists and is queryable.

## Loop Detection Tests (Phase 9+)

- Force an agent to send 3 envelopes with the same action. Verify kernel emits `loop_detected: action_repeat` and pauses the inbox.
- Force 5 `request_clarification` envelopes with no user input. Verify `loop_detected: clarification_spin`.
- Force 3 consecutive mutate attempts on the same step. Verify `loop_detected: mutate_exhausted` and auto-escalate.
- Force a step to exceed `cost_budget_per_step_usd`. Verify `budget_exhausted` and pause.
- Force a step to exceed `step.max_minutes`. Verify `step_timeout` and pause.

## Integration Tests (Phase 9+)

- Register an integration in the vault. Spawn an agent with the integration. Verify the agent receives a scoped, expiring token, not raw credentials.
- Rotate an integration's credentials while a workflow is running. Verify in-flight calls fail safely and `integration_unavailable` is emitted.
- An agent without an integration tries to use its tool. Verify kernel rejects with `capability_missing`.

## Refusal & Recovery Tests (Phase 9+)

- Inject a request that triggers a hard refusal. Verify the response is exactly `"REFUSAL: {reason}"` with no apology or explanation.
- Inject 3 same-action envelopes. Verify the loop-break protocol fires and the agent emits `ask_user`, `request_capability`, or `escalate` — not another retry.

## Plan Display Tests (Phase 8+)

- Approve a plan. Verify the dashboard's Plan Review modal closes, the workflow DAG appears, and execution begins.
- Reject a plan. Verify the modal stays open, the Main Agent re-emits a revised `plan_ready`, and the old plan is archived with `status: rejected`.

## Display Code Convention Tests (Phase 8+)

- Agent emits a status update with a file reference like `path/to/file.py:42-55`. Verify the dashboard renders it as a clickable code block.
- Agent emits a `{"type": "diff", ...}` envelope. Verify the dashboard renders it as a unified diff, not pre-formatted text.
- Agent emits a raw diff in a markdown code fence. Verify the dashboard strips it and logs `display_convention_violation`.
