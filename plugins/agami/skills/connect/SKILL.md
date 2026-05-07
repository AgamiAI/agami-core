---
name: connect
description: "Introspects the user's database and emits a strict Open Semantic Interchange (OSI) v0.1.1 semantic model at the per-profile YAML file inside the .agami home directory. Generates seed NL-to-SQL few-shot examples (each EXPLAIN-validated against the live DB) at the per-profile examples file, then runs one demo query so the user immediately sees the skill working. Every model write is gated by the OSI + Agami validator — no breaking model is ever persisted."
when_to_use: "Auto-invoked by query-database the first time it runs (when the semantic model YAML is missing). Invoke explicitly when the user says 'connect to my database', 'introspect the schema', 'reload schema', 'add a new database', or after the user changes their schema and wants the model refreshed. Requires init to have run first (credentials must exist)."
argument-hint: "[reintrospect | profile NAME]"
---

# agami connect

**Before suggesting any slash command in chat, read [`shared/invocation-conventions.md`](../../shared/invocation-conventions.md).** The only working slash command for agami is `/init` (bare). Never tell the user to type `/agami:connect`, `/connect`, `/agami connect reintrospect`, or any other slash form — those don't exist. Phrase guidance as natural language ("say 'reload the schema'") and the connect skill's `when_to_use` will catch it.

You are setting up the agami semantic model for the user's database. Goal: by the end, there is a **strict OSI v0.1.1 model** at `~/.agami/<profile>.yaml`, a seeded examples library at `~/.agami/<profile>-examples.yaml`, and the user has seen one demo query execute end-to-end.

This skill orchestrates four phases:

1. **Introspect** — pull tables / columns / PK / FK from `information_schema` via the chosen execution tier.
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
4. **If credentials are missing, STOP this skill and invoke `init`.** Do not run introspection. Do not start tier detection. Do not write a temporary credentials file from values the user types. Tell the user in one short sentence "Your credentials file is missing — I'll re-run setup so you can enter them in the file" and hand off to `init`.
5. **NEVER put the password (or any credential field) in a Bash command line.** That includes `export PGPASSWORD='<value>'`, `export MYSQL_PWD='<value>'`, `psql -W <password>`, `mysql -p<password>`, or any heredoc form that interpolates the password into stdin. Hosts render Bash tool calls in chat — anything in the command leaks. Use the auth files generated by `scripts/setup_pgauth.py`: `PGPASSFILE=$HOME/.agami/.pgpass psql -h ... -U ... -d ... -c "$SQL" --csv` (psql) or `mysql --defaults-file=$HOME/.agami/.mysql.cnf --defaults-group-suffix=_<profile> ...` (mysql). For tier 3 use `python3 scripts/execute_sql.py`. See [`shared/connection-reference.md → HARD RULES`](../../shared/connection-reference.md).

If you find yourself reaching for any command that doesn't fit the rules above, stop and re-read this section.

### Preflight steps

1. **Credentials check (binding)**: read `~/.agami/credentials` if present, OR check `AGAMI_DATABASE_URL` env var. If neither exists, invoke `init` and **stop this skill**. Do not continue. Do not probe anything.
2. Apply the credentials chmod check from the agami-init skill's permissions-enforcement section. Refuse to proceed if too permissive.
3. Resolve `<profile>` in this order: `AGAMI_PROFILE` env var → `active_profile` field in `~/.agami/.config` → literal string `"default"` (legacy fallback). The OSI `semantic_model[].name` MUST equal the resolved `<profile>`.
4. Resolve the connection fields from the credentials file's `[<profile>]` section (or parse from `AGAMI_DATABASE_URL`):
   - **postgres / redshift / mysql:** `db_type`, `host`, `port`, `database`, `user`, `password` (plus optional `sslmode`).
   - **snowflake:** `db_type`, `account`, `user`, `password` (or `authenticator`), plus optional `warehouse`, `database`, `schema`, `role`. **No `host`/`port` for Snowflake** — its connector uses the account identifier directly.
   - **sqlite:** `db_type`, `path`.

   Never substitute a value that's missing — surface a clear "your credentials file is missing field X for profile Y; please add it" message and stop.
