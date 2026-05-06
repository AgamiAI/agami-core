---
name: connect
description: "Introspects the user's database and emits a strict Open Semantic Interchange (OSI) v0.1.1 semantic model at $HOME/.agami/<profile>.yaml. Generates seed NL-to-SQL few-shot examples (each EXPLAIN-validated against the live DB) at $HOME/.agami/<profile>-examples.yaml, then runs an engagement-moment demo query so the user immediately sees the skill working. Every model write is gated by the OSI + Agami validator — no breaking model is ever persisted."
when_to_use: "Auto-invoked by query-database the first time it runs (when the semantic model YAML is missing). Invoke explicitly when the user says 'connect to my database', 'introspect the schema', 'reload schema', 'add a new database', or after the user changes their schema and wants the model refreshed. Requires init to have run first (credentials must exist)."
argument-hint: "[reintrospect | profile <name>]"
---

# agami connect

You are setting up the agami semantic model for the user's database. Goal: by the end, there is a **strict OSI v0.1.1 model** at `~/.agami/<profile>.yaml`, a seeded examples library at `~/.agami/<profile>-examples.yaml`, and the user has seen one demo query execute end-to-end.

This skill orchestrates four phases:

1. **Introspect** — pull tables / columns / PK / FK from `information_schema` via the chosen execution tier.
2. **Build the OSI model** — assemble the YAML strictly to the OSI v0.1.1 spec, with Agami metadata (column types, choice fields, performance hints) packed under `custom_extensions[].vendor_name: COMMON` per [`shared/agami-osi-extensions.md`](../../shared/agami-osi-extensions.md).
3. **Validate, then write** — run the validator at `plugins/agami/scripts/validate_semantic_model.py`. If it fails, **DO NOT WRITE THE FILE.** Surface the errors and stop.
4. **Seed examples + run demo query** — generate few-shot pairs, EXPLAIN-validate each, then pick one for the engagement-moment Yes/No/Skip prompt.

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

