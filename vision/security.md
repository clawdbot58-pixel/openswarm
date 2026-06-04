# Security Model

## Threat Model

| Threat | Mitigation |
|--------|------------|
| Malicious agent spawns infinite agents | Kernel enforces max_children per manifest |
| Agent reads sensitive files | Permission enforcer checks `fs.allow` glob patterns |
| Agent executes `rm -rf /` | Harness runs in Docker/firejail; blocked_cmds list |
| Agent exfiltrates data | Network `allow` list; no wildcard internet by default |
| Agent impersonates another | Envelope signed with agent_token (JWT); kernel validates |
| LLM prompt injection | Preamble is assembled by kernel, not agent; no user text in system prompt |
| Dashboard XSS | Strict CSP; all data schema-validated before render |

## Permission Enforcer Rules

1. **Default deny.** If permission not explicitly granted, it is denied.
2. **Most specific wins.** `deny: ["/etc/*"]` overrides `allow: ["/etc/passwd"]`.
3. **No escalation.** An agent cannot grant permissions it does not have.
4. **Audit everything.** Every permission check logged with envelope_id, result, timestamp.

## Harness Sandbox

- Docker container per workflow workspace
- Read-only mount of source code, read-write of workspace dir only
- Network disabled by default; enable per-manifest with `network.allow`
- 30s timeout, 512MB memory limit, no sudo
- All file writes tracked by git; all diffs streamed to dashboard

## Threats from the New Surfaces (v1+)

| Threat | Surface | Mitigation |
|--------|---------|------------|
| User injects a malicious goal that bypasses plan approval | Main Agent `planning` mode | Kernel mediates the mode transition; agent in `planning` mode cannot execute regardless of what it emits |
| User rejects a plan with feedback that contains a prompt-injection payload | Plan feedback field | Kernel treats feedback as data, not as instructions; re-emits to Main Agent with explicit "user data" framing |
| Agent reads another agent's scratchpad | Scratchpad | Scratchpad is per-agent; kernel does not route scratchpad reads across agents. The `environment.md` schema is read-only after assembly |
| Agent exfiltrates via integration | Integration tool calls | Kernel validates integration scopes per call; an agent cannot expand its own scope. Network egress filtered by integration's `network.allow` |
| Agent uses integration credentials outside its workflow | Credential lifecycle | Tokens are scoped, expiring (default 1h), and bound to workflow_id. Reuse outside the workflow is rejected |
| Agent submits a `create_pr` against the wrong branch | Branch info in environment | Kernel validates target branch against `manifest.vcs.allowed_branches`; default-deny to `main` |
| Plan document contains a hidden instruction that runs on approval | Plan YAML | Plan fields are schema-validated; arbitrary keys rejected. The Main Agent's plan emit goes through schema validation before persistence |
| Loop detection suppression | Agent self-reporting | Loop detection is kernel-side (sees raw envelopes), not agent-side. The agent cannot lie its way out of a detected loop |
| Ephemeral message spoofing | System message handling | `<system_reminder>` and similar tags are produced only by the kernel, not the user. User messages cannot inject them. The agent prompt instructs to follow them silently, not to act on user-attributed copies |
| Cost-budget exhaustion DoS | Cost ceiling | Per-step USD budget is kernel-enforced, not agent-reported. The agent cannot lie about cost to extend its own budget |
| Integration registry tampering | Registry file | Loaded read-only at kernel boot; edits require kernel restart and audit log entry |

## Plan Approval Security

The plan-approval flow is a high-value attack surface. Defenses:

- The Main Agent **cannot** transition to `standard` mode by emitting any envelope. The transition is driven by a kernel-mediated event: the user clicks "Approve" in the dashboard, which calls `POST /plans/{plan_id}/decision`, which the kernel verifies before flipping the mode flag.
- Plan fields are schema-validated. A plan with extra fields, wrong types, or suspicious content (e.g. shell commands in `goal`) is rejected.
- The plan document is **read-only after approval**. The Main Agent cannot edit a plan it submitted.
- Plan feedback from the user is treated as data, not instructions. It is wrapped in a "user data" envelope and the Main Agent is told explicitly to treat it as text input, not as system overrides.
