# Glossary

| Term | Definition |
|------|------------|
| **Agent** | A process that receives envelopes, thinks, and returns results. Defined by a manifest. |
| **Bus** | The kernel's message router. All communication flows through it. |
| **Checkpoint** | Serialized workflow state after each step. Enables resume after failure. |
| **Conductor** | The main agent. Orchestrates workflows, does not execute. |
| **Envelope** | The universal message format. Every packet in the system. |
| **Ephemeral Agent** | Lives for one task, then dies. No state preserved. |
| **Event** | Fire-and-forget message. No reply expected. |
| **Harness** | Sandboxed execution environment for code. Docker-based. |
| **Heartbeat** | Periodic liveness signal. Missing heartbeat = zombie. |
| **Intent** | Single-sentence purpose of an agent. Used for selection. |
| **Kernel** | The gateway control plane. Routes, registers, enforces, monitors. |
| **Lifecycle** | How an agent is born, lives, and dies. Includes restart policy. |
| **Loop** | A thinking pattern: direct, cot, reflection, tree, etc. |
| **Manifest** | JSON defining an agent's identity, capabilities, and config. |
| **Meta-Agent** | An agent that assembles thinking loops, not executes them. |
| **Model Tier** | fast / standard / powerful. Cost/quality tradeoff. |
| **Mutate** | Change agent config (model, loop) on retry. Self-healing. |
| **Orchestrator** | The main agent. See Conductor. |
| **Preamble** | Context prepended before every LLM call: intent + permissions + memory + loop config. |
| **Primitive** | A basic reasoning operation: generate, critique, vote, revise, branch, merge. |
| **Registry** | SQLite database of all agents and their manifests. |
| **Session Agent** | Lives for one workflow, then drains. |
| **Skill** | A loadable expertise file (SKILL.md) injected into preamble. |
| **Spawn** | Create a new agent instance mid-workflow. |
| **Swarm** | The collection of all agents managed by one kernel. |
| **Workflow** | A DAG of steps. The unit of work requested by a user. |
| **Zombie** | An agent that missed its heartbeat. Marked for replacement. |
