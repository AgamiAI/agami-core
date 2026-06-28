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
AGAMI_ADMIN_PASSWORD=… \
AGAMI_ADMIN_PROVIDER=google \
AGAMI_OIDC_GOOGLE_CLIENT_ID=… AGAMI_OIDC_GOOGLE_CLIENT_SECRET=… \
python -m mcp_http
```

The admin is identified by **email** (`AGAMI_ADMIN_USERNAME`). Their sign-in method is whatever you
configure — **a password and/or a pinned social provider, at least one**: set `AGAMI_ADMIN_PASSWORD`,
and/or `AGAMI_ADMIN_PROVIDER` (`google` | `microsoft`, which must also have its
`AGAMI_OIDC_<PROVIDER>_CLIENT_ID/SECRET` set). The admin login then offers the same Google/Microsoft
option as the MCP login. Register **one** OAuth redirect URI with the provider —
`{base}/oauth/oidc/callback` — it serves both the connector and the admin flows.

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

### Onboarding a teammate

The admin adds a teammate by **email + name** (a *pending* user). How they finish setting up follows
the deployment's **single auth method** — uniform for everyone, set by what you configured:

- **OIDC deployment** (a Google/Microsoft client is configured): the teammate just adds `{base}/mcp` to
  Claude and signs in with that provider — the IdP verifies their email and binds the account on first
  login. No link to share.
- **Password deployment** (no OIDC configured): the admin **copies the teammate's setup link** from the
  Users tab and shares it out-of-band; the teammate opens it and sets their own password. The link is a
  signed, time-boxed token and is single-use (it stops working once the account is set up).

The login surfaces show only the configured method (the admin keeps a password **break-glass** fallback
on `/admin/login`).

> **Trust note.** OIDC onboarding binds the account to whoever first proves the teammate's email at the
> configured IdP — so add a teammate by an email the *right* person controls there. The setup link and
> the other pre-auth endpoints aren't rate-limited in-process; put them behind your proxy/LB if exposed.

### Activity views

The admin console also has two read-only activity tabs:

- **Tool calls** — *every* MCP tool call, newest first: who (the authenticated user), the tool,
  datasource, and for a query the SQL, row count, latency, and status. This is **audit-grade** — the
  server observes it directly, so it's always accurate.
- **Sessions** — those queries grouped into a conversation, and *within* it into **turns**: each turn
  is one user question and the **N agent queries** Claude ran to answer it (*"User asked X → 2
  queries"*). This is **best-effort** — the MCP protocol carries neither the user's question, a
  conversation id, nor a turn boundary, so Claude self-reports them: a `user_question` (kept verbatim),
  a `thread_id` (per conversation), and a `correlation_id` (per turn). The turn's question is taken from
  the **first** call in the turn (the model sometimes drifts it on later refinements). When Claude
  doesn't supply a `correlation_id`, each query simply shows as its own turn — the view degrades, never
  errors. Treat the self-reported fields as a hint, not a record.

The `tool_calls` log grows one row per call and has **no automatic retention** — it's your local store,
so prune it on your own schedule if it gets large.

### Model view

The **Model** tab (`{base}/admin/model`) is a **read-only** explorer of the semantic model you've
deployed — the same tree the MCP tools serve, so it can't drift from what Claude actually reads. It's a
catalog: a browse rail (datasource → subject area → table) and one page at a time —

- a **datasource overview** (description, glossary, storage-connection names/types, the subject areas),
- a **subject-area landing** (its tables, metrics, entities),
- a **table page** — the schema, with each column's type and description, and flags only where they
  carry signal (**PK / FK / sensitive / unit / enum / caveat**). Trust is shown as a single table-level
  **confidence** badge; **caveats** (the domain gotchas) are elevated to a callout; wide tables collapse
  behind "show all N", and tables that author `column_groups` render those as collapsible sections,
- a **Relationships** page (when the model has cross-area joins) — the org-level relationships that
  span subject areas, **grouped by area-pair**, so the cross-area topology is readable in one place,
- a **domain-context** page (your `ORGANIZATION.md`, rendered as safe markdown).

It is **read-only by construction** — a single GET endpoint, no write path. Editing the model stays
conversational in Claude (the in-app editor is a Hosted feature); connection **credentials are never
rendered** (names and types only). Deploy a change in Claude and it shows up here on the next deploy.

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
