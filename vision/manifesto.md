# OpenSwarm Manifesto

## Why We Exist

Current AI agent systems are either:
- **Monolithic** (one brain does everything, context explodes, quality drops)
- **Chaotic** (multi-agent with no contracts, agents talk past each other)
- **Black-box** (you can't see what happened, can't debug, can't improve)

OpenSwarm is a **contract-first, observable, self-healing agent swarm**.

## Core Beliefs

1. **Contracts before code.** Every message, every agent, every workflow is schema-validated. If it doesn't conform to the contract, it doesn't exist.
2. **The Main Agent is a conductor, not a musician.** It orchestrates. It does not execute tools, write code, or touch files.
3. **Agents are cattle, not pets.** Any agent can die and be replaced. The system continues. Checkpointing makes workflows immortal.
4. **Thinking is a first-class primitive.** Reasoning loops (direct, CoT, reflection, tree) are composable, measurable, and optimizable.
5. **The dashboard is not decoration.** It is the control plane. You see the swarm breathe, debug in real-time, and intervene.
6. **Self-healing is not a feature.** It is the default. When an agent fails, the system tries harder (better model, better loop, fresh instance) before giving up.

## What We Are NOT

- Not a chatbot wrapper
- Not a single-agent "do everything" system
- Not a framework that hides complexity (we expose it, you control it)
- Not production-ready on day one (but architected to get there)
