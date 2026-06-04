# Security Checklist

## Before Every Phase

- [ ] All file paths in manifests use absolute or workspace-relative paths
- [ ] No wildcard `allow: ["*"]` in fs permissions
- [ ] `blocked_cmds` includes: `rm -rf /`, `curl | sh`, `wget | bash`, `eval(...)`, `exec(...)`
- [ ] Network `allow` is explicit host list, not `*`
- [ ] Docker containers run with `--read-only` rootfs where possible
- [ ] Harness workspace is NOT the host filesystem
- [ ] Agent tokens are JWT with expiry, not static strings
- [ ] Dashboard has CSP headers, no inline scripts
- [ ] SQLite connections use parameterized queries (no SQL injection)
- [ ] Envelope payload size capped at 10MB
- [ ] WebSocket rate limiting: max 100 messages/sec per agent

## Harness Specific

- [ ] Container runs as non-root user
- [ ] `--cap-drop=ALL` unless specific capability needed
- [ ] `--network=none` by default
- [ ] Memory limit enforced (cgroups)
- [ ] CPU limit enforced (cgroups)
- [ ] Timeout enforced (kernel sends SIGKILL after limit)
- [ ] All writes go to workspace volume only
- [ ] Source code mounted read-only

## Audit Requirements

- [ ] Every permission check logged: envelope_id, agent_id, action, result, timestamp
- [ ] Every tool execution logged: agent_id, tool_name, params, stdout, stderr, exit_code
- [ ] Every model call logged: agent_id, model, tokens_in, tokens_out, cost_usd, latency_ms
- [ ] Logs retained for 30 days
- [ ] Dashboard can export audit trail per workflow
