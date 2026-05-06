---
name: connect
description: "Introspects the user's database, writes a semantic model YAML at $HOME/.agami/<dbname>.yaml (tables, columns, FK relationships, entity hints), generates seed NL-to-SQL few-shot examples at $HOME/.agami/<dbname>-examples.yaml (each validated via EXPLAIN), and runs an engagement-moment demo query so the user immediately sees the skill working. Re-introspects on demand when the schema drifts."
when_to_use: "Auto-invoked by query-database the first time it runs (when the semantic model YAML is missing). Invoke explicitly when the user says 'connect to my database', 'introspect the schema', 'reload schema', 'add a new database', or after the user changes their schema and wants the model refreshed. Requires init to have run first (credentials must exist)."
argument-hint: "[reintrospect | profile <name>]"
---

# agami connect

You are setting up the agami semantic model for the user's database. Goal: by the end of this skill, the user has a working `~/.agami/<dbname>.yaml` and `~/.agami/<dbname>-examples.yaml`, and they've seen one demo query execute successfully end-to-end.

This skill orchestrates three things:

1. **Introspect** the database schema — tables, columns, primary/foreign keys, basic stats. Writes `~/.agami/<dbname>.yaml` per [`shared/schema-reference.md`](../../shared/schema-reference.md).
2. **Seed prompt examples** — generate 8–15 NL→SQL few-shot pairs covering common query patterns. Each is validated via EXPLAIN before write. Writes `~/.agami/<dbname>-examples.yaml`.
3. **Demo query** — pick an FK-spanning question, execute it, ask the user "does this look right? Yes / No / Skip". This is the engagement moment — the user feels the value within their first minute.

For schema YAML format: [`shared/schema-reference.md`](../../shared/schema-reference.md).
For introspection SQL: [`shared/introspect-queries.md`](../../shared/introspect-queries.md).
For FK validation: [`shared/fk-validation.md`](../../shared/fk-validation.md).
For SQL dialect rules: [`shared/dialect-rules.md`](../../shared/dialect-rules.md).
For SQL safety: [`shared/sql-generation-rules.md`](../../shared/sql-generation-rules.md).
For DB error classification: [`shared/db_error_classifier.md`](../../shared/db_error_classifier.md).

## Conversation style

- **Combine acknowledge + next question** — don't waste turns on "Got it!"
- **Use AskUserQuestion for every Yes/No/Skip** — never inline-bullet options. Mark exactly one option `(Recommended)` first.
- **Keep the user oriented** — print one-line progress markers between phases (`✓ Introspected 12 tables`, `✓ Generated 10 examples`, etc.).

---

## Phase 0: Preflight

