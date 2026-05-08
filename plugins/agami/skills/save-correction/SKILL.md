---
name: save-correction
description: "Saves a user correction so future queries learn from it. Always appends a (question, corrected_sql) pair to the per-database examples YAML in the .agami home directory. Additionally, classifies the correction and — when applicable — applies a surgical edit to the OSI semantic model itself (relationship fix, field metadata, or new metric). Every model edit is OSI-conformant and validated before write; the validator is the binding gate. Shows the user a model diff for approval before any model mutation."
when_to_use: "Use when the user says 'save this as a correction', '/save-correction', 'remember this', 'use this SQL next time', or after the user manually fixes a query result and wants future similar questions to use the fix. Also use when the demo query in connect/SKILL.md gets a 'No' answer — that's a correction in disguise."
argument-hint: "[corrected SQL or NL feedback]"
---

# agami save-correction

**Before suggesting any slash command in chat, read [`shared/invocation-conventions.md`](../../shared/invocation-conventions.md).** The only working slash command for agami is `/init` (bare). Never tell the user to type `/agami:save-correction`, `/save-correction`, `/agami:init`, or any other slash form — those don't exist. Phrase guidance as natural language ("say 'save this as a correction'", "say 'remember this'") and the relevant skill's `when_to_use` will catch it.

You are recording a user correction. Goal: persist the fix so similar questions get better answers next time.

This skill does two things, in this order:

1. **Always**: append the `(question, corrected_sql)` pair to `~/.agami/<profile>-examples.yaml` (few-shot library).
2. **When applicable**: surgically update `~/.agami/<profile>.yaml` (the OSI semantic model) with the knowledge implied by the correction. The model edit is **always** OSI-conformant and **always** validated before write. If the user's correction would break OSI, refuse the model update (the example still gets saved).

For the OSI format spec: [`shared/schema-reference.md`](../../shared/schema-reference.md).
For Agami's `custom_extensions`: [`shared/agami-osi-extensions.md`](../../shared/agami-osi-extensions.md).
For SQL safety: [`shared/sql-generation-rules.md`](../../shared/sql-generation-rules.md).
For dialect rules: [`shared/dialect-rules.md`](../../shared/dialect-rules.md).
For DB error classification: [`shared/db_error_classifier.md`](../../shared/db_error_classifier.md).

---

## Phase 1: Identify the correction

### 1a — resolve the active profile

Resolve `<profile>` in this order: `AGAMI_PROFILE` env var → `active_profile` field in `~/.agami/.config` → literal string `"default"` (legacy fallback). All `~/.agami/<profile>.yaml` and `~/.agami/<profile>-examples.yaml` paths in this skill use the resolved name.

### 1b — find the most recent query

Read the last entry in `~/.agami/query_log.jsonl`. Need `question` and `sql`.

If the log is empty: "I don't have a recent query to attach this correction to. Ask the question first, then save the correction." Stop.

### 1c — get the corrected SQL

Determine what the user gave:
- **They pasted SQL** (`$ARGUMENTS` looks like a SELECT, contains `FROM` / `JOIN` / `GROUP BY`) → use directly as the corrected SQL.
- **They described what's wrong** ("the join should be on customer_id, not user_id"; "amount is in cents") → regenerate SQL using the OSI model + the original question + their feedback as additional context. Same prompt assembly as `query-database` Phase 2b.
- **No arguments and no recent feedback** → ask: "Paste the corrected SQL, or tell me what's wrong with the result."

### 1d — EXPLAIN-validate the corrected SQL

Run `EXPLAIN <sql>` (or `EXPLAIN QUERY PLAN <sql>` for SQLite) via the cached database tool from `~/.agami/.config`. Same validate-then-save contract as `connect/SKILL.md` Phase 4b:

- EXPLAIN succeeds → continue.
- EXPLAIN fails → route through [`shared/db_error_classifier.md`](../../shared/db_error_classifier.md). Surface the one-line remediation. Do **not** save anything. Ask the user to fix the SQL and try again.

Apply [`shared/sql-generation-rules.md`](../../shared/sql-generation-rules.md):
- Refuse DDL/DML (DROP, DELETE, INSERT, UPDATE, ALTER, etc.).
- Refuse system-table queries unless the user explicitly asked about schema metadata.

---

## Phase 2: Always append to the examples library

