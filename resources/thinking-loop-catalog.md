# Thinking Loop Catalog

## Premade Loops

### direct
```
generate(prompt) → output
```
- **When:** Simple, deterministic tasks. High confidence.
- **Cost:** 1x
- **Latency:** Lowest
- **Examples:** Format JSON, classify sentiment, extract entities.

### cot (Chain-of-Thought)
```
generate("Let's think step by step. {prompt}") → output
```
- **When:** Multi-step reasoning. Math, logic, debugging.
- **Cost:** 1x
- **Latency:** Low
- **Examples:** Solve equation, trace bug, plan migration.

### reflection
```
draft = generate(prompt)
critique = generate("Critique this output: {draft}")
output = generate("Revise based on critique: {draft} + {critique}")
```
- **When:** Quality matters more than speed. Writing, review, design.
- **Cost:** 3x
- **Latency:** Medium
- **Examples:** Write documentation, review code, design API.

### tree (Tree of Thoughts)
```
candidates = [generate(prompt, seed=i) for i in range(3)]
scores = [critique(c) for c in candidates]
best = candidates[argmax(scores)]
output = merge(best)
```
- **When:** Exploration needed. Architecture decisions, creative tasks.
- **Cost:** 4x (3 generate + 1 critique + 1 merge)
- **Latency:** High
- **Examples:** Choose tech stack, design database schema, write story.

### debate
```
side_a = generate("Argue FOR: {prompt}")
side_b = generate("Argue AGAINST: {prompt}")
verdict = vote([side_a, side_b])
output = merge(verdict)
```
- **When:** High-stakes, controversial, or safety-critical decisions.
- **Cost:** 4x
- **Latency:** High
- **Examples:** Security policy, ethical decision, architecture tradeoff.

### ensemble
```
outputs = [generate(prompt, model=m) for m in models]
output = vote(outputs)
```
- **When:** Maximum accuracy needed. Can afford cost.
- **Cost:** Nx (N models)
- **Latency:** Highest
- **Examples:** Medical diagnosis, legal review, financial analysis.

## Custom Assembly Syntax

```json
{
  "nodes": [
    {"id": "draft", "primitive": "generate", "model": "gpt-4o-mini", "prompt_template": "draft_v1"},
    {"id": "check", "primitive": "critique", "model": "claude-sonnet"},
    {"id": "fix", "primitive": "revise", "model": "gpt-4o"},
    {"id": "final_check", "primitive": "critique", "model": "claude-sonnet"}
  ],
  "edges": [
    {"from": "draft", "to": "check"},
    {"from": "check", "to": "fix"},
    {"from": "fix", "to": "final_check"}
  ],
  "stop_conditions": [
    "final_check.score > 8",
    "iteration_count > 3"
  ]
}
```

## Loop Selection Heuristics (Main Agent)

| Task Type | Default Loop | If Fails |
|-----------|-------------|----------|
| Code generation | direct | reflection |
| Bug fix | cot | reflection |
| Architecture | tree | debate |
| Review | reflection | ensemble |
| Research | cot | tree |
| Testing | direct | reflection |
| Deployment | direct | direct (no room for error) |
