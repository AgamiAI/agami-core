<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="plugins/agami/shared/agami-logo-dark.svg">
    <img src="plugins/agami/shared/agami-logo-light.svg" alt="agami" width="240">
  </picture>
</p>

<p align="center"><strong>The trust layer between AI and your data. Local. Private. Yours.</strong></p>

<p align="center"><sub><strong>agami-core</strong> — the fair-code core of <a href="https://agami.ai">Agami</a>.</sub></p>

<p align="center">
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-fair--code-blue.svg" alt="License: fair-code"></a>
  <img src="https://img.shields.io/badge/version-0.3.0-blue" alt="Version">
  <img src="https://img.shields.io/badge/status-pre--public-orange" alt="Status">
  <a href="#quickstart-under-5-minutes"><img src="https://img.shields.io/badge/try%20the%20sample-no%20database%20needed-brightgreen" alt="Try the sample"></a>
</p>

<!-- HERO VISUAL: drop docs/assets/demo.gif (a ~10s sample-flow capture) and uncomment the line below.
     See docs/assets/README.md for what to capture. A visual is the highest-leverage addition here. -->
<!-- <p align="center"><img src="docs/assets/demo.gif" alt="agami: a governed answer with its provenance receipt" width="760"></p> -->

Point an AI agent at a database and it answers by **guessing** — at the join, at what *"revenue"* means, at which rows it's allowed to read. **agami-core** is the governed layer between the agent and your data: it turns your schema into a semantic model where every join is FK-derived or human-approved, every metric is **signed off** by name and role, and every answer ships a **receipt** — the exact SQL, the model version it pinned, and who vouched for each definition. The rules live in the model, **not the prompt**. And it all runs on your machine — credentials, schema, and results never leave it.

## What you get

- ✅ **Governed answers, not guesses** — every join is FK-derived or human-approved; every metric is signed off (name + role) before the runtime trusts it.
- 🧾 **A receipt on every answer** — the literal SQL, the tables touched, the relationships used, and the model version it pinned. Reproducible, auditable.
- 🔒 **Local and private** — runs inside Claude Code via Bash/Read/Write. Credentials, schema, and results never touch a server we operate.
- 🧩 **A portable semantic model** — plain, git-native YAML you own (subject areas, tables, entities, metrics, relationships). No lock-in.
- 🗄️ **Works with your database** — Postgres · Supabase · Redshift · MySQL · Snowflake · BigQuery · SQL Server · Oracle · Databricks · Trino · DuckDB · SQLite.
- 🛠️ **Zero infra** — no backend, no proxy. If you have a DB CLI you have everything; an optional local MCP server lets Claude Desktop use the same model.

## Quickstart (under 5 minutes)

### Fastest path — the built-in sample (no database, no credentials)

agami ships with **Acme Store**, a small local SQLite dataset (commerce +
subscriptions) and a ready-made, signed-off semantic model. You get a governed
answer — with a full receipt — in under a minute, and nothing leaves your machine.

```bash
# 1. Install the plugin (see Install below for the per-host steps).

# 2. Try the sample — no connection needed:
/agami-connect sample      # or just say "I don't have a database" / "try the sample"

# 3. Ask a question — see governance ENFORCED:
who are the top 5 customers by total spend?

# 4. Now watch the model get BUILT — see governance MADE:
/agami-connect reintrospect
```

**Step 3** deliberately crosses a **fan trap** (orders → line items → payments),
where a naive agent silently double-counts. agami's pre-flight catches it and
returns the correct number *with the join receipt* — the trust layer, enforced on
your first query.

**Step 4 is the real point.** Natural-language querying is everywhere; what's rare
is *where the trust comes from*. `reintrospect` re-derives the sample's model from
the live schema — introspect → infer + confidence-score every join → gate metric
sign-off → re-validate examples — so you watch the governed model take shape rather
than just consuming it. To see it built **from a blank slate** (full LLM
enrichment, descriptions and all), run `/agami-connect sample` and pick **"build
the model from scratch so I can see it work"** the first time. Either way it takes
a few minutes — it's the demo of *how* the receipts you saw in step 3 are earned.
Full flow: [docs/usage.md](docs/usage.md).

