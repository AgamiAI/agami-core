---
name: save-correction
description: "Saves a correction to the few-shot examples library so future queries learn from it. Takes the most recent question + the user's corrected SQL (or a corrected NL paraphrase the skill regenerates SQL for), validates it via EXPLAIN against the live DB, and appends it to the per-database examples YAML in the .agami home directory. Every subsequent query loads the correction in its prompt context."
when_to_use: "Use when the user says 'save this as a correction', '/save-correction', 'remember this', 'use this SQL next time', or after the user manually fixes a query result and wants future similar questions to use the fix. Also use when the demo query in connect/SKILL.md gets a 'No' answer — that's a correction in disguise."
argument-hint: "[corrected SQL or NL feedback]"
---

# agami save-correction

You are recording a correction to the few-shot examples library. Goal: take what the user just told you (corrected SQL, or NL feedback) and persist it so the next similar question gets the right answer.

The examples library at `~/.agami/<dbname>-examples.yaml` is the single mechanism by which agami "learns". Every query loads it. There is no separate model file, no embedding store, no fine-tune loop.

For SQL safety: [`shared/sql-generation-rules.md`](../../shared/sql-generation-rules.md).
For dialect-specific syntax: [`shared/dialect-rules.md`](../../shared/dialect-rules.md).
For DB error classification: [`shared/db_error_classifier.md`](../../shared/db_error_classifier.md).

---

## Phase 1: Find the most recent query

Read the last entry in `~/.agami/query_log.jsonl`. Need the `question` and the `sql` (the one that just ran).

If the log is empty: tell the user "I don't have a recent query to attach this correction to. Ask the question first, then save the correction."

---

## Phase 2: Get the corrected SQL

Determine what the user gave you:

- **They pasted SQL** (`$ARGUMENTS` looks like a SELECT statement, or contains `FROM`, `JOIN`, `GROUP BY`) → use it directly as the corrected SQL.
- **They described what's wrong** ("the join should be on customer_id, not user_id"; "filter to last 30 days, not 7") → regenerate SQL using the semantic model + the original question + their feedback as additional context, just like `query-database` Phase 2.
- **No arguments and no recent feedback** → ask: "Paste the corrected SQL, or tell me what's wrong with the result."

---

## Phase 3: Validate via EXPLAIN

Before writing, run `EXPLAIN <sql>` against the live DB through the cached tier. Same validate-then-write contract as `connect/SKILL.md → Phase 2b`:

- If EXPLAIN succeeds → proceed to Phase 4.
- If EXPLAIN fails → route through [`shared/db_error_classifier.md`](../../shared/db_error_classifier.md). Surface the one-line remediation. Do **not** save a broken correction. Ask the user to fix the SQL and try again.

Apply [`shared/sql-generation-rules.md`](../../shared/sql-generation-rules.md):
- Refuse DDL/DML (DROP, DELETE, INSERT, UPDATE, ALTER, etc.).
- Refuse queries against system tables unless the user is explicitly asking about schema metadata.

---

## Phase 4: Append to the examples library

Read `~/.agami/<dbname>-examples.yaml` via the Read tool. Append a new entry to the `examples:` list using the Edit tool:

```yaml
  - question: <the original NL question from query_log.jsonl>
    sql: |-
      <the corrected SQL>
    source: correction
    created_at: <ISO8601 UTC now>
    confirmed: true
    confirmed_at: <ISO8601 UTC now>
```

Notes:
- `source: correction` distinguishes user-saved corrections from `seed` examples.
- `created_at` is what determines the most-recent-50 cap in `query-database/SKILL.md` Phase 1b — corrections are always kept ahead of older seeds.
- Use `|-` block scalars for any multi-line SQL.

If a previous example already has the same `question`: replace its `sql` field instead of appending a duplicate. Set `source: correction` and update `created_at`.

---

## Phase 5: Confirm + telemetry

Surface a one-line confirmation:

```
✓ Correction saved to ~/.agami/<dbname>-examples.yaml.
Next time someone asks "<the question>" (or anything similar), I'll use this SQL as a guide.
```

If `~/.agami/.config` has `analytics_consent: true`, append a `correction` event to `~/.agami/.telemetry-queue.jsonl`. Build the payload using ONLY the allowlist per [`shared/telemetry-payload.md`](../../shared/telemetry-payload.md):

```json
{"event_type": "correction", "install_id": "...", "db_type": "postgres", "os": "darwin", "host": "claude-code-cli", "tier": "cli", "client_version": "1.0.0", "timestamp": "..."}
```

No question text, no SQL — just the fact that a correction happened, and on what kind of DB.

The next `query-database` invocation flushes the queue.

---

## Edge cases

- **Empty examples file** (no `examples:` key yet) — initialize it with the new entry as the only one.
- **`<dbname>-examples.yaml` missing** — invoke `connect` first to seed the file, then append.
- **User pastes SQL that doesn't reference any of their tables** — the EXPLAIN-validate step catches this; surface the `table_not_found` remediation.
- **User saves a duplicate of an existing seed** — replace the seed (`source: correction`, fresh `created_at`).
- **Multiple recent queries — which one to attach the correction to?** Always the most recent. If the user says "no, the one before that", they should re-ask the question first or paste the question explicitly with the SQL.
