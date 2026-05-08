---
name: agami-query-database
description: "Answers natural-language questions about the user's database. Loads the OSI v0.1.1 semantic model and few-shot examples from the .agami home directory, generates SQL by composing OSI datasets/fields/relationships/metrics into a prompt (and reading Agami extensions for type info, choice fields, and performance hints), executes it locally via the user's chosen tool (psql / mysql / snowsql / sqlite3 native CLI, DuckDB binary, or the Python driver `execute_sql.py`), returns results as a markdown table with optional CSV export, and renders Chart.js HTML charts on request. All execution is local — no data leaves the machine."
when_to_use: "Use when the user asks 'how many', 'show me', 'top N', 'trend over time', 'compare', 'breakdown by', 'group by', 'average', or any other data question against their configured database. Also use for CSV export ('export this'), chart rendering ('make that a bar chart'), or to follow up on a previous result ('drill into the EU region')."
argument-hint: "[question] [--csv] [--chart bar|line|pie|doughnut|scatter]"
---

# agami query-database

You answer the user's natural-language question about their database. Goal: generate correct SQL from the OSI semantic model + the few-shot examples, execute it locally, return rows + an insight, and offer a chart / export when appropriate. Everything runs on the user's machine.

This skill orchestrates:

1. **Setup** (once per session) — load the OSI model and examples library, verify the configured database tool still works.
2. **Generate SQL** — compose a prompt from the OSI structure (datasets/fields/relationships/metrics + Agami extensions for type info / choice fields / performance hints), produce one SQL statement, run safety checks.
3. **Execute** — run via the chosen tool; auto-retry on classified errors; risk-assess large-table queries.
4. **Present** — markdown table; CSV via `--csv` or "export this"; Chart.js HTML via `--chart` or "make that a chart".
5. **Log + post-install GitHub-star ask + telemetry** — write `~/.agami/query_log.jsonl`, ask the user (once, after first successful query) to star us on GitHub, flush telemetry queue.

For the OSI format spec: [`shared/schema-reference.md`](../../shared/schema-reference.md).
For Agami's `custom_extensions`: [`shared/agami-osi-extensions.md`](../../shared/agami-osi-extensions.md).
For SQL safety: [`shared/sql-generation-rules.md`](../../shared/sql-generation-rules.md).
For dialect-specific syntax: [`shared/dialect-rules.md`](../../shared/dialect-rules.md).
For connection method + execution: [`shared/connection-reference.md`](../../shared/connection-reference.md).
For DB error classification: [`shared/db_error_classifier.md`](../../shared/db_error_classifier.md).
For chart template: [`shared/chart-template.html`](../../shared/chart-template.html).
For telemetry payload allowlist: [`shared/telemetry-payload.md`](../../shared/telemetry-payload.md).

## Invocation conventions

**Read [`shared/invocation-conventions.md`](../../shared/invocation-conventions.md) before suggesting any slash command in chat.** All four agami slash commands (`/agami-init`, `/agami-connect`, `/agami-query-database`, `/agami-save-correction`) work — the `agami-` prefix avoids collision with Claude Code's built-in `/init` and other plugins. Never write the un-prefixed forms (`/init`, `/connect`, `/query-database`, `/save-correction`) or colon-namespaced forms (`/agami:init`, etc.) — those don't exist.

For chat replies, **prefer natural language over slash commands** — it reads better and the skill's `when_to_use` matcher routes correctly:

- Re-introspect the schema → "say 'reload the schema'" or "say 'reintrospect my database'"
- Save a correction → "say 'save this as a correction'" or "say 'remember this'"
- Ask a data question → just type the question
- Set up agami / switch profiles → `/agami-init` (the one place the slash form is genuinely cleaner than natural language)

## Conversation style

- **One question per turn unless they're truly bundled.**
- **Use AskUserQuestion sparingly** — only when the user must pick before the skill can proceed (large-table HIGH-risk approval, the post-install GitHub-star ask, the demo-query Yes/No/Skip in agami-connect). **Do NOT use AskUserQuestion for follow-up suggestions** — those are 5 plain numbered bullets per Phase 4f.
- **Insights, not narration** — lead with the answer ("Carol Chen has the highest spend at $148.95"), not the SQL or the process.
- **Round numbers in prose**, exact in the table.
- **Don't echo the SQL in chat prose** — that's enforced as a hard rule in Phase 2. Don't paste the raw Bash CSV — Phase 3.

---

## Phase −1: Plan-mode check

Run the detection + ask logic from [`shared/plan-mode-check.md`](../../shared/plan-mode-check.md). agami-query-database needs Bash (SQL execution) and Write (chart HTML) — both are blocked in plan mode. If the user picks `Stay in plan mode`:

- **Reopen-last-chart intent** (Phase 2a.1 below) — re-displaying an existing HTML chart only needs `Read` plus `open <path>`. Run that flow if matched.
- **Anything else** — refuse: "I can't run SQL in plan mode. Switch to Default or Auto-accept and re-invoke."

If plan mode is not active, skip this phase silently and go to Phase 1.

---

## Phase 1: Setup (once per session)

### HARD RULES — connection rules

These are non-negotiable.

1. **Connect ONLY to the host/port/database/user/password in `~/.agami/credentials`** (or `AGAMI_DATABASE_URL` if set). Never substitute `localhost` or any other host as a fallback. Never connect to anywhere not in the credentials.
2. **Never ask the user for connection details in chat.** If credentials are missing, stop and invoke the agami-init skill — that flow walks the user through editing the credentials file.
3. **Never scan or guess.** No `pgrep`, no `ps`, no `find /`, no `ls /Applications/Postgres.app`, no listing port-listeners. The only Bash probes allowed during setup are `which <tool>` for a database tool on `PATH` and `python3 -c 'import <module>'` for a Python driver.
4. **NEVER put the password (or any credential field) in a Bash command line.** That includes `export PGPASSWORD='<value>'`, `export MYSQL_PWD='<value>'`, `psql -W <password>`, `mysql -p<password>`, or any heredoc / stdin form that interpolates the password. Hosts render Bash tool calls as collapsibles in their UI — anything in the command becomes visible in the chat. Use the auth files generated by `scripts/setup_pgauth.py` (see [`shared/connection-reference.md → HARD RULES`](../../shared/connection-reference.md)). For native CLI queries the visible Bash command is `PGPASSFILE=$HOME/.agami/.pgpass psql -h ... -U ... -d ... -c "$SQL" --csv`. For the Python driver path use `python3 scripts/execute_sql.py --sql-file ...`.

These rules apply to every phase of this skill, not just Phase 1.

### 1a — credentials check (binding)

Read `~/.agami/credentials` (or check `AGAMI_DATABASE_URL`). If neither exists, invoke the agami-init skill and **stop this skill**. Do not continue to load the OSI model. Do not run any other Bash commands.

### 1b — load the OSI model (per-schema layout)

Resolve `<profile>` in this order: `AGAMI_PROFILE` env var → `active_profile` field in `~/.agami/.config` → literal string `"default"` (legacy fallback).