### Real database

When you're ready to connect your own database:

```bash
# 1. Run connect — picks your DB type, writes a credentials template (first run only)
/agami-connect

# 2. Fill in the template, then save + lock it down
$EDITOR <artifacts_dir>/local/credentials.example
mv <artifacts_dir>/local/credentials.example <artifacts_dir>/local/credentials
chmod 600 <artifacts_dir>/local/credentials

# 3. Re-run connect to introspect: build the semantic model + seed examples
/agami-connect

# 4. Ask a question
how many orders did we ship last month?
```

`/agami-connect` is one-stop: it picks up missing credentials, introspects the
live DB, computes confidence on every entity, auto-approves the high-signal ones
(FK joins, DBA-commented fields, structural names), gates a metric sign-off
*before* generating seed examples, and leaves the low-confidence long tail in an
optional panel that self-approves as you query. Full flow:
[docs/usage.md](docs/usage.md). Credentials per dialect:
[docs/credentials.md](docs/credentials.md).

## Install

> **Platform note.** Validated on **macOS and Linux**. On **Windows** the skills
> need **[Git for Windows](https://git-scm.com/downloads/win)** (Claude Code uses
> Git Bash for its Bash tool); Windows is not yet validated end-to-end. The
> optional local MCP server is pure-stdlib Python and is cross-platform.

The same plugin works across Claude Code CLI, VS Code, and Cursor.

**Claude Code CLI** — in the Claude Code prompt:
```
/plugin marketplace add AgamiAI/agami-core
/plugin install agami-core@agami
```
Verify with `/plugin list` → you should see `agami-core@agami v0.3.0`.

**VS Code / Cursor** — install the **Claude Code** extension, type `/plugin` in the
chat to open **Manage Plugins**, add the `AgamiAI/agami-core` marketplace, then install
`agami-core` from the Plugins tab.

Per-host walkthroughs: [docs/install/](docs/install/).

## The trust layer

Most AI data agents quietly pick a join, quietly pick a definition of "revenue",
and quietly return a number. agami makes every one of those decisions auditable:

- **Confidence + review state on every entry.** Each join, metric, and entity
  carries a flat trust block (`confidence`, `review_state`, and a sign-off
  identity once approved) — no vendor blobs, no scores to tune. DB-declared FKs
  and structural column names auto-approve; everything inferred stays
  `unreviewed` and surfaces in the Review tab.
- **Metrics must be signed off (Rule 1).** A metric needs an approver email, a
  role, and a non-empty `calculation` before the runtime treats it as truth —
  one bad metric skews every report that uses it. Joins & entities are lazy
  (Rule 2): usable while unreviewed, flagged on the receipt until confirmed.
- **Every answer ships a receipt** — the literal SQL, tables + row counts,
  relationships used (with confidence/state), metric definitions with author +
  date, data freshness, and the model snapshot hash. A warning banner appears if
  any unreviewed entry was used. Nothing is silently trusted.
- **Snapshot-pinned + git-native.** The model is YAML under
  `~/agami-artifacts/<profile>/`, `git init`'d on first introspect. Every answer
  records the model snapshot hash, so old answers reproduce exactly and schema
  drift flips affected entries to `stale` instead of silently changing the number.

Full mechanics — the trust block, Rule 1/Rule 2, the review queue, the receipt,
examples validation: **[docs/trust-layer.md](docs/trust-layer.md)**.

## Skills (slash commands)

Natural-language phrasing routes to each skill automatically — "open the review
dashboard" / "save this as a correction" / "introspect my schema" all work without
typing the slash command.

