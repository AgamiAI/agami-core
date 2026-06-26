# agami-core (library)

The importable core behind agami: the governed **semantic model**, the shared **MCP `TOOLS`
harness** (stdio entrypoint), and the **unified local query executor** (`execute_sql` + the
read-only safety pass + unit formatting).

One package serves every consumer — the local Claude Code skill, the MCP server, and any
downstream that imports the same flat module names.

## Install

```bash
pip install -e packages/agami-core            # executor + stdio harness (pure-stdlib)
pip install -e 'packages/agami-core[model]'   # + the semantic model (pydantic / sqlglot / pyyaml)
```

## Flat module names (an invariant)

`semantic_model`, `mcp_harness`, `execute_sql`, `agami_paths` are top-level importable names —
no `sys.path` manipulation, no parent package — so a consumer's imports resolve unchanged:

```python
from mcp_harness import TOOLS
import semantic_model
import execute_sql
```

## Entry points

```bash
python -m mcp_harness          # the stdio MCP server (Claude Desktop)
python -m execute_sql --sql …  # the local query executor
python -m semantic_model.cli   # the semantic-model CLI (driven by the `sm` launcher)
python -m mcp_http             # the networked HTTP MCP server (see below)
```

## HTTP server — networked, with auth (`python -m mcp_http`)

The `[server]` extra (`pip install -e 'packages/agami-core[server]'`) adds a networked MCP
transport: the same `TOOLS` surface as the stdio server, but over HTTP with OAuth + a small admin
console. It's the self-host shape of the hosted product.

```bash
PUBLIC_BASE_URL=https://your-host \
AGAMI_SIGNING_SECRET=$(openssl rand -hex 32) \
AGAMI_DB_URL=postgresql://… \
AGAMI_ADMIN_USERNAME=you@example.com \
AGAMI_ADMIN_FIRST_NAME=Alex AGAMI_ADMIN_LAST_NAME=Kim \
# the admin's credential — a password and/or a pinned social provider (at least one):
AGAMI_ADMIN_PASSWORD=… \
AGAMI_ADMIN_PROVIDER=google \
AGAMI_OIDC_GOOGLE_CLIENT_ID=… AGAMI_OIDC_GOOGLE_CLIENT_SECRET=… \
python -m mcp_http
```

The admin is identified by **email** (`AGAMI_ADMIN_USERNAME`). Their sign-in method is whatever you
configure: a password, and/or a **pinned** social provider (`AGAMI_ADMIN_PROVIDER` = `google` |
`microsoft`, which must also have its `AGAMI_OIDC_<PROVIDER>_CLIENT_ID/SECRET` set). The admin login
then offers the same Google/Microsoft option as the MCP login. Register **one** OAuth redirect URI
with the provider — `{base}/oauth/oidc/callback` — it serves both the connector and the admin flows.

### One host, two entry points

A deployment is one host (`PUBLIC_BASE_URL`). Everything lives under it:

| URL | Who | What |
|---|---|---|
| `{base}/mcp` | a teammate, in Claude | the **only** URL to add as a custom connector — Claude auto-discovers the OAuth endpoints from it |
| `{base}/admin` | the admin, in a browser | the console to add/enable/disable users |

### Access model — two separate credentials

- **Query surface (`/mcp`)** — gated by a **Bearer JWT** from the OAuth flow. **Any** onboarded user
  who signs in can query (that's the product). No token → `401` + `WWW-Authenticate`, which starts
  Claude's OAuth. Admin-ness does **not** gate `/mcp`.
- **Admin surface (`/admin`)** — gated by a **session cookie** *and* the admin-gate
  (`AGAMI_ADMIN_USERNAME`). The admin signs in with their **pinned** social provider or a password; a
  valid non-admin is refused (and a social identity for the admin email via a *different* provider is
  refused — the pin closes IdP-confusion). An `/mcp` bearer token is useless here (different
  credential). Unset `AGAMI_ADMIN_USERNAME` ⇒ the admin console is disabled entirely.

The admin adds a teammate by **email + name**; the teammate then signs in at the connector. (Letting
that teammate choose their own sign-in method on first login — Google/Microsoft or a self-set
password — is the next increment.)

### Local end-to-end test (HTTPS via a tunnel)

OAuth and the `Secure` admin cookie need HTTPS, so expose the local server through a tunnel:

```bash
# terminal 1 — the server
PUBLIC_BASE_URL=https://<your-subdomain>.trycloudflare.com \
AGAMI_SIGNING_SECRET=$(openssl rand -hex 32) \
AGAMI_DB_URL=sqlite:///$PWD/agami.db \
AGAMI_ADMIN_USERNAME=you@example.com AGAMI_ADMIN_PASSWORD=choose-a-strong-one \
python -m mcp_http

# terminal 2 — the HTTPS tunnel (prints the https URL to use as PUBLIC_BASE_URL)
cloudflared tunnel --url http://127.0.0.1:8000
```

Open `{base}/admin`, sign in, add a user — then add `{base}/mcp` as a connector in Claude.
