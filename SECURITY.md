# Security Policy

## Reporting a Vulnerability

Please report security vulnerabilities **privately** rather than opening a public issue.

- Email: [security@agami.ai](mailto:security@agami.ai)
- Or use GitHub Security Advisories: <https://github.com/AgamiAI/agami-core/security/advisories/new>

We will acknowledge receipt within **3 business days** and aim to provide a fix or mitigation timeline within **14 days** for confirmed vulnerabilities. Please give us a reasonable window to respond before any public disclosure.

When reporting, include where possible:

- A description of the issue and its impact
- Steps to reproduce (proof-of-concept welcome)
- Affected versions / commit SHAs
- Any suggested remediation

## Supported Versions

LiteBi is in early development. We only support the **latest released version**; older releases will not receive security backports.

## Scope

In scope:

- The LiteBi Claude Code plugin (`plugins/agami/`) and the SQL execution pipelines it ships.
- The plugin marketplace manifests (`.claude-plugin/marketplace.json`, `plugins/agami/.claude-plugin/plugin.json`).

Out of scope:

- Third-party databases, drivers, or services LiteBi connects to.
- Vulnerabilities in Claude Code itself (report those to Anthropic).
- Issues that require physical access to a user's machine or already-compromised credentials.

Thank you for helping keep LiteBi and its users safe.
