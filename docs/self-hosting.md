# Self-hosting: manual install & configuration reference

> **Most people should not read this page.** The one-command path — [**deploy/README.md**](../deploy/README.md),
> driven by the `/agami-deploy` skill — writes a ready-to-run Docker bundle (docker-compose + Caddy
> auto-TLS + a filled `.env`) from the published image `ghcr.io/agamiai/agami-core`, no clone and no
> build. This page is the **by-hand alternative and the environment-variable reference** — for when you
> want to run the server without the bundle (a bare `pip install`, a serverless platform, etc.).

agami's **HTTP MCP server** lets a team point their Claude at one shared, governed model. It's
**cloud-neutral** — a VM + Postgres, or a stateless platform (Cloud Run / Container Apps) + managed
Postgres. No GCP service is required to boot: no Cloud SQL connector, no Secret Manager, no Cloud
Logging (a regression test enforces this).

## Install

The server lives in the published `agami-core` package's `[server]` extra:

```bash
pip install "agami-core[server]"
```

## Run

Both steps read `AGAMI_DB_URL` (the store) — set it in the environment or inline as shown:

```bash
export AGAMI_DB_URL=postgresql://user:pass@host:5432/agami

# 1. migrate the store + load the model into Postgres (idempotent, fail-closed)
python -m model_deploy

# 2. serve (also re-migrates + seeds the admin on startup; both idempotent)
PUBLIC_BASE_URL=https://your-host python -m mcp_http
```

All serving state lives in Postgres — a fresh instance with only `AGAMI_DB_URL` serves identically,
so the server survives restarts and stateless platforms. The serving path is **LLM-free and
zero-egress by default**: the client is the brain, `execute_sql` runs SQL against your own database,
and the other tools just read the model. Nothing leaves your environment.

## Configuration (environment variables)

| Variable | Required | Purpose |
|----------|----------|---------|
| `AGAMI_DB_URL` | yes (server) | The store: `postgresql://…` in prod, `sqlite://…` for a small/local run. `APP_DATABASE_URL` is accepted as an alias for the cloud-platform convention (`AGAMI_DB_URL` wins if both are set). Unset ⇒ the local file path. |
| `PUBLIC_BASE_URL` | yes (server) | Backs OAuth/MCP discovery + the `WWW-Authenticate` resource URL. Set it explicitly — it can't be auto-detected behind a proxy/LB; the server fails fast at startup if it's missing. |
| `AGAMI_ORG_ID` | no | The single configured org id (default `local`). The server is single-tenant by default. |
| `AGAMI_SIGNING_SECRET` | yes (auth) | ≥32-byte secret the server signs its own session JWTs with. When set, the server validates real tokens (the admin password login flow); unset ⇒ the bearer-presence local default. |

Admins sign in to `/admin` with a **username and password**; team members get per-user OAuth on `/mcp`.
Single sign-on (Google / Microsoft) is part of the hosted cloud — see
[what's free vs hosted](open-vs-hosted.md).

> **Serverless note:** on a managed-container platform (Cloud Run, ECS/Fargate, Container Apps) the server
> runs under a cloud identity — scope it to a dedicated least-privilege one, not the platform default. See
> [Runtime identity on serverless platforms](../deploy/README.md#runtime-identity-on-serverless-platforms).

The serving path stays **LLM-free and zero-egress**: the client is the brain, `execute_sql` runs SQL
against your own database, and nothing leaves your environment.