Resolve `<artifacts_dir>` per [`shared/file-layout.md → Configuring artifacts_dir`](../../shared/file-layout.md#configuring-artifacts_dir): `AGAMI_ARTIFACTS_DIR` env var → `~/.agami/.config.artifacts_dir` → default `$HOME/agami-artifacts`.

Look for the model in this priority order:

1. **`<artifacts_dir>/<profile>/index.yaml`** — current layout (v1.2+). Read `index.yaml` first, then every `<schema>.yaml` it references (`schemas[].file`). Build a merged in-memory view from all the loaded schema yamls plus `index.yaml.cross_schema_relationships[]`.
2. **`<artifacts_dir>/<profile>/index.yaml`** — v1.1 layout (under the secrets dir, before the artifacts split). If this exists but `<artifacts_dir>/<profile>/` doesn't, surface a one-line message ("Detected v1.1 layout — your model is under `~/.agami/`. Say 'reload the schema' to move it to the sharable artifacts dir, or I'll keep using the old location for now.") and read it as-is.
3. **`~/.agami/<profile>.yaml`** — v1.0 single-file layout. If this exists but neither directory does, surface: "Detected v1.0 layout — migrating to per-schema directory takes ~30–90s. Say 'reload the schema' to migrate, or I'll keep using the old file for now."
4. **None of the above** → invoke the `agami-connect` skill.

For directory-layout, sanity-check:
- `index.yaml.version: "0.1.1"` (warn but proceed if different)
- Each `<schema>.yaml` has `version: "0.1.1"` and `semantic_model[0]` with a `name` and `datasets`

Cache the parsed merged model in working memory for the rest of the session. Build:

```text
schemas_by_name : { schema_name → list of dataset objects (with their schema attached) }
```

so Phase 2b can choose to render the prompt grouped by schema (helps the LLM disambiguate when datasets in different schemas share names).

### 1c — index the model for fast access

Build these in-memory views you'll reference repeatedly during SQL generation. For directory-layout the inputs are the merged datasets / relationships / metrics across every loaded `<schema>.yaml`, plus `index.yaml.cross_schema_relationships[]`.

```text
datasets_by_name : { dataset.name → dataset object }                           # bare names
datasets_by_qname: { "<schema>.<dataset>" → dataset object }                   # qualified
fields_by_qname  : { "<dataset.name>.<field.name>" → field object }
relationships_by_endpoints : { (from, to) → relationship object }              # both within-schema and cross-schema
metrics_by_name  : { metric.name → metric object }
```

Cross-schema relationships from `index.yaml.cross_schema_relationships[]` are merged into `relationships_by_endpoints` keyed by qualified `<schema>.<dataset>` endpoints. Within-schema relationships are keyed by bare `<dataset>` names. The graph traversal in Phase 2b's join-path picker handles both transparently.

If two schemas have a same-named dataset, prefer the qualified form in prompts and warn the user once: `Note: 'users' exists in both 'public' and 'archive' — disambiguating by schema in this query.`

For each field, also extract:
- `type`     ← `agami.type` from `custom_extensions[].vendor_name=COMMON` JSON. If the extension is absent, fall back to inferring from the SQL expression (treat unknown as `string`).
- `choice_field` ← `agami.choice_field` if present (used for synonym matching: "closed-won deals" → `WHERE stage_name = 'Closed Won'`).
- `unit`     ← `agami.unit` if present (used for currency / percentage formatting in result presentation).
- `is_time`  ← `dimension.is_time` if present.

For each dataset, extract `agami.performance_hints` if present — feeds Phase 2d risk assessment.

For each relationship, treat as a directed JOIN edge in a graph: `from` → `to` via `from_columns`/`to_columns`. The SQL generator uses this graph to pick the shortest join path between two datasets the user references.

### 1d — load the examples library

Read `<artifacts_dir>/<profile>/examples.yaml` (current layout). Fall back to `~/.agami/<profile>-examples.yaml` if only the v1.0 layout exists. Take the **most recent 50** entries (newest `created_at` first).

If empty → warn the user, e.g. "I don't have any few-shot examples for this database yet — answers may be lower quality. Say 'introspect the schema' or 'connect to my database' and I'll seed the examples library." (Natural language reads better than a slash command in chat — `/agami-connect` works too, but only suggest the slash form when the user specifically asks "what do I type?".)

### 1d.1 — load USER_MEMORY.md

Read `<artifacts_dir>/USER_MEMORY.md` (if present). Strip HTML comments (`<!--...-->`), then keep the rest. If the file is missing, treat it as empty — never error. See [`shared/user-memory-format.md`](../../shared/user-memory-format.md) for what's in it.

This file holds free-form **user preferences across every database** (default filters, display preferences). Inject it into the SQL-generation prompt in Phase 2b under a labeled `## User memory (preferences and policies)` section — the LLM uses it as steering context.

### 1d.2 — load ORGANIZATION.md

Read `<artifacts_dir>/<profile>/ORGANIZATION.md` (if present). Strip HTML comments. If missing or empty, treat as empty — never error. See [`shared/organization-context-format.md`](../../shared/organization-context-format.md).

This file holds **domain context for this specific database** (terminology, key metrics, what the data represents). Inject into the SQL-generation prompt in Phase 2b under `## Organization context`, **before** the `## User memory` section — domain knowledge precedes display preferences in the LLM's reading order.

Order in Phase 2b prompt:
1. Schema context (datasets / fields / relationships from the OSI model)
2. `## Organization context` ← from ORGANIZATION.md
3. `## User memory (preferences and policies)` ← from USER_MEMORY.md
4. Few-shot examples
5. The user's question

### 1e — verify the configured database tool

Look up the cached connection method from `~/.agami/.config`. Run a `SELECT 1` probe via that tool. Route any error through [`shared/db_error_classifier.md`](../../shared/db_error_classifier.md). Common cases:

- `auth` / `dsn` → credentials may have rotated; point at `~/.agami/credentials`.
- `network` → check VPN / DB endpoint reachability.
- `driver_missing` → fall through to the next available method.

If the cached method doesn't work, re-run tool detection per [`agami-init/SKILL.md → Phase 3`](../agami-init/SKILL.md#phase-3-tool-detection).

---

## Phase 2: Generate SQL

### HARD RULE — never echo SQL in chat prose

The generated SQL belongs in two places only: (1) the Bash invocation that executes it (which the host shows as a collapsible tool call — outside our control), and (2) the collapsible "SQL" section of the HTML report written in Phase 4. **Never paste, quote, or summarize the SQL in the assistant's narrated text.** No `SELECT ...` lines, no fenced ```sql blocks, no "I'm running this query: ..." prose. Users get the SQL by clicking the SQL details element in the HTML report.

This rule applies to every retry, every fallback, every regenerate. The chat prose stays focused on approach, fetching, and insight.

### 2a — classify the input

Check intents in this order. The first match wins; only that branch runs.

1. **Reopen-last-chart intent** (handled in 2a.1 below). Triggered by short messages that ask to re-display the most recent chart without re-running SQL. Trigger phrases:
   - "reopen", "reopen the chart", "reopen that"
   - "open the last chart", "open that again", "open my last report"
   - "show me that chart again", "show me the last chart", "show that"
   - "open the previous chart", "show that report"
   - Any message ≤ 8 words that combines an open-verb (open / show / see / view / display) with a chart-noun (chart / report / it / that / last / again).

   If matched → jump to **2a.1** and skip Phases 2b–4.

2. **A question** (contains `?` or starts with how/what/show/list/which/count/give/get/find/total/average/top/which AND isn't matched by the reopen intent above) → save it as the user's data question. Continue to 2b.

3. **Empty** → ask the user; suggest 2-3 questions from the model's `ai_context.examples` if present, or inferred from `datasets[].description`.

4. **Flag-only** (`--csv` / `--chart bar`) → re-run the previous query with the flag applied.

5. **Follow-up like "make that a chart"** → see Phase 4e.

### 2a.1 — Reopen-last-chart flow (no new SQL)

If the user's intent is to re-display the most recent chart:

1. Read the last entry of `~/.agami/query_log.jsonl` (each line is a JSON object — take the last non-empty line).
2. Look at the `chart_path` field. Possible cases:
   - **`chart_path` set AND the file exists on disk** → run `open <path>` (macOS), `xdg-open <path>` (Linux), or `start <path>` (Windows). Surface a one-liner in chat:
     ```
     Reopened: ~/.agami/charts/20260507-150912.html
     ```
     Done. Skip every other phase. Don't re-execute SQL. Don't re-render. Don't add 5 follow-ups (this is a UI action, not a fresh answer).
   - **`chart_path` is null** (last query was a 1×1 scalar that didn't render a chart) → surface: "The last answer didn't render a chart (it was a single number). Ask me a new question and I'll generate a fresh report."
   - **`chart_path` set but the file is missing** (user deleted `~/.agami/charts/`) → surface: "The chart file is gone — `<path>` no longer exists. Ask me the question again and I'll regenerate it."
   - **Query log empty or missing** → surface: "I don't have any prior queries to reopen. Ask me a question first."

This phase neither logs anything new to `query_log.jsonl` nor sends telemetry — re-opening an existing artifact isn't a query event.

### 2b — assemble the prompt for the SQL generator

The prompt assembly branches on **two axes**: profile count and dataset count.

| Axis | Branch | When |
|---|---|---|
| Profile count | **single-profile** | The question routes to one profile only (the default) |
| Profile count | **federation** | The question references datasets from ≥ 2 different profiles (cross-DB join) |
| Dataset count | **small mode** | ≤ 50 datasets in scope |
| Dataset count | **large mode** | > 50 datasets — use two-pass retrieval |

Federation mode is described in **2b.federation** further below. The single-profile branches (small mode and large mode) are described first.

Users can override the threshold by writing `always use full-schema mode` (or similar) in `<artifacts_dir>/USER_MEMORY.md`. Default to large mode for `> 50` datasets.

#### Small mode (≤ 50 datasets)

Build the prompt in this order:

1. **System** — "You are a SQL generator. Write one valid SQL statement for `<DB_TYPE>` (dialect: ANSI_SQL with `<DB_TYPE>`-specific tweaks per dialect-rules.md) that answers the user's question. Output ONLY the SQL, no commentary. **When filtering or joining on a large table** (`estimated_row_count > 100k`), prefer columns that appear in that table's `Indexes:` list — index lookups are orders of magnitude faster than full scans. For composite indexes `(a, b, c)`, only the left-prefix is index-eligible (`WHERE a = ?` uses the index, `WHERE b = ?` doesn't)."

2. **Schema context** — render the merged OSI model as compact text the LLM can reason over. The shape matters:
   ```
   Datasets:
     <schema>.<dataset.name> (<dataset.source>) [<row_count if known>]
       Description: <dataset.description>
       Synonyms: <ai_context.synonyms>
       Fields:
         <field.name>  type=<agami.type>  expr=<expression>  [time]  [choices: a,b,c]
       Indexes (prefer these for WHERE / JOIN on this dataset):
         (col1)
         (col1, col2)         # composite — left-prefix matches
       Performance hints: <if present, list recommended_filters and selective_filters>

   Relationships:
     <name>: <from>.<from_cols> → <to>.<to_cols>           # within-schema (bare)
     <name>: <schema>.<from>.<from_cols> → <schema>.<to>.<to_cols>   # cross-schema (qualified)

   Metrics:
     <name>: <expression>  -- <description>
       Synonyms: <ai_context.synonyms>
   ```
   The schema-qualified prefix on dataset names disambiguates same-named datasets across schemas. Within a single-schema setup, the prefix is still emitted but harmless.

3. **Organization context** — content of `<artifacts_dir>/<profile>/ORGANIZATION.md` from Step 1d.2, under a heading `## Organization context`. Skip if empty after stripping comments. The LLM treats this as binding domain context — apply terminology, respect business-rule definitions.

4. **User memory** — content of `<artifacts_dir>/USER_MEMORY.md` from Step 1d.1, under a heading `## User memory (preferences and policies)`. Skip if empty. Cross-database preferences (default filters, display rules).

5. **Few-shot examples** — the up-to-50 `(question, sql)` pairs from the examples library.

6. **User question** — the question from Step 2a.

Generate one SQL statement. If the model produces multiple statements separated by `;`, take only the first.

**Use OSI metrics by name when applicable.** If the user asks about "revenue" and the model has a `metrics[]` entry named `total_revenue` (with a synonym matching), prefer that metric's expression over building a fresh aggregate from scratch.

#### Large mode (> 50 datasets) — two-pass retrieval

Two-pass uses the existing model — no embedding store, no new dependencies.

**Pass 1 — relevance.** Build a *table-index* prompt that lists every `<schema>.<table>` with its 1-line description, plus the relationships graph. Skip the per-field detail.

```
You are a relevance picker. Given a question, return the JSON list of every
<schema>.<table> tuple that's likely needed to answer it. Don't include
peripheral tables — only what the SQL would actually FROM/JOIN.

Tables (all <K> across <S> schemas):
  public.orders — Customer-facing orders.
  public.customers — End-customer accounts.
  analytics.daily_revenue — Pre-aggregated daily revenue.
  ... (full list)

Relationships:
  public.orders.customer_id → public.customers.id
  analytics.daily_revenue.customer_id → public.customers.id
  ... (full list)

Question: <user's question>

Output: JSON array of {"schema": "<schema>", "table": "<table>"} objects.
```

The model returns a small list (typically 1–5 tuples). Validate that every returned tuple resolves to a real loaded dataset; drop any that don't.

**Pass 2 — SQL generation.** Build the small-mode prompt above, but render the schema-context section using **only the picked tables' full field definitions** plus any relationships whose endpoints are entirely within the picked set. Cross-schema relationships are included if both endpoints are picked.

If Pass 1 picks zero tables (the model couldn't infer relevance), fall back to small mode and surface a one-liner: "I couldn't narrow down which tables to use — sending the full schema. This may be slower."

**Why no embedding store:** the table count we're optimizing for is 1000s, not 100k+. The model can re-rank a 1000-tuple list per query within latency budget. Embedding-based retrieval is a future option if users hit > 5000 tables; it's deliberately out of scope for v1.1.

#### 2b.federation — cross-database queries

When the question references datasets from ≥ 2 different profiles (e.g., ITSM in Redshift × finance in MySQL), the skill routes the SQL through DuckDB, which ATTACHes both databases in one session and runs a native federated JOIN.

**Detecting federation.** Extend Pass 1 of the two-pass retrieval to pick `(profile, schema, table)` tuples instead of just `(schema, table)`. If the picked set spans `len({tuple.profile}) > 1`, federation mode is on.

For small databases (under 50 tables), build the union of every profile's index up front and run Pass 1 against that combined index — the picker then chooses across profiles automatically. For larger setups Pass 1 already runs; just include `profile` in each entry.

The Pass 1 prompt loads `~/.agami/cross_profile_relationships.yaml` (if present) so the picker knows about declared cross-profile JOIN paths. If the file is missing, the picker falls back to inferring relationships from column-name/type matching across profile indexes — best-effort, with a warning to the user that confidence is lower.

**`~/.agami/cross_profile_relationships.yaml`** (optional) — declares known JOIN paths across profiles:

```yaml
version: "0.1.1"
relationships:
  - name: itsm_assets_to_finance_cost_centers
    from_profile: itsm
    from_dataset: public.assets
    from_columns: [department_id]
    to_profile: finance
    to_dataset: dbo.cost_centers
    to_columns: [dept_id]
    description: ITSM assets carry the same dept_id as finance cost centers.
```

Loaded at session start the same way per-profile indexes are loaded.

**Building the federated SQL.** When federation mode is active, the schema-context section of the prompt uses **three-part dataset names** matching the DuckDB ATTACH alias: `<profile>.<schema>.<table>` (e.g., `itsm.public.assets`, `finance.dbo.cost_centers`). Cross-profile relationships from `cross_profile_relationships.yaml` are rendered alongside per-profile relationships. The model produces SQL using these three-part names.

**Verifying DuckDB is available.** Look up `tool_paths.duckdb` from `~/.agami/.config`. If missing, surface:

```
Cross-database queries need DuckDB. Install it with `brew install duckdb`
(or apt / download) and re-run.
```

…and stop.

**Verifying credentials are set up for every profile.** For each profile in the picked set, check that the corresponding auth file exists (`~/.agami/.pgpass`, `~/.agami/.mysql.cnf`). Missing → run `python3 "$AGAMI_PLUGIN_ROOT/scripts/setup_pgauth.py" --profile <profile>` for each gap, then re-check.

**Generating the temp init file.**

```bash
init_file=$(python3 "$AGAMI_PLUGIN_ROOT/scripts/build_duckdb_attach.py" \
  --profiles "$P1" "$P2")
# init_file is the path of a chmod-600 file in ~/.agami/.duckdb_init_*.sql.
# Credentials are inside that file, NOT on the command line.
```

**Running the SQL.**

```bash
duckdb -init "$init_file" -c "$FEDERATED_SQL" --csv
```

The visible Bash command shows only the path — DuckDB reads the ATTACH credentials silently from the init file.

**Tear-down.** After the query completes (success or failure), delete the init file:

```bash
rm -f "$init_file"
```

The next invocation also self-cleans any `.duckdb_init_*.sql` older than 1 hour in case a prior run crashed:

```bash
find "$HOME/.agami" -maxdepth 1 -name '.duckdb_init_*.sql' -mmin +60 -delete 2>/dev/null
```

**Performance warning.** Federated joins through DuckDB scanners are bounded by network round-trips. If both sides of the join estimate to > 100k rows, surface a one-liner before running:

> This federated query may take 30–120s (network round-trips for `<P1>` × `<P2>`). Want to tighten the filter first?

Options: `Run anyway (Recommended for one-off)` / `Let me add a filter` / `Cancel`.

**Type alignment.** Postgres `numeric(10,2)` joined with MySQL `decimal(10,2)` works. Mismatched types (date vs string, integer vs uuid) need an explicit `CAST` in the generated SQL. The Phase 2b prompt instructs the LLM about this:

> When joining across profiles, prefer explicit `CAST(<col> AS <type>)` for any pair where the types might differ (e.g., timestamps stored as strings on one side, dates on the other).

**No Snowflake federation.** DuckDB's `snowflake_scanner` is experimental and not packaged with the standard binary. If a profile in the picked set has `db_type=snowflake`, `build_duckdb_attach.py` exits with a clear error: surface it to the user and suggest pre-aggregating one side as a CSV.

### 2c — safety checks

Apply [`shared/sql-generation-rules.md`](../../shared/sql-generation-rules.md):

- **No DDL/DML.** Refuse on `DROP`, `DELETE`, `INSERT`, `UPDATE`, `ALTER`, `TRUNCATE`, `CREATE`, `GRANT`, `REVOKE`. Regenerate with explicit "SELECT only" framing.
- **No system tables.** Refuse on `pg_catalog`, `information_schema`, `mysql.*`, `sys.*` unless the user is explicitly asking about schema metadata.
- **NULL-safe division** via `NULLIF(denominator, 0)`.
- **`agami.type` consistency** — if the SQL applies a numeric aggregate (`SUM`, `AVG`) to a field whose `agami.type` is `string` or `boolean`, refuse and regenerate. Type info exists for a reason.

### 2d — risk assessment + time estimate for large tables

For each dataset touched by the SQL, look up its `agami.performance_hints`:

- `estimated_row_count > 1_000_000` AND no WHERE clause matches a `recommended_filters[].column`:
  → **HIGH risk**. Surface a banner before executing: "This query scans `<dataset>` (~<row_count>) without a date filter. Estimated time: <est>. Add a date range, or proceed anyway?" AskUserQuestion: `Add a filter` / `Proceed anyway` / `Cancel`.
- `100k–1M` rows with no recommended filter → **MEDIUM**. Note in response footer; proceed.
- Otherwise → **LOW**. Proceed silently.

**Time estimate (announced BEFORE Phase 3 execution).** Long-running queries kill the user's confidence — they don't know if the skill is hung or actually working. Before running any non-LOW query, surface a one-liner with the rough wall-clock estimate so they can wait without anxiety:

```
Running this against ~12M rows in <dataset> — estimated 30–90s. I'll narrate when results land.
```

Estimation table (rough, calibrated to common Postgres / Snowflake shapes — adjust as needed from the latency log over time):

| Largest scanned dataset | With indexed filter (`WHERE` matches `agami.performance_hints.indexes`) | Without indexed filter (full scan) |
|---|---|---|
| < 100k rows | < 1s | < 2s |
| 100k–1M | 1–5s | 5–30s |
| 1M–10M | 5–15s | 30–120s |
| 10M–100M | 15–60s | **2–10 min** — ALWAYS warn even if filter is present |
| > 100M | 30–120s | **> 10 min** — block as HIGH risk; offer to add filter or sample |

Snowflake-specific: add 5–30s on top of any estimate for warehouse spin-up if the warehouse has been idle (the query log can detect "first query in this session" → assume cold). Federation (Phase 2b.federation) doubles or triples estimates due to network round-trips — surface "this federated query may take 30–120s" before running, regardless of estimated_row_count.

If the estimate exceeds 30s, also surface: "Cancel anytime — Ctrl+C in CLI, or just send another message." The user should know they're not trapped waiting.

---

## Phase 3: Execute

### HARD RULE — never paste raw output in chat

The Bash result (CSV stdout, stderr, exit code) is for the skill to parse, not for the user to read. **Never paste the raw CSV / TSV from the Bash result into the assistant's response text.** No "Here's what came back: …", no markdown code-fence dumps of the result. Parse internally, then surface the polished output per Phase 4. The host shows the Bash tool call as a collapsible — that's enough provenance for users who want to dig.

### 3a — run the SQL

Invoke the tool-specific command from [`shared/connection-reference.md → CLI Connection Commands`](../../shared/connection-reference.md#cli-connection-commands). Wrap in a high-resolution timer to capture latency in ms.

Capture: stdout (rows as CSV), stderr (errors), exit code.

### 3b — error handling + auto-retry

Route any non-zero exit through [`shared/db_error_classifier.md`](../../shared/db_error_classifier.md). Behavior per kind:

| `error_kind` | Behavior |
|---|---|
| `auth`, `dsn`, `network` | Stop. Surface the one-line remediation. No retry. |
| `driver_missing` | Fall through to the next available method (native CLI → DuckDB → Python driver). |
| `permission` | Stop. DB user lacks SELECT on the touched dataset. |
| `column_not_found`, `table_not_found`, `syntax` | Auto-retry up to **2** times. Pass the error back to the SQL generator: "The previous SQL failed with `<one-line classifier message>`. Regenerate using only OSI dataset / field names from the schema context above." |
| `timeout` | Stop. Suggest adding a filter using the `recommended_filters` from the dataset's performance hints. |
| `other` | Stop. Surface raw error truncated to 200 chars. |

After 2 retries with no success, stop. Don't loop.

### 3c — parse rows

Parse the CSV stdout. Header row = column names. Body rows = data.

**Sanitize column headers before display.** SQL aliasing slips happen — bare `n`, `cnt`, single-letter columns, `?column?` (Postgres unaliased), and shouty all-uppercase Snowflake names are common. Apply this header transform **once** before any rendering surface (4d markdown, 4e HTML, chart axis labels):

| Raw header | Display header | Why |
|---|---|---|
| `n`, `N`, `cnt`, `CNT` | `Count` | Bare counts are unreadable; "Count" is universal |
| `?column?` (Postgres unaliased), `_col0`, `_col1` | Drop the column entirely from the visible output (parse warning to log: "unaliased column in result; SQL generator should always alias") |
| Single uppercase word (Snowflake default — `STATUS`, `AMOUNT`) | `Status`, `Amount` (title-case the word, lowercase trailing letters) |
| Snake_case (`order_count`, `customer_name`) | Title-case with spaces (`Order Count`, `Customer Name`) — except when ending in a unit (`_amount` → `_amount` raw is OK if unit shows in the header parens, like `Avg Outstanding (INR)`) |
| Already title-case or sentence-case | Leave as-is |

The header transform is purely cosmetic — the underlying alias stays in the SQL and the `tables_used` log. Don't rename the column in the result data; only its rendered label.

Format every cell per its column's `agami.type` (and `agami.unit` if present). The same formatting applies to **every** downstream surface — chat markdown table (4d), HTML `table_rows` (4e.iii), AND chart `labels` (4e.iii). Format once here in 3c; downstream phases consume the already-formatted values.

| `agami.type` | `agami.unit` | Format |
|---|---|---|
| `decimal` / `integer` | `usd`, `dollars` | `$148.95` (always show `$`, 2 decimals, locale grouping) |
| `decimal` / `integer` | `eur` | `€148.95` |
| `decimal` / `integer` | `gbp` | `£148.95` |
| `decimal` / `integer` | `jpy` | `¥148` (no decimals — JPY has no minor unit) |
| `decimal` / `integer` | `inr` | `₹2,162,087` (Indian Rupee symbol; for amounts > 100k optionally use `2.16 Cr` / `21.6 L` if USER_MEMORY says "use Indian numbering") |
| `decimal` / `integer` | `cad` | `CA$148.95` (or `$148.95` if locale is Canadian) |
| `decimal` / `integer` | `aud` | `A$148.95` |
| `decimal` / `integer` | `chf` | `CHF 148.95` (no symbol convention; show code + space) |
| `decimal` / `integer` | other ISO 4217 code | `<UPPERCASE_CODE> <number>` — show the code as a prefix (`SEK 148.95`, `BRL 148.95`). Never strip the unit silently. |
| `decimal` | `percent` | `12.4%` (1 decimal) |
| `decimal` | (other / none) | `1,234.56` (commas, 2 decimals) |
| `integer` | (no currency unit) | `1,234` (commas, no decimals) |
| `date` | — | **`May 6, 2026`** (`MMM D, YYYY`) — never raw ISO `2026-05-06`, never epoch numbers |
| `timestamp` | — | **`May 6, 2026 3:14 PM`** (`MMM D, YYYY h:mm A`) — never raw ISO with `T` separator, never microsecond precision |
| `boolean` | — | `Yes` / `No` (or `true` / `false` if the user prefers — read USER_MEMORY) |
| `string` (with `choice_field`) | — | the choice's display label, not the stored value |
| `string` (other) | — | as-is |

**Hard rule for currency:** if the field has a currency `agami.unit`, show the symbol or code in **every** cell value — never omit it because the column header already mentions the currency. The user's screenshot showed bare `2,162,087` in an "Avg Outstanding (INR)" column; the cell should read `₹2,162,087`. Headers can drop redundant unit (the column header `Avg Outstanding` is enough when every cell is `₹...`), but cells should always carry the symbol so a screenshot or copy-paste of a single cell stays unambiguous.

**Hard rule for dates: never display ISO timestamps like `2026-05-07T15:14:00.000Z`, epoch seconds, or any other machine-format string.** Cell values OR chart-axis labels — both must be human-readable. If the column shows months only (typical for time-series charts grouped by month), use `MMM YYYY` (e.g. `May 2026`); for daily granularity use `MMM D` if all data is in the current year, else `MMM D, YYYY`. The skill is responsible for inferring the right grain from the data.

If the user has stated a date-format preference in `<artifacts_dir>/USER_MEMORY.md` (e.g. "use ISO dates" or "use DD/MM/YYYY"), respect that. The defaults above apply only when USER_MEMORY is silent.

If row count > 30:
- The chat preview shows the first **30** rows only — markdown tables past ~30 rows scroll forever and bury the insight.
- The HTML report (Phase 4e) contains the full set in a paginated table.
- For row counts > 30, **auto-write the CSV to `~/.agami/exports/<ts>.csv`** at the same time as the HTML report, without waiting for the user to ask. Surface both paths in Phase 4d's footer.
- Footer line under the table: `Showing first 30 of <N> rows · full set in CSV: <csv-path> · HTML report: <html-path>`. Use thousands separators on `<N>` (e.g. `4,213`).
- If `<N>` is huge (> 100k), additionally suggest tightening the filter inline: "If you want to slice by region or date, say so and I'll re-run."

CSVs open natively in Excel / Numbers / Google Sheets, so "export to Excel" routes to this same CSV path — no separate `.xlsx` flow in v1.

If row count == 0:
- "No rows matched. The query was: …" (show SQL).
- Suggest a relaxation if applicable.

---

## Phase 4: Present

The chat reply follows this **strict order**, with NO other content interleaved (no SQL, no Bash output, no "let me know if…" filler):

  4a Approach → 4b Fetching → 4c Insight → 4d Table → 4e HTML report path → 4f Numbered follow-ups → (optional 4g CSV path)

Each step is short. The whole reply should fit in a typical chat viewport without scrolling.

### 4a — Approach (one sentence, plain English)

Open with a single sentence describing **how** you'll answer, in plain English. No SQL keywords (`SELECT`, `JOIN`, `GROUP BY`). No table or column names — describe the dimensions in user-language. Examples:

- "I'll group orders by status across the last 30 days and rank by count."
- "I'll pull total spend per customer and sort to find the top 5."
- "I'll compare this month's revenue to last month's, broken down by region."

For multi-section reports (broad questions), the approach sentence describes the narrative across all sections: "I'll cover four angles — revenue trend, top customers, order status, and region split."

### 4b — Fetching (one sentence, counts only)

Right after the approach, one sentence about the data we just pulled, in counts and dimensions. Examples:

- "Pulled 6 rows across 4 statuses."
- "Pulled 247 orders spanning the last 30 days."
- "Pulled 12 monthly buckets with revenue totals."

For multi-section: "Pulled data for 4 sections (12 + 5 + 6 + 3 rows)."

### 4c — Insight first

One sentence stating the answer. Lead with the most surprising or actionable finding. Examples:

- "**Carol Chen** is the top spender at **$148.95** — about 3x the next customer."
- "Revenue is up **12% MoM**; the surge came from EU customers."
- "60% of orders shipped on time this month, up from 48% last month."

For multi-section: a 1–3 sentence executive summary across all sections (the same summary that goes into `REPORT_SUMMARY` for the HTML).

### 4d — Markdown table (single-section reports only)

Render the rows as a GitHub-flavored markdown table. Right-align numeric columns. Format numbers per Phase 3c (commas, currency, percentages, ISO dates). Wide tables (> 8 cols) → vertical layout, with a one-line note "wide table — see HTML for the full grid".

**Cap the chat preview at 30 rows** per Phase 3c — even when the user asked for "all leads with credit rating > 700" and the result is 4,213 rows, the chat shows the first 30 and points them at the CSV + HTML report. The full set lives in the artifacts on disk. The footer line ("Showing first 30 of 4,213 rows · full set in CSV: …") is the contract that tells the user where to find everything.

**Multi-section reports skip the table in chat.** The chat already has the insight; the per-section tables live in the HTML report. Multi-section chat output is: approach + fetching + summary + a short bulleted list of section titles + HTML path + 5 follow-ups. No tables in chat.

### 4e — Build ONE coherent HTML report (one file, N sections)

The output is **one self-contained HTML file** at `~/.agami/charts/<ts>.html`, no matter how broad the question is. Broad questions decompose into multiple sub-questions; each sub-question becomes a **section** inside the same file. Each section has its own chart + table + insight + SQL. **Never write multiple HTML files for one user question. Never open multiple browser tabs.**

Skip the report only when the result is a single 1×1 scalar (e.g., `SELECT COUNT(*) FROM orders` returning `42`) — for those, the chat answer is enough.

#### 4e.i — decompose the question into sections

If the user asked something narrow ("top 5 customers by spend"), produce **one** section. Done.

If the user asked something broad ("how is the business doing", "tell me about our customers", "how did we do last quarter"), break it into **2–5 sub-questions** that together tell a narrative. Pick the dimensions that matter for that schema. Examples:

- "How is the business doing?" →
  1. Revenue trend over the last 12 months
  2. Top 5 customers by spend this quarter
  3. Order count by status this quarter
  4. Top 5 products by revenue this quarter

- "Tell me about our customers" →
  1. Customer count by region
  2. Top 10 customers by lifetime spend
  3. New customers per month
  4. Active vs inactive split

Choose sub-questions that:
- Each map to ONE SQL query
- Each return a result shape that produces a useful chart (or a small table when no chart applies)
- Don't repeat the same data sliced differently — pick distinct angles
- Are bounded — never more than 5 sections in v1; if the schema invites more, ship the top 4–5 and add a "What else can I look at?" follow-up

When in doubt about how broad the user wants to go, ask via AskUserQuestion before generating: "I can answer this as a focused query or build a 4-section report. Which?"

#### 4e.ii — pick a chart type per section

For each section's SQL result, read `agami.type` for each result column and pick:

| Result shape | `chart_type` |
|---|---|
| 1 categorical (`string`) + 1 numeric | `bar` (use `pie` / `doughnut` if ≤ 6 categories) |
| 1 time (`timestamp` / `date`) + 1+ numeric | `line` |
| 2 numeric | `scatter` |
| 1 categorical + multiple numeric | grouped `bar` (still `bar`) |
| Categorical-only / single-column / 1×1 scalar | `null` — section still renders without a chart |

If the user override-says `--chart pie|line|...` for the **whole** report, apply it to every section that supports a chart.

#### 4e.iii — build the SECTIONS_JSON

For each section, build an object:

```json
{
  "title":         "<sub-question or short heading>",
  "insights":      "<1-3 sentence plain-English insight for this section>",
  "chart_type":    "bar | line | pie | doughnut | scatter | null",
  "labels":        ["<x-axis or pie labels>"],
  "datasets":      [{"label": "<header>", "data": [<numeric values>]}],
  "table_headers": ["<col1>", "<col2>", ...],
  "table_rows":    [[<v>, <v>, ...], ...],
  "sql":           "<SQL that produced this section's data, NOT HTML-escaped — the template handles it>"
}
```

When `chart_type` is `null`, set `labels` and `datasets` to `null` (or omit). The template skips the chart card cleanly.

**Use the formatted values from Phase 3c — not the raw CSV strings.** Specifically:

- `labels` for time-series line charts must be human-readable date labels (`May 2026`, `Q2 2026`, `May 7`, `May 7, 2026`) — NOT raw ISO timestamps like `2026-05-07T15:14:00.000Z` and NOT epoch numbers. The granularity follows the SQL grouping (monthly bucket → `MMM YYYY`, daily → `MMM D` or `MMM D, YYYY`).
- `labels` for categorical charts use the choice-field display label (e.g. `Closed Won`, not the stored value `closed_won`).
- `table_rows` cells use the formatted values across the board: `$148.95` not `148.95`, `May 7, 2026 3:14 PM` not `2026-05-07T15:14:00Z`, `Yes` not `true`.

`datasets[].data` is the **only** place raw numeric values belong (Chart.js needs numbers, not formatted strings, to draw the chart). Currency / percent formatting on the chart itself happens via the template's tooltip callbacks, not by passing pre-formatted strings.

The whole report's `SECTIONS_JSON` is a JSON array of these objects.

#### 4e.iv — render via `render_chart.py` (do NOT inline-substitute through the Write tool)

The HTML report is produced by a Python helper that reads the template + SVG logos once and substitutes placeholders. **Do not Read the template + Write the rendered HTML through the LLM** — that path costs ~30KB of token I/O per query and is the dominant slowness in chart rendering.

Instead:

1. Build the sections JSON file at `/tmp/agami-sections-<ts>.json`. The shape is the JSON array built in 4e.iii — a list of section objects (`title`, `insights`, `chart_type`, `labels`, `datasets`, `table_headers`, `table_rows`, `sql`).

2. Run the renderer:

```bash
ts=$(date +%Y%m%d-%H%M%S)
mkdir -p ~/.agami/charts
python3 "$AGAMI_PLUGIN_ROOT/scripts/render_chart.py" \
  --title "$USER_QUESTION" \
  --summary "$EXECUTIVE_SUMMARY" \
  --sections-file "/tmp/agami-sections-$ts.json" \
  --out "$HOME/.agami/charts/$ts.html"
```

The helper reads `shared/chart-template.html` + the two logo SVGs once, validates each section, runs `template.replace(...)` for each placeholder, and writes the file. Stdlib only — no extra deps.

3. Delete the temp sections file: `rm -f /tmp/agami-sections-<ts>.json`.

`--summary` is the executive summary used for multi-section reports; for single-section reports pass an empty string and the section's own insight covers it. `--title` is the user's original question.

If the user pinned a chart type via `--chart bar` (etc.), the LLM still chooses per section — the flag from 2a is hint, not override. Multi-section reports often need different chart types per section.

#### 4e.vi — auto-open the file in the user's default browser

Immediately after writing the HTML, try to launch the browser. **Real-world testing has shown the chart often doesn't auto-open** — the host's permission cache may not include the `open` command pattern, the path may have an unexpected character, or the user is in a headless environment. Treat the open call as best-effort, not load-bearing — the path printed in 4e.vii is the contract.

**Run a multi-command fallback chain** in one Bash invocation. The host typically caches the first successful pattern, so subsequent queries skip straight to it:

```bash
chart="$HOME/.agami/charts/<ts>.html"
( command -v open    >/dev/null 2>&1 && open "$chart" ) || \
( command -v xdg-open >/dev/null 2>&1 && xdg-open "$chart" ) || \
( command -v start    >/dev/null 2>&1 && start "$chart" ) || \
( command -v cmd      >/dev/null 2>&1 && cmd /c start "" "$chart" ) || \
echo "agami: couldn't auto-open the chart — open manually: $chart"
```

Surface the outcome explicitly in chat (don't let it disappear into the bash collapsible):

- **Open succeeded** (exit 0, no fallback message printed) — surface in chat: `✓ Chart opened in your browser. (Path: ~/.agami/charts/<ts>.html)`
- **Fallback printed** (the `agami: couldn't auto-open` line) — surface: `Couldn't auto-open the chart in this environment. Open it yourself: ~/.agami/charts/<ts>.html`
- **First time the user runs a query in this host** — `open` may prompt for permission. The shipped `.claude/settings.json` allowlists `Bash(open ~/.agami/charts/*.html)` precisely so this prompt doesn't fire, but if the user's local settings override or strip that, they'll see a one-time approval modal. Tell them in chat: "First-run permission prompt — approve `open` and the chart will pop up. Future queries skip the prompt."

If the user reports "the chart never opens", check (a) the path printed in chat exists on disk, (b) `command -v open` returns 0 in their host, (c) their `.claude/settings.json` includes the allowlist. The skill cannot fix mode-blocked hosts on its own; the path-in-chat fallback is the universal-truth surface.

#### 4e.vii — surface in chat

After writing the file and triggering `open`:

- For a **single-section** report: surface the section's insight + the markdown table (Phases 4c + 4d). End with the chart's path as **plain text** (NOT a markdown link):
  ```
  Chart: ~/.agami/charts/20260507-150912.html
  ```
- For a **multi-section** report: surface the executive summary + a tight bulleted list of section titles. **Don't** repeat each section's table in the chat. End with:
  ```
  Report (N sections): ~/.agami/charts/20260507-150912.html
  ```

**Do NOT format the path as `[Open chart](file://...)` or any other clickable markdown link.** Some hosts (notably VS Code's Claude Code chat sandbox) only route workspace-relative paths through their click handler; `file://` URLs and absolute paths outside the workspace die silently. A fake-clickable link is worse UX than a plain path the user knows they can `open` from their terminal.

If you genuinely detect that you're running in Claude Desktop (which has a working preview pane via path clicks), you may format the path as ``Open `~/.agami/charts/<ts>.html` `` (backticks, not a link) — Desktop users get the click-to-preview experience naturally.

For hosts that support inline artifacts, also embed the HTML as a Claude artifact block (a single block; don't emit one per section).

### 4e.5 — GitHub-star ask (one-time, gates Phase 4f)

**This step is required between 4e and 4f.** Do not emit the 5 follow-up bullets in 4f without first running this check.

```bash
test -f ~/.agami/.optins
```

- **Exit 0** (`.optins` exists) — skip this step. Continue to 4f.
- **Exit 1** (`.optins` missing) AND the query just completed successfully — surface the GitHub-star ask via `AskUserQuestion`. **End the turn here.** Do NOT emit Phase 4f. The full ask + handling logic is documented in [Phase 6 below](#phase-6-post-install-github-star-ask-interrupt-the-follow-ups-not-the-answer) — but the trigger lives here, in Phase 4e.5, because if it lives only in Phase 6 (which appears textually after Phase 4f) the LLM hits Phase 4f first and the ask never fires.

The `.optins` file is the never-re-prompt gate. Once it's written (with any of the three response values), this check skips for every future query. If the user reports they never see the ask, they probably had `.optins` from an earlier install — `ls -la ~/.agami/.optins` will show whether the file exists, and `rm ~/.agami/.optins` re-arms the prompt for the next query.

### 4f — Numbered follow-up suggestions (always 5)

End every successful answer with **exactly 5 numbered follow-up questions**, formatted as a plain markdown ordered list. Always — even for narrow questions, even if some feel slightly broader. **Do not use AskUserQuestion for follow-ups** — that surfaces a modal picker and feels intrusive. The numbered list lets the user glance, ignore, type a number, or type a fresh question.

Format exactly:

```
What next?
1. <follow-up question 1>
2. <follow-up question 2>
3. <follow-up question 3>
4. <follow-up question 4>
5. <follow-up question 5>

Reply with a number, or ask anything else.
```

**Picking the 5 questions** — aim for distinct angles, not 5 variations of the same question:

| Slot | Pattern | Example for "Top 5 customers by spend" |
|---|---|---|
| 1 | Drill into the top result | "Drill into Carol Chen's order history" |
| 2 | Compare across time | "How did this list look 3 months ago?" |
| 3 | Slice by another dimension | "Top 5 customers by region" |
| 4 | Inverse / negative angle | "Customers with no orders in the last 90 days" |
| 5 | Adjacent metric | "Average order value per customer" |

These are templates, not rules — adjust to the schema. If a slot doesn't fit, replace with a more interesting angle. Keep each follow-up under 80 characters.

**When the user is replying to follow-ups**: if the user's next message is a single digit `1`–`5` or a numbered form like `1.` / `1)` / `#1`, treat it as the n-th follow-up from the previous reply. Auto-fill the question text and re-enter Phase 2 with that question. Free-form replies are a fresh question. Genuinely ambiguous replies (`yes`, `do that`) get one short clarifier inline ("which of the 5?") — never via AskUserQuestion.

**Saving a correction is NOT a follow-up bullet.** When the user expresses dissatisfaction with the answer, the skill suggests it inline as a single sentence outside the numbered list, in plain language: *"If that's not the answer you wanted, say 'save this as a correction' and I'll update the examples library."* (Natural language reads better here than `/agami-save-correction` — though that slash form does work. Phrases like "save this as a correction" / "remember this" / "use this SQL next time" route to the agami-save-correction skill via its `when_to_use` matching.) The numbered list stays focused on **what to ask next**, not how to fix what we just said.

### 4g — CSV export (`--csv` or "export this")

Two ways the CSV gets written:

1. **Auto-export for large results** (row count > 30, per Phase 3c). The CSV is written alongside the HTML report without the user asking, and the path is surfaced in Phase 4d's footer.
2. **Explicit `--csv` / "export this" / "export to Excel"** for any result, including small ones. The user explicitly wants a flat file.

Either way:

- Single-section report → one CSV at `~/.agami/exports/<ts>.csv`.
- Multi-section report → one CSV per section at `~/.agami/exports/<ts>-<section-slug>.csv`. Surface all paths.

```bash
ts=$(date +%Y%m%d-%H%M%S)
mkdir -p ~/.agami/exports
# write header + rows per section, RFC 4180 escaping
```

CSVs open natively in Excel / Numbers / Google Sheets — when the user asks for "Excel", the CSV path is the answer. If they specifically want a `.xlsx` (formulas, multiple sheets, formatting), tell them to open the CSV in Excel and Save As — v1 doesn't ship a native `.xlsx` writer.

Surface the path(s) inline. For the auto-export case, the path is already in the Phase 4d footer; the explicit-export case adds a separate confirmation line before Phase 4f.

---

## Phase 5: Log

Append one line to `~/.agami/query_log.jsonl`:

```json
{
  "ts": "2026-05-07T15:14:00Z",
  "question": "<NL question>",
  "sql": "<executed SQL>",
  "row_count": 5,
  "execution_ms": 250,
  "tier": "cli",
  "risk": "LOW",
  "error_kind": null,
  "feedback": null,
  "chart_path": "/Users/me/.agami/charts/20260507-141500.html",
  "tables_used": ["public.orders", "public.customers"],
  "retrieval_mode": "small"
}
```

`chart_path` is the **absolute** path of the HTML report written in Phase 4e — or `null` if no report was rendered (the result was a 1×1 scalar). Phase 2a.1 reads this field to power the reopen-intent flow.

`tables_used` is a list of qualified `<schema>.<table>` strings — the datasets the executed SQL actually FROMs/JOINs. For large-mode (Phase 2b two-pass retrieval), this is the set Pass 1 picked. For small-mode, derive it by parsing the SQL's FROM/JOIN clauses. Used for the verification step ("did Pass 1 pick the right tables?") and for `feedback: "good"/"bad"` analytics.

`retrieval_mode` is `"small"` or `"large"` — records which Phase 2b branch ran. Useful for tuning the 50-table threshold if it turns out to be wrong in practice.

**Local-only** — never sent. The user owns it.

If the user takes a positive follow-up action — picking one of the 5 numbered follow-ups, requesting an export, drilling into a row — set `feedback: "good"` retroactively on the previous entry. If they rephrase the same question or say something dissatisfied ("that's wrong", "no, I meant…"), set `feedback: "bad"`.

---

## Phase 6: Post-install GitHub-star ask (full spec — triggered from Phase 4e.5)

**This is the full spec for the GitHub-star ask. The trigger lives in Phase 4e.5 above** (between 4e and 4f) — the textual order of phases here is misleading because the ask must run *between* 4e and 4f, not after Phase 5. If you read straight through Phase 4f → 5 → 6 the ask never fires; that's why the trigger is duplicated up in 4e.5 with a pointer down here for the details.

A one-time, low-friction ask after the user's first successful query: "if this was useful, give us a star on GitHub". No email collection, no list. **The order matters:** the answer has to be readable, the ask has to feel like a discrete decision, and the 5 follow-up bullets must come AFTER the user has answered — not before. Otherwise the user reads "What next? 1. … 2. …" and then sees a modal pop up, loses context, and the follow-ups feel like clutter.

Sequence:

1. Render the answer (Phase 4a–4e: approach, fetching, insight, table, chart path).
2. If `~/.agami/.optins` does not exist AND the query just succeeded: **surface the GitHub-star ask NOW**, before Phase 4f's follow-up bullets. Use `AskUserQuestion`:

   > Quick one — first query worked. **If this was useful, would you star us on GitHub?**
   >
   > It's the only signal we have that we're on the right track. No email, no list, no follow-up — just a click. github.com/AgamiAI/LiteBi

   Options:
   - `Yes — open GitHub now` — runs `open https://github.com/AgamiAI/LiteBi` (macOS), `xdg-open` (Linux), `start` (Windows) and surfaces a one-line "Thanks — opening GitHub. Star is in the top-right when you get there." (Failure-tolerant: if the open command fails, fall through with the URL printed in chat.)
   - `Maybe later` — write `.optins` so we don't ask again, surface "No problem. The link is github.com/AgamiAI/LiteBi if you change your mind." (No `(Recommended)` marker — we'd genuinely prefer "Yes" if the user found it useful, but no marker on any of the three options keeps the ask non-pushy.)
   - `Already starred — thank you!` — surface "🙏 thanks for the early support" and write `.optins`.
3. **Wait for the user to answer the modal.** That's the end of this turn. Do NOT emit the 5 follow-up bullets yet.
4. Next turn: process the decision (write `~/.agami/.optins`). Then show the 5 follow-up bullets per Phase 4f, with a tiny acknowledgment line ("Now, where next?") before the numbered list.

If `~/.agami/.optins` already exists, skip the ask entirely and emit the 5 follow-ups in the same turn as the answer (Phase 4f as today).

`~/.agami/.optins` shape (chmod 600):

```json
{
  "schema_version": 1,
  "github_star_asked": true,
  "github_star_response": "yes_opened" | "maybe_later" | "already_starred",
  "ts": "2026-05-08T15:30:00Z"
}
```

The existence of the file (with `github_star_asked: true`) is the never-re-prompt gate. We deliberately don't track whether the user actually starred — that's their call and we can't observe it from a local skill anyway.

---

## Phase 7: Telemetry flush (if opted in)

If `~/.agami/.config` has `analytics_consent: true`:

1. Append a `query` event to `~/.agami/.telemetry-queue.jsonl` using **only** the allowlisted fields per [`shared/telemetry-payload.md`](../../shared/telemetry-payload.md). No `query_text`. No `error_message`. Just the 11 enums/numbers.

2. Check `~/.agami/.telemetry-last-flush`. If absent or > 24h old, flush via `curl -sS -m 5 -X POST https://analytics.agami.ai/v1/events -H "Content-Type: application/json" -d @<batch> || true`. On 200, truncate the queue.

Failure-tolerant — never block the user on telemetry.

---

## Closing

End with:
- The result table
- One natural follow-up suggestion if applicable
- File paths for any artifacts (CSV export, chart HTML)

---

## Error handling cheat sheet

| Symptom | Action |
|---|---|
| `<artifacts_dir>/<profile>/index.yaml` missing AND `~/.agami/<profile>.yaml` missing | Invoke `connect` |
| Model file fails to parse as YAML | Surface error; tell the user "say 'reload the schema' to re-introspect from your DB" (the agami-connect skill handles it) |
| `version` ≠ `"0.1.1"` | Warn but proceed; suggest "say 'reload the schema'" to regenerate the model in the latest format |
| Credentials chmod wrong | Refuse, offer `chmod 600` |
| Cached database tool broken | Re-detect, update `.config` |
| SQL has DDL/DML | Refuse, regenerate |
| Type mismatch (numeric aggregate on string field) | Refuse, regenerate |
| Auto-retry exhausted (2 tries) | Stop. Show all 3 attempts and their error kinds. |
| HIGH-risk query without filter | Block, AskUserQuestion |
| Chart for empty result | Skip the chart, just show empty-result message |
| Telemetry POST fails | Silent — keep events in queue, retry next flush |
| Browser open fails for the GitHub-star ask | Tell user "Couldn't open the browser — the link is github.com/AgamiAI/LiteBi". Save the response anyway. |
