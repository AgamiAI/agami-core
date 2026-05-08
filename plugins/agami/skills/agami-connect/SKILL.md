---
name: agami-connect
description: "Introspects the user's database and emits a strict Open Semantic Interchange (OSI) v0.1.1 semantic model at the per-profile YAML file inside the .agami home directory. Generates seed NL-to-SQL few-shot examples (each EXPLAIN-validated against the live DB) at the per-profile examples file, then runs one demo query so the user immediately sees the skill working. Every model write is gated by the OSI + Agami validator — no breaking model is ever persisted."
when_to_use: "Auto-invoked by agami-query-database the first time it runs (when the semantic model YAML is missing). Invoke explicitly when the user says 'connect to my database', 'introspect the schema', 'reload schema', 'add a new database', or after the user changes their schema and wants the model refreshed. Requires agami-init to have run first (credentials must exist)."
argument-hint: "[reintrospect | profile NAME]"
---

# agami connect

**Before suggesting any slash command in chat, read [`shared/invocation-conventions.md`](../../shared/invocation-conventions.md).** All four agami slash commands (`/agami-init`, `/agami-connect`, `/agami-query-database`, `/agami-save-correction`) work. Never write the un-prefixed forms (`/init`, `/connect`, etc.) or colon forms (`/agami:connect`) — those don't exist. For chat replies, prefer natural language ("say 'reload the schema'", "say 'introspect my database'") — the agami-connect skill's `when_to_use` matcher routes correctly without an explicit slash command.

You are setting up the agami semantic model for the user's database. Goal: by the end, there is a **per-schema OSI v0.1.1 model** at `~/.agami/<profile>/` (`index.yaml` + one `<schema>.yaml` per database schema), a seeded examples library at `~/.agami/<profile>/examples.yaml`, an `ORGANIZATION.md` template the user can edit, and the user has seen one demo query execute end-to-end.

This skill orchestrates four phases:

1. **Introspect** — pull tables / columns / PK / FK from `information_schema` via the chosen database tool (psql / mysql / snowsql / sqlite3 / DuckDB / `execute_sql.py`).
2. **Build the OSI model** — assemble the YAML strictly to the OSI v0.1.1 spec, with Agami metadata (column types, choice fields, performance hints) packed under `custom_extensions[].vendor_name: COMMON` per [`shared/agami-osi-extensions.md`](../../shared/agami-osi-extensions.md).
3. **Validate, then write** — run the validator at `plugins/agami/scripts/validate_semantic_model.py`. If it fails, **DO NOT WRITE THE FILE.** Surface the errors and stop.
4. **Seed examples + run demo query** — generate few-shot pairs, EXPLAIN-validate each, then pick one to run as a demo and ask the user Yes / No / Skip.

For the OSI format spec: [`shared/schema-reference.md`](../../shared/schema-reference.md).
For the bundled JSON schema: [`shared/osi-schema.json`](../../shared/osi-schema.json).
For Agami's documented `custom_extensions`: [`shared/agami-osi-extensions.md`](../../shared/agami-osi-extensions.md).
For introspection SQL: [`shared/introspect-queries.md`](../../shared/introspect-queries.md).
For FK validation: [`shared/fk-validation.md`](../../shared/fk-validation.md).
For SQL dialect rules: [`shared/dialect-rules.md`](../../shared/dialect-rules.md).
For SQL safety: [`shared/sql-generation-rules.md`](../../shared/sql-generation-rules.md).
For DB error classification: [`shared/db_error_classifier.md`](../../shared/db_error_classifier.md).

## Conversation style

- **Combine acknowledge + next question** — don't waste turns on "Got it!"
- **Use AskUserQuestion for every Yes/No/Skip** — never inline-bullet options. Mark exactly one option `(Recommended)` first.
- **Keep the user oriented** — print one-line progress markers between phases (`✓ Introspected 12 tables`, `✓ Validator passed`, `✓ Generated 10 examples`).

---

## Phase 0: Preflight

### HARD RULES — read before doing anything

These are non-negotiable. They override every other instruction in this file when they conflict.

1. **Connect ONLY to the host/port/database/user/password in `~/.agami/credentials`** (or, if set, in `AGAMI_DATABASE_URL`). Never connect to anything else. Never probe `localhost` "to see if there's a database running there" unless the credentials file explicitly says `host = localhost`. Never substitute defaults for missing credential fields.
2. **Never ask the user for host / port / database / user / password values in chat.** Not even "as a temporary thing while we set up". Credentials live in `~/.agami/credentials` only — that's the contract.
3. **Never scan or guess.** No `pgrep`, no `ps`, no `find /` for databases, no `ls /Applications/Postgres.app`, no `ls /Library/PostgreSQL`, no listing port-listeners, no testing connections to common hostnames. The only acceptable Bash probes in this phase are `which <tool>` (to find a CLI binary on `PATH`) and `python3 -c 'import <module>'` (to test a driver). Nothing else.
4. **If credentials are missing, STOP this skill and invoke `agami-init`.** Do not run introspection. Do not start tool detection. Do not write a temporary credentials file from values the user types. Tell the user in one short sentence "Your credentials file is missing — I'll re-run setup so you can enter them in the file" and hand off to `agami-init`.
5. **NEVER put the password (or any credential field) in a Bash command line.** That includes `export PGPASSWORD='<value>'`, `export MYSQL_PWD='<value>'`, `psql -W <password>`, `mysql -p<password>`, or any heredoc form that interpolates the password into stdin. Hosts render Bash tool calls in chat — anything in the command leaks. Use the auth files generated by `scripts/setup_pgauth.py`: `PGPASSFILE=$HOME/.agami/.pgpass psql -h ... -U ... -d ... -c "$SQL" --csv` (psql) or `mysql --defaults-file=$HOME/.agami/.mysql.cnf --defaults-group-suffix=_<profile> ...` (mysql). For the Python driver path use `python3 scripts/execute_sql.py`. See [`shared/connection-reference.md → HARD RULES`](../../shared/connection-reference.md).

If you find yourself reaching for any command that doesn't fit the rules above, stop and re-read this section.

### Preflight steps

