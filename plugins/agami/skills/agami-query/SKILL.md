---
name: agami-query
description: "Answers natural-language questions about the user's database. Loads the agami semantic model (subject areas, tables, columns, relationships with join cardinality, entities, metrics) and few-shot examples from <artifacts_dir>/<profile>/, generates SQL via the examples-first traversal (pick subject area → match examples → resolve entities/metrics → compound table context), executes it locally via the user's chosen tool (psql / mysql / snowsql / sqlite3 native CLI, DuckDB binary, or the Python driver `execute_sql.py` — which runs a fan-trap/chasm-trap pre-flight + auto-applies default_filters), returns results as a markdown table with optional CSV export, and renders Chart.js HTML charts on request. All execution is local — no data leaves the machine."
when_to_use: "Use when the user asks 'how many', 'show me', 'top N', 'trend over time', 'compare', 'breakdown by', 'group by', 'average', or any other data question against their configured database. Also use for CSV export ('export this'), chart rendering ('make that a bar chart'), or to follow up on a previous result ('drill into the EU region')."
argument-hint: "[question] [--csv] [--chart bar|line|pie|doughnut|scatter]"
---

# agami query-database

You answer the user's natural-language question about their database. Goal: generate correct SQL from the semantic model + the few-shot examples via the examples-first traversal, execute it locally, return rows + an insight, and offer a chart / export when appropriate. Everything runs on the user's machine.

This skill orchestrates:

1. **Setup** (once per session) — resolve the profile + the semantic model at `<artifacts_dir>/<profile>/`, verify the configured database tool still works.
2. **Generate SQL** — examples-first traversal: pick the subject area → match curated examples → (cold start) resolve entities/metrics + identify opaque literals → compound `get_table_context` → produce one SQL statement → safety checks.
3. **Execute** — run via the chosen tool; the Python tier runs the fan/chasm pre-flight + applies default_filters; auto-retry on classified errors; risk-assess large-table queries.
4. **Present** — markdown table; CSV via `--csv` or "export this"; Chart.js HTML via `--chart` or "make that a chart".
5. **Log + post-install GitHub-star ask** — write `~/.agami/query_log.jsonl` and ask the user (once, after first successful query) to star us on GitHub; once they answer, point them to `/agami-serve` (wire the model into Claude Desktop — the experience their business users get).

For the model format: [`scripts/semantic_model/__init__.py`](../../scripts/semantic_model/__init__.py) (layout) + `scripts/semantic_model/models.py`.
For SQL safety: [`shared/sql-generation-rules.md`](../../shared/sql-generation-rules.md).
For dialect-specific syntax: [`shared/dialect-rules.md`](../../shared/dialect-rules.md).
For connection method + execution: [`shared/connection-reference.md`](../../shared/connection-reference.md).
For DB error classification: [`shared/db_error_classifier.md`](../../shared/db_error_classifier.md).
For chart template: [`shared/chart-template.html`](../../shared/chart-template.html).

## Invocation conventions