5. Look up the cached execution tier and tool paths from `~/.agami/.config`. If absent, run tier detection per the init skill's Phase 3.
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

Surface a one-liner like:

> Setting up your `<profile>` connection — this typically takes **<low>–<high> seconds** for `<db_type>` (introspecting tables, validating relationships, generating examples). I'll narrate as I go.

Then proceed.

### Phase 1.1 — existing-model check

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

First, **load `~/.agami/USER_MEMORY.md`** (strip HTML comments). Per [`shared/user-memory-format.md`](../../shared/user-memory-format.md), this file holds default filters, domain vocabulary, and avoid rules the user wants applied to every query.

For each example:
- Build `(question, sql)` using the model from Phase 3.
- Reference fields by their **OSI dataset.field name** (which equals the DB column name in the simple introspect case).
- Use SQL safety rules from [`shared/sql-generation-rules.md`](../../shared/sql-generation-rules.md) and dialect-specific syntax from [`shared/dialect-rules.md`](../../shared/dialect-rules.md).
- **Apply USER_MEMORY policies.** If USER_MEMORY says "exclude test users where email matches @example.com", every seed example that touches `customers` includes that filter. If it says "default time window: last 30 days", date-relevant examples honor that default.

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

## Phase 5: Run a demo query

Pick one of the seed examples and run it end-to-end. The user gets to verify the skill works against their actual data before they start asking real questions. Show the result, ask Yes / No / Skip on the example. **Do NOT use the phrase "engagement moment" anywhere the user can see — it's internal phasing and looks marketing-speak in a chat.**

Pick **one** example from Phase 4 that:
1. Spans ≥ 2 datasets via a relationship (uses a JOIN).
2. Returns ≤ 20 rows so it displays cleanly.
3. Is unambiguously interesting (a "top N", a "by category" breakdown, a recency filter).

Tell the user what you picked and why. Show the generated SQL. Execute via the chosen tier. Render result as a markdown table.

Then **AskUserQuestion**:

> Does this result look right?
> - **Yes (Recommended)** — confirms the example, marks it `confirmed: true` in `~/.agami/<profile>-examples.yaml`
> - **No** — opens the correction flow: ask the user what's wrong, take their corrected SQL, route through the save-correction skill (don't say "/save-correction" to the user — phrase it as "let me know what's wrong and I'll save it as a correction")
> - **Skip** — moves on, doesn't change the example

Branch:
- **Yes** → set `confirmed: true` and `confirmed_at: <ISO>` on the example.
- **No** → invoke the save-correction skill with the user's feedback (which may also update the OSI model — see save-correction/SKILL.md). When telling the user about it, say "let me know what's wrong and I'll save it as a correction" — never tell them to type `/save-correction` since that slash command isn't reliably surfaced across Claude Code hosts.
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

`init/SKILL.md` already wrote a base `.config` with `tier` and `host`. Update it in place — don't overwrite the existing fields.

```bash
install_id=""
if [ "$CONSENT" = "true" ]; then
  install_id=$(python3 -c 'import uuid; print(uuid.uuid4())' 2>/dev/null || uuidgen | tr '[:upper:]' '[:lower:]')
fi
ts=$(date -u +"%Y-%m-%dT%H:%M:%SZ")

# Merge new fields into the existing .config (preserve tier + host)
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
✓ ~/.agami/<profile>.yaml — OSI v0.1.1 semantic model (validated)
✓ ~/.agami/<profile>-examples.yaml — <N> NL→SQL examples
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
| Cached tier no longer works | Re-detect, update `~/.agami/.config` |
| Introspection SQL fails | Route through `db_error_classifier.md`, surface the one-line remediation |
| **Validator fails** | **Refuse to write `~/.agami/<profile>.yaml`. Show errors verbatim. Stage at `/tmp/agami-staging-<profile>.yaml`. Loop on edits + re-validate.** |
| EXPLAIN fails for a seed example | Auto-fix once → if still bad, move to `~/.agami/.rejected/`. Don't block the connect flow. |
| Reintrospect would lose hand-edits | Phase 2 hard rule #7 — preserve descriptions, ai_context, choice_fields, metrics. |