1. **Credentials check (binding)**: read `~/.agami/credentials` if present, OR check `AGAMI_DATABASE_URL` env var. If neither exists, invoke `agami-init` and **stop this skill**. Do not continue. Do not probe anything.
2. Apply the credentials chmod check from the agami-init skill's permissions-enforcement section. Refuse to proceed if too permissive.
3. Resolve `<profile>` in this order: `AGAMI_PROFILE` env var → `active_profile` field in `~/.agami/.config` → literal string `"default"` (legacy fallback). The OSI `semantic_model[].name` MUST equal the resolved `<profile>`.
4. Resolve the connection fields from the credentials file's `[<profile>]` section (or parse from `AGAMI_DATABASE_URL`):
   - **postgres / redshift / mysql:** `db_type`, `host`, `port`, `database`, `user`, `password` (plus optional `sslmode`).
   - **snowflake:** `db_type`, `account`, `user`, `password` (or `authenticator`), plus optional `warehouse`, `database`, `schema`, `role`. **No `host`/`port` for Snowflake** — its connector uses the account identifier directly.
   - **sqlite:** `db_type`, `path`.

   Never substitute a value that's missing — surface a clear "your credentials file is missing field X for profile Y; please add it" message and stop.
5. Look up the cached connection method and tool paths from `~/.agami/.config`. If absent, run tool detection per the agami-init skill's Phase 3.
6. If `$ARGUMENTS` is `reintrospect`: skip Phase 1's "already-have-a-model?" check and re-introspect from scratch. **Hand-edits the user made (descriptions, ai_context, choice_fields, metrics) MUST be preserved** — re-introspection only updates what the DB unambiguously tells us (table list, columns, types, PK, FK).

---

## Phase 1: Introspect

### Phase 1.0 — set expectations before kicking off

Introspecting can take a while, especially against cloud DBs. Tell the user **before** the first probe so they don't think the skill has hung. The estimate depends on the database type:

| db_type | Typical setup time | Why |
|---|---|---|
| `sqlite` | < 5 seconds | Local file, instant metadata. |
| `postgres` (local) | 5–15 seconds | Local network, fast `information_schema` queries. |
| `postgres` (cloud — Supabase / Neon / RDS) | 15–60 seconds | Network round-trip per query, plus FK validation join checks. |
| `mysql` | 10–30 seconds | Similar to postgres. |
| `redshift` | 30–120 seconds | Cloud + Redshift's metadata can be slow to return. |
| **`snowflake`** | **60–180 seconds** | Cold warehouse spin-up + per-table SHOW commands + EXPLAIN-validation against the live warehouse. Sometimes longer for accounts with many schemas. |

Surface a one-liner with **per-step duration estimates** so the user can tell the skill apart from a hang at any moment, not just the first:

> Setting up your `<profile>` connection — for `<db_type>` this typically runs:
> - Listing schemas (~5s)
> - Discovering tables (~<10–30>s depending on schema size)
> - Generating descriptions (~30–60s for ~50 tables)
> - Seeding examples (~20s)
> - Demo query (~5s)
>
> Total: **<low>–<high> seconds**. I'll narrate as I go.

Then proceed. **For reintrospect:** prepend "Re-introspecting (this takes about as long as initial setup)." so the user knows the estimate still applies.

### Phase 1.1 — existing-model check + legacy-layout migration

The current layout is `~/.agami/<profile>/` (a directory with `index.yaml` + per-schema yamls). v1.0 installs used a single file at `~/.agami/<profile>.yaml` — auto-migrate those.

```bash
profile_dir="$HOME/.agami/$profile"
legacy_file="$HOME/.agami/$profile.yaml"

if [ -d "$profile_dir" ] && [ -f "$profile_dir/index.yaml" ]; then
  layout=existing-directory
elif [ -f "$legacy_file" ]; then
  layout=legacy-single-file
else
  layout=fresh
fi
```

**Branch on `layout`:**

- **`existing-directory`** and `$ARGUMENTS` is not `reintrospect`:
  - "I already have a model for `<profile>` at `~/.agami/<profile>/`. What would you like to do?"
  - AskUserQuestion: `Re-introspect from DB` / `Verify and continue (Recommended)` / `Skip to seeding examples`.

- **`legacy-single-file`**:
  - Tell the user: "Upgrading your model to the new per-schema layout (it's faster for large databases and lets you give per-schema descriptions). Backing up your old model and re-introspecting now (~30–90s)."
  - `mkdir -p "$profile_dir" && chmod 700 "$profile_dir"`
  - `mv "$legacy_file" "$profile_dir/_legacy.yaml.bak"`
  - Also migrate `~/.agami/<profile>-examples.yaml` if present: `mv "$HOME/.agami/<profile>-examples.yaml" "$profile_dir/examples.yaml"` (no rewrite needed; format is unchanged).
  - Force `$ARGUMENTS=reintrospect` for the rest of this skill so we re-introspect from the DB.

- **`fresh`**:
  - `mkdir -p "$profile_dir" && chmod 700 "$profile_dir"`
  - Continue to introspection.

Otherwise, continue to schema selection.

### Phase 1.2 — list schemas

Run the dialect-specific schema query from [`shared/introspect-queries.md`](../../shared/introspect-queries.md). For SQLite, skip this entirely and use the single implicit `main` schema.

Capture: list of schema names the connected user has access to.

Surface: `Found <K> schemas: <name1>, <name2>, …`

### Phase 1.3 — schema picker (multi-select)

For non-SQLite databases, ask the user which schemas to introspect. Skipping this step means the skill defaults to *every* schema, which is rarely what the user wants on a Snowflake account with 50+ schemas.

**AskUserQuestion** with multi-select:

> Which schemas should I introspect? (Pick one or more — I'll only build the model for what you select.)

Options:
- One option per discovered schema. Pre-check `public` (Postgres), the credentials' `database` (MySQL), or `PUBLIC` (Snowflake) by default.
- A top option `All schemas (Recommended for small DBs)` — useful when the user has only 2–3 schemas.
- A `Just <default> for now (skip the rest)` shortcut — useful when the user knows they only need one schema right now.

Record the selection: `selected_schemas := [...]`. The next phases (1a list tables, 1b per-table, 1c FK validation, 1d descriptions) constrain to these schemas only. Reintrospect later only touches the same schemas unless the user explicitly says "also introspect the `<x>` schema."

The selection is recorded in `index.yaml.schemas[]` (see Phase 2). Schemas not selected do NOT appear in `index.yaml`.

Run introspection. For every step, use the SQL from [`shared/introspect-queries.md`](../../shared/introspect-queries.md), executed via the chosen tool (psql / mysql / snowsql / sqlite3 / DuckDB / `execute_sql.py`):

### Phase 1.4 — collect a one-paragraph organization context

After the schema picker (1.3) but before the heavy per-table work, prompt the user once for domain context. Domain context boosts NL→SQL accuracy a lot — a 30-second ask that often pays for itself.

If `~/.agami/<profile>/ORGANIZATION.md` exists AND has been edited beyond the default template (any line longer than the template's parenthetical guidance), skip this phase.

Otherwise, **AskUserQuestion**:

> Want to give me a one-paragraph description of what this database is about? It improves NL→SQL accuracy a lot — without it I have to guess the domain from table/column names alone.
>
> Examples of useful context: what the company / product is, what "MRR" or "active user" means in your terms, what kinds of users / customers you have.

Options:
- `Yes — I'll type it now (Other field)` — capture the user's free-form paragraph and write it to `~/.agami/<profile>/ORGANIZATION.md` under a `# About this database` heading. Add the rest of the default template (terminology / who's in this data / what we don't track) as commented prompts the user can fill in later.
- `Skip — I'll edit ORGANIZATION.md later (Recommended)` — write only the default template (untouched) so the user knows where the file lives.

In both cases, write to `chmod 600`. Format and content rules: see [`shared/organization-context-format.md`](../../shared/organization-context-format.md).

### Phase 1.5 — optional: data-model document upload

Many users have an existing artifact describing their schema — an ERD, a data dictionary, a "what each table means" Confluence page. Feeding it to the description generator is a big lift on accuracy with zero extra introspect work.

Ask **once**, low-friction:

**AskUserQuestion**:

> Got a data-model document I can read for additional context? Things like an ERD diagram, data dictionary, schema doc, or anything else that explains what your tables are for.
>
> Drag-and-drop a file here, or paste a path. **PDF, PNG / JPG, plain text, markdown, or CSV** all work. For Excel or Word docs, save them as PDF first (File → Save As PDF) — it's the fastest way and works in every editor.

Options:
- `Yes — I'll attach it now (Other field)` — user pastes a path or drags a file into chat.
- `Skip — no doc to share (Recommended)` — proceed to introspection.

If the user provides a path:

1. Use the `Read` tool against the path. Claude's `Read` handles PDFs (with `pages` for large files), images (multimodal — can see diagrams), markdown, plain text, and CSV natively. No format-specific parsing logic needed in the skill.
2. If the file is `.xlsx` / `.docx` / another binary office format, surface a one-liner: "I can't read `.xlsx` / `.docx` directly. Save it as PDF (File → Save As PDF) and re-attach, or paste the relevant content as text and I'll use it as-is." Don't block — proceed without the doc.
3. If the file is huge (> 50 pages PDF or > 100KB markdown), trim to a summary: read the first 20 pages, surface "Loaded the first 20 pages of <name>; let me know if there's a specific section I should focus on." For tabular data (CSV with > 200 rows), keep only the first 50 rows + the header.
4. Stash the loaded content in a working-memory variable: `$DATA_MODEL_DOC_TEXT` (or for images, the multimodal block).
5. **Phase 1d's description-generation prompt** receives this content under a labeled heading: `## User-provided data-model document` (placed BEFORE the schema's tables/columns/sample rows so the LLM treats it as a domain prior, similar to ORGANIZATION.md).

The doc is **never written to disk** — it lives only in the description-generation prompt's context, then is discarded. Same privacy posture as the per-table sample rows from Phase 1d.i.

If the user uploads but the file is unreadable (corrupted PDF, unreachable path, etc.), surface "Couldn't read `<path>` — proceeding without it. You can always re-introspect later if you want to retry." Don't block.

### 1a — list tables (within `selected_schemas` only)

Filter to the `selected_schemas` from Phase 1.3. (System schemas are already filtered by Phase 1.2's discovery query, so they're not in `selected_schemas`.)

For Postgres / Redshift:

```sql
SELECT table_schema, table_name
FROM information_schema.tables
WHERE table_type = 'BASE TABLE'
  AND table_schema IN (<selected_schemas>);
```

Adapt the `IN (...)` clause for MySQL / Snowflake / SQLite (SQLite always introspects the implicit `main` schema).

Surface: `Found <N> tables across <K> schema(s).`

### 1b — for each table, pull columns + PK + FK + row count + indexes

Use the per-dialect queries from [`shared/introspect-queries.md`](../../shared/introspect-queries.md).

For each column: capture `name`, `data_type` (raw DB type), nullability. Map to the simple OSI-extension type set (`string | integer | decimal | timestamp | date | boolean`) using the type mapping table at the bottom of `introspect-queries.md`. Keep the raw DB type as `agami.original_type`.

For each table:
- **Row count** from `pg_stat_user_tables` (Postgres) or `information_schema.tables.table_rows` (MySQL). Tables with > 100k rows get a `agami.performance_hints` extension; tables ≤ 100k don't need one.
- **Indexes** via the index-discovery query (Postgres `pg_indexes`, MySQL `information_schema.statistics`, Snowflake doesn't have traditional indexes — use clustering keys instead, SQLite `PRAGMA index_list`). Capture each index as a list of column names. Skip auto-generated PK indexes (the PK is already in `primary_key`). Persist as `agami.performance_hints.indexes: [[col1], [col2, col3], ...]`.
  - **Why we capture indexes**: the SQL generator in agami-query-database (Phase 2b) uses this to prefer indexed columns for `WHERE` filters and `JOIN` conditions on large tables. A query that filters on an indexed column runs in milliseconds; the same query on an un-indexed column scans the whole table. The LLM doesn't know which columns are indexed unless we tell it.
  - **For all tables, not just > 100k rows**: even on smaller tables, knowing which columns are indexed informs join planning. The `agami.performance_hints` extension is created for any table with indexes, even if `estimated_row_count` is small. (Earlier guidance was "only if > 100k" — overruling that here for indexes specifically.)

**Progress narration (Phase F):** print one line per table as it completes, so the user can see the skill working through their schema. Format:

```
[3/47] public.orders — 12 columns, 2 FKs (description: "Customer-facing orders…")
```

`<i>/<N>` is the table index across all selected schemas. For batched description generation (Phase 1d below), additionally print one line per batch:

```
[batch 2/3] generating descriptions for tables 51–100 in public…
```

Keep narration to ≤ 80 chars per line — long lines wrap in some hosts and look messy.

### 1c — FK validation (live join check)

Run the orphan-ratio query from [`shared/fk-validation.md`](../../shared/fk-validation.md) against every detected FK. Drop any with > 5% orphans. For each FK that survives, record the result as a `agami.fk_validation` extension on the resulting `relationships[]` entry.

If the database had **zero declared FKs**, run heuristic FK inference per `fk-validation.md` and ask:

> I detected N likely foreign-key relationships from column-name conventions:
> - `orders.customer_id` → `customers.id` (1 orphan in 2403 rows)
> - …
>
> Add these to the model?

AskUserQuestion: `Add all (Recommended)` / `Add only zero-orphan ones` / `Skip — let me edit by hand later`.

### 1c.5 — detect `agami.choice_field` (low-cardinality scan)

For each candidate column in each introspected dataset, run the low-cardinality detection query from [`shared/introspect-queries.md → Choice-field detection`](../../shared/introspect-queries.md#choice-field-detection-low-cardinality-scan). If the column has ≤ 20 distinct non-null values, capture them as a `choice_field` map under the field's `agami` extension.

**Candidate selection** (apply all):

- `agami.type` is `string` or `integer`
- Not in the dataset's `primary_key`
- Not in any FK's `from_columns` (foreign-key values aren't enums even when finite)
- Column name matches enum-y patterns (`status`, `state`, `type`, `kind`, `category`, `priority`, `tier`, `level`, `mode`, `flag`, `role`, `phase`, `stage`) **OR** the name is ≤ 32 chars and not in the free-text exclusion list (`name`, `description`, `notes`, `comment`, `body`, `content`, `email`, `address`, `url`, `path`, `slug`, `title`, `subject`, `message`)

**For tables with `estimated_row_count > 10_000_000`**, sample first per the introspect-queries doc — don't scan the full column.

**Output.** For each detected choice_field, write into the field's `custom_extensions` JSON:

```yaml
- name: status
  expression: { dialects: [{ dialect: ANSI_SQL, expression: status }] }
  custom_extensions:
    - vendor_name: COMMON
      data: '{"agami": {"type": "string", "choice_field": {"pending": "pending", "shipped": "shipped", "delivered": "delivered", "cancelled": "cancelled"}}}'
```

Display labels default to the stored value (`label = value`). Don't invent prettier labels — that's a `field_metadata` correction the user can apply later via agami-save-correction.

**Progress narration:** print one line per detected choice_field so the user sees what was found:

```
[choice_field] public.orders.status — 4 values: pending, shipped, delivered, cancelled
```

If a candidate column has > 20 distinct values, skip silently. Don't narrate misses.

### 1d — auto-generate descriptions (per-schema batched, evidence-grounded)

Generate a one-line `description` for **every table and every column** in the selected schemas. Without descriptions, NL→SQL quality drops sharply on large schemas — this pass is mandatory, not optional.

#### 1d.i — sample rows per table

For each table fetch up to 5 sample rows for evidence:

```sql
SELECT * FROM <schema>.<table> LIMIT 5;
```

Snowflake-only: for tables with `estimated_row_count > 10_000_000` use the `SAMPLE` clause to avoid scanning a huge prefix. See [`shared/introspect-queries.md`](../../shared/introspect-queries.md#sample-rows-for-phase-c).

The sample is **never written to disk and never sent in telemetry.** It lives in the description-generation prompt's context, then is discarded.

#### 1d.ii — per-schema batched generation

Process schemas one at a time. For each schema, build a prompt with:

- The user-provided data-model document from Phase 1.5 (`$DATA_MODEL_DOC_TEXT`, or multimodal image block if it's a diagram), if present — placed **first** so it acts as the dominant domain prior. Header: `## User-provided data-model document`.
- The user's `~/.agami/<profile>/ORGANIZATION.md` (if non-empty) — domain context for the database. Header: `## Organization context`.
- The schema's tables, columns (with types), FKs, choice-field hints
- The 5 sample rows per table

Ask the model to emit, for each table:
- A 1-line `description` summarizing what the table holds
- For each column, a 1-line `description` — but **skip if blindingly obvious from the column name** (e.g., `id`, `created_at`). Leave such columns' description empty.

#### 1d.iii — width bounding for large schemas

If a single schema has > 100 tables, batch within the schema: process **50 tables at a time**. Each batch sees the full column list + sample rows for *its* tables, but only summary names of the rest of the schema (for FK context).

Print one line per batch (Phase F narration):

```
[batch 2/4] Generated descriptions for tables 51–100 of public.
```

#### 1d.iv — validate-then-write per schema

After every schema completes, validate that schema's yaml as a standalone OSI doc:

```bash
python3 "$AGAMI_PLUGIN_ROOT/scripts/validate_semantic_model.py" "/tmp/agami-staging-<profile>/<schema>.yaml"
```

If a schema fails validation, the staging file stays at `/tmp/agami-staging-<profile>/<schema>.yaml`. Surface the errors and continue with the next schema — don't block the rest of the introspection. The user gets a single end-of-Phase-1 summary listing which schemas need attention. Phase 3 then runs `--directory` mode on the merged result.

#### 1d.v — what NOT to invent

- Don't invent column meanings for opaque names (`v_1`, `tmp_col`, `x`). Leave empty.
- Don't invent business semantics not supported by sample rows (e.g., don't claim a `status` column is "active vs cancelled" if the samples only show `pending`).
- Don't translate column names ("`amt`" → "amount") — keep descriptions about what the column *means*, not what it's *named*.
- The user can hand-edit any `~/.agami/<profile>/<schema>.yaml` and the changes will survive future re-introspections (Phase 2 hard rule #8 — preserve descriptions, ai_context, choice_fields, metrics).

### 1e — detect units (`agami.unit`) + currency ask

After descriptions but before metric suggestions, scan numeric fields for unit hints. Three categories of unit can be inferred or asked for:

#### 1e.i — auto-detect (no user prompt)

For each `decimal` or `integer` field, infer unit from:

- **Percent**: column name ends with `_pct`, `_percent`, `_rate`, `_ratio` AND sample values are between 0 and 100 (or 0 and 1). Set `agami.unit: "percent"`.
- **Duration**: column name ends with `_seconds`, `_sec`, `_secs`, `_ms`, `_milliseconds`, `_minutes`, `_min`, `_hours`, `_hrs`, `_days`. Set `agami.unit: "<seconds|ms|minutes|hours|days>"` matching the suffix.
- **Bytes**: column name ends with `_bytes`, `_kb`, `_mb`, `_gb`. Set `agami.unit: "<bytes|kb|mb|gb>"`.

These don't need user input — the unit is unambiguous from naming convention.

#### 1e.ii — currency ask (one prompt per profile)

For each numeric field whose name suggests a money amount — `amount`, `price`, `cost`, `revenue`, `total`, `subtotal`, `fee`, `tax`, `discount`, `paid`, `balance`, `salary`, `wage`, `payment`, `charge`, or anything ending in `_usd`, `_eur`, `_gbp`, etc. (the suffix is the answer; skip the prompt for those) — collect the field into a list of currency-candidates.

If the candidate list is non-empty AND there's no per-column suffix giving the answer, ask the user **once per profile** (not per column):

**AskUserQuestion**:

> I detected `<N>` numeric fields that look like money amounts: `<table.field>, <table.field>, ...`. **What currency are these in?**
>
> Pick one — I'll annotate every field with the right unit so charts and totals format correctly.

Options: `USD` / `EUR` / `GBP` / `JPY` / `INR` / `Other (Other field — e.g., AUD, CAD, CHF)` / `Mixed — different fields use different currencies, I'll edit by hand`

If the user picks a single currency, set `agami.unit: "<CURRENCY_CODE>"` (lowercase ISO 4217: `usd`, `eur`, `gbp`, etc.) on every detected money column. If "Mixed", skip — no auto-annotation, surface a one-liner ("OK, leaving currency fields unannotated. Edit by hand or save as a correction later.")

#### 1e.iii — record and continue

The unit annotation is part of `agami.unit` per the existing extension allowlist; no schema changes needed. Phase 4 (chart rendering) and Phase 3c (cell formatting) in agami-query-database already use `agami.unit` for currency / percent / duration formatting.

### 1f — suggest metrics (user-confirmed only — never auto-write)

agami-query-database treats `metrics[]` as canonical aggregations the user wants reused across queries (e.g., `total_revenue` is `SUM(orders.amount)` filtered to non-cancelled). Metrics drift fast across domains, so we don't auto-detect — but we do **suggest** plausible ones during introspection so the user can pick.

#### 1f.i — generate candidates

Per schema, propose **3–7 candidate metrics** based on:

- Numeric fields named like aggregates (`amount`, `revenue`, `cost`, `quantity`, `count`) — propose a SUM metric and (if the field has multiple decimals) an AVG metric.
- Tables that look fact-shaped (have FKs to dimension tables, time field, numeric measures) — propose `count_<table>` (`COUNT(*) FROM <table>`) as a baseline metric.
- Time fields — for tables with timestamps, propose a `daily_<count|sum>_<x>` metric grouped by day, especially if the table looks high-traffic (`> 100k` rows).
- ORGANIZATION.md hints — if it defines vocabulary like "MRR" or "active user", propose a metric matching that definition.
- The user-uploaded data-model document (Phase 1.5) — if it lists KPIs by name, propose those.

For each candidate, include:
- A snake_case `name` (e.g. `total_revenue`, `count_orders`, `avg_order_value`)
- The aggregation expression in ANSI_SQL referencing `<dataset>.<field>`
- A 1-line description
- 2-3 synonyms

#### 1f.ii — confirm with the user, batch-style

**AskUserQuestion** with the candidate list as multi-select:

> I'd suggest adding these metrics to your model — they're reusable aggregations the skill will use whenever you ask about them by name (or synonym). Pick which ones make sense for your domain.

Options: one option per candidate (pre-checked when the candidate is grounded in ORGANIZATION.md or the data-model doc; un-checked otherwise). Plus `None — skip metric suggestions for now (Recommended if you're not sure)`.

For each metric the user picks: write into the schema yaml's `metrics[]`. Validate before write (the per-schema yaml must still pass OSI). If a metric fails validation (e.g., references a non-existent column), drop it silently and surface a one-liner: "Skipped `<name>` — couldn't validate against your model."

If the user picks "None", write nothing. They can add metrics later via agami-save-correction (`new_metric` correction kind).

#### 1f.iii — what NOT to suggest

- Don't suggest metrics that depend on choice_field literals you didn't detect (e.g., don't propose `MRR = SUM(price) WHERE plan='subscription'` if you never saw `plan='subscription'` in the choice_field detection).
- Don't suggest more than 7 candidates — the AskUserQuestion gets cluttered. Pick the highest-confidence ones.
- Don't propose metrics that span multiple schemas in the multi-schema case unless `cross_schema_relationships` already wires the join. Cross-schema metrics belong in `index.yaml` (future) — for now, scope each metric to a single schema.

---

## Phase 2: Build the per-schema OSI model

Output is a directory: `~/.agami/<profile>/` containing `index.yaml` plus one `<schema>.yaml` per database schema. Each `<schema>.yaml` is a **standalone OSI v0.1.1 document** for that schema's datasets.

### Per-schema yaml shape

```yaml
version: "0.1.1"

semantic_model:
  - name: <profile>
    description: <plain-English summary of this schema's role>
    ai_context:
      instructions: <how the LLM should use this schema>
      synonyms: [...]

    custom_extensions:
      - vendor_name: COMMON
        data: '{"agami": {"profile": "<profile>", "db_type": "<db_type>", "schema": "<schema_name>"}}'

    datasets:
      - name: <table_name>                                # source table name verbatim
        source: <database>.<schema>.<table>               # ALWAYS three-part
        primary_key: [<col>, ...]
        unique_keys:
          - [<col>]
        description: <plain English or empty string>
        ai_context:
          synonyms: [...]
        fields:
          - name: <column_name>
            expression:
              dialects:
                - dialect: ANSI_SQL
                  expression: <column_name>
            dimension:
              is_time: <true if timestamp/date else false>
            description: <empty string is OK>
            custom_extensions:
              - vendor_name: COMMON
                data: '{"agami": {"type": "<simple_type>", "original_type": "<DB native type>"}}'
        custom_extensions:
          - vendor_name: COMMON
            data: '{"agami": {"performance_hints": {...}}}'   # only when row count > 100k

    relationships:
      - name: <from>_to_<to>
        from: <from_dataset_name>                         # bare name; both endpoints must be in this schema
        to: <to_dataset_name>
        from_columns: [<col>, ...]
        to_columns: [<col>, ...]
        custom_extensions:
          - vendor_name: COMMON
            data: '{"agami": {"fk_validation": {...}}}'

    metrics: []                                            # empty on first introspect
```

`agami.schema` at the model level **must equal** the schema's `name` in `index.yaml` — the validator's `--directory` mode rejects mismatches.

### `index.yaml` shape

```yaml
version: "0.1.1"
profile: <profile>
db_type: <db_type>
schemas:
  - name: <schema_name>
    file: <schema_name>.yaml
    table_count: <int>
    description: <one-line schema summary or empty>
cross_schema_relationships:                                # only relationships that span schemas
  - name: <from_schema>_<from_table>_to_<to_schema>_<to_table>
    from: <from_schema>.<from_dataset>                    # qualified
    to: <to_schema>.<to_dataset>                          # qualified
    from_columns: [<col>, ...]
    to_columns: [<col>, ...]
    description: <optional>
introspect_meta:
  introspected_at: <ISO>
  tier: <cli|duckdb|python>
  source_db_version: <version string>
```

Within-schema relationships go in the schema's yaml. Cross-schema relationships go **only** in `index.yaml.cross_schema_relationships` — never in any individual schema yaml.

### Hard rules when building

1. **Every field must have an `expression.dialects[]` with at least one entry.** Even for plain column references — write `expression: { dialects: [{ dialect: ANSI_SQL, expression: <column_name> }] }`. No exceptions.
2. **`agami.type` is mandatory** on every field. If the DB native type is exotic and you can't map it, default to `string` and put the original in `agami.original_type`.
3. **Relationships are top-level** under the model. Never nest them inside datasets. Each one needs a unique `name`. Within-schema: `<from>_to_<to>` (suffix with `_<col>` if multiple FK pairs share endpoints). Cross-schema: include the schema names: `<from_schema>_<from>_to_<to_schema>_<to>`.
4. **`from_columns` and `to_columns` MUST have the same length.** Composite keys are arrays.
5. **`source` must be three-part dotted notation.** `database.schema.table` — never bare table name. For sqlite use `file_basename.main.<table>`.
6. **Don't invent `custom_extensions` keys.** Only emit the keys documented in [`shared/agami-osi-extensions.md`](../../shared/agami-osi-extensions.md). Adding a new key requires updating that doc + the validator's allowlist + a test.
7. **Dataset name uniqueness across schemas.** The validator's `--directory` mode does NOT allow the same dataset name to appear in two different schema yamls. If you find a collision (rare — typically the same table name in `public` and `archive`), pick the most-current and skip the other; record the skip in the schema's `description` so the user can hand-edit if they want both.
8. **Reintrospect preserves hand-edits.** When `$ARGUMENTS == reintrospect` and an existing `~/.agami/<profile>/<schema>.yaml` exists:
   - Read the existing schema yaml first.
   - For each existing field: keep its `description`, `ai_context`, and any `agami.choice_field` / `agami.unit` extensions. Refresh only `agami.type` / `agami.original_type` from the DB.
   - For each existing dataset: keep its `description`, `ai_context`. Refresh `agami.performance_hints` from the DB.
   - For each existing relationship: keep it as-is if both endpoints still exist. Drop if the underlying FK is gone.
   - Keep all existing `metrics[]` entries — those are user-authored and we never lose them.
   - For `index.yaml.cross_schema_relationships`: same preservation rules.

---

## Phase 3: Validate, then write

This phase is the keystone. **No file is ever written to `~/.agami/<profile>/` without the directory-mode validator passing.**

### 3a — stage the directory

Stage the new layout at `/tmp/agami-staging-<profile>/` (a fresh directory), then run the validator. Never touch `~/.agami/<profile>/` until validation passes.

```bash
staging="/tmp/agami-staging-$profile"
rm -rf "$staging" && mkdir -p "$staging"
# Write index.yaml + every <schema>.yaml into $staging.
python3 "$AGAMI_PLUGIN_ROOT/scripts/validate_semantic_model.py" --directory "$staging"
```

### 3b — handle the result

- **Exit 0** (PASSED): atomically promote the staging directory.
  ```bash
  rm -rf "$HOME/.agami/$profile.tmp_old" 2>/dev/null
  if [ -d "$HOME/.agami/$profile" ]; then
    # Preserve ORGANIZATION.md and examples.yaml from the existing dir if reintrospect.
    cp -p "$HOME/.agami/$profile/ORGANIZATION.md" "$staging/" 2>/dev/null || true
    cp -p "$HOME/.agami/$profile/examples.yaml"   "$staging/" 2>/dev/null || true
    mv "$HOME/.agami/$profile" "$HOME/.agami/$profile.tmp_old"
  fi
  mv "$staging" "$HOME/.agami/$profile"
  chmod 700 "$HOME/.agami/$profile"
  chmod 600 "$HOME/.agami/$profile/"*.yaml "$HOME/.agami/$profile/ORGANIZATION.md" 2>/dev/null
  rm -rf "$HOME/.agami/$profile.tmp_old"
  ```
  Surface: `✓ Validator passed. Wrote ~/.agami/<profile>/ (<K> schemas, <N> datasets total, <M> fields, <R> relationships).`

- **Exit 1** (FAILED): surface the validator's error list verbatim. **Do NOT promote the staging directory.** Tell the user "I built a model but it failed OSI validation. Here's what's wrong: …" and offer to attempt a fix or stop. Re-validate after every edit until clean. The staging dir remains at `/tmp/agami-staging-<profile>/` for inspection.

- **Exit 2** (TOOLING ERROR — missing dependencies, missing schema): surface the error and ask the user to install `pyyaml` and `jsonschema`.

### 3c — never bypass

If the validator can't be run for any reason (missing Python, missing dependencies, missing schema file), **DO NOT PROMOTE THE STAGING DIRECTORY**. Tell the user the validator is unavailable and offer to install the dependencies. The files in `~/.agami/<profile>/` are the source of truth for every future query — a broken model breaks every query that follows.

---

## Phase 4: Seed prompt examples

Generate **10–15** NL→SQL examples covering this distribution. The bias is intentionally toward multi-table joins — that's where NL→SQL gets hard, and seed examples covering 3- and 4-table joins lift answer quality on real questions far more than another COUNT(*) example.

| # | Pattern | Min tables | Example shape |
|---|---------|---|---------------|
| 1 | Count rows | 1 | "How many orders are there?" |
| 2 | Filter + count | 1 | "How many orders are still pending?" |
| 3 | GROUP BY | 1 | "Orders by status" |
| 4 | Date range | 1 | "Orders placed last month" |
| 5 | Top N (single table) | 1 | "Top 5 statuses by order count" |
| 6 | JOIN (2 tables) | 2 | "Total spend per customer" |
| 7 | JOIN (3 tables) | 3 | "Top 10 products by revenue" |
| 8 | **JOIN (3 tables) + filter** | **3** | **"Top 10 customers by spend on shipped orders this quarter"** |
| 9 | **JOIN (4 tables)** | **4** | **"Revenue per category per region last 90 days"** (orders → order_items → products → categories) |
| 10 | **JOIN (4 tables) + GROUP BY two dimensions** | **4** | **"Order count by customer-segment and product-category last quarter"** |
| 11 | Boolean filter | 1 | "Active customers only" |
| 12 | Aggregate | 1 | "Average order size" |
| 13 | Combined (filter + JOIN + GROUP BY + ORDER BY) | 2-3 | "Top 5 active customers by spend last 30 days" |

**Hard rule: at least 3 examples must touch ≥ 3 tables, and at least 1 must touch ≥ 4 tables** — when the user's schema supports it (i.e., the relationships graph is deep enough). If the schema only has 2 tables connected by FKs, skip patterns 7-10 and document why in the staging log: "schema only has 2 connected tables; skipped multi-join examples".

Skip patterns that don't fit the user's schema (e.g., no time field → no "last month" example; no boolean column → no #11). The 3- and 4-table patterns require enough relationships in the graph to traverse — use the relationships from Phase 1c when picking which tables to join.

### 4a — generate

First, **load `~/.agami/USER_MEMORY.md`** (strip HTML comments) and **`~/.agami/<profile>/ORGANIZATION.md`** (same — strip HTML comments). USER_MEMORY holds cross-database preferences; ORGANIZATION.md holds domain context for *this* database. Both improve seed-example quality.

For each example:
- Build `(question, sql)` using the model from Phase 3.
- Reference fields by their **OSI dataset.field name** (which equals the DB column name in the simple introspect case).
- Use SQL safety rules from [`shared/sql-generation-rules.md`](../../shared/sql-generation-rules.md) and dialect-specific syntax from [`shared/dialect-rules.md`](../../shared/dialect-rules.md).
- **Apply USER_MEMORY policies.** If USER_MEMORY says "exclude test users where email matches @example.com", every seed example that touches `customers` includes that filter. If it says "default time window: last 30 days", date-relevant examples honor that default.
- **Apply ORGANIZATION.md domain vocabulary.** If ORGANIZATION.md defines "active user = signed in in the last 30 days", any "active user" example uses that definition.

### 4b — EXPLAIN-validate each

Before adding to the YAML, run `EXPLAIN <sql>` (or `EXPLAIN QUERY PLAN <sql>` for SQLite) via the chosen tool. If EXPLAIN fails:
1. Read the error through [`shared/db_error_classifier.md`](../../shared/db_error_classifier.md).
2. Make ONE auto-fix attempt (typically a column-name typo or missing alias).
3. If still failing, move that example to `~/.agami/.rejected/` (with the error) and continue. Don't block.

### 4c — write `~/.agami/<profile>/examples.yaml`

This file is **NOT OSI** — it's an agami-bespoke few-shot library. Format:

```yaml
# ~/.agami/<profile>/examples.yaml
# NL → SQL few-shot examples loaded by the agami-query-database skill.
# Corrections appended by /agami-save-correction.

examples:
  - question: How many orders are there?
    sql: SELECT COUNT(*) AS order_count FROM orders
    source: seed
    created_at: 2026-05-06T12:00:00Z

  - question: Orders by status
    sql: |-
      SELECT status, COUNT(*) AS count
      FROM orders
      GROUP BY status
      ORDER BY count DESC
    source: seed
    created_at: 2026-05-06T12:00:00Z
```

`source` is `seed` here, `correction` for entries added by `/agami-save-correction`. The query-database skill loads at most 50 most-recent.

Surface: `✓ Generated <N> examples (<R> rejected, see ~/.agami/.rejected/). Saved to ~/.agami/<profile>/examples.yaml.`

---

## Phase 5: Run a demo query

Pick one of the seed examples and run it end-to-end. The user gets to verify the skill works against their actual data before they start asking real questions. Show the result, ask Yes / No / Skip on the example. **Do NOT use the phrase "engagement moment" anywhere the user can see — it's internal phasing and looks marketing-speak in a chat.**

Pick **one** example from Phase 4 that:
1. Spans ≥ 2 datasets via a relationship (uses a JOIN).
2. Returns ≤ 20 rows so it displays cleanly.
3. Is unambiguously interesting (a "top N", a "by category" breakdown, a recency filter).

Tell the user what you picked and why. Show the generated SQL. Execute via the chosen tool. Render result as a markdown table.

Then **AskUserQuestion**:

> Does this result look right?
> - **Yes (Recommended)** — confirms the example, marks it `confirmed: true` in `~/.agami/<profile>/examples.yaml`
> - **No** — opens the correction flow: ask the user what's wrong, take their corrected SQL, route through the agami-save-correction skill (don't say "/agami-save-correction" to the user — phrase it as "let me know what's wrong and I'll save it as a correction")
> - **Skip** — moves on, doesn't change the example

Branch:
- **Yes** → set `confirmed: true` and `confirmed_at: <ISO>` on the example.
- **No** → invoke the agami-save-correction skill with the user's feedback (which may also update the OSI model — see agami-save-correction/SKILL.md). When telling the user about it, prefer "let me know what's wrong and I'll save it as a correction" over a slash form — natural language reads better mid-conversation. (`/agami-save-correction` does work if the user prefers it.)
- **Skip** → leave example as-is.

Surface: `✓ Demo run complete.`

---

## Phase 6: Telemetry opt-in, THEN follow-up suggestions (correct order matters)

This is the first moment the user sees the skill produce real value. Ask for analytics consent here — not at install time. **And — important — the telemetry consent has to fully resolve BEFORE the user sees any "what to ask next" follow-up suggestions.** If they see follow-ups and *then* the consent modal pops up, they lose context for the follow-ups. The flow:

1. **Phase 5 demo finishes.** User answered Yes / No / Skip on the demo example.
2. Surface a one-line closing for the demo: `✓ Demo run complete.`
3. **Phase 6 (this phase): ask telemetry consent NOW.** AskUserQuestion modal. End the turn here. Do not yet emit follow-up suggestions about what to query next — the user is in a "decide about telemetry" mode.
4. **Next turn:** the user answered consent. Process it (write `~/.agami/.config`, send install event if opted in). Then **Phase 7 below** surfaces follow-up suggestions ("Now that you're set up, here are five things you could ask…"). These are the *first* five suggestions the user sees post-setup.

If `~/.agami/.config` already has an `analytics_consent` field set (true or false), **skip Phase 6 entirely** (only ask once) and go straight to Phase 7's follow-up suggestions.

If `~/.agami/.config` already has an `analytics_consent` field set (true or false), **skip this phase entirely**. Only ask once.

Otherwise, use **AskUserQuestion** with this exact question and three options. The text matters — read it back to yourself before sending. Do not paraphrase the "what we send / never send" lists.

> **You just saw agami work end-to-end. Help us improve it by sending anonymous usage stats?**
>
> What we send:
> - Counts of installs, queries, errors (no content)
> - Database type (postgres/mysql/sqlite), OS, which host (Claude Code / Cowork)
> - Latency percentiles
> - A random install ID — not tied to you
>
> What we never send:
> - Your queries, your schema, your data
> - Your hostname, paths, credentials
> - Anything we couldn't read out loud at a conference
>
> You can change your mind any time.

Options:
- `Yes (Recommended)` — "Send anonymous usage stats. Helps us prioritize fixes."
- `No` — "Don't send anything. Skill works the same."
- `Read more` — "Open `docs/privacy.md` and the full payload allowlist."

If `Read more`: open [`docs/privacy.md`](../../../../docs/privacy.md) and [`shared/telemetry-payload.md`](../../shared/telemetry-payload.md), then re-prompt with just `Yes (Recommended)` / `No`.

### 6a — persist the choice into the existing `~/.agami/.config`

`agami-init/SKILL.md` already wrote a base `.config` with the chosen connection method (the internal `tier` field) and `host`. Update it in place — don't overwrite the existing fields.

```bash
install_id=""
if [ "$CONSENT" = "true" ]; then
  install_id=$(python3 -c 'import uuid; print(uuid.uuid4())' 2>/dev/null || uuidgen | tr '[:upper:]' '[:lower:]')
fi
ts=$(date -u +"%Y-%m-%dT%H:%M:%SZ")

# Merge new fields into the existing .config (preserve the connection method + host)
python3 - <<PY
import json, pathlib
p = pathlib.Path.home() / ".agami" / ".config"
cfg = json.loads(p.read_text()) if p.exists() else {"schema_version": 1}
cfg["analytics_consent"] = $CONSENT_BOOL
cfg["install_id"] = "$install_id" if "$install_id" else None
cfg["consent_ts"] = "$ts"
p.write_text(json.dumps(cfg, indent=2))
PY
chmod 600 ~/.agami/.config
```

(Substitute `$CONSENT_BOOL` with literal `true` or `false` based on the user's choice.)

### 6b — send the install event (only if consent is true)

```bash
if [ "$CONSENT" = "true" ]; then
  curl -sS -m 5 -X POST https://analytics.agami.ai/v1/events \
    -H "Content-Type: application/json" \
    -d "$(cat <<JSON
{
  "schema_version": 1,
  "events": [{
    "event_type": "install",
    "install_id": "$install_id",
    "db_type": "$db_type",
    "os": "$(uname -s | tr '[:upper:]' '[:lower:]')",
    "host": "$host",
    "tier": "$tier",
    "client_version": "1.0.0",
    "timestamp": "$ts"
  }]
}
JSON
)" || true
fi
```

Build the payload **only** from the allowlist in [`shared/telemetry-payload.md`](../../shared/telemetry-payload.md). If you find yourself reaching for any other field, stop — there's nothing else to send.

Failure-tolerant: `|| true` so a network blip doesn't break the connect flow.

### 6c — queue the connect event (only if consent is true)

After the install event sends, append a `connect` event to `~/.agami/.telemetry-queue.jsonl` using only the allowlisted fields. Don't flush yet — that happens daily from `query-database`.

---

## Phase 7: Post-setup follow-up suggestions (only after telemetry decision is recorded)

Show **five** numbered suggestions for things the user can ask now, drawn from the schema we just introspected. This phase fires only after Phase 6's consent has been answered (or skipped because `analytics_consent` was already set). Format follows the same shape as `query-database`'s Phase 4f — five numbered bullets, plain markdown, no AskUserQuestion modal.

Pick suggestions that show off the schema's distinctive shape. If the model has tables like `orders` and `customers`, suggest things grounded in those. If it's a content/CRM schema, pick something domain-relevant. Keep each under 80 characters.

Format exactly:

```
✓ ~/.agami/<profile>/ — OSI v0.1.1 semantic model (<K> schemas, validated)
✓ ~/.agami/<profile>/examples.yaml — <N> NL→SQL examples
✓ Demo query verified
✓ Telemetry: <enabled | disabled — your call>

Now that you're set up, here are five things you could ask:

1. <a count question grounded in a real table — "How many orders shipped last month?">
2. <a top-N grouped question — "Top 10 customers by total spend">
3. <a time-series — "Revenue trend over the last 6 months">
4. <a comparison or breakdown — "Order count by status this quarter">
5. <a broader narrative — "How is the business doing this quarter?">

Reply with a number, or ask anything else.
```

Then end the turn. The user picking a number routes the chosen question into `query-database` for a real answer.

---

## Error handling

| Symptom | Action |
|---|---|
| Credentials chmod wrong | Refuse, offer to `chmod 600` |
| Cached connection tool no longer works | Re-detect, update `~/.agami/.config` |
| Introspection SQL fails | Route through `db_error_classifier.md`, surface the one-line remediation |
| **Validator fails** | **Refuse to promote `/tmp/agami-staging-<profile>/` to `~/.agami/<profile>/`. Show errors verbatim. Loop on edits + re-validate.** |
| EXPLAIN fails for a seed example | Auto-fix once → if still bad, move to `~/.agami/.rejected/`. Don't block the connect flow. |
| Reintrospect would lose hand-edits | Phase 2 hard rule #8 — preserve descriptions, ai_context, choice_fields, metrics. |
| Legacy single-file install detected | Auto-migrate: backup to `~/.agami/<profile>/_legacy.yaml.bak`, re-introspect into the new directory layout. |