**Read [`shared/invocation-conventions.md`](../../shared/invocation-conventions.md) before suggesting any slash command in chat.** Agami slash commands: `/agami-connect`, `/agami-query`, `/agami-model`, `/agami-save-correction`, `/agami-reconcile`. (`/agami-model`'s Review tab absorbed the former `/agami-review`.) Never write the un-prefixed forms (`/init`, `/connect`, `/query-database`, etc.) or colon-namespaced forms (`/agami:init`, etc.) — those don't exist. **`/agami-init` was folded into `/agami-connect` Phase 0a** — credential setup now lives there.

For chat replies, **prefer natural language over slash commands** — it reads better and the skill's `when_to_use` matcher routes correctly:

- Re-introspect the schema → "say 'reload the schema'" or "say 'reintrospect my database'"
- Save a correction → "say 'save this as a correction'" or "say 'remember this'"
- Ask a data question → just type the question
- Set up agami / switch profiles → `/agami-connect` (the one place the slash form is genuinely cleaner than natural language — agami-connect handles credentials too via Phase 0a)

## Conversation style

- **One question per turn unless they're truly bundled.**
- **Use AskUserQuestion sparingly** — only when the user must pick before the skill can proceed (large-table HIGH-risk approval, the post-install GitHub-star ask, the demo-query Yes/No/Skip in agami-connect). **Do NOT use AskUserQuestion for follow-up suggestions** — those are 5 plain numbered bullets per Phase 4f.
- **Insights, not narration** — lead with the answer ("Carol Chen has the highest spend at $148.95"), not the SQL or the process.
- **Round numbers in prose**, exact in the table.
- **Don't echo the SQL in chat prose** — that's enforced as a hard rule in Phase 2. Don't paste the raw Bash CSV — Phase 3.

---

## Phase −1: Plan-mode check

Run the detection + ask logic from [`shared/plan-mode-check.md`](../../shared/plan-mode-check.md). agami-query needs Bash (SQL execution) and Write (chart HTML) — both are blocked in plan mode.

**If plan mode is active and the user picks `Stay in plan mode`:**

- **Reopen-last-chart intent** (Phase 2a.1 below) — re-displaying an existing HTML chart only needs `Read` plus `open <path>`. Run that flow if matched.
- **Anything else** — refuse and end the turn. **DO NOT write a plan file. DO NOT call `ExitPlanMode`.** Refusal text (verbatim):

  > I can't run SQL in plan mode. Switch to **Auto** or **Edit Automatically** mode (Shift+Tab to cycle) and re-invoke me.

If plan mode is not active, skip this phase silently and go to Phase 1.

---

## Phase 1: Setup (once per session)

### HARD RULES — connection rules

These are non-negotiable.

1. **Connect ONLY to the host/port/database/user/password in `~/.agami/credentials`** — the sole credential source (no env-var bypass). Never substitute `localhost` or any other host as a fallback. Never connect to anywhere not in the credentials.
2. **Never ask the user for connection details in chat.** If credentials are missing, stop and invoke `/agami-connect` — its Phase 0a runs the DB-type picker, writes `~/.agami/credentials.example`, and ends the turn for the user to fill it in.
3. **Never scan or guess.** No `pgrep`, no `ps`, no `find /`, no `ls /Applications/Postgres.app`, no listing port-listeners. The only Bash probes allowed during setup are `which <tool>` for a database tool on `PATH` and `python3 -c 'import <module>'` for a Python driver.
4. **NEVER put the password (or any credential field) in a Bash command line.** That includes `export PGPASSWORD='<value>'`, `export MYSQL_PWD='<value>'`, `psql -W <password>`, `mysql -p<password>`, or any heredoc / stdin form that interpolates the password. Hosts render Bash tool calls as collapsibles in their UI — anything in the command becomes visible in the chat. Use the auth files generated by `scripts/setup_pgauth.py` (see [`shared/connection-reference.md → HARD RULES`](../../shared/connection-reference.md)). For native CLI queries the visible Bash command is `PGPASSFILE=$HOME/.agami/.pgpass psql -h ... -U ... -d ... -c "$SQL" --csv`. For the Python driver path use `"$PY" scripts/execute_sql.py --sql-file ...`.

These rules apply to every phase of this skill, not just Phase 1.

### 1a — credentials check (binding)

Read `~/.agami/credentials`. If the file (or the active profile's section) is missing, invoke `/agami-connect` (its Phase 0a handles first-time credential setup) and **stop this skill**. Do not continue to load the semantic model. Do not run any other Bash commands.

### 1b — load the semantic model

Resolve `<profile>`: `AGAMI_PROFILE` → `active_profile` in `~/.agami/.config` → `"main"`.
Resolve `<artifacts_dir>` per [`shared/file-layout.md`](../../shared/file-layout.md#configuring-artifacts_dir): `AGAMI_ARTIFACTS_DIR` → `.config.artifacts_dir` → `$HOME/agami-artifacts`.

The model is the semantic-model tree at `<artifacts_dir>/<profile>/` (`org.yaml` + `subject_areas/<area>/…`). There is no legacy-layout fallback — the model is the only format.

**Never hand-read the YAML tree** to understand the model — don't `cat`/`Read` `org.yaml`, `subject_areas/**`, `tables/*.yaml`, or `relationships.yaml`. The CLI returns the same data structured, and the layout is already known (relationships + entities + metrics live at the **area** level, not inside a table file). `sm areas "$ROOT"` is the **one-call model map**: per area it returns `table_count`, `entity_count`, `metric_count`, `relationship_count` + description — the whole shape in a single call. (Column-level detail → `sm context`; browsable table/column tree → `sm model-tree`.)

**Don't run a separate existence probe either** (no `ls org.yaml`, and never probe for the plugin's own scripts — `sm`, `execute_sql.py`, `semantic_model/` always ship with the plugin). That same first `sm areas` call doubles as the check: model present → you get the map; absent → the CLI returns `{"error":"no_model"}` with **exit code 3** → invoke `agami-connect` and stop.

Drive everything through the CLI (the `sm` wrapper resolves the interpreter + deps) or the MCP tools — both return the same shapes:

```bash
ROOT="<artifacts_dir>/<profile>"
bash "$AGAMI_PLUGIN_ROOT/scripts/sm" areas "$ROOT"; rc=$?      # subject-area index; rc 3 = no model → agami-connect
bash "$AGAMI_PLUGIN_ROOT/scripts/sm" context "$ROOT" --area A --tables t1 t2   # compound table context
bash "$AGAMI_PLUGIN_ROOT/scripts/sm" examples "$ROOT" --area A --query "…"     # examples-first ranking
```

The model loader already drops `review_state: rejected` entries from what it serves and applies the area's `expose_column_groups` scoping, so you never see excluded tables/columns/relationships. (Rejections are the curator's choice via `/agami-model` — surfaced nowhere.) When a query would touch a `stale` entry, warn once: *"This would use `<entity>`, marked stale (schema drift). Run /agami-connect to re-introspect, then /agami-model to reconcile."*

### 1c — what the model gives you

You don't build hand-rolled indexes — the loader returns structured objects. The pieces you'll use during SQL generation:

- **Subject areas** — the primary scoping unit (replaces "load every table"). Each has a description, a table list, entities, metrics, and an intra-area relationship graph (each edge carries join **cardinality** + a trust block).
- **`get_table_context(area, tables)`** — columns (scoped by `expose_column_groups`), `default_filters`, relationships, `caveats`, `value_transforms`, metrics — in one call.
- **Entities** — the vocabulary users say (name/plural/other_names → `maps_to` table.column, with a `value_pattern` for opaque IDs). Use `resolve_entities` / `identify_entity`.
- **Metrics** — reusable aggregations with prose `calculation` + per-dialect `bindings`. **Use the binding SQL VERBATIM** when the user asks for a metric by name or synonym; don't hand-roll the aggregate.
- **Cross-subject-area relationships** (org level) — for joins that span two areas.

### 1d — load the examples library

Examples live per subject area at `<artifacts_dir>/<profile>/prompt_examples/<area>/examples.yaml`. Use `cli examples "$ROOT" --area <area> --query "<question>"` to rank them (the **examples-first** signal — step 2a). If a high-confidence match returns, mirror its tagged tables/columns/SQL shape and skip cold-start resolution.

If there are no examples for the relevant area → warn: "I don't have few-shot examples for this database yet — answers may be lower quality. Say 'introspect the schema' to seed them." (Slash form `/agami-connect` only if the user asks "what do I type?".)

### 1d.1 — load USER_MEMORY.md

Read `<artifacts_dir>/USER_MEMORY.md` (if present). Strip HTML comments (`<!--...-->`), then keep the rest. If the file is missing, treat it as empty — never error. See [`shared/user-memory-format.md`](../../shared/user-memory-format.md) for what's in it.

This file holds free-form **user preferences across every database** (default filters, display preferences). Inject it into the SQL-generation prompt in Phase 2b under a labeled `## User memory (preferences and policies)` section — the LLM uses it as steering context.

### 1d.2 — load domain context

Run `cli org-context "$ROOT"` — it returns the **full** domain context for this database in one block: the human's ORGANIZATION.md narrative (HTML comments stripped) **plus** the model-derived summary that the file does NOT contain — subject areas, conventions, and the **decoded glossary** (`key_terminology` + enum legends), assembled fresh from the structured model. Don't `Read` ORGANIZATION.md by hand: the file holds only the human narrative; the glossary and summary live in the model, and this command is the one that combines them. If there's no model, treat as empty — never error. See [`shared/organization-context-format.md`](../../shared/organization-context-format.md).

Inject the result into the SQL-generation prompt in Phase 2b under `## Organization context`, **before** the `## User memory` section — domain knowledge precedes display preferences in the LLM's reading order.

Order in Phase 2b prompt:
1. Schema context (tables / columns / relationships / metrics from the semantic model)
2. `## Organization context` ← from `cli org-context` (narrative + derived summary + glossary)
3. `## User memory (preferences and policies)` ← from USER_MEMORY.md
4. Few-shot examples
5. The user's question

### 1e — verify the configured database tool

Look up the cached connection method from `~/.agami/.config`. Run a `SELECT 1` probe via that tool. **Use the EXACT invocation pattern below for the tier — don't guess flags.** `execute_sql.py` does NOT accept positional SQL, a `--format` flag, or any flag not listed below; guessing produces "unrecognized arguments" errors that waste turns.

| tier | SELECT 1 invocation |
|---|---|
| `cli` (postgres) | `PGPASSFILE="$HOME/.agami/.pgpass" psql -h <host> -U <user> -d <db> -c 'SELECT 1' --csv` |
| `cli` (mysql) | `mysql --defaults-file="$HOME/.agami/.mysql.cnf" --defaults-group-suffix="_<profile>" -e 'SELECT 1' --batch` |
| `cli` (snowflake) | `snowsql --config "$HOME/.agami/.snowsql.cnf" -c "<profile>" -q 'SELECT 1' -o output_format=csv -o friendly=false` |
| `cli` (sqlite) | `sqlite3 -csv "<path>" 'SELECT 1'` |
| `duckdb` (any) | `duckdb -init "$init_file" -c 'SELECT 1' --csv` (see `build_duckdb_attach.py` for `$init_file`) |
| `python` (all DBs) | `AGAMI_PROFILE="<profile>" "$PY" "$AGAMI_PLUGIN_ROOT/scripts/execute_sql.py" --sql 'SELECT 1'` |

The Python tier's CLI is **`--sql <string>`** or **`--sql-file <path>`** — those are the only two ways to pass SQL. Optional flag: `--profile <profile>` (overrides `AGAMI_PROFILE` env). **Output is RFC-4180 CSV on stdout**, always — no `--format` flag exists. If you need JSON, post-process the CSV.

Route any error through [`shared/db_error_classifier.md`](../../shared/db_error_classifier.md). Common cases:

- `auth` / `dsn` → credentials may have rotated; point at `~/.agami/credentials`.
- `network` → check VPN / DB endpoint reachability.
- `driver_missing` → fall through to the next available method.

If the cached method doesn't work, re-run tool detection per [`agami-connect/SKILL.md → Phase 0a.5`](../agami-connect/SKILL.md#0a5--tool-detection).

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
     Reopened: ~/.agami/charts/<profile>/20260507-150912.html
     ```
     Done. Skip every other phase. Don't re-execute SQL. Don't re-render. Don't add 5 follow-ups (this is a UI action, not a fresh answer).
   - **`chart_path` is null** (last query was a 1×1 scalar that didn't render a chart) → surface: "The last answer didn't render a chart (it was a single number). Ask me a new question and I'll generate a fresh report."
   - **`chart_path` set but the file is missing** (user deleted `~/.agami/charts/<profile>/`) → surface: "The chart file is gone — `<path>` no longer exists. Ask me the question again and I'll regenerate it."
   - **Query log empty or missing** → surface: "I don't have any prior queries to reopen. Ask me a question first."

This phase neither logs anything new to `query_log.jsonl` nor sends telemetry — re-opening an existing artifact isn't a query event.

### 2b — assemble the prompt via the examples-first traversal

For a single profile, follow the **examples-first canonical loop** — the subject area is the scoping unit, so you never dump the whole schema. (Cross-profile federation is **2b.federation** below; it's orthogonal to this loop.)

**Step 1 — pick the subject area(s).** `cli areas "$ROOT"` → choose the area(s) whose description matches the question's intent. Most questions touch one area; cross-area ones (a join spanning two areas) select both, and the org's `cross_subject_area_relationships` supply the join.

**Step 2 — examples first (strongest signal).** `cli examples "$ROOT" --area <area> --query "<question>"`. If `high_confidence` is true, mirror the top match's tagged tables / columns / metric / SQL shape and jump to step 5 — skip cold-start resolution.

**Step 3 *(cold start only)* — resolve entities + metrics + opaque literals.** Match the question's terms to the area's entities (and `metrics`). For any opaque literal in the question (an ID-looking token), `cli`/MCP `identify_entity` recognizes its type via `value_pattern`; if it returns `clarify`, ask the user one targeted question rather than guessing.

**Step 4 *(cold start only)* — choose tables + columns** from what resolved (entity `maps_to`, metric `source_tables`).

**Step 5 — compound context fetch.** `cli context "$ROOT" --area <area> --tables … [--columns …]` returns columns (scoped by the area's `expose_column_groups` — wide tables disclose only their exposed groups), `default_filters`, relationships (with cardinality + signers), `caveats`, `value_transforms`, and metrics, in one round-trip.

**Step 6 — assemble the generator prompt** in this order, then produce ONE SQL statement (first statement only if several are emitted):

1. **System** — "Write one valid SQL statement for `<DB_TYPE>` (ANSI_SQL + `<DB_TYPE>` tweaks per dialect-rules.md). Output ONLY SQL. Prefer indexed/`recommended_filters` columns on large tables. Apply each column's `value_transform` when selecting/filtering it. **Never SELECT a `sensitive` column's raw values** — aggregate or omit. Use a metric's `bindings` SQL VERBATIM when the question names that metric (or a synonym)."
2. **Schema context** — the `get_table_context` output for the chosen tables (columns + types + caveats + value_transforms), the area's relationships (rendered as `from.col → to.col [cardinality]`), and the area's metrics (`<name>: <binding> -- <calculation>` + synonyms). `default_filters` need not be enumerated — `execute_sql` auto-applies them (step below) — but DO honor any caveats.

   **Unreviewed metrics are USED, not refused.** When the question names a metric whose `review_state ≠ approved`, still use its binding and answer — do NOT block or refuse on it. The trust layer surfaces it as a **warning on the receipt** (Phase 4e.iii.5: *"Used metric `X` which has not been signed off"*), not a hard gate. The loader already drops only `rejected` metrics; an `unreviewed`/`proposed` one is yours to use, with the warning carrying the honesty. (Same for unreviewed joins/entities and `stale` entries — warn, never refuse.)
3. **Organization context** — `ORGANIZATION.md` (step 1d.2), heading `## Organization context`. Binding domain context.
4. **User memory** — `USER_MEMORY.md` (step 1d.1), heading `## User memory (preferences and policies)`.
5. **Few-shot examples** — the ranked matches from step 2.
6. **User question.**

**default_filters + fan/chasm safety run via a pre-execution step, on every tier.** Before you execute the generated SQL, pass it through `sm prepare "$ROOT" --area <area> --sql-file <path>` (Phase 3a). It runs the fan-trap / chasm-trap pre-flight (auto-rewrites the safe cases; refuses shape-changing ones) AND AND-s in the area's `default_filters`, and returns the SQL to actually run. Tier-independent — works whether you execute via psql, the Python driver, or DuckDB — so the safety guarantees never depend on the execution path. The receipt panel surfaces the rewrite + applied filters.

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

`recommended_filters` is a **list of column names** (introspection seeds it with a large table's date/time columns — the columns worth filtering on to avoid a full scan). Check whether the generated SQL's WHERE filters on **any** of them.

- `estimated_row_count > 1_000_000` AND the WHERE filters on **none** of the table's `recommended_filters`:
  → **HIGH risk**. Surface a banner before executing — **name a suggested column** when one exists: "This query scans `<dataset>` (~<row_count>) with no filter on `<recommended_filters[0]>`. Estimated time: <est>. Narrow it — e.g. a date range on `<recommended_filters[0]>` — or proceed anyway?" AskUserQuestion: `Add a filter` / `Proceed anyway` / `Cancel`. If `recommended_filters` is **empty** (no known good filter for this table), use the generic "…without a filter. Add one, or proceed?" wording.
- `100k–1M` rows with no filter on a `recommended_filters` column → **MEDIUM**. Note in response footer; proceed.
- A query that **does** filter on a `recommended_filters` column → treat as narrowed: drop a risk tier (don't HIGH-warn just because the table is big). Otherwise → **LOW**. Proceed silently.

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

### 3a — safety pass, then run the SQL

**Step 1 — prepare (every tier).** Write the generated SQL to a temp file, then run the tier-independent safety pass:

```bash
bash "$AGAMI_PLUGIN_ROOT/scripts/sm" prepare "$ROOT" --area <area> --sql-file /tmp/agami-q.sql
```

It returns JSON: `{action, risk, sql, rewritten, applied_filters, units, reason}` (on a refuse it instead returns `{action, risk, reason, suggestion, sql}` with exit 1). (`units` is the `{output_column: unit}` map traced through the final SQL — keep it for the table render in 4d.)
- `action: "refuse"` (a fan/chasm trap that would change result shape) → **don't execute, and don't silently guess.** Read `reason` + `suggestion`, then branch on whether a faithful rebuild is unambiguous:
  - **Single faithful rebuild** — there's one restructuring that preserves exactly what the user asked for (typically a chasm trap: pre-aggregate each measure in its own CTE, then outer-join; or move the aggregate into a window function to keep raw rows). Regenerate the SQL per `suggestion`, re-prepare, and **note in the receipt** that the query was restructured to avoid a `<risk>`. The user gets the right number *with* provenance — not a silent swap.
  - **Ambiguous rebuild** — the fan-out join is also filtering or grouping, so the candidate rewrites return *different numbers* (e.g. "loans with ≥1 payment since January" vs a payment-weighted total). Do **not** pick one. Surface the ambiguity to the user as a short "Did you mean…?" with 2–3 concrete interpretations, one plain-language sentence each (no SQL). Generate SQL for the interpretation they choose, then re-prepare.
  - If you can't form a faithful rebuild, or `prepare` still refuses after restructuring: stop and tell the user plainly what the conflict is (the `reason`, in one sentence). Don't loop.
- `action: "auto_rewrite"` → a fan/chasm trap was auto-corrected; use the returned `sql` (note `rewritten: true` + `reason` in the receipt).
- `action: "allow"` → use the returned `sql` (it already has `applied_filters` AND-ed in; surface them in the receipt).

**Step 2 — execute the returned `sql`** via the tier's tool from [`shared/connection-reference.md → CLI Connection Commands`](../../shared/connection-reference.md#cli-connection-commands) — psql / mysql / snowsql / sqlite3 / DuckDB, or the Python driver. (If you use `execute_sql.py`, pass `--no-safety` since `prepare` already ran the pass — avoids doubling it.) Wrap in a high-resolution timer; capture stdout (CSV rows), stderr (errors), exit code. Route a non-zero exit through the error classifier (Phase 3b).

### 3b — error handling + auto-retry

Route any non-zero exit through [`shared/db_error_classifier.md`](../../shared/db_error_classifier.md). Behavior per kind:

| `error_kind` | Behavior |
|---|---|
| `auth`, `dsn`, `network` | Stop. Surface the one-line remediation. No retry. |
| `driver_missing` | Fall through to the next available method (native CLI → DuckDB → Python driver). |
| `permission` | Stop. DB user lacks SELECT on the touched dataset. |
| `column_not_found`, `table_not_found`, `syntax` | Auto-retry up to **2** times. Pass the error back to the SQL generator: "The previous SQL failed with `<one-line classifier message>`. Regenerate using only table / column names from the schema context above." |
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

Format every cell per its column's `type` and **`unit`** (both come from `get_table_context` — `unit` is the structured currency/unit set during onboarding's currency ask or by a correction). For a metric result column, use the **metric's** `unit`. The canonical mapping is `semantic_model/units.py` (`format_value`) — the table below mirrors it; when in doubt, match it. The same formatting applies to **every** downstream surface — chat markdown table (4d), HTML `table_rows` (4e.iii), AND chart `labels`. **Also pass the value column's `unit` into each chart section's `"unit"` field** so the chart's y-axis + tooltips format the symbol + grouping deterministically (the chart template applies it; you don't hand-format chart axes — and the `datasets` data stays RAW numbers).

**Numbers are formatted by code, not by you.** The chat markdown table (4d) is rendered by `sm format-table` and the chart by the template — both via `units.py`, in full and exact (a verification surface; never abbreviate or round). Your job in 3c is the **non-numeric** display (header sanitization, dates → `MMM D, YYYY`, booleans → Yes/No, `choice_field` → label) and supplying each column's **`unit`**; the numeric cells are then emitted deterministically downstream. The type/unit table below is the reference `units.py` implements.

| `type` | `unit` | Format |
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
| `integer` / `string` | `epoch_s`/`epoch_ms`/`epoch_us`/`epoch_ns`, `yyyymmdd` | a **date encoding** — `format-table` renders it human-readably (epoch → `YYYY-MM-DD HH:MM:SS UTC`) deterministically; don't hand-format. **In the SQL, convert it** (`to_timestamp(created_ts)` for epoch_s, `to_timestamp(created_ts/1000)` for ms, etc.) so filters/grouping work and the result is a real date. |
| `boolean` | — | `Yes` / `No` (or `true` / `false` if the user prefers — read USER_MEMORY) |
| `string` (with `choice_field`) | — | the choice's display label, not the stored value |
| `string` (other) | — | as-is |

**Timezone:** when a date/timestamp result column has a `timezone` in `get_table_context` (e.g. `UTC` for epoch columns, `offset-aware` for a TZ-aware timestamp), **state it once in the prose insight** ("times shown in UTC") so the reader isn't guessing — and especially flag it when the stored value is a naive/`offset-aware` timestamp whose zone differs from the user's. Epoch columns are UTC by definition; `format-table` already labels each rendered cell `… UTC`.

**Hard rule for currency:** if the field has a currency `agami.unit`, show the symbol or code in **every** cell value — never omit it because the column header already mentions the currency. The user's screenshot showed bare `2,162,087` in an "Avg Outstanding (INR)" column; the cell should read `₹2,162,087`. Headers can drop redundant unit (the column header `Avg Outstanding` is enough when every cell is `₹...`), but cells should always carry the symbol so a screenshot or copy-paste of a single cell stays unambiguous.

**Hard rule for dates: never display ISO timestamps like `2026-05-07T15:14:00.000Z`, epoch seconds, or any other machine-format string.** Cell values OR chart-axis labels — both must be human-readable. If the column shows months only (typical for time-series charts grouped by month), use `MMM YYYY` (e.g. `May 2026`); for daily granularity use `MMM D` if all data is in the current year, else `MMM D, YYYY`. The skill is responsible for inferring the right grain from the data.

If the user has stated a date-format preference in `<artifacts_dir>/USER_MEMORY.md` (e.g. "use ISO dates" or "use DD/MM/YYYY"), respect that. The defaults above apply only when USER_MEMORY is silent.

If row count > 30:
- The chat preview shows the first **30** rows only — markdown tables past ~30 rows scroll forever and bury the insight.
- The HTML report (Phase 4e) contains the full set in a paginated table.
- For row counts > 30, **auto-write the CSV to `~/.agami/exports/<profile>/<ts>.csv`** at the same time as the HTML report, without waiting for the user to ask. Surface both paths in Phase 4d's footer.
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

**Render the table deterministically — do NOT hand-type the number cells.** This is a verification surface; an exact number is mandatory (no rounding, no `1.2L`/`2.16Cr` abbreviation, no dropped decimals). Pass the result CSV (first 30 rows, headers sanitized per 3c) and the **`units` map that `sm prepare` already returned in 3a** through the packaged formatter, then **embed its output verbatim**:
```bash
bash "$AGAMI_PLUGIN_ROOT/scripts/sm" format-table --csv-file /tmp/agami-result.csv --units "$PREPARE_UNITS"
```
`prepare`'s `units` is keyed by output column and **traced through the SQL** — so `SUM(amount) AS total_outstanding` correctly carries amount's currency (a bare name match would miss the summed total). `format-table` then formats every numeric cell in full via `units.py` (symbol + Indian/western grouping, exact decimals) and passes non-numbers through. Same map + formatter the **MCP** uses, so verification numbers are identical regardless of host/LLM. (If a header was prettified in 3c, key the units to the prettified name.) Wide tables (> 8 cols) → vertical layout + "wide table — see HTML for the full grid".

**Cap the chat preview at 30 rows** per Phase 3c — even when the user asked for "all leads with credit rating > 700" and the result is 4,213 rows, the chat shows the first 30 and points them at the CSV + HTML report. The full set lives in the artifacts on disk. The footer line ("Showing first 30 of 4,213 rows · full set in CSV: …") is the contract that tells the user where to find everything.

**Multi-section reports skip the table in chat.** The chat already has the insight; the per-section tables live in the HTML report. Multi-section chat output is: approach + fetching + summary + a short bulleted list of section titles + HTML path + 5 follow-ups. No tables in chat.

### 4e — Build ONE coherent HTML report (one file, N sections)

The output is **one self-contained HTML file** at `~/.agami/charts/<profile>/<ts>.html`, no matter how broad the question is. Broad questions decompose into multiple sub-questions; each sub-question becomes a **section** inside the same file. Each section has its own chart + table + insight + SQL. **Never write multiple HTML files for one user question. Never open multiple browser tabs.**

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
  "sql":           "<the EXACT, COMPLETE SQL that produced this section's data — verbatim>"
}
```

**`sql` is the exact, complete, runnable query — never a paraphrase.** Paste the *verbatim* SQL string you executed for this section (the text you passed to `sm prepare` / the executor — keep each section's query around for this). It must be:
- **Complete** — every CTE, column, JOIN, WHERE, GROUP BY. The receipt exists so a data engineer can re-run it and defend the number; a partial query defeats that.
- **Literal SQL, not prose** — no `...`/`…`, no `-- net revenue = (line_total - gst) - ...`, no "ON paid book lines". If you find yourself writing English where SQL goes, you're summarizing — stop and paste the real query.
- **Never empty when the section has data.** Every section that ran a query has its query. A section with no `sql` only makes sense for a hand-built/derived table with no underlying SQL — and that is rare; double-check before omitting.

Do NOT HTML-escape it — the template handles escaping. Do NOT shorten it to save tokens; the renderer reads it from a file, so length is free.

When `chart_type` is `null`, set `labels` and `datasets` to `null` (or omit). The template skips the chart card cleanly.

**Use the formatted values from Phase 3c — not the raw CSV strings.** Specifically:

- `labels` for time-series line charts must be human-readable date labels (`May 2026`, `Q2 2026`, `May 7`, `May 7, 2026`) — NOT raw ISO timestamps like `2026-05-07T15:14:00.000Z` and NOT epoch numbers. The granularity follows the SQL grouping (monthly bucket → `MMM YYYY`, daily → `MMM D` or `MMM D, YYYY`).
- `labels` for categorical charts use the choice-field display label (e.g. `Closed Won`, not the stored value `closed_won`).
- `table_rows` cells use the formatted values across the board: `$148.95` not `148.95`, `May 7, 2026 3:14 PM` not `2026-05-07T15:14:00Z`, `Yes` not `true`.

`datasets[].data` is the **only** place raw numeric values belong (Chart.js needs numbers, not formatted strings, to draw the chart). Currency / percent formatting on the chart itself happens via the template's tooltip callbacks, not by passing pre-formatted strings.

The whole report's `SECTIONS_JSON` is a JSON array of these objects.

#### 4e.iii.5 — build the trust receipt

Every answer ships with a **trust receipt** that documents provenance: tables touched, relationships used, metric definitions invoked, named filters applied, source-data freshness, and the model version pin. This is the single most important UX element for a data engineer — it lets them defend any number to a CxO with one click.

Build the receipt as a single JSON object. Schema (see [`shared/chart-template.html` → `RECEIPT_JSON`](../../shared/chart-template.html) for the canonical version):

```json
{
  "model_version": "<short hash from index.yaml.introspect_meta.model_version>",
  "tables_used": [
    {"qname": "public.orders", "rows": <integer or null>, "freshness": "<ISO8601 + cadence note, or null>"}
  ],
  "relationships": [
    {"name": "<rel name>", "from_to": "<from> → <to>",
     "confidence": <float in [0,1]>, "review_state": "approved|unreviewed|...", "origin": "fk|...",
     "signed_off_by": "<email or 'agami_introspect_v1'>", "signed_off_role": "<enum>",
     "signed_off_at": "<ISO>"}
  ],
  "metrics": [
    {"name": "<metric>", "area": "<subject area>", "definition_prose": "<plain English>",
     "expression": "<the SQL fragment / binding>",
     "confidence": <float in [0,1]>, "review_state": "approved|unreviewed|...", "origin": "<enum | ad_hoc>",
     "signed_off_by": "<email>", "signed_off_role": "<enum>", "signed_off_at": "<ISO>"}
  ],
  "named_filters": [
    {"name": "<filter>", "expression": "<predicate>", "definition_prose": "<plain English>",
     "confidence": <float in [0,1]>, "review_state": "approved|unreviewed|...", "origin": "<enum>",
     "signed_off_by": "<email>", "signed_off_role": "<enum>", "signed_off_at": "<ISO>"}
  ],
  "assumptions": [
    {"column": "<schema>.<table>.<column>", "meaning": "<the AI-written description>", "source": "ai_unvalidated"}
  ],
  "warnings": ["<one-liner per unreviewed entry that the answer used>"]
}
```

How to populate each field:

- **`model_version`** — derive from the **newest directory** under `<artifacts_dir>/<profile>/.snapshots/`. The directory name itself IS the version (a 12-char content hash). Lookup:
  ```bash
  model_version=$(ls -t "$artifacts_dir/$profile/.snapshots/" 2>/dev/null | head -n1)
  ```
  If `.snapshots/` doesn't exist or is empty (legacy v1.2 model that pre-dates trust-layer), pass `null` and surface a one-liner: *"this model pre-dates the trust-layer launch; reintrospect to enable receipts."* **Do not look for `model_version` inside `index.yaml`** — it's not stored there; the snapshot directory is the source of truth.
- **`tables_used`** — every distinct `<schema>.<table>` referenced in the SQL's FROM/JOIN clauses. `rows` is the **table's size** — pass the table's `performance_hints.estimated_row_count` from the model (the *same* value Phase 3 reads for the scan-risk check, e.g. `12000000`). This is "how big is this dataset," not the query's result-row count. Also pass **`rows_as_of`** = the table's `performance_hints.estimated_row_count_at` (when that estimate was last measured at introspection) — the receipt renders "≈N rows (estimated as of <date>)" so the reader knows it's a point-in-time estimate, not a live count. Only pass `null` for `rows` if the table genuinely has no `estimated_row_count` in the model — don't default to `null` out of caution when the model has it.
  **`freshness`** — pass the **raw ISO timestamp** from `agami.introspect_meta.introspected_at` (e.g., `"2026-05-10T11:57:13Z"`). The chart template prettifies it to `"introspected May 10, 2026, 11:57 AM"` automatically. Don't prefix with "introspected" yourself or you'll double-prefix. If the upstream load cadence is known (e.g., daily ETL at 2am UTC), pass a pre-formatted string instead — e.g., `"2026-05-10T02:00:00Z (daily 2am UTC ETL)"` — and the template passes it through unchanged. Pass `null` when freshness is unknowable.
- **`relationships`** — for every JOIN edge in the SQL, look up the relationship in the model (from `get_table_context`). **Pull EVERY trust field** carried on the relationship: `relationship` (cardinality), `confidence` (`confirmed`/`inferred`/`proposed`), `review_state` (enum), `signed_off_by`, `signed_off_role`, `signed_off_at`, plus `on:` if it's a CAST/compound join. The template's `approvalPhrase` reads all of them to render "confirmed (FK declared)" vs "approved by jane@x.com (cfo), Mar 15" vs "proposed (inferred join — confirm)". For composite or multi-hop joins, list each edge. Also surface any **auto-rewrite** the pre-flight applied (fan/chasm) and the **default_filters** that were applied (from execute_sql's stderr notes).
- **`metrics`** — TWO kinds of entry, both go here:
  1. **Model metrics** whose `bindings` SQL matches a fragment in the generated SQL. **Pull EVERY trust field:** `calculation` → `definition_prose`, `confidence`, `review_state`, `signed_off_by`, `signed_off_role`, `signed_off_at`, `source`/`origin`, and the metric's `area`. If a metric is genuinely unreviewed, that's fine — the receipt shows it honestly. Don't half-populate (it renders a meaningless "unreviewed (?)").
  2. **On-the-fly metrics you calculated for this answer** — any non-trivial aggregation/ratio you composed that ISN'T an approved model metric (e.g. you wrote `(SUM(net_revenue) - SUM(cogs)) / SUM(net_revenue)` for "gross margin" because no `gross_margin` metric exists). Emit one entry per such calc with `review_state: "unreviewed"`, **`origin: "ad_hoc"`**, a snake_case `name` (your suggested metric name), the `area` it belongs to, `definition_prose` (the calculation in plain English), and `expression` (the SQL fragment). This is what feeds the **top-of-report "metrics you haven't approved" banner**, where the user can Approve it (→ saved as a real model metric, signed off) or Change the definition — both routed back to you as feedback. **Don't flag trivial primitives** (a bare `COUNT(*)`, `SUM(amount)` with no logic) as ad-hoc metrics — only genuine, reusable definitions. When in doubt, include it; an over-flag is a one-click dismiss, a miss means an unapproved number ships silently.
- **`named_filters`** — every filter from `agami.named_filters[]` (model-level) whose `expression` appears in the SQL's WHERE / HAVING. **Pull EVERY trust field** from the filter's entry: `expression`, `definition_prose`, `confidence`, `review_state`, `origin`, `signed_off_by`, `signed_off_role`, `signed_off_at`. Same rationale as relationships and metrics.
- **`assumptions`** — the AI-derived column meanings this answer **leaned on**, so a wrong/unknown one is caught in context instead of via an upfront review of hundreds of descriptions (see [`docs/design/validated-through-use-descriptions.md`](../../../../docs/design/validated-through-use-descriptions.md)). For each column the SQL used in a **load-bearing** way — SELECT / WHERE / GROUP BY / ORDER BY / a metric binding, **not** pure join plumbing — look it up in `get_table_context` and surface two cases:
  - `description_source == "ai_unvalidated"` (a guess) AND `description` non-empty → `{column, meaning: <description>, source: "ai_unvalidated"}`.
  - `description_source == "ai_unknown"` (agami couldn't read it) → `{column, meaning: null, source: "ai_unknown"}` — agami used a column it doesn't understand; flag it loudly.
  Columns with `description_source` of `human`, `ai_validated`, or `null` are NOT surfaced. **Cap at 3**, ranked by load-bearing-ness (filter/group-by > select > order-by) and putting `ai_unknown` first (an unknown the query relied on is the riskiest). Pass `[]` when none qualify. Advisory — never blocks or warns; it's a "here's what I assumed / didn't know" so the user can correct, confirm, or describe.
- **`warnings`** — for every **non-metric** entry above whose `review_state ≠ approved` (joins, rewrites, applied filters), push a one-line warning naming the entry and its confidence. Example: `"Used 1 unreviewed join (orders → customers, conf 0.62)."`. **Do NOT add a warning line for unreviewed/ad-hoc metrics** — they get their own actionable **"metrics you haven't approved" banner** (from `receipt.metrics`) with Approve / Change controls, so a warning line would just duplicate it. If the receipt has any warnings, **append a final action line as the last warning**: `"Run /agami-model review to walk these items, or say 'open the review queue'."` This gives the user a clickable next step (the slash command renders as readable text in the warning banner). If the receipt has zero warnings, pass `[]` and the banner suppresses entirely. (Assumptions and metric-approvals are NOT warnings — keep them separate.)

Build the receipt at `/tmp/agami-receipt-<ts>.json` and pass it to `render_chart.py` via `--receipt-file` (see 4e.iv below). For a 1×1 scalar answer with no chart (Phase 4e skips the report), still construct the receipt mentally so you can surface warnings inline in the chat answer ("Note: this used 1 unreviewed join").

#### 4e.iv — render via `render_chart.py` (do NOT inline-substitute through the Write tool)

The HTML report is produced by a Python helper that reads the template + SVG logos once and substitutes placeholders. **Do not Read the template + Write the rendered HTML through the LLM** — that path costs ~30KB of token I/O per query and is the dominant slowness in chart rendering.

Instead:

1. Build the sections JSON file at `/tmp/agami-sections-<ts>.json`. The shape is the JSON array built in 4e.iii — a list of section objects (`title`, `insights`, `chart_type`, `labels`, `datasets`, `table_headers`, `table_rows`, `sql`).

2. Build the receipt JSON file at `/tmp/agami-receipt-<ts>.json` per Phase 4e.iii.5.

3. Run the renderer:

```bash
ts=$(date +%Y%m%d-%H%M%S)
# Per-profile subdir so multi-profile users can tell charts apart and
# clean per-profile via dev/reset-yamls.sh --clean-renders.
mkdir -p ~/.agami/charts/"$profile"
python3 "$AGAMI_PLUGIN_ROOT/scripts/render_chart.py" \
  --title "$USER_QUESTION" \
  --summary "$EXECUTIVE_SUMMARY" \
  --sections-file "/tmp/agami-sections-$ts.json" \
  --receipt-file "/tmp/agami-receipt-$ts.json" \
  --out "$HOME/.agami/charts/$profile/$ts.html"
```

The helper reads `shared/chart-template.html` + the two logo SVGs once, validates each section + receipt, runs `template.replace(...)` for each placeholder, and writes the file. Stdlib only — no extra deps.

4. Delete the temp files: `rm -f /tmp/agami-sections-<ts>.json /tmp/agami-receipt-<ts>.json`.

`--summary` is the executive summary used for multi-section reports; for single-section reports pass an empty string and the section's own insight covers it. `--title` is the user's original question.

`--receipt-file` is **MANDATORY — always build and pass it.** The receipt is the answer-side half of the trust pitch; without it the chart renders but loses provenance (no tables-touched list, no relationships-used, no model-version pin, no warning banner) — an answer no auditor can trace back to a model version. (The LLM has skipped this; don't.)

The receipt construction logic is in Phase 4e.iii.5 above. If trust fields are genuinely missing on every entry the SQL touched (very old model from before the trust spine shipped), build the receipt with empty `relationships: []`, `metrics: []`, `named_filters: []`, `warnings: []` — but always pass `tables_used`, `executed_sql`, and `model_version`. The receipt panel renders gracefully on partial data; what you must NEVER do is omit `--receipt-file` entirely.

If the user pinned a chart type via `--chart bar` (etc.), the LLM still chooses per section — the flag from 2a is hint, not override. Multi-section reports often need different chart types per section.

#### 4e.vi — auto-open the file in the user's default browser

Immediately after writing the HTML, try to launch the browser. **Real-world testing has shown the chart often doesn't auto-open** — the host's permission cache may not include the `open` command pattern, the path may have an unexpected character, or the user is in a headless environment. Treat the open call as best-effort, not load-bearing — the path printed in 4e.vii is the contract.

**Run a multi-command fallback chain** in one Bash invocation. The host typically caches the first successful pattern, so subsequent queries skip straight to it:

```bash
chart="$HOME/.agami/charts/$profile/<ts>.html"
( command -v open    >/dev/null 2>&1 && open "$chart" ) || \
( command -v xdg-open >/dev/null 2>&1 && xdg-open "$chart" ) || \
( command -v start    >/dev/null 2>&1 && start "$chart" ) || \
( command -v cmd      >/dev/null 2>&1 && cmd /c start "" "$chart" ) || \
echo "agami: couldn't auto-open the chart — open manually: $chart"
```

Surface the outcome explicitly in chat (don't let it disappear into the bash collapsible):

- **Open succeeded** (exit 0, no fallback message printed) — surface in chat: `✓ Chart opened in your browser. (Path: ~/.agami/charts/<profile>/<ts>.html)`
- **Fallback printed** (the `agami: couldn't auto-open` line) — surface: `Couldn't auto-open the chart in this environment. Open it yourself: ~/.agami/charts/<profile>/<ts>.html`
- **First time the user runs a query in this host** — `open` may prompt for permission. The shipped `.claude/settings.json` allowlists `Bash(open ~/.agami/charts/**/*.html)` precisely so this prompt doesn't fire, but if the user's local settings override or strip that, they'll see a one-time approval modal. Tell them in chat: "First-run permission prompt — approve `open` and the chart will pop up. Future queries skip the prompt."

If the user reports "the chart never opens", check (a) the path printed in chat exists on disk, (b) `command -v open` returns 0 in their host, (c) their `.claude/settings.json` includes the allowlist. The skill cannot fix mode-blocked hosts on its own; the path-in-chat fallback is the universal-truth surface.

#### 4e.vii — surface in chat

After writing the file and triggering `open`:

- For a **single-section** report: surface the section's insight + the markdown table (Phases 4c + 4d). End with the chart's path as **plain text** (NOT a markdown link):
  ```
  Chart: ~/.agami/charts/<profile>/20260507-150912.html
  ```
- For a **multi-section** report: surface the executive summary + a tight bulleted list of section titles. **Don't** repeat each section's table in the chat. End with:
  ```
  Report (N sections): ~/.agami/charts/<profile>/20260507-150912.html
  ```

**Do NOT format the path as `[Open chart](file://...)` or any other clickable markdown link.** Some hosts (notably VS Code's Claude Code chat sandbox) only route workspace-relative paths through their click handler; `file://` URLs and absolute paths outside the workspace die silently. A fake-clickable link is worse UX than a plain path the user knows they can `open` from their terminal.

If you genuinely detect that you're running in Claude Desktop (which has a working preview pane via path clicks), you may format the path as ``Open `~/.agami/charts/<profile>/<ts>.html` `` (backticks, not a link) — Desktop users get the click-to-preview experience naturally.

For hosts that support inline artifacts, also embed the HTML as a Claude artifact block (a single block; don't emit one per section).

**Assumption nudge (only when `assumptions` is non-empty).** If the receipt's `assumptions[]` has entries, add ONE short line right after the answer (before the follow-ups) so a wrong/unknown meaning is caught in context. Phrase by `source`:
- `ai_unvalidated` (a guess) → *"read `net_margin` as '(revenue − cost) ÷ revenue'"*.
- `ai_unknown` (no description) → *"used `xyz`, which I don't have a description for — is that the right column?"* (lead with these; they're riskier).

e.g.:
```
ℹ This answer used xyz, which I don't have a description for — is that the right column? It also read net_margin as "(revenue − cost) ÷ revenue". Tell me if anything's off (or what xyz means); say "looks right" to confirm the rest.
```
Keep it to the columns in `assumptions[]` (≤3), one clause each, plain English. Skip the line entirely when `assumptions` is empty. Soft nudge, never a blocking question.

#### 4e.viii — assumption confirm / correct loop

When the user responds to that nudge:

- **Confirms** ("looks right", "yes", "confirmed") → mark those descriptions validated so they never resurface. Build one curate ops array — one `edit` per confirmed column setting `description_source` to `ai_validated` — write it to `/tmp/agami-confirm-desc.json` (Write tool), and apply:
  ```bash
  bash "$AGAMI_PLUGIN_ROOT/scripts/sm" curate "$ROOT" --ops-file /tmp/agami-confirm-desc.json
  ```
  Each op: `{"op":"edit","kind":"table","area":"<area>","name":"<table>","column":"<col>","field":"description_source","value":"ai_validated"}`. Ack in one line: *"✓ Locked in — won't ask about those again."*
- **Corrects one, or describes an unknown** ("net_margin is actually …", "xyz means customer lifetime value") → route to **agami-save-correction**: it writes the column `description` (which flips `description_source` to `human` automatically via the curate edit) AND saves the (question, SQL) example. This is the path for both a wrong `ai_unvalidated` guess and a filled-in `ai_unknown` blank — either way the column now has a trusted, human description. Don't hand-edit here — let save-correction classify + write.
- **Ignores it / asks something else** → do nothing. An `ai_unvalidated` stays a guess; an `ai_unknown` stays unknown. Either may resurface if a later query uses that column in a load-bearing way. Never auto-confirm on silence — that's the rubber-stamp we're avoiding. (Note: you can't "confirm" an `ai_unknown` — there's nothing to confirm; it needs a description, so only a correction clears it.)

### 4e.5 — GitHub-star ask (one-time, gates Phase 4f)

**This step is required between 4e and 4f.** Do not emit the 5 follow-up bullets in 4f without first running this check.

```bash
test -f ~/.agami/.optins
```

- **Exit 0** (`.optins` exists) — skip this step. Continue to 4f.
- **Exit 1** (`.optins` missing) AND the query just completed successfully — surface the GitHub-star ask via `AskUserQuestion`. **End the turn here.** Do NOT emit Phase 4f. Full ask + handling in [Phase 6 below](#phase-6-post-install-github-star-ask-full-spec--triggered-from-phase-4e5); the trigger lives here (not only in Phase 6) so it fires before Phase 4f. **Read Phase 6's two HARD RULES (verbatim prose, literal URL `https://github.com/AgamiAI/LiteBi`) before emitting.**

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

**Saving a correction is NOT a follow-up bullet.** When the user expresses dissatisfaction with the answer, the skill suggests it inline as a single sentence outside the numbered list, in plain language: *"If the SQL's off or you want me to remember something, just tell me — e.g. 'fix the join to use customer_id' or 'note that amounts are in INR' — and I'll fix it and save it to the right place."* Handle that reply **inline via Phase 4h** (fix the SQL / add a note + route it); the user never needs to know about `/agami-save-correction` (though "save this as a correction" / "remember this" still route to that skill too). The numbered list stays focused on **what to ask next**, not how to fix what we just said.

### 4g — CSV export (`--csv` or "export this")

Two ways the CSV gets written:

1. **Auto-export for large results** (row count > 30, per Phase 3c). The CSV is written alongside the HTML report without the user asking, and the path is surfaced in Phase 4d's footer.
2. **Explicit `--csv` / "export this" / "export to Excel"** for any result, including small ones. The user explicitly wants a flat file.

Either way:

- Single-section report → one CSV at `~/.agami/exports/<profile>/<ts>.csv`.
- Multi-section report → one CSV per section at `~/.agami/exports/<profile>/<ts>-<section-slug>.csv`. Surface all paths.

```bash
ts=$(date +%Y%m%d-%H%M%S)
mkdir -p ~/.agami/exports/"$profile"
# write header + rows per section, RFC 4180 escaping; output path is
# $HOME/.agami/exports/$profile/$ts.csv (or with section-slug suffix).
```

CSVs open natively in Excel / Numbers / Google Sheets — when the user asks for "Excel", the CSV path is the answer. If they specifically want a `.xlsx` (formulas, multiple sheets, formatting), tell them to open the CSV in Excel and Save As — v1 doesn't ship a native `.xlsx` writer.

Surface the path(s) inline. For the auto-export case, the path is already in the Phase 4d footer; the explicit-export case adds a separate confirmation line before Phase 4f.

### 4h — Fix the SQL or add a note (inline correction + routing)

This is the **live-query twin of the onboarding seed-validation flow** (`edit N` to fix an example's SQL / attach `notes[]`). When the user's reply to an answer is a *correction*, handle it **in this turn** — don't bounce them to `/agami-save-correction`. Two shapes:

The correction can arrive two ways: the user **types** it, or they paste the block from the HTML report's **"Send feedback to Claude"** button (Phase 4e — each section has a *"✎ Fix the SQL or add a note"* editor + a footer button that packages every edit/note into a paste-ready block beginning *"Save this as a correction…"*). Either way, route it here.

- **Fix the SQL** — "that join is wrong, use `customer_id`", "exclude cancelled rows", or a pasted corrected query. Build the corrected SQL (write it from their words if they described the fix), **re-run it via Phase 3** to confirm it executes, and re-render the answer (4c–4f).
- **Add a note** — "TOTAL can be negative for refunds", "amounts are in INR", "always exclude test users". A durable fact; no SQL change needed.

**Where it goes — route with the SAME decision tree as [`agami-save-correction`](../agami-save-correction/SKILL.md) Phase 3 (one source of truth; do NOT re-derive a routing here).** The mechanics:

1. **Floor (always):** the corrected SQL for *this question* lands as a prompt example — `sm add-example "$ROOT" --area <area> --file <json>` (dedups by question, so it replaces). Exactly the onboarding seed behavior. A pure note with no SQL change skips this.
2. **The learning on top**, routed by the tree — a column's meaning/unit/encoding/value-map → `field_metadata` (`unit` / `value_transform` / `choice_field`); a join fix → `relationship`; a whole-table fact → `table_metadata`; a reusable aggregation → `new_metric`; a per-result caveat tied to this one question → the example's `notes[]`; a cross-cutting display convention for this DB → `ORGANIZATION.md`; a personal stylistic tic → `USER_MEMORY.md`. Model edits go through `sm curate`; build the ops JSON with the **Write tool** (never a heredoc / `python3 -c`).

**A shared-model edit needs the user's nod — an example save doesn't.** Saving the corrected SQL as an *example* (the floor) is automatic; it only shapes this question's few-shot. But a **shared-model edit** (a `caveat` / `unit` / `value_transform`, a relationship fix, a description, a new metric) changes how **every** future query reads that column or join — so **state the exact change and get a quick OK before writing it**, e.g. *"I'd add a caveat to `sales.discount_amount`: 'cart-level discount, not pushed to line items — allocate across lines.' Add it?"* This is the same gate `agami-save-correction` enforces ("show a diff before any model mutation") — handling it inline here does **not** drop it.

**This applies doubly when YOU discovered the issue, not the user.** If mid-analysis you find a model-level subtlety (a column that needs a caveat, a metric that's mis-defined), your interpretation can be wrong — so **propose** the edit, never auto-apply it to the shared model, and **never stamp the user's sign-off on their behalf.** Sign-off is the user's explicit act (the Review tab, or saying "approve") — a correction doesn't grant it. Fixing *your own* answer for the current turn is always fine; persisting a change to the shared model is what needs the nod.

**Make the routing visible** — name where each piece landed: *"Saved the corrected SQL as an example for `sales`, and (with your OK) set `ORDERS.TOTAL` unit → INR."* Never silent. Then set `feedback: "bad"` on the prior `query_log.jsonl` entry (Phase 5). `$ROOT` and `<area>` come from the model already loaded in Phase 1b; the original SQL is in this turn (and `query_log.jsonl`).

**Positive confirmation ("This looks good").** The report also has a **👍 This looks good** button (and the user may just say so) — a paste-back beginning *"This agami answer looked correct."* This is the inverse: set `feedback: "good"` on the log entry, and **consider** saving the question + SQL as a **confirmed** prompt example (`sm add-example`, `status: confirmed`) — **your discretion, not reflexively.** Add it when it's a genuinely reusable, non-trivial pattern (a real join, a metric definition exercised, a non-obvious filter); **skip** trivial one-offs (`SELECT COUNT(*)`), near-duplicates of an existing example, or throwaway exploration. Say what you did — *"Good to hear — saved it as an example so the next similar question reuses this SQL"* or *"Glad it's right (didn't add an example — it's close to one already in the library)."* A confirmed-good example is a strong few-shot, but the library's value is precision, not volume — don't dilute it.

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
  "chart_path": "/Users/me/.agami/charts/main/20260507-141500.html",
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

**HARD RULES — read before emitting the ask (the LLM has hallucinated both of these):**

1. **The repo URL is literally `https://github.com/AgamiAI/LiteBi`.** Copy it byte-for-byte from this SKILL. **Never construct it from any other source** — not the marketplace name (`litebi`), the plugin name (`agami`), or the `/plugin install agami@litebi` slash command (which has produced the wrong `github.com/litebi/agami`). Note the uppercase `A`'s and capital `B` in "LiteBi".
2. **Use the prompt prose VERBATIM** from the `AskUserQuestion` text below — don't paraphrase, "improve" the wording, or add emojis/marketing flourish. Paraphrasing drifts the ask off the discrete-decision shape it's tuned for and undermines the non-pushy framing.

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
4. Next turn: process the decision (write `~/.agami/.optins`). Then — **whatever they answered** — surface the one-time **`/agami-serve` pointer** (below). After it, show the 5 follow-up bullets per Phase 4f, with a tiny acknowledgment line ("Now, where next?") before the numbered list.

**The `/agami-serve` pointer (one-time, right after the star answer).** Plain prose — NOT an `AskUserQuestion`, NOT one of the numbered follow-ups. Surface it once, here, regardless of which of the three star responses they gave:

> One more thing worth trying: **`/agami-serve`** wires this same model into the **Claude Desktop app** as a local MCP server — so you can ask these questions in plain English from Desktop, no Claude Code needed. It's the local mirror of the hosted "Ask Agami" connector, so it's the exact experience your business users would get. Run `/agami-serve` whenever you want to set it up.

Keep it to ~2 sentences; don't oversell, don't repeat it in the follow-up bullets. Because it lives in the same turn that writes `.optins`, it fires exactly once — every later query takes the `.optins`-exists path below and skips both the star ask and this pointer.

If `~/.agami/.optins` already exists, skip the ask **and the `/agami-serve` pointer** entirely, and emit the 5 follow-ups in the same turn as the answer (Phase 4f as today).

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

(Phase 7 — telemetry flush — has been removed in the current 0.x line. The skill no longer reads `analytics_consent`, no longer appends to `.telemetry-queue.jsonl`, and no longer POSTs anywhere. agami has no telemetry — see `docs/privacy.md`.)

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
| Browser open fails for the GitHub-star ask | Tell user "Couldn't open the browser — the link is github.com/AgamiAI/LiteBi". Save the response anyway. |