| Command | What it does |
|---|---|
| `/agami-connect` | One-stop setup + introspect: detect/collect credentials, introspect the live DB into the semantic model (tables, columns, PK grain, FK relationships, sensitive-column flags), layer LLM enrichment, generate EXPLAIN-validated seed examples. Validator-gated; `git init` + snapshots. Also `/agami-connect sample` for the no-DB sample. |
| `/agami-query` | Answers a natural-language question: picks examples + relationships, generates and runs SQL, formats the result + chart, and surfaces a provenance receipt. Flags any unreviewed entry it relied on. (Usually you don't type this — plain language routes here.) |
| `/agami-model` | One dashboard to **browse, curate, and sign off** the model: every table/field/metric/entity/join, per-table/column Exclude/Include, edits, new metrics. The **Review** tab is the trust-layer sign-off queue. Open it on the queue with `/agami-model review`. |
| `/agami-save-correction` | Records a correction and routes it to the right home (SQL example, column metadata, display preference, business concept, or a new metric), showing its classification before writing. Attribution surfaces on future answers it influences. |
| `/agami-reconcile` | Point it at a legacy dashboard's CSV; it generates each question, runs it through agami, and shows a side-by-side diff with tolerances. Validate the model against numbers you already trust. |
| `/agami-serve` | Use agami from the **Claude Desktop** app: wires up the optional local MCP server (same tools as the hosted connector, backed by your local model + execution — stdio, read-only, no network). See [docs/mcp-server.md](docs/mcp-server.md). |

## Privacy

agami runs entirely locally — credentials (`chmod 600`), the semantic model, charts,
exports, and dashboards all live under `~/agami-artifacts/`, and the skill never
reads files outside those paths (except your DB tool's auth config, set up on first
connect with your permission). Details: [docs/privacy.md](docs/privacy.md).

## Fair-code vs hosted

agami is **fair-code** (source-available). The laptop plugin — introspection, the portable
semantic model, NL→SQL + local execution, the trust layer, corrections, and the
local MCP server — is **free to self-host for your own team**. Exposing data or the MCP to
people outside your organization is the paid line — the team cloud (a multi-tenant model
registry served over a remote MCP endpoint, shared governed context, always-on evals). The
boundary, stated plainly: [docs/open-vs-hosted.md](docs/open-vs-hosted.md).

## Documentation

- [Quickstart & usage](docs/usage.md) — first-run walkthrough + common workflows
- [Credentials](docs/credentials.md) — every dialect, the connection-method picker
- [The trust layer](docs/trust-layer.md) — confidence, sign-off, receipts, snapshots
- [Format spec](docs/format-spec.md) — the semantic-model layout + a worked example
- [MCP server](docs/mcp-server.md) — use agami from Claude Desktop
- [Troubleshooting & uninstall](docs/troubleshooting.md)
- [Fair-code vs hosted](docs/open-vs-hosted.md) · [Privacy](docs/privacy.md)

## Contributing

Issues + PRs welcome at
[github.com/AgamiAI/agami-core](https://github.com/AgamiAI/agami-core). See
[CONTRIBUTING.md](CONTRIBUTING.md) for test commands and the **version-bump
discipline** — every user-visible change needs a version bump in
`.claude-plugin/marketplace.json` (twice) and
`plugins/agami/.claude-plugin/plugin.json`, or existing installs stay on the cached
old version. Notable changes: [CHANGELOG.md](CHANGELOG.md).

Run the suite with `python3 -m pytest tests/ -q` (covers renderers, validator,
confidence formulas, applier, reconcile parser — everything that doesn't need an
LLM round-trip).

**If agami is useful to you, a ⭐ on the repo genuinely helps others find it.**

## License

**fair-code** (source-available) — the Agami Functional Use License. See [LICENSE](LICENSE) and [NOTICE](NOTICE).

What's free vs. paid (internal use vs. external exposure): [LICENSING.md](LICENSING.md).

Built by [Agami AI](https://agami.ai).
