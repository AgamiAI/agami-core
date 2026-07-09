# What's free, what's hosted

agami is **fair-code** (source-available): **free to self-host for your own
organization**, paid when you expose it to people **outside** it. The short version
of the license is in [LICENSING.md](../LICENSING.md); the binding terms are in
[LICENSE](../LICENSE).

## Free — self-host for your own team

Run agami on your own machines, for your own people (multi-user included):

- Schema introspection → a provider-portable **semantic model** (plain YAML you own).
- **NL→SQL + local execution** against your DB (Postgres / Supabase / Redshift /
  MySQL / Snowflake / BigQuery / SQL Server / Oracle / Databricks / Trino / DuckDB /
  SQLite).
- The **trust layer**: confidence, sign-off, receipts, snapshots, git-native history.
- **Corrections** + the `examples.yaml` few-shot library.
- The **local MCP server** (`agami serve`) — stdio, no auth, no network.
  See [mcp-server.md](mcp-server.md).
- The **self-hosted team server** (`/agami-deploy`) — an HTTPS MCP server your whole
  org connects to, with a password-protected admin console and per-user access to the
  query surface. See [deploy/README.md](../deploy/README.md).

## Paid — the hosted cloud

For teams that need it served, and for serving people **outside** your organization:

- A **multi-tenant model registry** over a remote MCP endpoint.
- **Shared, governed** examples + context across a team.
- **Feedback capture**: user corrections and query feedback rolled into the shared
  model and evals.
- **Continuous evals**: scheduled runs, regression alerting, golden-dataset management.
- **Single sign-on (Google / Microsoft)** and org-scale governance (**RBAC**, audit).
- **CI-gated deploys**.

Moving from local to hosted is a **backend swap** — the local server and the hosted
connector expose the same tools, so you point at a different backend, not a new product.