Read `~/.agami/<profile>/examples.yaml` via Read (fall back to `~/.agami/<profile>-examples.yaml` for v1.0 layouts). Append a new entry to `examples:` via Edit:

```yaml
- question: <the original NL question from query_log.jsonl>
  sql: |-
    <the corrected SQL>
  source: correction
  created_at: <ISO8601 UTC now>
  confirmed: true
  confirmed_at: <ISO8601 UTC now>
```

If a previous example has the same `question`: replace its `sql` and bump `created_at` rather than duplicating.

This phase is **non-conditional** — every correction always lands in the examples library, even if Phase 4 (model update) declines or fails.

Surface: `✓ Correction appended to ~/.agami/<profile>/examples.yaml.`

---

## Phase 3: Classify the correction

Compare the original SQL (from `query_log.jsonl`) to the corrected SQL. Identify what the user was teaching us. Use this taxonomy:

| Kind | Detection signal | Examples |
|---|---|---|
| `sql_fix` | Pure syntax / typo / missing alias / wrong literal — no domain knowledge implied | "missed the GROUP BY"; "you wrote `customer_idx` instead of `customer_id`"; "needs a `LIMIT 5`" |
| `relationship` | The JOIN `ON` clause changed to use different columns, OR a JOIN was added between two datasets where none existed | "join should be on customer_id, not user_id"; "products → categories via category_id" |
| `field_metadata` | Description / unit / type-implication of one field changed — but no SQL structure change beyond the literal value or a CAST | "amount is in cents, divide by 100"; "is_active means 1, not true"; "description for status should say…" |
| `table_metadata` | Description or `ai_context` of one **dataset** (table) changed — the user is teaching us what the table represents, not a single column. Trigger phrases: "the orders table also includes…", "this table is what we use for…", "<table> is really for…" | "the `orders` table includes both completed AND cancelled orders — never assume it's only completed"; "`metrics_daily` is materialized from `events` — use `metrics_daily` for date ranges > 7 days" |
| `new_metric` | The corrected SQL defines a reusable aggregation that didn't exist in the model — typically the user is teaching us a business metric | "MRR = SUM(price) WHERE plan_type='subscription'"; "active customers means is_active AND last_login > 30 days ago" |
| `user_preference` | A general policy that should apply to **every** future query, **across every database** — not specific to this question or this database. Trigger phrases: "from now on", "always", "never", "by default", "I prefer", "stop showing me…" | "always exclude test users where email matches @example.com"; "default time window is last 30 days unless I say otherwise"; "I prefer line charts for time-series" |
| `org_context` | Domain knowledge specific to **this database**: vocabulary, business definitions, what the data represents. Trigger phrases that reference business terms not derivable from the schema, or definitions like "<term> means…", "we use <term> to mean…", "in our world <term> is…" | "gold-tier customers means lifetime spend > $10k"; "MRR is what we call recurring revenue — only counts subscription plans"; "we DON'T track refunds in this database, those live in Stripe" |
| `mixed` | More than one of the above | "wrong join AND amount needs /100" |

### How to classify

Look at the **diff** between the original SQL and the corrected SQL:
- JOIN condition changed → likely `relationship`.
- New WHERE clause referencing a specific business term (e.g., `plan_type='subscription'`) AND new aggregation → likely `new_metric`.
- Math applied to one column (`/100`, `* 100.0`, `CAST(...)`) where the column wasn't aliased before → likely `field_metadata` (with a `unit` correction).
- Only structural / cosmetic SQL changes → `sql_fix`.

When ambiguous, **AskUserQuestion**:

> What kind of correction is this?
> - **A SQL fix** — the answer was wrong but the model is fine
> - **A join correction** — relationships in the model need updating
> - **A column meaning change** — e.g., amount is in cents, status means something specific
> - **A table meaning change** — the description / context for a whole table
> - **A new business metric** — let's add this as a reusable metric
> - **Domain context for this database** — e.g., "gold tier means lifetime spend > $10k" (saves to ORGANIZATION.md)
> - **A general preference** — applies across every database, not just this one (saves to USER_MEMORY.md)

The user's answer determines Phase 4 routing.

