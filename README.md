# agami

> **Lightweight BI for Claude. Local. Private. Yours.**

[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
![Version](https://img.shields.io/badge/version-1.0.0-brightgreen)

<!-- TODO(F9): replace placeholder with the recorded 60–90s demo GIF once Sandeep finishes Wk4 production -->
<p align="center">
  <em>[demo GIF — install → connect → query → chart → save-correction]</em>
</p>

Ask plain-English questions of your **Postgres** or **MySQL** database. Your credentials, schema, and query results never leave your machine — `agami` runs entirely inside Claude Code via the built-in Bash / Read / Write tools.

- **No MCP server.** No backend.
- **No `pip install`.** No Python required if you have `psql`, `mysql`, or [DuckDB](https://duckdb.org/).
- **Corrections persist.** Save a fix once → every subsequent query loads it as a few-shot example. The model gets sharper the more you use it.

---

## Contents

- [Why agami](#why-agami)
- [Quickstart (under 5 minutes)](#quickstart-under-5-minutes)
- [Install](#install) — [Claude Code CLI](#claude-code-cli) · [VS Code](#claude-code-in-vs-code) · [Cursor](#claude-code-in-cursor) · [Cowork](#claude-cowork)
- [Setup credentials](#setup-credentials)
- [First-run walkthrough](#first-run-walkthrough)
- [Common workflows](#common-workflows)
- [Privacy + telemetry](#privacy--telemetry)
- [Troubleshooting](#troubleshooting)
- [Format reference](#format-reference)
- [Contributing](#contributing)
- [License](#license)

---

## Why agami

Most NL→SQL tools either send your data through a hosted backend (Snowflake-flavored ChatBI, Hex, etc.) or require a heavy local install (a database proxy, a fine-tuned model, a Python package). `agami` does neither.

- **Local execution.** The skill reads your `~/.agami/credentials` file, runs SQL through your existing `psql` / `mysql` / `duckdb` binary, parses the rows, and shows you the answer. No data path through any server we operate.
- **Zero infra.** Just a Claude Code skill plugin and a tiny YAML file at `~/.agami/<dbname>.yaml`. If you have `psql`, you have everything you need.
- **Corrections persist.** When you say "no, the join should be on `customer_id`", we append your corrected SQL to `~/.agami/<dbname>-examples.yaml`. Every future query loads the entire examples library into the prompt — Claude weighs them and picks what's relevant. No embeddings, no fine-tune, no second tool to manage.

`agami` is open source under the MIT license. The code that runs on your machine is the code in this repo. Read it.

---

## Quickstart (under 5 minutes)

```bash
# 1. Install the plugin (any Claude Code variant — CLI / VS Code / Cursor / Cowork)
/plugin marketplace add AgamiAI/LiteBi
/plugin install agami@litebi

# 2. Run init — creates ~/.agami/, writes a credentials template
@agami init

# 3. Edit the template with your DB connection details
$EDITOR ~/.agami/credentials.example
mv ~/.agami/credentials.example ~/.agami/credentials
chmod 600 ~/.agami/credentials

# 4. Ask a question
@agami how many orders did we ship last month?
```

That's it. The skill auto-introspects on the first query, generates seed examples, runs a demo query for you to confirm, then answers your question.

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
You should see `agami@litebi v1.0.0`.

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

The `init` skill creates a template at `~/.agami/credentials.example`. Edit it and save as `~/.agami/credentials`:

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

### Full format reference

[`plugins/agami/shared/credentials-format.md`](plugins/agami/shared/credentials-format.md) — every field, every database, every edge case.

### No Python required

The skill drives execution through one of three tiers (highest available):

| Tier | What you need | Install if missing |
|---|---|---|
| 1 — native CLI | `psql` (Postgres) / `mysql` (MySQL) / `sqlite3` (SQLite) on `PATH` | `brew install postgresql` / `brew install mysql` |
| 2 — DuckDB universal binary | `duckdb` on `PATH` | `brew install duckdb` (or [duckdb.org](https://duckdb.org/)) |
| 3 — Python driver (optional) | Python + `psycopg2-binary` / `pymysql` | `pip install psycopg2-binary pymysql` |

If you have **none** of those, the `init` skill tells you exactly what to install for your OS. The recommended fallback for non-developers is `brew install duckdb` — one binary, talks to everything.

---

## First-run walkthrough

The first time you ask a data question, the skill runs an auto-setup flow. Here's what happens:

```
You: @agami how many orders shipped in May?

[agami runs Phase 0: state check]
  ✓ ~/.agami/ exists (chmod 700)
  ✓ ~/.agami/credentials present (chmod 600)
  ✓ Tier detected: psql (tier 1)

[agami invokes the connect skill — first time only]
  Found 4 tables across 1 schema.
  Introspecting columns + foreign keys...
  ✓ 4 tables, 18 columns, 3 relationships
  ✓ Wrote ~/.agami/shop.yaml

  Generating seed examples (8–15 NL→SQL pairs)...
  Validating each via EXPLAIN against the live DB...
  ✓ 10 examples written, 0 rejected
  ✓ Wrote ~/.agami/shop-examples.yaml

  Here's a demo question to test that everything's wired up:

  "Top 5 customers by total spend"

  Generated SQL:
    SELECT c.name, SUM(i.quantity * i.unit_price) AS total_spend
    FROM customers c
    JOIN orders o ON o.customer_id = c.id
    JOIN order_items i ON i.order_id = o.id
    GROUP BY c.id, c.name
    ORDER BY total_spend DESC
    LIMIT 5

  | name           | total_spend |
  |----------------|-------------|
  | Carol Chen     | 148.95      |
  | Bob Brown      | 45.00       |
  | Dave Davis     | 39.98       |
  | Alice Anderson | 74.97       |

  Does this look right?
  [Yes (Recommended)] [No — let me fix it] [Skip]

You: Yes

[agami answers the original question]
  6 orders were placed in May, 4 of which have shipped.

  | status     | count |
  |------------|-------|
  | shipped    | 4     |
  | pending    | 1     |
  | cancelled  | 1     |
```

Then it asks once for telemetry consent (Phase 4 of `init`) and once for email-update opt-in (after the first successful query). Both default to **off**.

---

## Common workflows

### Ask a question

```
@agami top 10 active customers by spend last 30 days
```

The skill loads your model + examples, generates SQL, runs it, returns a markdown table. If a touched table is large (> 1M rows) and you didn't include a date filter, it'll prompt you before running.

### Save a correction

When the answer's slightly off:

```
You: top customers should rank by lifetime spend, not just last 30 days
[agami regenerates and shows the corrected query]

You: /save-correction
[agami appends the (question, corrected_sql) pair to ~/.agami/<dbname>-examples.yaml]
```

The next time you (or anyone using your `~/.agami/`) asks a similar question, the corrected SQL is in the prompt as a few-shot example.

### Render a chart

```
You: make that a bar chart by customer
```

The skill writes `~/.agami/charts/<ts>.html` — a self-contained file with [Chart.js](https://www.chartjs.org/) embedded. Open it in any browser.

Supported types: `bar`, `line`, `pie`, `doughnut`, `scatter`. The skill picks one based on result shape; override with `--chart line`.

### Export to CSV

```
You: export this
```

Writes the full result (no row cap) to `~/.agami/exports/<ts>.csv`.

### Edit the semantic model by hand

Open `~/.agami/<dbname>.yaml` in your editor. Add a `description` to a column, a `measure` to a table, an `entity` mapping. Save. The next query picks it up — no skill restart needed.

Format reference: [`docs/format-spec.md`](docs/format-spec.md).

---

## Privacy + telemetry

`agami` ships with **all telemetry off by default**. The `init` skill asks once — you can change your mind any time by editing `~/.agami/.config` or asking the skill to "turn off analytics".

Full payload allowlist + plain-English what-we-send / what-we-never-send: [`docs/privacy.md`](docs/privacy.md).

Source-of-truth field allowlist (used by both client and server): [`plugins/agami/shared/telemetry-payload.md`](plugins/agami/shared/telemetry-payload.md).

There are 11 fields. None of them contain query text, schema content, result data, hostnames, paths, or PII. The Cloudflare Worker that receives the events ([`services/telemetry-endpoint/`](services/telemetry-endpoint/)) re-validates against the same allowlist server-side — defense in depth even if the open-source client is tampered with.

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `~/.agami/credentials must be chmod 600` | `chmod 600 ~/.agami/credentials` |
| `psql: command not found` | `brew install postgresql` (or use DuckDB: `brew install duckdb`) |
| `mysql: command not found` | `brew install mysql` (or DuckDB) |
| `psycopg2 not importable` (you didn't ask for tier 3) | Ignore — tier 1 or 2 should cover you |
| `connection refused` on a remote DB | Check VPN / firewall, then `psql -h <host> -p <port> -U <user>` directly to confirm |
| "I don't have a model for `<dbname>`" | Run `@agami connect` to introspect the schema |
| The generated SQL keeps using a column that doesn't exist | The model is stale. Run `@agami connect reintrospect` |
| Query times out on a large table | Add a date filter or `LIMIT`; the skill flags HIGH-risk scans before running |
| Want to switch profiles | `AGAMI_PROFILE=staging` then re-ask the question |

If you hit a case not in the table, file an issue at [github.com/AgamiAI/LiteBi/issues](https://github.com/AgamiAI/LiteBi/issues) with the exact error and the output of `@agami init verify`.

---

## Format reference

- **Semantic model YAML** ([`docs/format-spec.md`](docs/format-spec.md)) — what `~/.agami/<dbname>.yaml` looks like.
- **Examples library YAML** (same doc) — the format for `~/.agami/<dbname>-examples.yaml` (NL→SQL few-shots).
- **Credentials INI** ([`plugins/agami/shared/credentials-format.md`](plugins/agami/shared/credentials-format.md)) — `~/.agami/credentials`.
- **Telemetry payload** ([`plugins/agami/shared/telemetry-payload.md`](plugins/agami/shared/telemetry-payload.md)) — what gets sent if you opt in.
- **Connection / tier model** ([`plugins/agami/shared/connection-reference.md`](plugins/agami/shared/connection-reference.md)) — how the skill picks an execution path.

---

## Contributing

Issues + PRs welcome at [github.com/AgamiAI/LiteBi](https://github.com/AgamiAI/LiteBi).

To run the integration tests locally:

```bash
cd tests/integration
docker compose up -d              # spins up Postgres + MySQL fixtures
./test_postgres_e2e_cli.sh        # tier 1
./test_mysql_e2e_cli.sh
./test_postgres_e2e_duckdb.sh     # tier 2 (skipped if duckdb not on PATH)
docker compose down -v
```

Privacy invariant tests (no DB required):

```bash
pytest tests/test_telemetry_privacy.py -v
```

When adding a feature that touches telemetry, the privacy test must still pass — the allowlist is the contract.

A community Discord will land soon — once it's live the link will appear here and in [`init/SKILL.md`](plugins/agami/skills/init/SKILL.md).

---

## License

MIT. See [LICENSE](LICENSE).

Built by [Agami AI](https://agami.ai).
