# Self-hosting the team server

Beyond the local single-player setup, agami ships an **HTTP MCP server** so a team can point their
Claude at one shared, governed model. It's **cloud-neutral** — a VM + Postgres, or a stateless
platform (Cloud Run / Container Apps) + managed Postgres. No GCP service is required to boot: no
Cloud SQL connector, no Secret Manager, no Cloud Logging (a regression test enforces this).

> **The easy path:** the **`/agami-deploy`** skill writes a ready-to-run bundle (docker-compose +
> Caddy auto-TLS + a filled `.env`) that pulls the published image `ghcr.io/agamiai/agami-core` — no
> clone, no build. See [deploy/README.md](../deploy/README.md). The manual steps below are for when
> you'd rather wire it up yourself.

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
| `AGAMI_SIGNING_SECRET` | yes (auth) | ≥32-byte secret the server signs its own session JWTs with. When set, the server validates real tokens (the password / OIDC login flow); unset ⇒ the bearer-presence local default. |

## Optional: social login (OIDC)

"Sign in with Google / Microsoft" is **off by default**. Configure a provider's client id/secret to
enable it (the option is hidden when unset; username/password still works):

| Variable | Provider | Notes |
|----------|----------|-------|
| `AGAMI_OIDC_GOOGLE_CLIENT_ID` / `_SECRET` | Google | requires `email_verified` |
| `AGAMI_OIDC_MICROSOFT_CLIENT_ID` / `_SECRET` | Microsoft | also set `AGAMI_OIDC_MICROSOFT_TENANT` to a **pinned** tenant id (not `common`/`organizations`) — the tenant is the trust boundary |
| `AGAMI_PUBLIC_SIGNUP` | — | default off. When on, an unknown verified email self-provisions a **demo** account — intended only for a dedicated "Try for free" instance whose data is non-sensitive. Leave off for any real deployment (it's onboarded-only: an admin must add the user first). |

**Egress note:** OIDC is the one feature where the **hosted** server reaches out — it calls the
identity provider to verify a login. Everything else stays local. If you want a strictly no-egress
deployment, leave OIDC unconfigured (or run the local stdio server, `python -m mcp_harness`); the
skill and the query path never make a network call regardless.
