# Security Review Checklist

When to Use

- Reviewing a pull request for security issues
- Auditing a code change before merge
- Investigating a possible vulnerability

Examples

- Confirm all user input is validated at the trust boundary.
- Look for SQL injection: every query must use parameterised statements.
- Confirm secrets are loaded from the kernel vault, never hard-coded.
- Check that the kernel permission enforcer actually fires for the changed code path.

Common Pitfalls

- "Internal" inputs that are still attacker-controlled (HTTP headers, file uploads).
- Logging of secrets or PII at INFO level.
- Path traversal via filename parameters.
- Overly broad CORS or `Access-Control-Allow-Origin: *`.
