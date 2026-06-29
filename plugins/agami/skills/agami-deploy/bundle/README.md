# Your agami deploy bundle

`/agami-deploy` prepared this folder. It's self-contained — it **pulls** the published image
(`ghcr.io/agamiai/agami-core`), so there's nothing to build and no repo to clone.

## What's here
- `docker-compose.yml` — Caddy (auto-TLS, the only public service) + agami + bundled Postgres.
- `Caddyfile` — TLS for your hostname.
- `.env` — your config (filled by `/agami-deploy`; `deploy_preflight` generated the signing secret).
- `artifacts/` — your semantic model + warehouse credentials (mounted read-only).
- `deploy.sh` — pulls the image and brings the stack up.

## Run it
On a host with Docker + your hostname's DNS A-record pointed at it (or a Cloudflare tunnel):

```sh
./deploy.sh
```

Then open `<PUBLIC_BASE_URL>/admin` to sign in, and share `<PUBLIC_BASE_URL>/mcp` with your team.

## Recommended VM size
2 vCPU / 4 GB RAM / 20 GB disk runs the server + bundled Postgres comfortably for a small team. Open
only ports 80 and 443 (or use the `tunnel` profile to expose nothing inbound).

## Updating the model later
Refresh your model locally, re-run `/agami-deploy` (or re-copy `artifacts/`), then on the host:
`docker compose restart agami` — the server re-ingests the model on boot. No rebuild, no DB access.
