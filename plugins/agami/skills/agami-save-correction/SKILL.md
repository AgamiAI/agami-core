---
name: agami-save-correction
description: "Saves a user correction so future queries learn from it. Always appends a (question, corrected_sql) pair to the per-database examples YAML in the .agami home directory. Additionally, classifies the correction and — when applicable — applies a surgical edit to the OSI semantic model itself (relationship fix, field metadata, or new metric). Every model edit is OSI-conformant and validated before write; the validator is the binding gate. Shows the user a model diff for approval before any model mutation."
when_to_use: "Use when the user says 'save this as a correction', '/agami-save-correction', 'remember this', 'use this SQL next time', or after the user manually fixes a query result and wants future similar questions to use the fix. Also use when the demo query in agami-connect/SKILL.md gets a 'No' answer — that's a correction in disguise."
argument-hint: "[corrected SQL or NL feedback]"
---

# agami save-correction

**Before suggesting any slash command in chat, read [`shared/invocation-conventions.md`](../../shared/invocation-conventions.md).** Agami slash commands: `/agami-connect`, `/agami-query-database`, `/agami-review`, `/agami-model`, `/agami-save-correction`, `/agami-reconcile`. Never write the un-prefixed forms (`/save-correction`, `/init`, etc.) or colon forms (`/agami:save-correction`) — those don't exist. **`/agami-init` was folded into `/agami-connect` Phase 0a.** For chat replies, prefer natural language ("say 'save this as a correction'", "say 'remember this'") — the agami-save-correction skill's `when_to_use` matcher routes correctly.

You are recording a user correction. Goal: persist the fix so similar questions get better answers next time.

This skill does two things, in this order:

1. **Always**: append the `(question, corrected_sql)` pair to `<artifacts_dir>/<profile>/examples.yaml` (few-shot library).
2. **When applicable**: surgically update one of the OSI semantic model files at `<artifacts_dir>/<profile>/` (e.g. `<schema>.yaml` for a relationship/field/metric edit, or `index.yaml` for a cross-schema relationship) with the knowledge implied by the correction. The model edit is **always** OSI-conformant and **always** validated before write. If the user's correction would break OSI, refuse the model update (the example still gets saved).

For the OSI format spec: [`shared/schema-reference.md`](../../shared/schema-reference.md).
For Agami's `custom_extensions`: [`shared/agami-osi-extensions.md`](../../shared/agami-osi-extensions.md).
For SQL safety: [`shared/sql-generation-rules.md`](../../shared/sql-generation-rules.md).
For dialect rules: [`shared/dialect-rules.md`](../../shared/dialect-rules.md).
For DB error classification: [`shared/db_error_classifier.md`](../../shared/db_error_classifier.md).

---

## Phase −1: Plan-mode check

Run the detection + ask logic from [`shared/plan-mode-check.md`](../../shared/plan-mode-check.md). agami-save-correction needs Write (examples + model edits) and Bash (EXPLAIN-validation) — both are blocked in plan mode.

**If plan mode is active and the user picks `Stay in plan mode` (or this skill is invoked under an active plan-mode context):** refuse and end the turn. **DO NOT write a plan file. DO NOT call `ExitPlanMode`.** Refusal text (verbatim):

> I can't save corrections in plan mode — switch to **Auto** or **Edit Automatically** mode (Shift+Tab to cycle) and re-invoke. The correction won't persist otherwise.

If plan mode is not active, skip this phase silently and go to Phase 1.

---

## Phase 1: Identify the correction

### 1a — resolve the active profile and artifacts_dir

Resolve `<profile>` in this order: `AGAMI_PROFILE` env var → `active_profile` field in `~/.agami/.config` → literal string `"default"` (legacy fallback).

