# Model Tier Reference

## Tier Definitions

### fast
- **Models:** gpt-4o-mini, claude-haiku, llama-3.1-8b
- **Use for:** Simple classification, formatting, routing decisions
- **Cost target:** <$0.001 per task
- **Latency target:** <2s
- **Context:** 4k-8k tokens

### standard
- **Models:** gpt-4o, claude-sonnet, llama-3.1-70b
- **Use for:** General coding, writing, analysis
- **Cost target:** <$0.01 per task
- **Latency target:** <10s
- **Context:** 32k-128k tokens

### powerful
- **Models:** gpt-4o-128k, claude-opus, o1-preview
- **Use for:** Complex architecture, debugging, security review
- **Cost target:** <$0.10 per task
- **Latency target:** <60s
- **Context:** 128k-200k tokens

## Fallback Chains

Every agent manifest specifies ordered models:
```json
"models": ["claude-sonnet", "gpt-4o", "gpt-4o-mini"]
```

ModelRouter behavior:
1. Try primary model.
2. On rate limit / timeout / error, wait `backoff_ms` then try next.
3. On all failures, emit `event: model_exhausted` to main agent.
4. Main agent may upgrade tier or escalate.

## Cost Budget Enforcement

```json
"model_tier": {
  "tier": "standard",
  "cost_budget_per_task": 0.05
}
```

Kernel tracks spend per workflow. If budget exceeded:
1. Downgrade to next tier for remaining steps.
2. Emit `event: budget_exceeded` to main agent.
3. Main agent may approve override or accept downgrade.
