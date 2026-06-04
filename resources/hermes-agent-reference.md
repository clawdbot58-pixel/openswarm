# Hermes Agent / Multi-Agent Patterns Reference

## Hermes Key Concepts

### 1. Agent as State Machine
```
IDLE → PLANNING → EXECUTING → REVIEWING → COMPLETE
  ↑___________________________________________|
```
- Each state has entry/exit hooks
- State transitions are logged and observable
- Failed states can trigger rollback

### 2. Tool Use as Structured Generation
- Tools are not function calls. They are **structured outputs** from the LLM.
- LLM generates JSON matching tool schema
- Executor validates schema, then runs
- This prevents "hallucinated" tool calls

### 3. Plan-and-Execute Pattern
```
Planner: Break goal into subtasks
  ↓
Executor: Run each subtask (may spawn sub-agents)
  ↓
Reviewer: Verify output against criteria
  ↓
Either: Accept → Done, or Reject → Replan
```

### 4. Hierarchical Agent Teams
```
Manager Agent
├── Researcher Agent
├── Writer Agent
└── Editor Agent
    └── Fact-Checker Agent
```
- Manager delegates, aggregates, decides
- Workers are single-purpose
- Communication through structured messages only

### 5. Reflection Loop
```
Generate → Self-Critique → Revise → Final Output
```
- Same model or different model for critique
- Critique prompt is separate system prompt
- Can iterate N times with stop conditions

## What We Steal

| Hermes Pattern | OpenSwarm Equivalent |
|----------------|----------------------|
| State machine | Agent lifecycle + workflow status |
| Structured tool generation | Tool payload schema in envelope |
| Plan-and-execute | Workflow DAG with dependency edges |
| Hierarchical teams | Main agent + specialist agents |
| Reflection loop | `reflection` thinking primitive (Phase 4) |

## What We Differ

| Hermes | OpenSwarm |
|--------|-----------|
| Fixed team structure | Dynamic agent spawning mid-workflow |
| Manager does planning | Main agent delegates planning to planner agent |
| Single model per agent | Model fallback chain per agent |
| Static reflection | Dynamic loop assembly + scoring |
| No checkpointing | Full checkpoint + resume |