1. Verify `~/.agami/credentials` exists (or `AGAMI_DATABASE_URL` is set). If neither: invoke the `init` skill first.
2. Apply the credentials chmod check from [`init/SKILL.md`](../init/SKILL.md#permissions-enforcement). Refuse to proceed if too permissive.
3. Resolve `<profile>` (default: `default`, override with `AGAMI_PROFILE`). The OSI `semantic_model[].name` MUST equal `<profile>`.
4. Resolve `db_type` from credentials (`postgres` | `mysql` | `sqlite`).
5. Look up the cached execution tier from `~/.agami/.config`. If absent, run tier detection per [`init/SKILL.md`](../init/SKILL.md#phase-3-tier-detection).
6. If `$ARGUMENTS` is `reintrospect`: skip Phase 1's "already-have-a-model?" check and re-introspect from scratch. **Hand-edits the user made (descriptions, ai_context, choice_fields, metrics) MUST be preserved** — re-introspection only updates what the DB unambiguously tells us (table list, columns, types, PK, FK).

---

## Phase 1: Introspect

If `~/.agami/<profile>.yaml` exists and `$ARGUMENTS` is not `reintrospect`:
- "I already have a model for `<profile>` at `~/.agami/<profile>.yaml`. What would you like to do?"
- AskUserQuestion: `Re-introspect from DB` / `Verify and continue (Recommended)` / `Skip to seeding examples`.

Otherwise, run introspection. For every step, use the SQL from [`shared/introspect-queries.md`](../../shared/introspect-queries.md), executed via the chosen tier:

### 1a — list tables

Filter system schemas per [`shared/connection-reference.md → System Schema Exclusions`](../../shared/connection-reference.md#system-schema-exclusions). Surface: `Found <N> tables across <K> schema(s).`

### 1b — for each table, pull columns + PK + FK + row count

Use the per-dialect queries from [`shared/introspect-queries.md`](../../shared/introspect-queries.md).

For each column: capture `name`, `data_type` (raw DB type), nullability. Map to the simple OSI-extension type set (`string | integer | decimal | timestamp | date | boolean`) using the type mapping table at the bottom of `introspect-queries.md`. Keep the raw DB type as `agami.original_type`.

For each table: capture row count from `pg_stat_user_tables` (Postgres) or `information_schema.tables.table_rows` (MySQL). Tables with > 100k rows get a `agami.performance_hints` extension; tables ≤ 100k don't need one.

### 1c — FK validation (live join check)

Run the orphan-ratio query from [`shared/fk-validation.md`](../../shared/fk-validation.md) against every detected FK. Drop any with > 5% orphans. For each FK that survives, record the result as a `agami.fk_validation` extension on the resulting `relationships[]` entry.

If the database had **zero declared FKs**, run heuristic FK inference per `fk-validation.md` and ask:

> I detected N likely foreign-key relationships from column-name conventions:
> - `orders.customer_id` → `customers.id` (1 orphan in 2403 rows)
> - …
>
> Add these to the model?

AskUserQuestion: `Add all (Recommended)` / `Add only zero-orphan ones` / `Skip — let me edit by hand later`.

### 1d — light enrichment (table descriptions)

For each table, generate a one-line plain-English `description`. Don't make stuff up — leave empty if unsure. Examples:
- `orders` (with customer_id, status, placed_at, shipped_at) → "Customer orders with placement and shipment dates."
- `_audit_log` → leave empty.

This is a best-effort pass; the user can hand-edit `~/.agami/<profile>.yaml` and the changes will survive future re-introspections (Phase 0.6).

---

## Phase 2: Build the OSI model

Assemble the YAML structure **strictly** per [`shared/schema-reference.md`](../../shared/schema-reference.md). Every model you emit must match this exact shape:

```yaml
version: "0.1.1"

semantic_model:
  - name: <profile>
    description: <plain-English summary>
    ai_context:
      instructions: <how the LLM should use this model>
      synonyms: [...]

    custom_extensions:
      - vendor_name: COMMON
        data: '{"agami": {"profile": "<profile>", "db_type": "<db_type>", "introspect_meta": {"introspected_at": "<ISO>", "tier": "<cli|duckdb|python>", "source_db_version": "<version string>"}}}'

    datasets:
      - name: <table_name>                                # use the source table name verbatim
        source: <database>.<schema>.<table>               # ALWAYS three-part. For sqlite use file_basename.main.<table>.
        primary_key: [<col>, ...]                         # array (composite-friendly); omit if no PK
        unique_keys:                                       # optional, list of arrays
          - [<col>]
        description: <plain English or empty string>
        ai_context:
          synonyms: [...]
        fields:
          - name: <column_name>
            expression:
              dialects:
                - dialect: ANSI_SQL
                  expression: <column_name>               # for plain references; computed expressions allowed
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
      - name: <from>_to_<to>                              # snake_case auto-generated, must be unique
        from: <from_dataset_name>
        to: <to_dataset_name>
        from_columns: [<col>, ...]
        to_columns: [<col>, ...]
        custom_extensions:
          - vendor_name: COMMON
            data: '{"agami": {"fk_validation": {"validated_at": "<ISO>", "orphan_count": 0, "total_rows": <n>, "orphan_ratio": 0.0}}}'

    metrics: []                                            # empty on first introspect; user adds these via /save-correction
```

### Hard rules when building

1. **Every field must have an `expression.dialects[]` with at least one entry.** Even for plain column references — write `expression: { dialects: [{ dialect: ANSI_SQL, expression: <column_name> }] }`. No exceptions.
2. **`agami.type` is mandatory** on every field. If the DB native type is exotic and you can't map it, default to `string` and put the original in `agami.original_type`.
3. **Relationships are top-level** under the model. Never nest them inside datasets. Each one needs a unique `name` (use `<from>_to_<to>`; suffix with `_<col>` if multiple FK pairs share `from`+`to`).
4. **`from_columns` and `to_columns` MUST have the same length.** Composite keys are arrays.
5. **`source` must be three-part dotted notation.** `database.schema.table` — never bare table name.
6. **Don't invent `custom_extensions` keys.** Only emit the keys documented in [`shared/agami-osi-extensions.md`](../../shared/agami-osi-extensions.md). Adding a new key requires updating that doc + the validator's allowlist + a test.
7. **Reintrospect preserves hand-edits.** When `$ARGUMENTS == reintrospect` and an existing model file is at `~/.agami/<profile>.yaml`:
   - Read the existing model first.
   - For each existing field: keep its `description`, `ai_context`, and any `agami.choice_field` / `agami.unit` extensions. Refresh only `agami.type` / `agami.original_type` from the DB.
   - For each existing dataset: keep its `description`, `ai_context`. Refresh `agami.performance_hints` from the DB.
   - For each existing relationship: keep it as-is if both endpoints still exist. Drop if the underlying FK is gone.
   - Keep all existing `metrics[]` entries — those are user-authored and we never lose them.

---

## Phase 3: Validate, then write

This phase is the keystone. **No model file is ever written without passing the validator.**

### 3a — run the validator

```bash
python3 plugins/agami/scripts/validate_semantic_model.py /tmp/agami-staging-<profile>.yaml
```

(Stage the YAML at `/tmp/agami-staging-<profile>.yaml` first — never write to `~/.agami/<profile>.yaml` until validation passes.)

### 3b — handle the result

- **Exit 0** (PASSED): rename the staging file to `~/.agami/<profile>.yaml`, `chmod 600`, surface `✓ Validator passed. Wrote ~/.agami/<profile>.yaml (<N> datasets, <M> fields, <K> relationships).`
- **Exit 1** (FAILED): surface the validator's error list verbatim. **Do NOT write the model.** Tell the user "I built a model but it failed OSI validation. Here's what's wrong: …" and offer to attempt a fix or stop. Re-validate after every edit until clean. The staging file remains at `/tmp/agami-staging-<profile>.yaml` for inspection.
- **Exit 2** (TOOLING ERROR — missing dependencies, missing schema): surface the error and ask the user to install `pyyaml` and `jsonschema`.

### 3c — never bypass

If the validator can't be run for any reason (missing Python, missing dependencies, missing schema file), **DO NOT WRITE THE MODEL**. Tell the user the validator is unavailable and offer to install the dependencies. The model file at `~/.agami/<profile>.yaml` is the source of truth for every future query — a broken model breaks every query that follows.

---

## Phase 4: Seed prompt examples

Generate **8–15** NL→SQL examples covering this distribution:

| # | Pattern | Example shape |
|---|---------|---------------|
| 1 | Count rows | "How many orders are there?" |
| 2 | Filter + count | "How many orders are still pending?" |
| 3 | GROUP BY | "Orders by status" |
| 4 | Date range | "Orders placed last month" |
| 5 | Top N | "Top 5 customers by order count" |
| 6 | JOIN (2 tables) | "Total spend per customer" |
| 7 | JOIN (3 tables) | "Top 10 products by revenue" |
| 8 | Boolean filter | "Active customers only" |
| 9 | Combined | "Top 5 active customers by spend last 30 days" |
| 10 | Aggregate | "Average order size" |

Skip patterns that don't fit the user's schema (e.g., no time field → no "last month" example).

### 4a — generate

For each example:
- Build `(question, sql)` using the model from Phase 3.
- Reference fields by their **OSI dataset.field name** (which equals the DB column name in the simple introspect case).
- Use SQL safety rules from [`shared/sql-generation-rules.md`](../../shared/sql-generation-rules.md) and dialect-specific syntax from [`shared/dialect-rules.md`](../../shared/dialect-rules.md).

### 4b — EXPLAIN-validate each

Before adding to the YAML, run `EXPLAIN <sql>` (or `EXPLAIN QUERY PLAN <sql>` for SQLite) via the chosen tier. If EXPLAIN fails:
1. Read the error through [`shared/db_error_classifier.md`](../../shared/db_error_classifier.md).
2. Make ONE auto-fix attempt (typically a column-name typo or missing alias).
3. If still failing, move that example to `~/.agami/.rejected/` (with the error) and continue. Don't block.

### 4c — write `~/.agami/<profile>-examples.yaml`

This file is **NOT OSI** — it's an agami-bespoke few-shot library. Format:

```yaml
# ~/.agami/<profile>-examples.yaml
# NL → SQL few-shot examples loaded by the query-database skill.
# Corrections appended by /save-correction.

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

`source` is `seed` here, `correction` for entries added by `/save-correction`. The query-database skill loads at most 50 most-recent.

Surface: `✓ Generated <N> examples (<R> rejected, see ~/.agami/.rejected/). Saved to ~/.agami/<profile>-examples.yaml.`

---

## Phase 5: Demo query — engagement moment

Pick **one** example from Phase 4 that:
1. Spans ≥ 2 datasets via a relationship (uses a JOIN).
2. Returns ≤ 20 rows so it displays cleanly.
3. Is unambiguously interesting (a "top N", a "by category" breakdown, a recency filter).

Tell the user what you picked and why. Show the generated SQL. Execute via the chosen tier. Render result as a markdown table.

Then **AskUserQuestion**:

> Does this result look right?
> - **Yes (Recommended)** — confirms the example, marks it `confirmed: true` in `~/.agami/<profile>-examples.yaml`
> - **No** — opens the correction flow: ask the user what's wrong, take their corrected SQL, route through `/save-correction`
> - **Skip** — moves on, doesn't change the example

Branch:
- **Yes** → set `confirmed: true` and `confirmed_at: <ISO>` on the example.
- **No** → invoke `/save-correction` with the user's feedback (which may also update the OSI model — see save-correction/SKILL.md).
- **Skip** → leave example as-is.

Surface: `✓ Demo run complete. You're set up — ask me a question about your data.`

---

## Phase 6: Telemetry (if opted in)

If `~/.agami/.config` has `analytics_consent: true`, append a `connect` event to `~/.agami/.telemetry-queue.jsonl` using ONLY the allowlisted fields per [`shared/telemetry-payload.md`](../../shared/telemetry-payload.md). Don't flush yet — that happens daily from `query-database`.

---

## Closing message

```
✓ ~/.agami/<profile>.yaml — OSI v0.1.1 semantic model (validated)
✓ ~/.agami/<profile>-examples.yaml — <N> NL→SQL examples
✓ Demo query verified

You're ready. Ask me anything — e.g. "show top 10 active customers by spend".
```

---

## Error handling

| Symptom | Action |
|---|---|
| Credentials chmod wrong | Refuse, offer to `chmod 600` |
| Cached tier no longer works | Re-detect, update `~/.agami/.config` |
| Introspection SQL fails | Route through `db_error_classifier.md`, surface the one-line remediation |
| **Validator fails** | **Refuse to write `~/.agami/<profile>.yaml`. Show errors verbatim. Stage at `/tmp/agami-staging-<profile>.yaml`. Loop on edits + re-validate.** |
| EXPLAIN fails for a seed example | Auto-fix once → if still bad, move to `~/.agami/.rejected/`. Don't block the connect flow. |
| Reintrospect would lose hand-edits | Phase 2 hard rule #7 — preserve descriptions, ai_context, choice_fields, metrics. |
