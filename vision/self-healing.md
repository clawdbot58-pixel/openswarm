# Self-Healing Strategy

## Failure Taxonomy

| Failure | Detection | Response |
|---------|-----------|----------|
| Agent crash (process dies) | Heartbeat timeout | Kernel marks zombie → event to main agent |
| Agent timeout (step exceeds limit) | Kernel timer | Same as crash |
| LLM error (rate limit, malformed output) | Model router | Fallback to next model in manifest |
| Tool permission denied | Permission enforcer | Drop message, emit permission_denied event |
| Tool execution failure | Harness return code | Kernel emits step_failure event |
| Output quality too low | Critic score < threshold | Main agent decides mutate or escalate |
| Workflow deadlock | Dependency resolver | Kernel detects cycle, emits workflow_error |

## Recovery Hierarchy

```
Step fails
    ├── retry (same agent, same config)
    │       └── success? → continue
    │       └── fail? → mutate
    ├── mutate (upgrade model, upgrade loop, or spawn fresh)
    │       └── success? → continue
    │       └── fail? → fallback
    ├── fallback (run fallback_steps from workflow.error_handling)
    │       └── success? → continue
    │       └── fail? → compensate
    ├── compensate (run compensation_steps to undo partial work)
    │       └── then → escalate
    └── escalate (notify main agent, pause workflow, wait for user)
```

## Mutate Rules

When main agent decides to mutate on retry:

1. **Model upgrade:** `fast` → `standard` → `powerful`. Use fallback chain if primary fails.
2. **Loop upgrade:** `direct` → `cot` → `reflection` → `tree`. More compute, better quality.
3. **Spawn fresh:** Kill current agent instance, spawn new with modified manifest delta.
4. **Never mutate more than 3 times per step.** After 3, escalate.

## Checkpoint Recovery

- Kernel writes checkpoint after EVERY step completion.
- Checkpoint stored in SQLite + optionally filesystem.
- On kernel restart: scan `workflows WHERE status IN ('running','paused','recovering')`.
- Emit `event: workflow_resume` to main agent with checkpoint blob.
- Main agent decides: `continue_from_step`, `rollback_n_steps`, or `respawn_all_agents`.
- Kernel rebuilds agent assignments from checkpoint, resumes DAG.

## Main Agent as Recovery Brain

The main agent is the ONLY entity that decides recovery strategy.
The kernel executes. It does not decide.

This separation of concerns is critical:
- Kernel = reliable, deterministic, fast
- Main Agent = adaptive, heuristic, slow (LLM call)

## Kernel-Side Loop Detection

The kernel watches for stuck patterns at the envelope level, independent of any LLM call. When detected, the kernel **emits a `loop_detected` event** to the Main Agent with a structured report. The agent must break the loop — see `prompt-engineering.md` §16 for the protocol.

**Detection heuristics (kernel runs these in real-time per agent)**:

| Pattern | Threshold | Kernel action |
|---------|-----------|---------------|
| Same `action_type` + similar args | 3 consecutive envelopes | Emit `loop_detected: action_repeat` |
| `request_clarification` with no new user input | 5 consecutive envelopes | Emit `loop_detected: clarification_spin` |
| Same tool, similar error | 3 consecutive failures | Emit `loop_detected: tool_failure_repeat` |
| Same step failing across mutate chain | 3 mutate attempts | Emit `loop_detected: mutate_exhausted`, auto-escalate |
| `cost_budget_remaining_usd` exceeded | 1 envelope | Emit `budget_exhausted`, pause workflow |
| Step wall-clock > `step.max_minutes` | 1 step | Emit `step_timeout`, pause workflow |

**When the kernel emits a loop event**:
1. Kernel pauses the agent's inbox.
2. Kernel sends the loop report to the Main Agent.
3. Main Agent must respond with one of: `retry_with_different_approach`, `escalate_to_user`, `mutate_config`, or `cancel_workflow`.
4. If Main Agent does not respond within `loop_response_timeout_s` (default 60s), kernel auto-escalates to the user via dashboard.

**This catches what the LLM's self-reporting misses.** The agent may honestly not realize it is looping; the kernel's view of the envelope stream is ground truth.

## Cost Ceiling (Hard Guardrail)

The mutate chain has a **per-step USD budget**, not just a retry count. After the budget is exhausted, the step auto-escalates regardless of how many retries remain.

- Default: `cost_budget_per_step_usd` from the workflow's `error_handling.budget_per_step_usd`, falling back to `manifest.model_tier.cost_budget_per_task`, falling back to `0.50`.
- Kernel tracks `cost_usd` per step in real-time (model router reports it on every call).
- On budget exhaustion: emit `budget_exhausted` event, pause step, escalate to Main Agent.
- Main Agent may issue a one-time `budget_override` decision (logged in audit trail) to allow over-budget execution; this is visible in the dashboard.

This is a hard guardrail against runaway loops burning cash. The "3 retries max" rule alone is insufficient.
