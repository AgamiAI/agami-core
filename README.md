# agami

> **The trust layer between AI agents and your data warehouse. Local. Private. Yours.**

[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
![Version](https://img.shields.io/badge/version-0.1.0-blue)
![Status](https://img.shields.io/badge/status-pre--public-orange)

<!-- TODO(F9): replace placeholder with the recorded 60–90s demo GIF once Sandeep finishes Wk4 production -->
<p align="center">
  <em>[demo GIF — install → connect → review → query → receipt]</em>
</p>

Ask plain-English questions of your **Postgres / MySQL / Snowflake / BigQuery / Redshift / SQLite** database, with a trust layer wrapped around every answer. Your credentials, schema, and query results never leave your machine — `agami` runs entirely inside Claude Code via the built-in Bash / Read / Write tools.

- **No MCP server.** No backend. No `pip install` if you have a native CLI for your DB.
- **Every join is FK-derived or human-approved.** Every metric and named filter is signed off with a name, role, and timestamp. The dashboard tells you which, with the source signal.
- **Every answer ships a receipt** — the literal SQL that ran, the model version it pinned, the relationships used, and the freshness of the source tables.
- **Corrections persist with attribution.** Save a fix once → every subsequent query loads it as a few-shot example, with the original author and date surfaced when it influences a future answer.

---

## Contents

- [Why agami](#why-agami)
- [The trust layer](#the-trust-layer)
- [Quickstart (under 5 minutes)](#quickstart-under-5-minutes)
- [Install](#install) — [Claude Code CLI](#claude-code-cli) · [VS Code](#claude-code-in-vs-code) · [Cursor](#claude-code-in-cursor) · [Cowork](#claude-cowork)
- [Setup credentials](#setup-credentials)
- [Skills (slash commands)](#skills-slash-commands)
- [First-run walkthrough](#first-run-walkthrough)
- [Common workflows](#common-workflows)
- [Privacy](#privacy)
- [Troubleshooting](#troubleshooting)
- [Format reference](#format-reference)
- [Contributing](#contributing)
- [License](#license)

---

## Why agami

Most NL→SQL tools either send your data through a hosted backend (Snowflake-flavored ChatBI, Hex, etc.) or require a heavy local install (a database proxy, a fine-tuned model, a Python package). `agami` does neither — and goes further: it gives a data engineer the mechanical primitives to verify *why* an answer is correct, not just whether it ran.

- **Local execution.** The skill reads your `~/.agami/credentials` file, runs SQL through your existing `psql` / `mysql` / `snowsql` / `bq` / `sqlite3` / DuckDB binary, parses the rows, and shows you the answer. No data path through any server we operate.
- **Zero infra.** Just a Claude Code skill plugin and a tree of YAML files under `~/agami-artifacts/<profile>/`. If you have a DB CLI, you have everything you need.
- **Diffable, git-native.** The semantic model is per-table YAML at `~/agami-artifacts/<profile>/<schema>/<table>.yaml`. `agami-connect` runs `git init` on that tree and commits each introspect — every model change is a diff you can review, blame, and revert.
- **Snapshot-pinned answers.** Every query records the model snapshot hash it ran against. Old answers reproduce exactly. Schema drift flips affected entries to `stale` instead of silently changing the number.
- **Corrections persist with attribution.** When you say "no, the join should be on `customer_id`", the corrected SQL lands in `examples.yaml` with the author and date. When a future answer is influenced by it, the receipt names the correction's author so the audit trail stays clean.

`agami` is open source under the MIT license. The code that runs on your machine is the code in this repo. Read it.

---

## The trust layer

Most "AI BI" tools quietly pick a join, quietly pick a definition of "revenue", and quietly return a number. `agami` makes every one of those decisions auditable — with one knob per workspace and one queue per curator.

### Every entry carries a confidence + a review state

`agami-connect` emits the semantic model with these fields on every join, metric, named filter, and field description:

```yaml
custom_extensions:
  - vendor_name: COMMON
    data: '{"agami": {
       "confidence": 0.62,                # 0.0–1.0, computed from signals
       "signal_breakdown": {              # which signals contributed
         "fk_declared": false,
         "unique_index_match": true,
         "column_type_match": true,
         "column_name_similarity": 0.92,
         "plural_pattern_match": true
       },
       "review_state": "unreviewed",       # unreviewed | approved | rejected | stale | not_applicable
       "origin": "introspect_heuristic",   # fk | introspect_heuristic | column_comment | llm_suggested | human_authored | no_description
       "signed_off_by": null,
       "signed_off_at": null,
       "signed_off_role": null
     }}'
```

Auto-approve rules collapse the queue to what actually needs human eyes:
- **FK declared** in DB metadata → relationship auto-approved (`origin: fk`).
- **DBA-authored column comment** present → field description auto-approved (`origin: column_comment`).
- **Single-column unique index + plural-of-table-name + column-type match** → relationship auto-approved (heuristic).
- **Empty `description` on a field** → marked `not_applicable` (no_description); the dashboard skips the card.

Everything else stays `unreviewed` and surfaces in the review dashboard.

### Rule 1 vs Rule 2

- **Rule 1** (always queue): every `metric` and every `named_filter` that's not yet approved — these have the highest blast radius (one bad metric breaks every report that uses it). Sign-off requires a `signed_off_by` email AND a `signed_off_role` (cfo / cto / data_lead / engineer / analyst / other) AND a non-empty `definition_prose`. The validator enforces all three before a metric can be approved.
- **Rule 2** (slider): every other entry whose `confidence < threshold` (default `0.7`). Lower the threshold to trim the queue; raise it for a Meta-bar trust posture.

At runtime, `agami-query-database` refuses to answer questions that depend on `unreviewed` metrics or named filters (the strict gate). Unreviewed joins / field descriptions surface as warnings in the receipt but don't block.

### The review dashboard

`/agami-review` (or "open the review dashboard") renders an HTML artifact with four tabs — **For Review · Approved Automatically · Manually Approved · Rejected** — grouped by entity type. Each card shows:

- The inferred SQL fragment / definition / mapping
- The signal breakdown that produced the confidence score (✓ FK declared, ✗ no DBA comment, ...)
- An inline editable textarea for the description / `definition_prose`
- Per-card Approve / Reject / Edit buttons + group-level "Approve all in this group"
- For Rule 1 cards: the email + role + role-picker, and a checklist of assumptions to confirm

Click your way through the queue, hit "Generate feedback for Claude" at the bottom, paste back into chat. agami applies each edit, runs the validator, commits the result to `<artifacts_dir>/<profile>/.git/`, and re-renders.

### Every answer ships a receipt

Every `agami-query-database` answer includes a "Provenance for this answer" panel:

- The literal SQL that ran (no paraphrase)
- Tables touched + row count per table
- Relationships used, each with its confidence + review state
- Metric definitions invoked, with author + sign-off date
- Named-filter predicates used (named, not anonymous)
- Source-data freshness per table (when the DB exposes it)
- Model snapshot hash (so the answer is reproducible from `<artifacts_dir>/<profile>/.snapshots/<hash>/`)
- A warning banner if any unreviewed entry was used

### Examples validation

Phase 5 of `agami-connect` generates 10–12 NL→SQL seed examples that each satisfy one of five **analytical shapes**: aggregation with a measure, segmentation, time comparison, filtered top-N with context, or cohort / retention. Plain row-listing is disqualified. Each seed is EXPLAIN-validated against the live DB, then surfaced in an examples-validation dashboard (`~/.agami/examples-validation/<ts>.html`) — same per-card pattern as the review dashboard, with Validate / Reject / Edit / Add note buttons + an inline "Add example" affordance.

---

## Quickstart (under 5 minutes)

```bash
# 1. Install the plugin (any Claude Code variant — CLI / VS Code / Cursor / Cowork)
/plugin marketplace add AgamiAI/LiteBi
/plugin install agami@litebi

# 2. Run connect — picks your DB type, writes ~/.agami/credentials.example
#    (first time only; subsequent runs introspect directly)
/agami-connect

# 3. Edit the template with your DB connection details
$EDITOR ~/.agami/credentials.example
mv ~/.agami/credentials.example ~/.agami/credentials
chmod 600 ~/.agami/credentials

# 4. Re-run connect to introspect: build the per-schema semantic model + seed examples
/agami-connect

# 5. (Optional) walk the trust review queue
/agami-review

# 6. Ask a question
how many orders did we ship last month?
```

`/agami-connect` introspects the live DB, computes confidence on every entity, auto-approves the high-signal ones (FK joins, DBA-commented fields), renders an examples-validation dashboard for the seed NL→SQL pairs, and offers the review dashboard for what needs human eyes. From step 5 onward you're answering questions with the receipt panel showing exactly which entries each answer touched.

---

## Install

The same plugin works across all four hosts. Pick yours:

### Claude Code CLI

```bash
# In any terminal:
claude
```

In the Claude Code prompt:
```
/plugin marketplace add AgamiAI/LiteBi
/plugin install agami@litebi
```

Verify:
```
/plugin list
```
You should see `agami@litebi v0.1.0`.

Detailed walkthrough: [`docs/install/claude-code-cli.md`](docs/install/claude-code-cli.md).

### Claude Code in VS Code

1. Install the **Claude Code** extension from the VS Code marketplace (publisher: Anthropic).
2. Open the Claude pane (Cmd+Shift+P → "Claude Code: Open").
3. In the chat input, run:
   ```
   /plugin marketplace add AgamiAI/LiteBi
   /plugin install agami@litebi
   ```
4. Verify with `/plugin list`.

Detailed walkthrough: [`docs/install/claude-code-vscode.md`](docs/install/claude-code-vscode.md).

### Claude Code in Cursor

1. Install the **Claude Code** extension from the Cursor extensions marketplace (or via `cursor --install-extension anthropic.claude-code`).
2. Open the Claude pane.
3. Run the same `/plugin marketplace add` + `/plugin install` commands.
4. Verify.

Detailed walkthrough: [`docs/install/claude-code-cursor.md`](docs/install/claude-code-cursor.md).

### Claude Cowork

1. Open Claude Cowork in your browser.
2. Settings → Plugins → **Add marketplace** → paste `AgamiAI/LiteBi` → submit.
3. Find `agami` in the plugin list and click **Install**.
4. Verify by typing `@agami` in a Cowork chat — autocomplete should suggest the agami skills.

Detailed walkthrough: [`docs/install/claude-cowork.md`](docs/install/claude-cowork.md).

---

## Setup credentials

`agami` reads database connection details from `~/.agami/credentials`. Same pattern as `~/.aws/credentials`, `~/.dbt/profiles.yml`, `~/.pgpass`.

`/agami-connect` creates a template at `~/.agami/credentials.example` on first run (its Phase 0a — formerly the separate `/agami-init` skill). Edit it and save as `~/.agami/credentials`:

```ini
[default]
type     = postgres
host     = localhost
port     = 5432
database = mydb
user     = myuser
password = mypassword
```

Then **make it readable only by you**:

```bash
chmod 600 ~/.agami/credentials
```

`agami` refuses to read the file unless it's `chmod 600` — the same protection `ssh` uses for private keys.

### Multiple databases

Add more `[<profile>]` sections. Switch with `AGAMI_PROFILE=staging`:

```ini
[default]
type = postgres
host = prod-db.example.com
...

[staging]
type = postgres
host = staging-db.example.com
...
```

### Skip the file with an env var

```bash
export AGAMI_DATABASE_URL=postgres://user:password@host:5432/mydb
```

When set, `~/.agami/credentials` is ignored. Useful for piping in from `op read`, `vault read`, `sops`, etc.

### MySQL example

```ini
[default]
type     = mysql
host     = 127.0.0.1
port     = 3306
database = analytics
user     = analyst
password = secret
```

### Snowflake example

```ini
[finance]
type      = snowflake
account   = xy12345.us-east-1.aws
user      = analyst@example.com
password  = secret
warehouse = COMPUTE_WH
role      = ANALYST_ROLE
database  = ANALYTICS
schema    = PUBLIC
# Or use SSO:
# authenticator = externalbrowser
```

### BigQuery example

```ini
[gcp]
type                = bigquery
project             = my-gcp-project
dataset             = analytics                  # optional default dataset
service_account     = /abs/path/to/key.json      # omit to use Application Default Credentials
location            = US                          # optional, defaults to US
```

### Redshift example

```ini
[warehouse]
type     = redshift
host     = my-cluster.abc123.us-west-2.redshift.amazonaws.com
port     = 5439
database = analytics
user     = readonly
password = secret
sslmode  = require           # default; verify-full / verify-ca / disable all accepted
```

### SQLite example

```ini
[local]
type = sqlite
path = /Users/me/data/local.db
```

### Full format reference

[`plugins/agami/shared/credentials-format.md`](plugins/agami/shared/credentials-format.md) — every field, every database, every edge case.

### No Python required (usually)

The skill picks the first available connection method, in this order:

| Method | What you need | Install if missing |
|---|---|---|
| **Native CLI** | `psql` (Postgres / Redshift) / `mysql` (MySQL) / `snowsql` (Snowflake) / `bq` (BigQuery) / `sqlite3` (SQLite) on `PATH` | `brew install postgresql` / `brew install mysql` / [snowsql download](https://docs.snowflake.com/en/user-guide/snowsql-install-config) / [`gcloud` SDK](https://cloud.google.com/sdk/docs/install) |
| **DuckDB** universal binary | `duckdb` on `PATH` (covers Postgres / MySQL / SQLite, not Snowflake / BigQuery) | `brew install duckdb` (or [duckdb.org](https://duckdb.org/)) |
| **Python driver** (fallback) | Python + `psycopg2-binary` / `pymysql` / `snowflake-connector-python` / `google-cloud-bigquery` | `pip install psycopg2-binary pymysql snowflake-connector-python google-cloud-bigquery` |

`/agami-connect` Phase 0a tells you exactly what to install for your OS if nothing is detected.

---

## Skills (slash commands)

| Command | What it does |
|---|---|
| `/agami-connect` | **One-stop setup + introspect.** First run: detects missing credentials, runs the DB-type picker (Postgres / MySQL / Snowflake / BigQuery / Redshift / Other), writes `~/.agami/credentials.example` for you to fill in, verifies the connection method, and ends the turn. Re-invoke after filling in the file → introspects the live DB, builds the per-schema OSI v0.1.1 semantic model at `~/agami-artifacts/<profile>/`, computes confidence on every entity, auto-approves the high-signal ones, generates 10–12 analytical-shape seed examples (each EXPLAIN-validated), and opens the examples-validation dashboard. Runs `git init` and snapshots the model under `.snapshots/<hash>/`. |
| `/agami-query-database` | Answers a NL question. Picks examples + relationships, generates SQL, runs it, formats the result, and surfaces a SQL receipt panel (provenance + model-version pin). Refuses if any required Rule 1 entry is unreviewed. (You usually don't need to type this — natural language routes here.) |
| `/agami-review` | Opens the trust review dashboard: For Review / Approved Automatically / Manually Approved / Rejected tabs, grouped by entity type. Click-to-act buttons + inline edit textareas. Generates a chat-back-channel command block when you click "Generate feedback for Claude." |
| `/agami-model` | Opens the model explorer — a static HTML browser of every schema / table / field with live search, filter chips, and per-table + per-column **Exclude / Include** buttons. Excluded entries are filtered out of the runtime model (joins, prompts, aggregates) but stay in the YAML for audit; the curator can include them back any time. |
| `/agami-save-correction` | Records a corrected `(question, SQL)` pair to `<artifacts_dir>/<profile>/examples.yaml`, with author + date + classification. The next answer that uses it surfaces the correction's attribution in the receipt. |
| `/agami-reconcile` | Reconciliation harness: point it at a legacy dashboard's CSV (label → number rows) and it generates each NL question, runs it through agami, and shows a side-by-side diff with tolerances. Use to validate the model against numbers you already trust. |

Natural-language phrasing routes to each skill automatically — "open the review dashboard" / "save this as a correction" / "introspect my schema" all work without typing the slash command.

---

## First-run walkthrough

```
$ /agami-connect
[Phase 0: preflight — no credentials yet, running first-time setup]
> Pick your database: PostgreSQL · MySQL · Snowflake · BigQuery · Other
You: Snowflake
✓ Wrote ~/.agami/credentials.example with a [main] section for Snowflake.
  Fill it in (account, user, password OR authenticator=externalbrowser,
  warehouse, role, database, schema) then save as ~/.agami/credentials.

# After filling in the file:
$ /agami-connect
[Phase 0: preflight]
  ✓ ~/.agami/credentials present (chmod 600)
  ✓ Tier detected: snowsql (Tier 2 — native CLI)

[Phase 1: introspect]
  ✓ 14 tables across 1 schema (BUREAU_DATA)
  ✓ 0 FK relationships declared (Snowflake — typical)
  ✓ 23 inferred relationships from column-name + unique-index match

[Phase 2c: trust spine]
  ✓ Confidence computed for every dataset, field, relationship
  ✓ 187 field descriptions auto-approved (DBA column comments)
  ✓ 21 relationships auto-approved (unique-index + plural-pattern match)
  ⚠ 8 metric proposals stamped Rule 1 (need human sign-off)
  ⚠ 14 inferred relationships below threshold 0.7 (need review)

[Phase 3: validate + write]
  ✓ Validator passed (universal trust block + OSI v0.1.1 schema)
  ✓ Wrote per-table YAMLs under ~/agami-artifacts/finbud/BUREAU_DATA/
  ✓ Snapshot pinned at .snapshots/45f0fefa2403/
  ✓ git init + initial commit

[Phase 4 + 5: seed examples + validation dashboard]
  ✓ Generated 11 seed examples (≥6 multi-table, ≥1 time-comparison shape)
  ✓ 11/11 EXPLAIN-validated
  Rendered dashboard: ~/.agami/examples-validation/20260511-204100.html

You (in chat): validate 1, 3, 4, 5, 7 by ashwin@agami.ai
               edit 8 sql>>>
               SELECT ...
               <<<
               done

✓ Validation complete: 6 validated, 1 edited, 4 unreviewed (errors).

[Phase 5.5: trust-layer landing]
  Summary at threshold 0.7:
  ✓ 14 datasets, 312 fields (auto-approved)
  ✓ 21 FK + unique-index relationships (auto-approved)
  ✓ 187 field descriptions from DBA comments (auto-approved)
  ⚠ 14 inferred relationships need review
  ⚠ 8 metric proposals need sign-off (Rule 1)

  46 items need your attention. Open the review dashboard? (y / threshold N / skip)

You: y

[/agami-review opens]
  ~/.agami/review/20260511-204318.html

You (in dashboard): click Approve on 32 cards, Edit on 5, Reject on 9.
                     Hit "Generate feedback for Claude" + paste back.

✓ Applied: 32 approved, 5 edited, 9 rejected. Re-rendered.
  ~/.agami/review/20260511-205400.html

You: how many applicants do we have with a score above 750?
```

The receipt panel on the answer shows the SQL that ran, the relationships used (with their confidence + review state), and the model version (`.snapshots/45f0fefa2403/`). If a query touched an unreviewed entry, the receipt has a warning banner pointing back at `/agami-review`.

---

## Common workflows

### Ask a question

```
top 10 active customers by spend last 30 days
```

The skill loads your model + examples, generates SQL, runs it, returns a markdown table AND a chart (by default — every result gets a chart unless the shape doesn't lend itself to one). The receipt panel below the chart shows the SQL, the tables touched, the relationships used (with confidence + review state), and the model snapshot hash. If a touched table is large (> 1M rows) without a date filter, the skill prompts you before running.

If the question relies on a definition with multiple candidates — e.g. you ask "show me revenue" and there are three `revenue`-synonym metrics — the skill asks you which one, instead of silently picking.

### Open the review dashboard

```
You: open the review dashboard
# or: /agami-review
# or: /agami-review threshold 0.5     ← raise the bar; more items appear
```

Walk the cards. Each shows the inferred SQL + signal breakdown + an inline editable textarea. Click Approve / Reject / Edit on the cards you want, then hit "Generate feedback for Claude" at the bottom and paste back. agami applies each edit, runs the validator, commits to `<artifacts_dir>/<profile>/.git/`, and re-renders to a new timestamped HTML file.

### Browse the model + exclude tables / columns

```
You: open the model explorer
# or: /agami-model
# or: "remove the staging tables and PII columns from the model"
```

Renders a self-contained HTML browser of every schema → table → field. Live search across names + types + descriptions, filter chips (All / Active / Excluded / Unreviewed / Queued for change), per-table + per-column Exclude / Include buttons. Useful when:

- You want PII columns hidden from agami without changing access at the DB level.
- A re-introspect pulled in staging / archive tables you don't want considered.
- You want to scan field names across the whole schema (e.g. "where do we have `created_at` columns?").

Excluded entries flip `agami.review_state` to `rejected`. The runtime model loader filters them out everywhere — they never appear in prompts, never get joined to, never get aggregated. The YAML still has them, so you can re-include later. The HTML is static and rendered by Python; **no LLM tokens are spent on the YAML walk**.

### Save a correction (with attribution)

```
You: top customers should rank by lifetime spend, not just last 30 days
[agami regenerates and shows the corrected query]

You: save this as a correction
[agami records who, when, why_prose to corrections.jsonl and appends a
 new entry to examples.yaml with source: correction]
```

The next answer that uses this correction surfaces the attribution in its receipt: *"this answer was influenced by a correction from ashwin@agami.ai on 2026-05-11: 'use lifetime spend not 30-day window.'"*

### Render a chart

Charts are produced by default for every query result. To request a specific shape:

```
You: make that a bar chart by customer
```

The skill writes `~/.agami/charts/<ts>.html` — self-contained Chart.js, the SQL receipt embedded as a collapsible panel. Supported: `bar`, `line`, `pie`, `doughnut`, `scatter`. Tables paginate at 20 rows.

### Export to CSV

```
You: export this
```

Writes the full result (no row cap) to `~/.agami/exports/<ts>.csv`.

### Reconcile against a legacy dashboard

When you've inherited a number from Tableau / Looker / a spreadsheet and want to verify the model returns the same:

```
You: /agami-reconcile ~/Downloads/q3-revenue-by-region.csv
```

Parses the CSV (auto-detects headers + number formatting — currency, magnitude suffixes, accounting parens, percentages), generates the matching NL question for each row, runs it through agami, and shows a side-by-side diff. Matches are green; mismatches drill into the receipt so you can see *why* the two numbers disagree (typically a definitional disagreement, which is exactly what the trust layer is for).

### Edit the semantic model by hand

Open the per-table YAML at `~/agami-artifacts/<profile>/<schema>/<table>.yaml`. Add a description, refine a metric's `expression`, populate `definition_prose`. Save. The next query picks it up — no skill restart needed.

If you flip a `review_state` from `unreviewed` to `approved` by hand, also set `signed_off_by`, `signed_off_at`, and (for Rule 1) `signed_off_role` — the validator will reject the file otherwise.

Format reference: [`plugins/agami/shared/agami-osi-extensions.md`](plugins/agami/shared/agami-osi-extensions.md).

### Snapshot reproducibility

Every introspect writes the canonical model to `~/agami-artifacts/<profile>/.snapshots/<hash>/`. Every query records that hash in its receipt. To reproduce an old answer exactly, `git checkout` the matching commit in `<artifacts_dir>/<profile>/.git/` — the model that produced the original number is byte-identical.

### Switch profiles (multi-database)

```bash
AGAMI_PROFILE=staging
```

Or in chat: *"switch to the staging profile"*. Per-profile artifacts live under `~/agami-artifacts/<profile>/`; credentials live in the same `~/.agami/credentials` file but under a different `[<profile>]` section.

---

## Privacy

`agami` runs entirely locally. There is **no telemetry** in the 0.x line — no install events, no usage pings, no anonymous metrics. The runtime is silent.

What lives on your machine:
- `~/.agami/credentials` (chmod 600) — DB connection details. Never read by anything outside the skill scripts in this repo.
- `~/.agami/.config` — your reviewer email + role (for trust-layer sign-offs) and optional `artifacts_dir` override.
- `~/agami-artifacts/<profile>/` — the OSI semantic model (per-table YAML), examples, ORGANIZATION.md, snapshots, curation log, `corrections.jsonl`, `.git/` history.
- `~/.agami/charts/<ts>.html` — rendered charts.
- `~/.agami/exports/<ts>.csv` — CSV exports.
- `~/.agami/review/<ts>.html` and `~/.agami/examples-validation/<ts>.html` — review-flow dashboards.

The skill never reads files outside those paths (except your DB tool's auth config — `~/.pg_service.conf`, `~/.snowsql/config`, etc. — which it sets up on first connect with your permission).

The telemetry endpoint code under `services/telemetry-endpoint/` and the spec at `plugins/agami/shared/telemetry-payload.md` are preserved in the repo for a future re-enable that would require explicit opt-in, but they are not invoked by any skill in this build.

---

## Reduce permission prompts (built in)

Claude Code prompts for permission the first time it runs a Bash command pattern. agami ships its allowlist as part of the plugin's `.claude/settings.json` — when you install agami via the marketplace, the host picks up these defaults automatically. No copy-paste step needed.

The shipped allowlist covers the common agami invocation shapes: `psql` / `mysql` / `snowsql` with auth files, the bundled scripts (`execute_sql.py` / `setup_pgauth.py` / `validate_semantic_model.py` / `render_chart.py` / `build_duckdb_attach.py`), `mkdir`/`chmod` on `~/.agami/` and `~/agami-artifacts/`, `open` on chart files, and the GitHub-star ask URL. It does NOT auto-allow arbitrary `psql` / `mysql` invocations against your DB — only the wrapper scripts that read credentials safely.

To override per-user (e.g., to add commands you trust beyond agami), put them in `~/.claude/settings.local.json` — Claude Code merges that on top of the shipped allowlist. That file is gitignored; your additions stay private.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `~/.agami/credentials must be chmod 600` | `chmod 600 ~/.agami/credentials` |
| `psql: command not found` | `brew install postgresql` (or use DuckDB: `brew install duckdb`) |
| `mysql: command not found` | `brew install mysql` (or DuckDB) |
| `bq: command not found` | Install the [`gcloud` SDK](https://cloud.google.com/sdk/docs/install) and run `gcloud components install bq`. Or `pip install google-cloud-bigquery` for the Python path. |
| `snowsql` flag-guessing failures | Snowflake CLI is fussy about flag ordering; use the explicit invocation table in `connection-reference.md`. |
| `connection refused` on a remote DB | Check VPN / firewall, then connect with your native CLI (`psql -h ... -U ...` or `snowsql -a ... -u ...`) directly to confirm. |
| "I don't have a model for `<profile>`" | Tell agami "introspect my schema" or run `/agami-connect`. The skill picks up `AGAMI_PROFILE` automatically. |
| The generated SQL keeps using a column that doesn't exist | The model is stale. Run `/agami-connect` again — it preserves your hand-edits, refreshes from the DB, and surfaces any new entries in the review queue. |
| Query times out on a large table | Add a date filter or `LIMIT`; the skill flags HIGH-risk scans before running. |
| "agami refused to answer because revenue is unreviewed" | Run `/agami-review`, walk the metric card, sign off (or fix the `definition_prose` if it's wrong), then re-ask. |
| Validator rejects a hand-edited YAML | Read the error verbatim — it'll point at the exact line. Most common: Rule 1 metric set to `approved` without `definition_prose`, or `review_state: not_applicable` without `origin: no_description`. |
| Want to switch profiles | `AGAMI_PROFILE=staging` then re-ask the question. |

If you hit a case not in the table, file an issue at [github.com/AgamiAI/LiteBi/issues](https://github.com/AgamiAI/LiteBi/issues) with the exact error, your DB type, and what the validator says (`python3 plugins/agami/scripts/validate_semantic_model.py --directory ~/agami-artifacts/<profile>`).

---

## Format reference

- **Semantic model — OSI v0.1.1 base spec** ([Open Semantic Interchange](https://opensemanticinterchange.org/)) — the universal `version`, `semantic_model`, `datasets`, `fields`, `relationships`, `metrics`, `custom_extensions` shape.
- **agami trust-layer extensions** ([`plugins/agami/shared/agami-osi-extensions.md`](plugins/agami/shared/agami-osi-extensions.md)) — the `agami` keys carried in `custom_extensions[].vendor_name=COMMON` (confidence, signal_breakdown, review_state, origin, signed_off_by/at/role, definition_prose, assumptions, excludes, named_filters, etc.).
- **File layout** ([`plugins/agami/shared/file-layout.md`](plugins/agami/shared/file-layout.md)) — what lives where under `~/agami-artifacts/<profile>/` and how the snapshot directory works.
- **Examples library YAML** — `<artifacts_dir>/<profile>/examples.yaml`, the NL→SQL few-shot library. Entries carry `source: seed|correction|manual`, `state: unreviewed|validated|rejected`, `validated_by`, `validated_at`.
- **Credentials INI** ([`plugins/agami/shared/credentials-format.md`](plugins/agami/shared/credentials-format.md)) — `~/.agami/credentials` (all 6 DB types).
- **Connection methods** ([`plugins/agami/shared/connection-reference.md`](plugins/agami/shared/connection-reference.md)) — how the skill picks between psql / mysql / snowsql / bq / sqlite3 / DuckDB / Python drivers, including per-tier `SELECT 1` probe invocations.
- **Introspect queries** ([`plugins/agami/shared/introspect-queries.md`](plugins/agami/shared/introspect-queries.md)) — the dialect-specific INFORMATION_SCHEMA queries each `agami-connect` run uses.
- **SQL generation rules** ([`plugins/agami/shared/sql-generation-rules.md`](plugins/agami/shared/sql-generation-rules.md)) — safety + grain-guard rules applied before execution.

---

## Uninstalling

Removing the plugin via the Claude Code marketplace UI marks it disabled, but the on-disk cache (and your data + settings) survive in case you reinstall later. To fully clean up:

```bash
# 1. Optional: archive your tuned semantic model first (in case you come back)
tar czf ~/agami-backup-$(date +%Y%m%d).tar.gz ~/agami-artifacts ~/.agami

# 2. Remove the plugin's on-disk cache (Claude Code doesn't auto-purge this)
rm -rf ~/.claude/plugins/cache/litebi
rm -rf ~/.claude/plugins/cache/agami-skills   # only if you also installed our pre-LiteBi marketplace

# 3. Remove your data (only if you're sure you don't want it back)
#    Snapshot files are intentionally immutable — chmod first so rm can delete them.
chmod -R u+w ~/agami-artifacts 2>/dev/null
rm -rf ~/agami-artifacts                      # semantic model, examples, ORGANIZATION.md, USER_MEMORY.md, .snapshots/, .git/
rm -rf ~/.agami                               # credentials, .config, charts, exports, review + examples-validation dashboards

# 4. Restart Claude Code (full quit, not just close window)
```

If the slash commands `/agami-connect`, `/agami-query-database`, etc. still appear after step 4, you have another LiteBi version cached at a different path. `find ~/.claude -type d -name "litebi*"` will show every copy.

## Contributing

Issues + PRs welcome at [github.com/AgamiAI/LiteBi](https://github.com/AgamiAI/LiteBi). See [CONTRIBUTING.md](CONTRIBUTING.md) for the test commands and the **version-bump discipline** — every user-visible change needs a version bump in `.claude-plugin/marketplace.json` (twice) and `plugins/agami/.claude-plugin/plugin.json`, otherwise existing installs stay on the cached old version forever.

A community Discord will land soon — once it's live the link will appear here and in [`agami-connect/SKILL.md`](plugins/agami/skills/agami-connect/SKILL.md).

---

## License

MIT. See [LICENSE](LICENSE).

Built by [Agami AI](https://agami.ai).
