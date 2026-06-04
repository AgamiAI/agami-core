# What's open source, what's hosted

agami is **open core**. This page states the boundary plainly so you can build on
the open pieces without worrying we'll move the line under you.

## The principle

> **The laptop plugin is fully open (Apache-2.0) and always will be. The team
> cloud is our business.**

Concretely, the line falls where value stops being single-player and starts
requiring *more than one person* — which is also where self-hosting stops being
a weekend project and starts being real infrastructure.

## Open source (this repo, Apache-2.0 — single developer, local)

Everything you need to get value as one developer, on your own machine:

- Schema introspection → an **OSI v0.1.1 semantic model** (the format itself is an
  open standard — your model is portable, never locked in).
- NL→SQL generation and **local execution** against your DB (Postgres / MySQL /
  Snowflake / BigQuery / Redshift / SQLite).
- The **trust layer**: confidence scoring, single-reviewer sign-off, receipts,
  snapshots, git-native model history.
- **Corrections** + the `examples.yaml` few-shot library.
- The **local MCP server** (`agami serve`) — use agami from Claude Code / Claude
  Desktop. stdio, no auth, no network. See [mcp-server.md](mcp-server.md).
- You can run your own evals in CI (run `examples.yaml` against the model on a PR)
  and serve a shared model to your team via git + a server you operate. We won't
  obstruct that — it's a feature.

## Hosted (the Agami cloud — teams, governed, always-on)

The pieces whose value needs a team, and that are a genuine pain to self-host:

- A **multi-tenant semantic-model registry** served to many users/agents over a
  stable remote MCP endpoint (reachable from Claude/Cowork/web/mobile and ChatGPT).
- **Shared, governed** examples + memory/context across a team.
- **Continuous / real-time evals**: scheduled runs, regression alerting,
  dashboards, **golden-dataset management** + the cross-customer golden-set
  network effect.
- **CI-gated deploys**: gate a model change on eval-pass, versioned deploy + rollback.
- **Governance at team scale**: OAuth/SSO, RBAC, per-tool permissions, org-wide audit.

## Why this split (and why we won't relicense the core)

The defensive reason companies adopt source-available licenses (BSL/SSPL) is to
stop a competitor from re-hosting their open core as a competing SaaS. That threat
doesn't apply here: the open core is **single-player and file-based** — there's
nothing to re-SaaS without rebuilding the entire (closed) team backend. So we keep
the core permissive Apache-2.0, like dbt Core and the LlamaIndex library.

## The upgrade path is a backend swap

Because the local MCP server and the hosted connector expose the **same tool
surface**, moving a team from local to hosted means pointing at a different
backend — not learning a new product. You self-host the flat, commoditized part
for free; you pay for the part that compounds (evals, the golden-set network
effect, governance) the day it's worth more than running it yourself.
