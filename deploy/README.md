# Self-hosting agami (local → team)

Stand up the agami MCP server on your own host so your team can query your semantic model in Claude. We
ship Docker; you deploy it to your own VM/cloud. The default is **secure by construction**: Caddy gives you
automatic HTTPS and is the *only* public service — agami and Postgres stay on the internal network with no
exposed ports.

## What you need
- A host with Docker (a small cloud VM is plenty), with **only ports 80 and 443** open.
- A **hostname** you control (e.g. `agami.acme.com`) with a DNS **A-record → your VM's IP**. (No domain? Use
  the **Cloudflare Tunnel** profile below — no public IP or DNS needed.) TLS and OAuth require a name, not a
  bare IP.
- Your model — the local `agami-artifacts` folder you built with `agami-serve`.

## Deploy (the secure VM bundle — the default)
```bash
git clone <this repo> && cd <repo>/deploy
cp agami.env.example agami.env            # then edit: PUBLIC_BASE_URL (your hostname), admin email + password, DATASOURCE_URL
ln -s /path/to/your/agami-artifacts ./artifacts   # or set AGAMI_ARTIFACTS_DIR in agami.env
./deploy.sh                     # validates agami.env, generates the signing secret, builds + starts
```
That's it. `deploy.sh` runs the preflight (it generates and **persists** `AGAMI_SIGNING_SECRET` — keep `agami.env`,
it's what keeps connected sessions valid across restarts) then `docker compose up`. The container migrates the
database, loads your model into Postgres, and serves. Once Caddy issues the certificate (a few seconds):

- **Your team:** add `https://<your-host>/mcp` as a custom connector in Claude and sign in.
- **You:** manage who's allowed at `https://<your-host>/admin` (sign in with the admin email + password).

## Updating the model
Edit the model locally in Claude → refresh your `artifacts` → `docker compose restart agami`. The container
re-ingests the model on boot. **No rebuild, no new VM, no database access** — the container does the load.

## Variants (toggle `COMPOSE_PROFILES` in `agami.env`)
| You want… | `agami.env` | command |
|---|---|---|
| **Secure VM** (default) | `COMPOSE_PROFILES=bundled-db,edge` | `docker compose up -d` |
| **External / managed Postgres** (e.g. a Cloud-SQL-like service) | `COMPOSE_PROFILES=edge` + set `APP_DATABASE_URL` (a plain `postgresql://…?sslmode=require` URL — not a cloud connector) | `docker compose up -d` |
| **No public IP** (Cloudflare Tunnel) | `COMPOSE_PROFILES=bundled-db,tunnel` + set `CLOUDFLARE_TUNNEL_TOKEN` | `docker compose up -d` |
| **Cloud Run / serverless** (platform gives TLS) | set `APP_DATABASE_URL`; deploy the built image with these env vars | (your platform) |

## Security notes
- **Only Caddy is public** (80/443). agami and Postgres have no published ports — the DB is never reachable
  from the internet. Lock down SSH separately.
- The admin console is gated by the configured admin email + a session cookie; the `/mcp` query surface is
  gated by per-user OAuth. Both need the HTTPS that Caddy provides.
- `agami.env` holds the signing secret and DB creds — it stays on your host and is never committed. **No data ever
  leaves your environment.**
- **Always deploy via `./deploy.sh`** (it runs the preflight). If you `docker compose up` directly without
  having run the preflight, `AGAMI_PUBLIC_HOST` is unset and Caddy fails to start (loudly, never insecurely) —
  run `python -m deploy_preflight agami.env` first.
