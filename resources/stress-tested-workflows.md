# Stress-Tested Workflow Patterns

## Pattern 1: Code Review Pipeline
```
User: "Review this PR"
  ↓
Main Agent → Workflow:
  step_1_fetch:    fetcher-agent    → git clone / diff
  step_2_analyze:  analyzer-agent   → static analysis (depends on step_1)
  step_3_review:   reviewer-agent   → LLM review (depends on step_2)
  step_4_suggest:  suggester-agent  → improvement suggestions (depends on step_3)
  step_5_format:   formatter-agent  → markdown report (depends on step_4)
  ↓
Main Agent → User: "Here is your review..."
```
**Stress test:** 50 PRs queued. Agents spawn/kill per PR. Kernel must not deadlock.

## Pattern 2: Feature Build
```
User: "Build a login page"
  ↓
Main Agent → Workflow:
  step_1_plan:     planner-agent    → architecture + file list
  step_2_design:   designer-agent   → UI mock (depends on step_1)
  step_3_backend:  coder-agent      → API endpoints (parallel with step_4)
  step_4_frontend: coder-agent      → React components (parallel with step_3)
  step_5_test:     tester-agent     → unit tests (depends on step_3,4)
  step_6_review:   reviewer-agent   → code review (depends on step_5)
  step_7_deploy:   deployer-agent   → staging deploy (depends on step_6)
  ↓
Main Agent → User: "Deployed to staging. Review?"
```
**Stress test:** Step 3 fails (API incompatible). Kernel pauses step 4. Main agent respawns backend with different model. Resume.

## Pattern 3: Research & Report
```
User: "Research quantum computing for finance"
  ↓
Main Agent → Workflow:
  step_1_search:   researcher-agent → web search, papers
  step_2_synthesize: analyst-agent → summarize findings (depends on step_1)
  step_3_draft:    writer-agent     → report draft (depends on step_2)
  step_4_factcheck: critic-agent    → verify claims (depends on step_3)
  step_5_revise:   writer-agent     → final draft (depends on step_4)
  ↓
Main Agent → User: "Report ready. 12 sources cited."
```
**Stress test:** Step 4 finds 3 false claims. Main agent loops step_3→step_4 until critic score > 8.

## Pattern 4: Self-Healing Loop Optimization
```
Meta-Agent proposes: "Try tree-of-thought for architecture decisions"
  ↓
Kernel runs 10 architecture tasks with tree loop
  ↓
Critic scores outputs
  ↓
LoopOptimizer updates: tree_loop score = 8.5/10, cost = $0.12/task
  ↓
Main Agent now auto-selects tree loop for architecture steps
```
**Stress test:** 1000 tasks, 50 loop variants. SQLite must handle concurrent writes.

## Failure Scenarios to Test

1. **Agent dies mid-step:** Kernel detects zombie, main agent respawns, step retries from checkpoint.
2. **Model rate limited:** Model router falls back 3x, then escalates to main agent.
3. **Permission denied:** Enforcer drops message, main agent receives event, may grant temporary elevation.
4. **Infinite loop:** Step retries > 3 → escalate. Workflow paused.
5. **Deadlock:** Circular dependency in workflow → kernel detects on validation, rejects workflow.
6. **Disk full:** Harness returns error, main agent may request cleanup or larger workspace.
7. **Network partition:** Agent loses WebSocket. Kernel marks offline. On reconnect, agent re-registers, kernel resumes if checkpoint exists.
