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

Run `EXPLAIN <sql>` (or `EXPLAIN QUERY PLAN <sql>` for SQLite) via the cached tier from `~/.agami/.config`. Same validate-then-save contract as `connect/SKILL.md` Phase 4b:

- EXPLAIN succeeds → continue.
- EXPLAIN fails → route through [`shared/db_error_classifier.md`](../../shared/db_error_classifier.md). Surface the one-line remediation. Do **not** save anything. Ask the user to fix the SQL and try again.

Apply [`shared/sql-generation-rules.md`](../../shared/sql-generation-rules.md):
- Refuse DDL/DML (DROP, DELETE, INSERT, UPDATE, ALTER, etc.).
- Refuse system-table queries unless the user explicitly asked about schema metadata.

---

## Phase 2: Always append to the examples library

Read `~/.agami/<profile>-examples.yaml` via Read. Append a new entry to `examples:` via Edit:

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

Surface: `✓ Correction appended to ~/.agami/<profile>-examples.yaml.`

---

## Phase 3: Classify the correction

Compare the original SQL (from `query_log.jsonl`) to the corrected SQL. Identify what the user was teaching us. Use this taxonomy:

| Kind | Detection signal | Examples |
|---|---|---|
| `sql_fix` | Pure syntax / typo / missing alias / wrong literal — no domain knowledge implied | "missed the GROUP BY"; "you wrote `customer_idx` instead of `customer_id`"; "needs a `LIMIT 5`" |
| `relationship` | The JOIN `ON` clause changed to use different columns, OR a JOIN was added between two datasets where none existed | "join should be on customer_id, not user_id"; "products → categories via category_id" |
| `field_metadata` | Description / unit / type-implication of one field changed — but no SQL structure change beyond the literal value or a CAST | "amount is in cents, divide by 100"; "is_active means 1, not true"; "description for status should say…" |
| `new_metric` | The corrected SQL defines a reusable aggregation that didn't exist in the model — typically the user is teaching us a business metric | "MRR = SUM(price) WHERE plan_type='subscription'"; "active customers means is_active AND last_login > 30 days ago" |
| `user_preference` | A general policy that should apply to **every** future query — not specific to this question. Trigger phrases: "from now on", "always", "never", "by default", "I prefer", "stop showing me…" | "always exclude test users where email matches @example.com"; "default time window is last 30 days unless I say otherwise"; "never include cancelled orders"; "I prefer line charts for time-series" |
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
> - **A new business metric** — let's add this as a reusable metric
> - **A general preference** — apply this to every future query (saves to USER_MEMORY.md, not the model)

The user's answer determines Phase 4 routing.

---

## Phase 4: Apply surgical model edits (when applicable)

If the correction kind is `sql_fix`: **stop here**. Phase 2 already saved the example. Surface the closing message and skip to Phase 5.

For every other kind, you propose a model edit and run the validator BEFORE writing.

### 4a — propose the edit

Read `~/.agami/<profile>.yaml`. Build a **proposed new model** in memory by applying the edit type below.

#### `relationship` edit

If the user's corrected SQL implies a JOIN that:
- **Doesn't exist in `relationships[]`** → add it. Choose a unique `name` (`<from>_to_<to>`; suffix with `_<col>` if there's a name collision). Set `from`, `to`, `from_columns`, `to_columns` from the JOIN's `ON` clause.
- **Exists but with different columns** → update `from_columns` / `to_columns` on the matching `relationships[]` entry. Don't rename the relationship.
- **Reverses an existing relationship's direction** → ask the user before flipping: "I see this changes the direction of `<rel.name>`. Is that intended, or should I add a new relationship?"

If the user dropped a JOIN that was previously there, do **not** delete the relationship from the model — corrections delete only when the user explicitly says "remove the relationship".

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

A `user_preference` correction does NOT touch the OSI semantic model. It lands in `~/.agami/USER_MEMORY.md` (per [`shared/user-memory-format.md`](../../shared/user-memory-format.md)). Steps:

1. **Read** `~/.agami/USER_MEMORY.md` (it exists — `init` seeds it).
2. **Pick the right section** (`Default filters`, `Naming and synonyms`, `Display preferences`, or `Avoid`) based on the policy's nature. Add a new section if none of the four fits — keep this rare.
3. **Append the new bullet** under that section, in plain English (the user's wording, lightly cleaned). Don't paraphrase aggressively — preserve their voice.
4. **Show the user the diff** (per Phase 4b below) before writing.
5. **Strip nothing** — USER_MEMORY.md is intentionally free-form, not schema-validated. The validator (Phase 4c) is a no-op for `user_preference` corrections; the OSI model is unchanged.

The user's bullet should be self-contained — anyone reading USER_MEMORY.md should understand the policy without seeing the original conversation.

#### `mixed` edit

Apply each individual edit as above. Show the user the combined diff in 4b before validating. If the mix includes a `user_preference`, that part skips the validator (USER_MEMORY.md isn't validated); the OSI-model parts still go through the validator.

### 4b — show the diff to the user, get approval

Build a unified diff (or a compact "before / after" summary) of the proposed change against the existing model. Show it to the user via AskUserQuestion:

> I want to update `~/.agami/<profile>.yaml`:
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

Always include the validator step in 4c regardless of which option they pick (since "Yes" still has to validate).

### 4c — validate the proposed model BEFORE writing

This phase is binding. Stage the proposed model at `/tmp/agami-staging-<profile>.yaml`, then:

```bash
python3 plugins/agami/scripts/validate_semantic_model.py /tmp/agami-staging-<profile>.yaml
```

Three outcomes:

- **Exit 0** (PASSED) → rename staging file to `~/.agami/<profile>.yaml`, `chmod 600`. Surface `✓ Model updated and validated.`
- **Exit 1** (FAILED) → **DO NOT WRITE THE MODEL.** Surface the validator's errors verbatim. Tell the user: "Your correction would break the OSI model — here's what's wrong: …. The example is saved either way; the model wasn't updated." Offer to retry with a fix.
- **Exit 2** (TOOLING ERROR) → tell the user the validator is unavailable; ask them to install `pyyaml` and `jsonschema`. Don't write the model.

There is no override path. If validation fails, the model file at `~/.agami/<profile>.yaml` is unchanged. The example library still got the correction (Phase 2 already happened).

### 4d — confirmation

```
✓ Correction appended to ~/.agami/<profile>-examples.yaml
✓ Model updated:
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
- **`<profile>-examples.yaml` missing** — invoke `connect` first to seed, then append.
- **User pastes SQL referencing tables not in the OSI model** — EXPLAIN-validate catches it (`table_not_found`); surface the remediation, don't save.
- **User saves a duplicate of an existing seed** — replace the seed (`source: correction`, fresh `created_at`).
- **Most-recent query is itself a correction** — that's fine, attach to it.
- **Validator fails on a model edit but the user really wants it saved** — they can hand-edit `~/.agami/<profile>.yaml` directly and the next `query-database` will (try to) read it. The validator runs again from `connect verify` if they want to confirm. There is no "skip validation" path from this skill.
- **User says "actually undo my last correction"** — they hand-edit the YAML files; this skill doesn't track an undo log in v1.

---

## Hard rules

1. **Phase 2 (examples append) always runs.** Even if the user later changes their mind on the model edit, the example is already saved.
2. **Phase 4 model writes are gated by the validator.** Exit-0 from `validate_semantic_model.py` is the only way to write `~/.agami/<profile>.yaml`. No exceptions.
3. **Edits stay OSI-conformant.** Don't invent fields. Don't add `custom_extensions` keys not listed in [`shared/agami-osi-extensions.md`](../../shared/agami-osi-extensions.md). When you can't express a correction within the OSI shape + documented Agami extensions, fall back to `sql_fix` (example only) and tell the user "I can save this as a few-shot example but it doesn't fit a model edit; want me to extend the spec?"
4. **Show the diff before mutating the model.** The user always gets to see and approve the proposed change.
