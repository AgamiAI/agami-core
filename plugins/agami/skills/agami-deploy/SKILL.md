---
name: agami-deploy
description: "Prepares a ready-to-run, self-hosted agami deploy bundle ON THE USER'S MACHINE so a team can stand up a shareable MCP server their Claude connects to. Conversationally gathers the hard-floor inputs (hostname, admin identity), auto-detects the local model, writes docker-compose.yml + Caddyfile + a filled agami.env (referencing the PUBLISHED image ghcr.io/agamiai/agami-core — no clone, no build), and stages the model artifacts. Generates the signing secret via deploy_preflight; the admin password is typed by the user into the file (never in chat). Then runs `docker compose up` if Docker is local, otherwise prints the exact VM steps + the shareable MCP URL. Username/password auth only on this paved path."
when_to_use: "Use when the user says 'deploy agami', 'self-host agami', 'set up the agami server for my team', 'stand up a shared agami', 'host agami on a VM / in the cloud', '/agami-deploy', or otherwise wants the multi-user HTTP server (not the local single-player setup — that's agami-serve). Requires agami-connect to have run first (needs a semantic model + credentials). This is the TEAM path: it produces an internet-reachable server with OAuth + admin that claude.ai connects to."
---

# agami deploy — prepare a self-host bundle the user ships to their own host

You are preparing a **deploy bundle** on the user's machine so they can stand up the multi-user agami
server (the HTTP MCP server with OAuth + admin) that their team's Claude connects to. The bundle pulls
the **published image** (`ghcr.io/agamiai/agami-core`) — there is **no repo to clone and nothing to
build**. You gather a few inputs, write the bundle locally, and hand off the cloud steps you can't do.

The local mirror of this (single-player, no network, no auth) is `agami-serve` — if the user only wants
agami in their own Claude Desktop, point them there instead.

## HARD RULES (load-bearing — a deploy handles secrets)

1. **Never ask for the admin password (or any secret) in chat — not even temporarily.** The password is
   typed by the **user** directly into the `agami.env` file (Phase 2 hand-off), exactly like `agami-connect`
   does for DB credentials. You never see it.
2. **Never put a secret on a Bash command line.** `prepare_deploy.py` takes only non-secret values;
   `deploy_preflight` generates the signing secret *into the file*. Hosts render Bash calls in chat.
3. **Username/password is the only auth this skill sets up.** Do **not** collect Google/Microsoft client
   id/secret. Social login ships free but is a manual `agami.env` step — point the user at the in-repo deploy
   README if they ask, and move on.
4. **No signup, no license key, no LLM/embedding key.** None are required; don't ask for any.

## Conversation style
Tight and oriented. Print one-line progress markers (`✓ Bundle written to …`, `✓ agami.env validated`).
Be honest about what's the user's clicks (provision the VM, point DNS) vs what you automate.

## Phase −1: Plan-mode preflight
Run the detection logic from [`shared/plan-mode-check.md`](../../shared/plan-mode-check.md). This skill
writes files and may run Docker. If plan mode is active, refuse with: *"I can't prepare a deploy bundle
in plan mode — it writes the bundle + your agami.env and may run Docker. Switch to **Auto** or **Edit
Automatically** mode (Shift+Tab) and re-invoke me."* **DO NOT** write a plan file or call `ExitPlanMode`.

## Phase 0: Preflight
1. **Resolve the environment** — `python3 "$AGAMI_PLUGIN_ROOT/scripts/connect_resolve.py"` prints JSON;
   read `data.artifacts_dir` (the local model dir) and `data.interpreter.python3` (call it `$PY` — the
   interpreter that has the agami-core package; use it for `deploy_preflight`).
2. **Model present** — `<artifacts_dir>/<active_profile>/org.yaml` must exist. If there's no model yet,
   stop and invoke `/agami-connect` first — the deployed server has nothing to serve without one.

## Phase 1: Gather the hard floor, then write the bundle
Ask only these (everything else is defaulted or generated). Prefer one compact exchange:

1. **Hostname** — "What address will your team connect to?" It must be a **hostname, not a bare IP**
   (TLS + OAuth need a DNS name). → `PUBLIC_BASE_URL=https://<host>`.
   - If they have **no domain / can't open ports**, offer the **Cloudflare tunnel** path (profiles
     `bundled-db,tunnel`; they'll add `CLOUDFLARE_TUNNEL_TOKEN` to `agami.env`). The tunnel still needs a
     domain on Cloudflare — it removes the public IP, not the name.
