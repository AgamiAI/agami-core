# What's free, what's hosted

agami is **fair-code** (source-available): **self-hosting for your own organization is
free**, and the hosted cloud adds an org-scale governance layer on top (it's also the
licensed path for serving people **outside** your organization). The short version of the
license is in [LICENSING.md](../LICENSING.md); the binding terms are in [LICENSE](../LICENSE).

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
- The **self-hosted team server** (`/agami-deploy`) — *early access (in testing)* — an
  HTTPS MCP server your whole org connects to, with an admin console (sign in with a
  password or a single Google / Microsoft **SSO** provider) and per-user access to the
  query surface. Usable today, newer than the local path; see the
  [early-access note in deploy/README.md](../deploy/README.md).

## Paid — the hosted cloud

The org-scale layer — advanced governance, shared context, and continuous evals — for teams
that want it served (and the licensed path for serving people **outside** your organization):

- A **multi-tenant model registry** over a remote MCP endpoint.
- **Shared, governed** examples + context across a team.
- **Feedback capture**: user corrections and query feedback rolled into the shared
  model and evals.
- **Continuous evals**: scheduled runs, regression alerting, golden-dataset management.
- **Enterprise SSO**: per-org (multi-tenant) identity, **SAML**, and **SCIM**
  provisioning — plus org-scale governance (**RBAC**, audit). (Basic single-provider
  SSO is free, above.)
- **CI-gated deploys**.

Moving from local to hosted is a **backend swap** — the local server and the hosted
connector expose the same tools, so you point at a different backend, not a new product.