1. Verify `~/.agami/credentials` exists (or `AGAMI_DATABASE_URL` is set). If neither: invoke the `init` skill first.
2. Read the credentials chmod-check from [`init/SKILL.md`](../init/SKILL.md#permissions-enforcement). Refuse to proceed if too permissive.
3. Resolve the database type and `dbname` from the credentials (`type`, `database` fields, or DSN). The default profile is `[default]`; honor `AGAMI_PROFILE` if set.
4. Look up the chosen execution tier from `~/.agami/.config` (set by `init`). If absent, re-run tier detection per [`init/SKILL.md`](../init/SKILL.md#phase-3-tier-detection).
5. If `$ARGUMENTS` is `reintrospect`: skip Phase 1's "already-have-a-model?" check and re-introspect from scratch (preserving existing `description` strings as a courtesy if the user has hand-edited them).

---

## Phase 1: Introspect

If `~/.agami/<dbname>.yaml` already exists and `$ARGUMENTS` is not `reintrospect`:
- Briefly tell the user "I already have a model for `<dbname>` at `~/.agami/<dbname>.yaml`. Want to re-introspect, just verify it works, or move on to seeding examples?"
- AskUserQuestion: `Re-introspect` / `Verify and continue (Recommended)` / `Skip to examples`.

Otherwise, run introspection.

### Step 1a — list tables

Run the SQL from [`shared/introspect-queries.md`](../../shared/introspect-queries.md) (Postgres or MySQL section, matching the DB type) via the chosen tier. Filter system schemas per [`shared/connection-reference.md → System Schema Exclusions`](../../shared/connection-reference.md#system-schema-exclusions).

Surface a one-liner: `Found 12 tables across 1 schema.`

### Step 1b — for each table, pull columns + PK + FK

Use the per-dialect queries from [`shared/introspect-queries.md`](../../shared/introspect-queries.md). Map source types to the simple set in `schema-reference.md` ("Type mapping" section). Fill out:

```yaml
tables:
  - table_name: orders
    schema_name: public
    label: orders
    display_name: Orders
    description: ""
    columns:
      id:
        type: integer
        description: ""
        primary_key: true
      customer_id:
        type: integer
        description: ""
        foreign_key:
          table: public.customers
          column: id
      ...
    relationships:
      - from_column: customer_id
        to_table: public.customers
        to_column: id
        join_type: LEFT JOIN
        description: ""
    entities: []
    measures: {}
```

Leave `description` empty for now — you'll fill these in Step 1d.

### Step 1c — FK validation (live join check)

Run the orphan-ratio check from [`shared/fk-validation.md`](../../shared/fk-validation.md) on every detected FK. Drop any FK with > 5% orphans (likely a stale reference or denormalization, not a real relationship). Surface a one-liner: `12 foreign keys (1 dropped after orphan check).`

If the database had **zero declared FKs** (common with auto-generated schemas), run heuristic FK inference per [`shared/fk-validation.md`](../../shared/fk-validation.md) and ask the user before writing inferred FKs to the model:

> I detected 4 likely foreign-key relationships from column-name conventions:
> - `orders.customer_id` → `customers.id` (1 orphan in 2403 rows)
> - `order_items.order_id` → `orders.id` (0 orphans)
> - …
>
> Add these to the model?

AskUserQuestion: `Add all (Recommended)` / `Add only zero-orphan ones` / `Skip — let me edit by hand later`.

### Step 1d — light enrichment (table descriptions)

For each table, generate a one-line plain-English `description` based on the table name and its column list. Don't make stuff up — if you're unsure, leave it as "". Examples:

- `orders` (with customer_id, status, placed_at, shipped_at) → "Customer orders with placement and shipment dates."
- `users` (with email, name, created_at) → "Application users."
- `_audit_log` (with table_name, op, ts) → "" (don't guess at internal tables).

This is a best-effort pass — the user can edit `~/.agami/<dbname>.yaml` directly and the next query will pick up the changes.

### Step 1e — validate

Run the validation rules from [`shared/schema-reference.md → Validation Rules`](../../shared/schema-reference.md#validation-rules):

1. Every FK target table exists in the model.
2. Every FK column exists in the target table.
3. No two tables share a `label`.
4. Every table has at least one column.

If any rule fails: surface the specific failure ("orders.customer_id references public.customers but no such table is in the model") and stop. Do not write a broken model.

### Step 1f — write `~/.agami/<dbname>.yaml`

Use the Write tool. Surface: `✓ Wrote ~/.agami/<dbname>.yaml (12 tables, 47 columns, 11 relationships).`

---

## Phase 2: Seed prompt examples

Generate **8–15** NL→SQL examples covering this distribution of query patterns. Aim for at least one example per pattern that maps to the user's schema.

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
| 9 | Combined (filter + group + sort + limit) | "Top 5 active customers by spend last 30 days" |
| 10 | Aggregate measure | "Average order size" |

Skip patterns that don't fit the user's schema (e.g., no date column → no "last month" example).

### Step 2a — generate

For each example, build the `(question, sql)` pair using the model from Phase 1. Use the SQL safety rules from [`shared/sql-generation-rules.md`](../../shared/sql-generation-rules.md) and the dialect-specific syntax from [`shared/dialect-rules.md`](../../shared/dialect-rules.md).

### Step 2b — validate via EXPLAIN

For every generated SQL, run `EXPLAIN <sql>` against the live DB through the chosen tier:

```sql
-- Postgres
EXPLAIN <sql>;

-- MySQL
EXPLAIN <sql>;

-- SQLite
EXPLAIN QUERY PLAN <sql>;
```

If EXPLAIN fails:
1. Read the error through [`shared/db_error_classifier.md`](../../shared/db_error_classifier.md).
2. Make **one** auto-fix attempt (typically a column-name typo or missing alias).
3. If still failing, move that example to `~/.agami/.rejected/` (with the error) and continue. Don't block.

This is the EXPLAIN-validate-then-write contract — only well-formed examples land in the YAML.

### Step 2c — write `~/.agami/<dbname>-examples.yaml`

Format:

```yaml
# ~/.agami/<dbname>-examples.yaml
# Few-shot examples for NL→SQL. Loaded by the query-database skill.
# New corrections are appended here by /save-correction.
#
# Format: list of {question, sql} pairs.

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
  ...
```

`source` is `seed` for examples generated here, `correction` for ones added by `/save-correction`. `created_at` is ISO8601 UTC. The query-database skill caps loaded examples at 50 most-recent (newest first); seeds count toward that cap.

Surface: `✓ Generated 10 examples (1 rejected, see ~/.agami/.rejected/). Saved to ~/.agami/<dbname>-examples.yaml.`

---

## Phase 3: Demo query — the engagement moment

Pick **one** question from Phase 2 that:
1. Spans at least 2 tables (uses a JOIN).
2. Returns a small result (≤ 20 rows) so it displays cleanly in chat.
3. Is unambiguously interesting (a "top N", a "by category" breakdown, a recency filter).

Tell the user what you picked and why:

> Here's a demo question to test that everything's wired up:
>
> **"Top 5 customers by total spend"**
>
> Generated SQL:
> ```sql
> SELECT c.name, SUM(i.quantity * i.unit_price) AS total_spend
> FROM customers c
> JOIN orders o ON o.customer_id = c.id
> JOIN order_items i ON i.order_id = o.id
> GROUP BY c.id, c.name
> ORDER BY total_spend DESC
> LIMIT 5
> ```

Execute the SQL via the chosen tier. Render the result as a markdown table.

Then **ask via AskUserQuestion**:

> Does this result look right?
> - **Yes (Recommended)** — confirms the example, marks it as good in `~/.agami/<dbname>-examples.yaml`
> - **No** — opens correction flow: ask the user what's wrong, take their corrected SQL, append via `/save-correction`
> - **Skip** — moves on, doesn't change the example

Handle each branch:

- **Yes** → update the example in `~/.agami/<dbname>-examples.yaml`: add `confirmed: true` and `confirmed_at: <ISO ts>`.
- **No** → tell the user "Tell me what's off, or paste the correct SQL." Capture their reply. If they paste SQL, validate via EXPLAIN, replace the example's `sql` field, set `source: correction`. If they describe what's wrong in NL, regenerate SQL based on their feedback, EXPLAIN-validate, replace.
- **Skip** → leave the example as-is.

Either way, surface: `✓ Demo run complete. You're set up — ask me a question about your data.`

---

## Phase 4: Telemetry (if opted in)

If `~/.agami/.config` has `analytics_consent: true`, append a `connect` event to `~/.agami/.telemetry-queue.jsonl`. Build the payload using ONLY the allowlisted fields per [`shared/telemetry-payload.md`](../../shared/telemetry-payload.md):

```json
{"event_type": "connect", "install_id": "...", "db_type": "postgres", "os": "darwin", "host": "claude-code-cli", "tier": "cli", "client_version": "1.0.0", "timestamp": "..."}
```

Don't flush yet — that happens daily from `query-database`.

---

## Closing message

```
✓ ~/.agami/<dbname>.yaml — semantic model
✓ ~/.agami/<dbname>-examples.yaml — 10 NL→SQL examples
✓ Demo query verified

You're ready. Ask me anything — e.g. "show top 10 active customers by spend".
```

If the user wants to edit the model by hand, point them at `~/.agami/<dbname>.yaml` directly. The next query picks up changes automatically.

---

## Error handling

- All credential reads → chmod check from `init/SKILL.md`. Refuse on world-readable.
- All SQL execution → route exceptions through [`shared/db_error_classifier.md`](../../shared/db_error_classifier.md). Surface one-line remediations.
- EXPLAIN failures during seeding → one auto-fix retry, then move to `~/.agami/.rejected/`. Don't block.
- Validation failures → specific error + don't write the file. The user can fix the cause and re-run.