Resolve `<artifacts_dir>` per [`shared/file-layout.md → Configuring artifacts_dir`](../../shared/file-layout.md#configuring-artifacts_dir): `AGAMI_ARTIFACTS_DIR` env var → `~/.agami/.config.artifacts_dir` → default `$HOME/agami-artifacts`. All examples / OSI / ORGANIZATION.md paths in this skill resolve under `<artifacts_dir>/<profile>/`. USER_MEMORY.md is at `<artifacts_dir>/USER_MEMORY.md` (top-level, cross-database).

For v1.0 / v1.1 fallback paths (`~/.agami/<profile>.yaml`, `~/.agami/<profile>-examples.yaml`, `<artifacts_dir>/<profile>/`), only read; never write. Migration is agami-connect's job — this skill assumes the user has already migrated by the time they're saving corrections.

### 1b — find the most recent query

Read the last entry in `~/.agami/query_log.jsonl`. Need `question` and `sql`.

If the log is empty: "I don't have a recent query to attach this correction to. Ask the question first, then save the correction." Stop.

### 1c — get the corrected SQL

Determine what the user gave:
- **They pasted SQL** (`$ARGUMENTS` looks like a SELECT, contains `FROM` / `JOIN` / `GROUP BY`) → use directly as the corrected SQL.
- **They described what's wrong** ("the join should be on customer_id, not user_id"; "amount is in cents") → regenerate SQL using the OSI model + the original question + their feedback as additional context. Same prompt assembly as `query-database` Phase 2b.
- **No arguments and no recent feedback** → ask: "Paste the corrected SQL, or tell me what's wrong with the result."

### 1d — EXPLAIN-validate the corrected SQL

Run `EXPLAIN <sql>` (or `EXPLAIN QUERY PLAN <sql>` for SQLite) via the cached database tool from `~/.agami/.config`. Same validate-then-save contract as `agami-connect/SKILL.md` Phase 5b:

- EXPLAIN succeeds → continue.
- EXPLAIN fails → route through [`shared/db_error_classifier.md`](../../shared/db_error_classifier.md). Surface the one-line remediation. Do **not** save anything. Ask the user to fix the SQL and try again.

Apply [`shared/sql-generation-rules.md`](../../shared/sql-generation-rules.md):
- Refuse DDL/DML (DROP, DELETE, INSERT, UPDATE, ALTER, etc.).
- Refuse system-table queries unless the user explicitly asked about schema metadata.

---

## Phase 2: Always append to the examples library

Read `<artifacts_dir>/<profile>/examples.yaml` via Read (fall back to `~/.agami/<profile>-examples.yaml` for v1.0 layouts). Append a new entry to `examples:` via Edit:

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

This phase is **non-conditional** — every correction always lands in the examples library, even if Phase 5 (model update) declines or fails.

Surface: `✓ Correction appended to <artifacts_dir>/<profile>/examples.yaml.`

---

## Phase 3: Classify the correction

Compare the original SQL (from `query_log.jsonl`) to the corrected SQL. Identify what the user was teaching us, then route the correction to the right destination. **Mis-routing is the most common failure** — early-adopter feedback included three real cases where corrections landed in the wrong file:

| Category | What the user did (wrong) | What should have happened |
|---|---|---|
| Per-example commentary ("CRIF total can be negative on #12") | Captured in `examples.yaml` `notes[]` | Field description on `LOAN_DETAILS.TOTAL_DISBURSED_ALL` (`field_metadata`) |
| Gender normalization (Male / MALE / T / Unknown → "Male") | `ORGANIZATION.md` | `agami.choice_field` on `PII.GENDER` (`field_metadata`) |
| "Format counts with commas in outputs" | Captured in a markdown file as prose | `user_preference` in `USER_MEMORY.md` AND `TO_CHAR(…)` / aliases IN the seed example's SQL |

The decision tree below corrects these failures. **Walk it top to bottom — first match wins.** Don't fall back to `org_context` as a default; it's the catch-all that produces the wrong outcome in practice.

### Decision tree (top to bottom — first match wins)

```
Is the correction about a single column's MEANING, UNIT, ENCODING, or VALUE NORMALIZATION?
   (e.g., "amount is in cents", "1 means active not true", "TOTAL_DISBURSED can be
    negative for refunds", "Male/MALE/T all map to 'Male'", "STATUS='1' means active")
   → field_metadata
       → if it's specifically about value → canonical-display mapping, write to
         agami.choice_field on that field.
       → if it's about how the column is interpreted (unit, sign convention,
         encoding), update agami.unit + the field description prose.

Else: is the correction about a JOIN — which columns connect two tables?
   (e.g., "join on customer_id, not user_id"; "products → categories via category_id")
   → relationship

Else: is the correction about what a WHOLE TABLE represents, not one column?
   (e.g., "`orders` includes cancelled rows too", "`metrics_daily` is materialized
    from `events`, use it for date ranges > 7 days")
   → table_metadata

Else: does the corrected SQL define a REUSABLE AGGREGATION that didn't exist?
   (e.g., "MRR = SUM(price) WHERE plan_type='subscription'", "active customers
    means is_active AND last_login > 30 days ago")
   → new_metric
       → if also touches the predicate side (active customers), consider whether
         the predicate alone deserves a named_filter — same Rule 1 sign-off.

Else: is the correction a DISPLAY / FORMATTING / DEFAULT-FILTER preference that
   should apply to every database?
   (e.g., "always exclude @example.com test users", "default time window is
    last 30 days", "format counts with commas", "always show currency with $")
   → user_preference (writes to USER_MEMORY.md)
       → if it's about how SQL renders results (TO_CHAR, ROUND, ::text, AS aliases),
         ALSO update the relevant seed example's SQL so future answers actually
         apply the formatting — prose in USER_MEMORY.md tells the LLM what the
         user wants; the example demonstrates how to achieve it.

Else: is the correction about a BUSINESS TERM specific to this database's domain?
   (e.g., "gold tier means lifetime spend > $10k" — used as a category in many
    queries; "MRR" — the abstract concept; "we don't track refunds, those live
    in Stripe" — what the data fundamentally doesn't include)
   → org_context (writes to ORGANIZATION.md)
       → org_context is for ABSTRACT business concepts that aren't tied to one
         specific column. A correction tied to a specific column belongs in
         field_metadata, NOT here. Re-check the first rule of the tree before
         landing here.

Else: pure SQL syntax / typo with no domain knowledge implied
   (e.g., "missed the GROUP BY", "`customer_idx` is a typo of `customer_id`")
   → sql_fix
```

### Anti-patterns the LLM keeps producing (do NOT do these)

1. **Per-column rule → ORGANIZATION.md.** "`PII.GENDER` values normalize to Male" is NOT domain context — it's a column-value mapping. Route to `field_metadata` (`choice_field`).
2. **Per-column rule → examples.yaml notes.** This skill never writes to `examples.yaml.notes[]` (that path lives in agami-connect Phase 6d). If you find yourself wanting to write "the CRIF total can be negative" as a note on example #12, route it to `field_metadata` on the actual column instead — the lesson applies to every future query, not just to one example.
3. **Display preference → ORGANIZATION.md.** "Format counts with commas" is not a domain concept — it's a display preference. Route to `user_preference` (USER_MEMORY.md).
4. **Display preference → prose without changing SQL.** If the correction is "always format like X," ALSO modify the seed example's SQL to demonstrate the formatting (so future answers actually apply it, not just describe it).

### Diff-based hints (look at SQL changes for classification clues)

- JOIN condition changed → likely `relationship`.
- Math applied to one column (`/100`, `* 100.0`, `CAST(...)`) → likely `field_metadata` (with a `unit` correction).
- `CASE WHEN col = 'X' THEN 'Y' ELSE 'Z' END` for a column's display value → likely `field_metadata` with `choice_field` update.
- New WHERE clause referencing a specific business term (e.g., `plan_type='subscription'`) AND new aggregation → likely `new_metric`.
- `TO_CHAR(...)`, `ROUND(...)`, `AS my_alias` purely on the output side → likely `user_preference` (display) — ALSO update the seed example's SQL.
- Only structural / cosmetic SQL changes → `sql_fix`.

### When ambiguous, AskUserQuestion (use the rubric above as your option set)

> What kind of correction is this?
> - **A SQL fix** — the answer was wrong but the model is fine
> - **A column meaning change** — e.g., amount is in cents, status means something specific, Male/MALE/T all map to "Male"
> - **A join correction** — relationships in the model need updating
> - **A table meaning change** — the description / context for a whole table
> - **A new business metric** — let's add this as a reusable metric
> - **A display preference** — applies across every database (formatting, default filters, ...)
> - **Domain context for this database** — abstract business concepts not tied to one column (e.g., "gold tier means lifetime spend > $10k")

The user's answer determines Phase 5 routing.

**Distinguishing `org_context` vs `user_preference` vs `field_metadata`:**
- If the rule is tied to a specific column → `field_metadata`. **Always check this first.**
- Else ask: "would this guidance apply if I connected to a different database?" If yes → `user_preference`. If no (it's specific to this domain) → `org_context`.

### Phase 4 — surface classification + destination BEFORE Phase 5 writes anything

After classifying, **always** surface the decision to the user in chat as a one-line summary with explicit reasoning, then proceed with the edit. The contract:

```
Classification: <kind>
  → routing to: <destination file + the specific field/section that'll change>
  → reasoning: <one sentence explaining which rule of the decision tree matched>
```

Concrete examples (taken from real corrections):

```
Classification: field_metadata
  → routing to: BUREAU_DATA/LOAN_DETAILS.yaml → fields["TOTAL_DISBURSED_ALL"].description
  → reasoning: rule says "correction about a single column's meaning/encoding/sign convention" — you're teaching that this column can be negative because refunds carry a negative sign.

Classification: field_metadata
  → routing to: BUREAU_DATA/PII.yaml → fields["GENDER"].agami.choice_field
  → reasoning: rule says "value normalization mapping (Male/MALE/T → 'Male') belongs in choice_field" — not ORGANIZATION.md.

Classification: user_preference + seed-example update
  → routing to: USER_MEMORY.md (the prose preference) AND examples.yaml example #N (TO_CHAR in SQL)
  → reasoning: "always format counts with commas" is a display preference (cross-DB), and the seed example needs the formatting baked into its SQL so future answers actually apply it.
```

The user can override before any file is written. If they say "no, that belongs in X instead," re-route to X and re-surface the new classification before proceeding.

---

## Phase 5: Apply surgical model edits (when applicable)

If the correction kind is `sql_fix`: **stop here**. Phase 2 already saved the example. Surface the closing message and skip to Phase 6.

For every other kind, you propose a model edit and run the validator BEFORE writing.

### 4a — propose the edit

For OSI-model edits, identify the target file based on what's being edited and which layout the user has:

| Edit kind | Target file (v1.3) | Target file (v1.2) | Target file (v1.0) |
|---|---|---|---|
| `field_metadata`, `table_metadata`, single-table `new_metric` | `<artifacts_dir>/<profile>/<schema>/<table>.yaml` | `<artifacts_dir>/<profile>/<schema>.yaml` | `~/.agami/<profile>.yaml` |
| Within-schema `relationship`, multi-table `new_metric` within one schema | `<artifacts_dir>/<profile>/<schema>/_schema.yaml` | `<artifacts_dir>/<profile>/<schema>.yaml` | `~/.agami/<profile>.yaml` |
| Cross-schema `relationship` | `<artifacts_dir>/<profile>/index.yaml` (cross_schema_relationships[]) | `<artifacts_dir>/<profile>/index.yaml` (cross_schema_relationships[]) | `~/.agami/<profile>.yaml` |
| `org_context` | `<artifacts_dir>/<profile>/ORGANIZATION.md` (no validator) | same | same |
| `user_preference` | `<artifacts_dir>/USER_MEMORY.md` (no validator) | same | same |

To resolve the schema for a dataset:

1. Find the affected dataset in the merged in-memory view (built by `query-database` Phase 1c).
2. Look up the dataset's `source: <db>.<schema>.<table>` — the middle component is the schema.
3. Use the table from `agami.table` extension (v1.3) or the dataset's `name` (which equals the table name in v1.2).

Detect layout by checking if `<artifacts_dir>/<profile>/<schema>/_schema.yaml` exists — present means v1.3, absent means v1.2.

Build a **proposed new file** in memory by applying the edit type below.

#### `relationship` edit

If the user's corrected SQL implies a JOIN that:
- **Doesn't exist in `relationships[]`** → add it. Choose a unique `name` (`<from>_to_<to>`; suffix with `_<col>` if there's a name collision). Set `from`, `to`, `from_columns`, `to_columns` from the JOIN's `ON` clause.
- **Exists but with different columns** → update `from_columns` / `to_columns` on the matching `relationships[]` entry. Don't rename the relationship.
- **Reverses an existing relationship's direction** → ask the user before flipping: "I see this changes the direction of `<rel.name>`. Is that intended, or should I add a new relationship?"

If the user dropped a JOIN that was previously there, do **not** delete the relationship from the model — corrections delete only when the user explicitly says "remove the relationship".

For **cross-schema relationships** (the JOIN spans datasets in different schemas), edit `<artifacts_dir>/<profile>/index.yaml.cross_schema_relationships[]` instead of any individual schema yaml. Endpoints must be qualified `<schema>.<dataset>` per [`shared/schema-reference.md`](../../shared/schema-reference.md).

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

A `user_preference` correction does NOT touch the OSI semantic model. It lands in `<artifacts_dir>/USER_MEMORY.md` (per [`shared/user-memory-format.md`](../../shared/user-memory-format.md)) — the **global** preferences file that applies across every database. Steps:

1. **Read** `<artifacts_dir>/USER_MEMORY.md` (it exists — `init` seeds it).
2. **Pick the right section** (`Default filters`, `Naming and synonyms`, `Display preferences`, or `Avoid`) based on the policy's nature. Add a new section if none of the four fits — keep this rare.
3. **Append the new bullet** under that section, in plain English (the user's wording, lightly cleaned). Don't paraphrase aggressively — preserve their voice.
4. **Show the user the diff** (per Phase 4b below) before writing.
5. **Strip nothing** — USER_MEMORY.md is intentionally free-form, not schema-validated. The validator (Phase 4c) is a no-op for `user_preference` corrections; the OSI model is unchanged.

The user's bullet should be self-contained — anyone reading USER_MEMORY.md should understand the policy without seeing the original conversation.

#### `org_context` edit

An `org_context` correction lands in `<artifacts_dir>/<profile>/ORGANIZATION.md` — the **per-database** domain context file (per [`shared/organization-context-format.md`](../../shared/organization-context-format.md)). It does NOT touch the OSI semantic model.

Steps:

1. **Read** `<artifacts_dir>/<profile>/ORGANIZATION.md` (create with the default template if missing — `init`/`connect` normally seed it, but this is a safe fallback).
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

> I want to update `<artifacts_dir>/<profile>/public.yaml`:
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

This phase is binding for any edit that touches a `<table>.yaml`, `_schema.yaml`, or `index.yaml`. Stage the **whole target directory** at `/tmp/agami-staging-<profile>/` (copy all existing files, overwrite the one you're editing), then run the directory-mode validator — it walks every per-table yaml and merges per-schema before validating:

```bash
staging="/tmp/agami-staging-$profile"
rm -rf "$staging" && cp -R "$artifacts_dir/$profile" "$staging"
# Overwrite the one file the edit targets, e.g.:
#   $staging/<schema>/<table>.yaml   for field/table edits
#   $staging/<schema>/_schema.yaml   for within-schema relationship/metric edits
#   $staging/index.yaml              for cross-schema relationship edits
python3 "$AGAMI_PLUGIN_ROOT/scripts/validate_semantic_model.py" --directory "$staging"
```

For v1.2 layouts (single file per schema, no `_schema.yaml`), the same `--directory` invocation still works — the validator detects layout per-schema and dispatches accordingly.

For v1.0 single-file installs, fall back to single-file validation:

```bash
python3 "$AGAMI_PLUGIN_ROOT/scripts/validate_semantic_model.py" /tmp/agami-staging-<profile>.yaml
```

Three outcomes:

- **Exit 0** (PASSED) → atomically promote the staging directory:
  - Directory-mode: `mv "$artifacts_dir/$profile" "$artifacts_dir/$profile.tmp_old" && mv "$staging" "$artifacts_dir/$profile" && rm -rf "$artifacts_dir/$profile.tmp_old"`. `chmod 755` on dirs, `chmod 644` on yaml/md files.
  - Single-file mode (v1.0): `mv /tmp/agami-staging-<profile>.yaml ~/.agami/<profile>.yaml && chmod 600 ~/.agami/<profile>.yaml`.
  - Surface `✓ Model updated and validated.`
- **Exit 1** (FAILED) → **DO NOT PROMOTE.** Surface the validator's errors verbatim. Tell the user: "Your correction would break the OSI model — here's what's wrong: …. The example is saved either way; the model wasn't updated." Offer to retry with a fix.
- **Exit 2** (TOOLING ERROR) → tell the user the validator is unavailable; ask them to install `pyyaml` and `jsonschema`. Don't write the model.

There is no override path. If validation fails, the user's `<artifacts_dir>/<profile>/` is unchanged. The example library still got the correction (Phase 2 already happened).

For `org_context` / `user_preference` corrections (which only touch ORGANIZATION.md / USER_MEMORY.md): no validator step. Write the file directly with `chmod 600`.

### 4d — confirmation

```
✓ Correction appended to <artifacts_dir>/<profile>/examples.yaml
✓ Model updated in <artifacts_dir>/<profile>/public.yaml:
    - relationship orders_to_customers from_columns: [user_id] → [customer_id]
✓ Validator passed.

Next time someone asks "<question>" or anything similar, I'll use the corrected SQL AND know the right join is on customer_id.
```

---

(Phase 6 — telemetry emission on correction — has been removed in the current 0.x line. The skill no longer reads `analytics_consent` and no longer appends to `.telemetry-queue.jsonl`. agami has no telemetry — see `docs/privacy.md`.)

---

## Edge cases

- **Empty examples file** — initialize it with the new entry as the only one.
- **`examples.yaml` missing** — invoke `agami-connect` first to seed, then append.
- **User pastes SQL referencing tables not in the OSI model** — EXPLAIN-validate catches it (`table_not_found`); surface the remediation, don't save.
- **User saves a duplicate of an existing seed** — replace the seed (`source: correction`, fresh `created_at`).
- **Most-recent query is itself a correction** — that's fine, attach to it.
- **Validator fails on a model edit but the user really wants it saved** — they can hand-edit `<artifacts_dir>/<profile>/<schema>.yaml` directly and the next `query-database` will (try to) read it. The validator runs again from `connect verify` if they want to confirm. There is no "skip validation" path from this skill.
- **User says "actually undo my last correction"** — they hand-edit the YAML / Markdown files; this skill doesn't track an undo log in v1.
- **Edit affects datasets in two different schemas** — split into two separate edits, one per target file. The validator runs once per write (or once for the merged directory).

---

## Hard rules

1. **Phase 2 (examples append) always runs.** Even if the user later changes their mind on the model edit, the example is already saved.
2. **Phase 5 model writes are gated by the validator.** Exit-0 from `validate_semantic_model.py --directory` (or single-file mode for legacy installs) is the only way to write inside `<artifacts_dir>/<profile>/`. No exceptions. ORGANIZATION.md and USER_MEMORY.md edits skip the validator (free-form Markdown).
3. **Edits stay OSI-conformant.** Don't invent fields. Don't add `custom_extensions` keys not listed in [`shared/agami-osi-extensions.md`](../../shared/agami-osi-extensions.md). When you can't express a correction within the OSI shape + documented Agami extensions, fall back to `sql_fix` (example only) and tell the user "I can save this as a few-shot example but it doesn't fit a model edit; want me to extend the spec?"
4. **Show the diff before mutating the model.** The user always gets to see and approve the proposed change.