2. **Admin** — first name, last name, work **email** (the email is the admin identity).
3. *(only if they bring their own Postgres)* note it → use profiles `edge` (drops the bundled DB). The
   managed `postgresql://…` URL is a **credential**, so do **not** collect it in chat — after the bundle
   is written, the user sets `APP_DATABASE_URL` in `agami.env` themselves (the same hand-off as the password).

**Confirm where to write the bundle.** Ask: *"Where should I put the deploy bundle? (default `~/agami-deploy`)"*
and use their answer as `--target`. It must **not** be inside the artifacts dir (prepare_deploy rejects that —
it copies the model *out of* artifacts *into* the bundle). Then write it:

```bash
python3 "$AGAMI_PLUGIN_ROOT/scripts/prepare_deploy.py" \
  --target <chosen-dir, default ~/agami-deploy> \
  --artifacts-dir "<data.artifacts_dir>" \
  --public-base-url "https://<host>" \
  --admin-email "<email>" --admin-first "<first>" --admin-last "<last>" \
  --profiles "bundled-db,edge"
```

(Use `--profiles "bundled-db,tunnel"` for the tunnel, or `--profiles "edge"` for managed Postgres — then
have the user set `APP_DATABASE_URL` in `agami.env` by hand, never on the command line.) Report the status
line: `PREPARED <dir>` (fresh) or `PREPARED_KEPT_ENV <dir>` (an existing `agami.env` was preserved — tell the
user to edit it directly to change values).

## Phase 2: Hand off the secrets (then end the turn)
**Open the file for them** so they don't have to hunt for it (it's a plain visible file, `agami.env`, in the
bundle dir): `open -t "<target>/agami.env"` on macOS (opens it in the default text editor); on other platforms
just print the **absolute path**. Then tell the user (do **not** proceed past this in the same turn). These are
credentials — the user types them by hand; you never see them, they stay in the file on their machine:

> Open `<target>/agami.env` (I just opened it for you) and set, then save:
> - **`AGAMI_ADMIN_PASSWORD=`** — a strong admin password.
> - **`DATASOURCE_URL=`** — the warehouse the model queries, as a connection DSN
>   (e.g. `postgresql://user:pass@host:5432/db`; the scheme picks the type). A second datasource uses
>   `DATASOURCE_URL__<NAME>`. *(The warehouse creds live here now — they are **not** shipped in the bundle.)*
> - *(only if you chose managed Postgres)* **`APP_DATABASE_URL=`** — your Postgres URL.
>
> Then tell me to continue.

End the turn here. The user fills the secrets and re-invokes (or says "continue").

## Phase 3: Finalize + deploy
1. **Validate + generate the signing secret:** `$PY -m deploy_preflight ~/agami-deploy/agami.env`. If it
   reports missing inputs (e.g. the password still blank, or a non-https URL), relay them and stop. On
   success it has written `AGAMI_SIGNING_SECRET` + derived `AGAMI_PUBLIC_HOST` into the file.
2. **Bring it up:**
   - **Docker present here** (the user is on the target host, or testing locally) — run
     `cd ~/agami-deploy && ./deploy.sh` (pulls the image + `docker compose up -d`).
   - **No Docker / deploying to a remote VM** — hand off: have them copy `~/agami-deploy` to the host
     (`scp -r` or a synced folder), then run `./deploy.sh` there. Give them the **cloud checklist** below.
3. **Print the share lines:** the connector URL **`<PUBLIC_BASE_URL>/mcp`** (what teammates add in
   claude.ai → Connectors), and the admin console **`<PUBLIC_BASE_URL>/admin`**.

## The cloud steps you can't do (👤 — walk them through it)
- **A VM** (~2 vCPU / 4 GB RAM / 20 GB disk), Ubuntu LTS, **only ports 80 + 443 open** (or the tunnel).
- **DNS:** an **A-record** for their hostname → the VM's public IP. (Skip for the tunnel.)
- **Docker** on the host: `curl -fsSL https://get.docker.com | sudo sh`.
- Then `./deploy.sh` on the host → wait ~30s for Caddy to issue the cert → open `<PUBLIC_BASE_URL>/admin`.

## Updating the model later
They refresh the model locally → re-run `/agami-deploy` (re-stages `artifacts/`) → on the host
`docker compose restart agami`. The server re-ingests the model on boot — no rebuild, no DB access.

## Notes
- This is the **team** path. For a quick local feel of the same tools, that's `agami-serve` (stdio, no
  network). For a fully managed, governed, always-on server, that's the hosted product — see
  `docs/open-vs-hosted.md`.
- The bundle is self-contained and re-shippable: the generated signing secret lives in its `agami.env`, so a
  VM rebuild that re-uses the same bundle keeps every connected Claude working (no reconnect).
