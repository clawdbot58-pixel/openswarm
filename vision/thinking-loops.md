# Thinking Loops

## Philosophy

Reasoning is not magic. It is a **composable pipeline of primitives**.

OpenSwarm treats thinking like OpenClaw treats skills: loadable, measurable, improvable.

## Primitives

| Primitive | Description | Cost |
|-----------|-------------|------|
| `generate` | Single LLM call, returns output | 1x |
| `critique` | LLM evaluates another output, returns assessment | 1x |
| `vote` | LLM selects best from N candidates | 1x |
| `revise` | LLM rewrites output based on critique | 1x |
| `branch` | Generate N parallel candidates | Nx |
| `merge` | Combine multiple outputs into one | 1x |

## Premade Loops

### direct
```
generate → output
```
Fast. For simple tasks. No reflection.

### cot (Chain-of-Thought)
```
generate("Think step by step...") → output
```
Single call with reasoning prefix. Good for math, logic.

### reflection
```
generate → critique → revise → output
```
Self-correction. Good for writing, review, bug fixes.

### tree (Tree of Thoughts)
```
branch(3) → vote → merge(best) → output
```
Exploration. Good for design decisions, architecture.

### debate
```
branch(2 opposing views) → critique(each) → vote → merge → output
```
For controversial or high-stakes decisions.

## Dynamic Assembly

Main agent (or meta-agent) can assemble custom graphs:

```json
{
  "nodes": [
    {"id": "draft", "primitive": "generate", "model": "gpt-4o-mini"},
    {"id": "check", "primitive": "critique", "model": "claude-sonnet"},
    {"id": "fix", "primitive": "revise", "model": "gpt-4o"}
  ],
  "edges": [
    {"from": "draft", "to": "check"},
    {"from": "check", "to": "fix"}
  ]
}
```

Rules:
- Graph must be a DAG (no cycles).
- Each node specifies `primitive` and optional `model` override.
- Default model from manifest if node doesn't specify.
- Output of terminal node = loop result.

## Trial & Error / Optimization

1. Meta-agent proposes loop graph for task type.
2. Critic agent scores output quality (1-10).
3. Result stored in `loop_templates` table.
4. After N samples, main agent has ranked list per task type.
5. Main agent can auto-select top-performing loop or propose new variant.

## Loop Scoring

```
score = (critic_score * 0.6) + (1 / cost_usd * 0.3) + (1 / latency_sec * 0.1)
```

Quality weighted highest, then cost efficiency, then speed.
