# agami-core

**The governed trust layer between an AI agent and your database.** The importable core behind the
[agami](https://agami.ai) Claude Code skill and MCP server — the semantic model, the shared MCP
`TOOLS` harness, and the local query executor.

## What it does

Point an AI agent at a database and it answers by **guessing** — at the join, at what *"revenue"*
means, at which rows it's allowed to read. agami-core turns your schema into a **governed semantic
model** where every join is FK-derived or human-approved and every metric is signed off before the
runtime trusts it. Every answer then ships a **receipt** — the exact SQL it ran and the model
version it pinned — so a silent join mistake never reaches you as a confident wrong number.

It runs **locally**: credentials, schema, and query results never leave your machine.

## Most people don't `pip install` this

If you just want to *use* agami, install the **Claude Code plugin** — you don't touch this package
directly:

```
/plugin marketplace add AgamiAI/agami-core
/plugin install agami-core@agami
```

`pip install agami-core` is for the other two audiences: **importing the library** into your own
code, or **self-hosting the MCP server**. Full product docs and the plugin walkthrough live in the
[repository](https://github.com/AgamiAI/agami-core#readme).

## Install

```bash
pip install agami-core            # executor + stdio harness (pure-stdlib)
pip install 'agami-core[model]'   # + the semantic model (pydantic / sqlglot / pyyaml)
pip install 'agami-core[server]'  # + the networked HTTP MCP server (see below)
```

From a checkout, swap in the editable path — `pip install -e 'packages/agami-core[model]'`.

## Importing the library

The top-level names — `semantic_model`, `mcp_harness`, `execute_sql`, `agami_paths` — are a
deliberate flat invariant (no parent package, no `sys.path` juggling), so a consumer's imports
resolve unchanged:

```python
from mcp_harness import TOOLS
import semantic_model
import execute_sql
```

Module entry points:

```bash
python -m mcp_harness           # the stdio MCP server (Claude Desktop)
python -m execute_sql --sql …   # the local query executor
python -m semantic_model.cli    # the semantic-model CLI (driven by the `sm` launcher)
python -m mcp_http              # the networked HTTP MCP server (see below)
```

## HTTP MCP server (`[server]`) — early access, in testing

The `[server]` extra adds a networked MCP transport: the **same `TOOLS` surface** as the stdio
server, but over HTTP with OAuth and a small admin console. It's the self-host shape of the hosted
product — deploy it to your own host and a whole team connects their own Claude to one URL, still
zero-egress.

> 🧪 **Early access.** This team/server layer is usable today but newer than the local single-player
> path — expect the occasional rough edge, and please report anything broken via a
> [GitHub issue](https://github.com/AgamiAI/agami-core/issues). The local library and stdio server
> are the stable path.

A minimal local launch (SQLite store, password admin):

```bash
PUBLIC_BASE_URL=https://your-host \
AGAMI_SIGNING_SECRET=$(openssl rand -hex 32) \
AGAMI_DB_URL=sqlite:///$PWD/agami.db \
AGAMI_ADMIN_USERNAME=you@example.com AGAMI_ADMIN_PASSWORD=choose-a-strong-one \
python -m mcp_http
```

The full detail — the auth/access model, OIDC onboarding, the admin console (Activity + Model
views), and every environment variable — lives in the docs rather than here:

- **Deploy the Docker bundle** → [deploy/README.md](https://github.com/AgamiAI/agami-core/blob/main/deploy/README.md)
- **Manual install + full env-var reference** → [docs/self-hosting.md](https://github.com/AgamiAI/agami-core/blob/main/docs/self-hosting.md)
- **Use agami from Claude Desktop (stdio)** → [docs/mcp-server.md](https://github.com/AgamiAI/agami-core/blob/main/docs/mcp-server.md)

## Links

- **Repository & full docs** — [github.com/AgamiAI/agami-core](https://github.com/AgamiAI/agami-core#readme)
- **Homepage** — [agami.ai](https://agami.ai)
- **License** — fair-code (source-available), the Agami Functional Use License. Self-hosting for your
  own organization is free; serving people outside it needs a commercial license.
