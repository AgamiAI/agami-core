# What's fair-code, what's hosted

agami is **fair-code** (source-available). This page states the boundary plainly:
what's free to self-host for your own organization, and what's paid. The short
version of the license is in [LICENSING.md](../LICENSING.md); the binding terms are
in [LICENSE](../LICENSE).

## The principle

> **The laptop plugin is free to self-host for your own organization. Exposing it
> — the data or the MCP — to people outside your organization is the paid line.**

Concretely, the line falls where value stops being single-player and starts
requiring *more than one person* — which is also where self-hosting stops being
a weekend project and starts being real infrastructure.

## Free to self-host (this repo, fair-code — your own team, local)

Everything you need to get value as a developer or a team, on your own machines,
for your own people:

- Schema introspection → a **provider-portable semantic model** (subject areas,
  tables, entities, metrics, relationships with join cardinality — plain YAML you
  own, never locked in).
- NL→SQL generation and **local execution** against your DB (Postgres / Supabase /
  Redshift / MySQL / Snowflake / BigQuery / SQL Server / Oracle / Databricks /
  Trino / DuckDB / SQLite).
- The **trust layer**: confidence scoring, single-reviewer sign-off, receipts,
  snapshots, git-native model history.
- **Corrections** + the `examples.yaml` few-shot library.
- The **local MCP server** (`agami serve`) — use agami from Claude Code / Claude
  Desktop. stdio, no auth, no network. See [mcp-server.md](mcp-server.md).
- You can run your own evals in CI (run `examples.yaml` against the model on a PR)
  and serve a shared model **to your own team** via git + a server you operate.
  That's internal use — it's free, and it's a feature.

## Hosted (the Agami cloud — teams, governed, always-on)

The pieces whose value needs a team, and that are a genuine pain to self-host —
**and serving agami to people outside your own organization**:

- A **multi-tenant semantic-model registry** served to many users/agents over a
  stable remote MCP endpoint (reachable from Claude/Cowork/web/mobile and ChatGPT).
- **Shared, governed** examples + memory/context across a team.
- **Continuous / real-time evals**: scheduled runs, regression alerting,
  dashboards, **golden-dataset management** + the cross-customer golden-set
  network effect.
- **CI-gated deploys**: gate a model change on eval-pass, versioned deploy + rollback.
- **Governance at team scale**: OAuth/SSO, RBAC, per-tool permissions, org-wide audit.

## Why fair-code (not permissive, not closed)

The one thing the license has to prevent is someone taking the free core and
re-hosting **agami's own functionality as a managed service to other people** —
the BSL/SSPL/n8n concern. A permissive license (Apache/MIT) wouldn't stop that;
a fully closed license would kill the adoption the core is for. **Fair-code is the
middle path:** the core stays source-available and **free for your own internal
use** — a developer, a team, your whole org, multi-user, all free — while exposing
it to outside customers is the paid commercial line. It's the model n8n and the
Elastic family use, and it's why the boundary above can be a clean, friendly line
rather than a trap. The reasons are spelled out in [LICENSING.md](../LICENSING.md).

## The upgrade path is a backend swap

Because the local MCP server and the hosted connector expose the **same tool
surface**, moving a team from local to hosted means pointing at a different
backend — not learning a new product. You self-host the flat, commoditized part
for your own team for free; you pay for the part that compounds (evals, the
golden-set network effect, governance) — and for serving people outside your
organization — the day it's worth more than running it yourself.