**Distinguishing `org_context` vs `user_preference`:** ask "would this guidance apply if I connected to a different database?" If yes → `user_preference`. If no (it's specific to this domain) → `org_context`.

---

## Phase 4: Apply surgical model edits (when applicable)

If the correction kind is `sql_fix`: **stop here**. Phase 2 already saved the example. Surface the closing message and skip to Phase 5.

For every other kind, you propose a model edit and run the validator BEFORE writing.

### 4a — propose the edit

For OSI-model edits, the per-schema layout means the edit lands in **one specific** `<schema>.yaml` (or in `index.yaml` for cross-schema relationships). Identify the target file:

1. Find the affected dataset in the merged in-memory view (built by `query-database` Phase 1c).
2. Look up the dataset's `source: <db>.<schema>.<table>` — the middle component is the schema.
3. The edit lands in `~/.agami/<profile>/<schema>.yaml`.
4. **Cross-schema relationship edits** (`from` and `to` are in different schemas) land in `~/.agami/<profile>/index.yaml` under `cross_schema_relationships[]`.

For v1.0 single-file installs, the edit still lands in `~/.agami/<profile>.yaml` — the routing logic above degrades gracefully.

Build a **proposed new schema yaml** (or new `index.yaml`) in memory by applying the edit type below.

#### `relationship` edit

If the user's corrected SQL implies a JOIN that:
- **Doesn't exist in `relationships[]`** → add it. Choose a unique `name` (`<from>_to_<to>`; suffix with `_<col>` if there's a name collision). Set `from`, `to`, `from_columns`, `to_columns` from the JOIN's `ON` clause.
- **Exists but with different columns** → update `from_columns` / `to_columns` on the matching `relationships[]` entry. Don't rename the relationship.
- **Reverses an existing relationship's direction** → ask the user before flipping: "I see this changes the direction of `<rel.name>`. Is that intended, or should I add a new relationship?"

If the user dropped a JOIN that was previously there, do **not** delete the relationship from the model — corrections delete only when the user explicitly says "remove the relationship".

For **cross-schema relationships** (the JOIN spans datasets in different schemas), edit `~/.agami/<profile>/index.yaml.cross_schema_relationships[]` instead of any individual schema yaml. Endpoints must be qualified `<schema>.<dataset>` per [`shared/schema-reference.md`](../../shared/schema-reference.md).

#### `table_metadata` edit

For the dataset implicated:
- Update its `description` to reflect the user's note ("Customer orders, including both completed and cancelled.").
- If the user added domain instructions (e.g., "use this table for date ranges > 7 days"), append to `ai_context.instructions` (or set it if absent).
- If the user gave alternate names, append to `ai_context.synonyms[]`.

Never change `source`, `primary_key`, `unique_keys`, `fields`, or relationships from a `table_metadata` edit. Those are structural — table-metadata corrections are about WHAT the table represents, not HOW to read it.

#### `field_metadata` edit

For the field implicated:
- Update `description` to reflect the user's note ("Order amount in cents.").
- If the user mentioned a unit (`cents`, `percent`, `dollars`), update the field's `custom_extensions[].vendor_name=COMMON` JSON to include `agami.unit: <value>`. Add the extension entry if absent; preserve existing keys (`type`, `original_type`).
- If the user implied an enum mapping ("status `1` means active, `0` means inactive"), update `agami.choice_field` similarly.
- Append the user's verbatim note as an `ai_context.synonyms[]` entry if it's a synonym ("revenue means amount/100").

Never change `expression` from a `field_metadata` edit. Expressions are about HOW to read the column; descriptions/units/choices are about WHAT it means.

#### `new_metric` edit

Add a new entry to top-level `metrics[]`:

```yaml
- name: <derived_from_user_request>          # snake_case, unique within model
  expression:
    dialects:
      - dialect: ANSI_SQL
        expression: <the aggregation expression from the corrected SQL>
  description: <one-sentence summary>
  ai_context:
    synonyms: [<the user's term, plus a few obvious variants>]
```

**Always reference fields by `<dataset_name>.<field_name>`** in the metric expression — that's the OSI convention. Strip any WHERE clauses that are about specific user filtering (e.g., `WHERE customer_id = 42`) — keep only the WHERE clauses that are part of the metric's definition (e.g., `WHERE plan_type = 'subscription'` for MRR).

#### `user_preference` edit

A `user_preference` correction does NOT touch the OSI semantic model. It lands in `~/.agami/USER_MEMORY.md` (per [`shared/user-memory-format.md`](../../shared/user-memory-format.md)) — the **global** preferences file that applies across every database. Steps:

1. **Read** `~/.agami/USER_MEMORY.md` (it exists — `init` seeds it).
2. **Pick the right section** (`Default filters`, `Naming and synonyms`, `Display preferences`, or `Avoid`) based on the policy's nature. Add a new section if none of the four fits — keep this rare.
3. **Append the new bullet** under that section, in plain English (the user's wording, lightly cleaned). Don't paraphrase aggressively — preserve their voice.
4. **Show the user the diff** (per Phase 4b below) before writing.
5. **Strip nothing** — USER_MEMORY.md is intentionally free-form, not schema-validated. The validator (Phase 4c) is a no-op for `user_preference` corrections; the OSI model is unchanged.

The user's bullet should be self-contained — anyone reading USER_MEMORY.md should understand the policy without seeing the original conversation.

#### `org_context` edit

An `org_context` correction lands in `~/.agami/<profile>/ORGANIZATION.md` — the **per-database** domain context file (per [`shared/organization-context-format.md`](../../shared/organization-context-format.md)). It does NOT touch the OSI semantic model.

Steps:

1. **Read** `~/.agami/<profile>/ORGANIZATION.md` (create with the default template if missing — `init`/`connect` normally seed it, but this is a safe fallback).
2. **Pick the right section.** Most domain-context entries land under `## Key terminology` as `- "<term>" = <definition>` bullets. If the user is describing what the data represents at a higher level, append a paragraph under `# About this database` instead. If they're describing *what's not in this database*, append under `## What we DON'T track here`. Add a new section only if none of the existing ones fit.
3. **Append the new bullet (or paragraph)** in plain English, preserving the user's wording.
4. **Show the user the diff** (Phase 4b) before writing.
5. **No validation** — ORGANIZATION.md is free-form. The OSI model and all schema yamls are unchanged.

The user's bullet should be self-contained — anyone reading ORGANIZATION.md should understand the term without seeing the original conversation. Example output:

```markdown
## Key terminology

- "MRR" = monthly recurring revenue, computed as SUM(price) WHERE plan='subscription'
- "active user" = signed in within the last 30 days
- "gold tier" = lifetime spend > $10k                  ← appended by save-correction
```

#### `mixed` edit

Apply each individual edit as above. Show the user the combined diff in 4b before validating. If the mix includes a `user_preference` or `org_context`, those parts skip the validator (USER_MEMORY.md / ORGANIZATION.md aren't validated); the OSI-model parts still go through the validator.

### 4b — show the diff to the user, get approval

Build a unified diff (or a compact "before / after" summary) of the proposed change against the existing target file. Name the file in the prompt so the user knows what they're approving. Show via AskUserQuestion:

> I want to update `~/.agami/<profile>/public.yaml`:
>
> ```
> [Relationship] orders_to_customers
> - from_columns: [user_id]
> + from_columns: [customer_id]
> ```
>
> Approve?
> - **Yes (Recommended)** — apply and validate
> - **No** — leave the model as-is, the example is still saved
> - **Edit first** — let me tweak before applying

For `org_context` / `user_preference` the file is `ORGANIZATION.md` / `USER_MEMORY.md` instead of a schema yaml — same prompt shape, just a different filename.

Always include the validator step in 4c regardless of which option they pick (since "Yes" still has to validate, except for ORGANIZATION.md / USER_MEMORY.md which aren't validated).

### 4c — validate the proposed model BEFORE writing

This phase is binding for any edit that touches `~/.agami/<profile>/<schema>.yaml` or `~/.agami/<profile>/index.yaml`. Stage the **whole target directory** at `/tmp/agami-staging-<profile>/` (copy the existing files, then overwrite the one you're editing), then run the directory-mode validator:

```bash
staging="/tmp/agami-staging-$profile"
rm -rf "$staging" && cp -r "$HOME/.agami/$profile" "$staging"
# Overwrite the one file the edit targets (e.g., $staging/public.yaml or $staging/index.yaml)
python3 "$AGAMI_PLUGIN_ROOT/scripts/validate_semantic_model.py" --directory "$staging"
```

For v1.0 single-file installs, fall back to single-file validation:

```bash
python3 "$AGAMI_PLUGIN_ROOT/scripts/validate_semantic_model.py" /tmp/agami-staging-<profile>.yaml
```

Three outcomes:

- **Exit 0** (PASSED) → atomically promote the target file (or, for directory-mode, the whole directory):
  - Directory-mode: `mv "$HOME/.agami/$profile" "$HOME/.agami/$profile.tmp_old" && mv "$staging" "$HOME/.agami/$profile" && rm -rf "$HOME/.agami/$profile.tmp_old"`. `chmod 700` on the dir, `chmod 600` on each file.
  - Single-file mode: `mv /tmp/agami-staging-<profile>.yaml ~/.agami/<profile>.yaml && chmod 600 ~/.agami/<profile>.yaml`.
  - Surface `✓ Model updated and validated.`
- **Exit 1** (FAILED) → **DO NOT PROMOTE.** Surface the validator's errors verbatim. Tell the user: "Your correction would break the OSI model — here's what's wrong: …. The example is saved either way; the model wasn't updated." Offer to retry with a fix.
- **Exit 2** (TOOLING ERROR) → tell the user the validator is unavailable; ask them to install `pyyaml` and `jsonschema`. Don't write the model.

There is no override path. If validation fails, the user's `~/.agami/<profile>/` is unchanged. The example library still got the correction (Phase 2 already happened).

For `org_context` / `user_preference` corrections (which only touch ORGANIZATION.md / USER_MEMORY.md): no validator step. Write the file directly with `chmod 600`.

### 4d — confirmation

```
✓ Correction appended to ~/.agami/<profile>/examples.yaml
✓ Model updated in ~/.agami/<profile>/public.yaml:
    - relationship orders_to_customers from_columns: [user_id] → [customer_id]
✓ Validator passed.

Next time someone asks "<question>" or anything similar, I'll use the corrected SQL AND know the right join is on customer_id.
```

---

## Phase 5: Telemetry (if opted in)

If `~/.agami/.config` has `analytics_consent: true`, append a `correction` event to `~/.agami/.telemetry-queue.jsonl` using ONLY the allowlisted fields per [`shared/telemetry-payload.md`](../../shared/telemetry-payload.md):

```json
{"event_type": "correction", "install_id": "...", "db_type": "postgres", "os": "darwin", "host": "claude-code-cli", "tier": "cli", "client_version": "1.0.0", "timestamp": "..."}
```

No question text, no SQL, no model diff — just the fact that a correction happened. The kind of correction (sql_fix / relationship / field_metadata / new_metric) is **not** sent in v1; if we add it, document the new field in [`shared/telemetry-payload.md`](../../shared/telemetry-payload.md) and update the privacy invariant test before shipping.

The next `query-database` invocation flushes the queue.

---

## Edge cases

- **Empty examples file** — initialize it with the new entry as the only one.
- **`examples.yaml` missing** — invoke `connect` first to seed, then append.
- **User pastes SQL referencing tables not in the OSI model** — EXPLAIN-validate catches it (`table_not_found`); surface the remediation, don't save.
- **User saves a duplicate of an existing seed** — replace the seed (`source: correction`, fresh `created_at`).
- **Most-recent query is itself a correction** — that's fine, attach to it.
- **Validator fails on a model edit but the user really wants it saved** — they can hand-edit `~/.agami/<profile>/<schema>.yaml` directly and the next `query-database` will (try to) read it. The validator runs again from `connect verify` if they want to confirm. There is no "skip validation" path from this skill.
- **User says "actually undo my last correction"** — they hand-edit the YAML / Markdown files; this skill doesn't track an undo log in v1.
- **Edit affects datasets in two different schemas** — split into two separate edits, one per target file. The validator runs once per write (or once for the merged directory).

---

## Hard rules

1. **Phase 2 (examples append) always runs.** Even if the user later changes their mind on the model edit, the example is already saved.
2. **Phase 4 model writes are gated by the validator.** Exit-0 from `validate_semantic_model.py --directory` (or single-file mode for legacy installs) is the only way to write inside `~/.agami/<profile>/`. No exceptions. ORGANIZATION.md and USER_MEMORY.md edits skip the validator (free-form Markdown).
3. **Edits stay OSI-conformant.** Don't invent fields. Don't add `custom_extensions` keys not listed in [`shared/agami-osi-extensions.md`](../../shared/agami-osi-extensions.md). When you can't express a correction within the OSI shape + documented Agami extensions, fall back to `sql_fix` (example only) and tell the user "I can save this as a few-shot example but it doesn't fit a model edit; want me to extend the spec?"
4. **Show the diff before mutating the model.** The user always gets to see and approve the proposed change.
