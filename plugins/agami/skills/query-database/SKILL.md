---
name: query-database
description: "Answers natural-language questions about the user's database. Loads the OSI v0.1.1 semantic model and few-shot examples from the .agami home directory, generates SQL by composing OSI datasets/fields/relationships/metrics into a prompt (and reading Agami extensions for type info, choice fields, and performance hints), executes it locally via the user's chosen execution tier (psql/mysql native CLI, DuckDB binary, or Python driver), returns results as a markdown table with optional CSV export, and renders Chart.js HTML charts on request. All execution is local — no data leaves the machine."
when_to_use: "Use when the user asks 'how many', 'show me', 'top N', 'trend over time', 'compare', 'breakdown by', 'group by', 'average', or any other data question against their configured database. Also use for CSV export ('export this'), chart rendering ('make that a bar chart'), or to follow up on a previous result ('drill into the EU region')."
argument-hint: "[question] [--csv] [--chart bar|line|pie|doughnut|scatter]"
---

# agami query-database

You answer the user's natural-language question about their database. Goal: generate correct SQL from the OSI semantic model + the few-shot examples, execute it locally, return rows + an insight, and offer a chart / export when appropriate. Everything runs on the user's machine.

This skill orchestrates:

1. **Setup** (once per session) — load the OSI model and examples library, verify the execution tier still works.
2. **Generate SQL** — compose a prompt from the OSI structure (datasets/fields/relationships/metrics + Agami extensions for type info / choice fields / performance hints), produce one SQL statement, run safety checks.
3. **Execute** — run via the chosen tier; auto-retry on classified errors; risk-assess large-table queries.
4. **Present** — markdown table; CSV via `--csv` or "export this"; Chart.js HTML via `--chart` or "make that a chart".
5. **Log + post-install opt-in + telemetry** — write `~/.agami/query_log.jsonl`, prompt for email opt-in once after first success, flush telemetry queue.

For the OSI format spec: [`shared/schema-reference.md`](../../shared/schema-reference.md).
For Agami's `custom_extensions`: [`shared/agami-osi-extensions.md`](../../shared/agami-osi-extensions.md).
For SQL safety: [`shared/sql-generation-rules.md`](../../shared/sql-generation-rules.md).
For dialect-specific syntax: [`shared/dialect-rules.md`](../../shared/dialect-rules.md).
For connection / tier execution: [`shared/connection-reference.md`](../../shared/connection-reference.md).
For DB error classification: [`shared/db_error_classifier.md`](../../shared/db_error_classifier.md).
For chart template: [`shared/chart-template.html`](../../shared/chart-template.html).
For telemetry payload allowlist: [`shared/telemetry-payload.md`](../../shared/telemetry-payload.md).

## Conversation style

- **One question per turn unless they're truly bundled.**
- **Use AskUserQuestion for every multi-choice prompt** (chart type, save correction, post-install opt-in).
- **Insights, not narration** — lead with the answer ("Carol Chen has the highest spend at $148.95"), not the SQL or the process.
- **Round numbers in prose**, exact in the table.

---

## Phase 1: Setup (once per session)

### 1a — load the OSI model

Resolve `<profile>` (default `default`, override `AGAMI_PROFILE`). Read `~/.agami/<profile>.yaml`.

If missing → invoke the `connect` skill.

If present, sanity-check the top-level shape:
- `version: "0.1.1"` (warn but proceed if different — future spec versions may still be readable)
- `semantic_model[0]` exists with a `name` and `datasets`

Cache the parsed model in working memory for the rest of the session.

### 1b — index the model for fast access

Build these in-memory views you'll reference repeatedly during SQL generation:

```text
datasets_by_name : { dataset.name → dataset object }
fields_by_qname  : { "<dataset.name>.<field.name>" → field object }
relationships_by_endpoints : { (from, to) → relationship object }
metrics_by_name  : { metric.name → metric object }
```

For each field, also extract:
- `type`     ← `agami.type` from `custom_extensions[].vendor_name=COMMON` JSON. If the extension is absent, fall back to inferring from the SQL expression (treat unknown as `string`).
- `choice_field` ← `agami.choice_field` if present (used for synonym matching: "closed-won deals" → `WHERE stage_name = 'Closed Won'`).
- `unit`     ← `agami.unit` if present (used for currency / percentage formatting in result presentation).
- `is_time`  ← `dimension.is_time` if present.

For each dataset, extract `agami.performance_hints` if present — feeds Phase 2d risk assessment.

For each relationship, treat as a directed JOIN edge in a graph: `from` → `to` via `from_columns`/`to_columns`. The SQL generator uses this graph to pick the shortest join path between two datasets the user references.

### 1c — load the examples library

Read `~/.agami/<profile>-examples.yaml`. Take the **most recent 50** entries (newest `created_at` first).

If empty → warn the user and offer `/connect` to seed examples.

### 1c.1 — load USER_MEMORY.md

Read `~/.agami/USER_MEMORY.md` (if present). Strip HTML comments (`<!--...-->`), then keep the rest. If the file is missing, treat it as empty — never error. See [`shared/user-memory-format.md`](../../shared/user-memory-format.md) for what's in it.

This file holds free-form user preferences (default filters, domain vocabulary, display preferences). Inject it into the SQL-generation prompt in Phase 2b under a labeled `## User memory (preferences and policies)` section — the LLM uses it as steering context.

### 1d — verify the execution tier

Look up the cached tier from `~/.agami/.config`. Run a `SELECT 1` probe via that tier. Route any error through [`shared/db_error_classifier.md`](../../shared/db_error_classifier.md). Common cases:

- `auth` / `dsn` → credentials may have rotated; point at `~/.agami/credentials`.
- `network` → check VPN / DB endpoint reachability.
- `driver_missing` → fall through to next tier if available.

If the cached tier doesn't work, re-run tier detection per [`init/SKILL.md → Phase 3`](../init/SKILL.md#phase-3-tier-detection).

---

## Phase 2: Generate SQL

### 2a — classify the input

If `$ARGUMENTS` looks like:
- A question (contains `?` or starts with how/what/show/list/which/count/give/get/find/total/average/top/which) → save it.
- Empty → ask the user; suggest 2-3 questions from the model's `ai_context.examples` if present, or inferred from `datasets[].description`.
- Flag-only (`--csv` / `--chart bar`) → re-run the previous query with the flag applied.
- Follow-up like "make that a chart" → see Phase 4d.

### 2b — assemble the prompt for the SQL generator

Build the prompt in this order — this is what reaches the model that produces SQL:

1. **System** — "You are a SQL generator. Write one valid SQL statement for `<DB_TYPE>` (dialect: ANSI_SQL with `<DB_TYPE>`-specific tweaks per dialect-rules.md) that answers the user's question. Output ONLY the SQL, no commentary."

2. **Schema context** — render the OSI model as compact text the LLM can reason over. The shape matters:
   ```
   Datasets:
     <dataset.name> (<dataset.source>) [<row_count if known>]
       Description: <dataset.description>
       Synonyms: <ai_context.synonyms>
       Fields:
         <field.name>  type=<agami.type>  expr=<expression>  [time]  [choices: a,b,c]
       Performance hints: <if present, list recommended_filters and selective_filters>

   Relationships:
     <name>: <from>.<from_cols> → <to>.<to_cols>

   Metrics:
     <name>: <expression>  -- <description>
       Synonyms: <ai_context.synonyms>
   ```
   This is the "compact OSI rendering" — derived from the model, not the raw YAML. The LLM gets just enough structure to write correct SQL without parsing OSI directly.

3. **User memory** — content of `~/.agami/USER_MEMORY.md` from Step 1c.1, under a heading `## User memory (preferences and policies)`. Skip this section if the file is empty after stripping comments. The LLM treats this as binding context — apply default filters, respect avoid lists, use the user's domain vocabulary.

4. **Few-shot examples** — the up-to-50 `(question, sql)` pairs from the examples library.

5. **User question** — the question from Step 2a.

Generate one SQL statement. If the model produces multiple statements separated by `;`, take only the first.

**Use OSI metrics by name when applicable.** If the user asks about "revenue" and the model has a `metrics[]` entry named `total_revenue` (with a synonym matching), prefer that metric's expression over building a fresh aggregate from scratch.

### 2c — safety checks

Apply [`shared/sql-generation-rules.md`](../../shared/sql-generation-rules.md):

- **No DDL/DML.** Refuse on `DROP`, `DELETE`, `INSERT`, `UPDATE`, `ALTER`, `TRUNCATE`, `CREATE`, `GRANT`, `REVOKE`. Regenerate with explicit "SELECT only" framing.
- **No system tables.** Refuse on `pg_catalog`, `information_schema`, `mysql.*`, `sys.*` unless the user is explicitly asking about schema metadata.
- **NULL-safe division** via `NULLIF(denominator, 0)`.
- **`agami.type` consistency** — if the SQL applies a numeric aggregate (`SUM`, `AVG`) to a field whose `agami.type` is `string` or `boolean`, refuse and regenerate. Type info exists for a reason.

### 2d — risk assessment for large tables

For each dataset touched by the SQL, look up its `agami.performance_hints`:

- `estimated_row_count > 1_000_000` AND no WHERE clause matches a `recommended_filters[].column`:
  → **HIGH risk**. Surface a banner: "This query scans `<dataset>` (~<row_count>) without a date filter. Add a date range, or proceed anyway?" AskUserQuestion: `Add a filter` / `Proceed anyway` / `Cancel`.
- `100k–1M` rows with no recommended filter → **MEDIUM**. Note in response footer; proceed.
- Otherwise → **LOW**. Proceed silently.

---

## Phase 3: Execute

### 3a — run the SQL

Invoke the tier-specific command from [`shared/connection-reference.md → CLI Connection Commands`](../../shared/connection-reference.md#cli-connection-commands). Wrap in a high-resolution timer to capture latency in ms.

Capture: stdout (rows as CSV), stderr (errors), exit code.

### 3b — error handling + auto-retry

Route any non-zero exit through [`shared/db_error_classifier.md`](../../shared/db_error_classifier.md). Behavior per kind:

| `error_kind` | Behavior |
|---|---|
| `auth`, `dsn`, `network` | Stop. Surface the one-line remediation. No retry. |
| `driver_missing` | Fall through to next tier (CLI → DuckDB → Python). |
| `permission` | Stop. DB user lacks SELECT on the touched dataset. |
| `column_not_found`, `table_not_found`, `syntax` | Auto-retry up to **2** times. Pass the error back to the SQL generator: "The previous SQL failed with `<one-line classifier message>`. Regenerate using only OSI dataset / field names from the schema context above." |
| `timeout` | Stop. Suggest adding a filter using the `recommended_filters` from the dataset's performance hints. |
| `other` | Stop. Surface raw error truncated to 200 chars. |

After 2 retries with no success, stop. Don't loop.

### 3c — parse rows

Parse the CSV stdout. Header row = column names. Body rows = data.

Format numeric columns per their `agami.type` and `agami.unit`:
- `decimal` + `unit: dollars` → `$148.95` (currency formatting, 2 decimals)
- `decimal` + `unit: percent` → `12.4%`
- `integer` → `1,234` (commas)
- `timestamp` / `date` → human-readable (`May 6, 2026, 12:00 PM`)
- otherwise → as-is

If row count > 1000:
- Truncate display to 1000.
- Footer: "Showing 1000 of <total>. Reply 'show all' or 'export csv'."

If row count == 0:
- "No rows matched. The query was: …" (show SQL).
- Suggest a relaxation if applicable.

---

## Phase 4: Present

### 4a — insight first (in chat)

Lead with one sentence. Don't restate the SQL or the question.

### 4b — markdown table (in chat)

Right-align numeric columns. Format per Phase 3c. Wide tables (> 8 cols) → vertical layout, warn user.

### 4c — Always render the HTML report (unless result is a 1×1 scalar)

**Rule:** if the result has at least 2 rows OR at least 2 columns, ALWAYS render the HTML report. Don't ask. The report contains the chart, the data table, the insights, and the SQL — one self-contained file the user can open or share. Skip the report only when the result is a single scalar (1 row × 1 column, e.g., `SELECT COUNT(*) FROM orders` returning `42`) — for those, the chat answer is enough.

#### 4c.i — pick the chart type from column types

Read `agami.type` for each result column. Mapping:

| Result shape | Chart |
|---|---|
| 1 categorical (`string`) + 1 numeric | `bar` (use `pie` / `doughnut` if ≤ 6 categories) |
| 1 time (`timestamp` / `date`) + 1+ numeric | `line` |
| 2 numeric | `scatter` |
| 1 categorical + multiple numeric | grouped `bar` |
| Categorical-only or single column | no chart applies — see 4c.iii |

If the user explicitly says `--chart pie|line|doughnut|scatter` (or "render as a line chart"), honor that override.

#### 4c.ii — build the placeholder values

```text
TITLE         = the user's question (or a tight summary)
INSIGHTS      = the 1-3 sentence insight from 4a, HTML-escaped
CHART_TYPE    = one of bar | line | pie | doughnut | scatter
LABELS        = JSON array of x-axis (or pie) labels
DATASETS      = JSON array of Chart.js dataset objects:
                [{ "label": "<numeric column header>", "data": [<numeric values>] }, ...]
TABLE_HEADERS = JSON array of column header strings (e.g. ["name", "spend"])
TABLE_ROWS    = JSON array of arrays — one inner array per row, in display order
SQL           = the executed SQL, HTML-escape & < >
GENERATED_AT  = ISO8601 UTC
AGAMI_LOGO_DARK_TEXT  = entire SVG content of shared/agami-logo-dark.svg
AGAMI_LOGO_LIGHT_TEXT = entire SVG content of shared/agami-logo-light.svg
```

#### 4c.iii — when no chart applies (categorical-only or single-column results)

The report still ships — table + insights + SQL. For the chart placeholders, use a no-op fallback that produces an empty chart card:

```text
CHART_TYPE = "bar"
LABELS     = []
DATASETS   = []
```

Chart.js will render an empty canvas, which the CSS hides cleanly. The user gets the table + insights + SQL without an awkward "no chart" message.

#### 4c.iv — render the HTML

1. Read [`shared/chart-template.html`](../../shared/chart-template.html).
2. Read [`shared/agami-logo-dark.svg`](../../shared/agami-logo-dark.svg) and [`shared/agami-logo-light.svg`](../../shared/agami-logo-light.svg). Substitute their full contents into `{{AGAMI_LOGO_DARK_TEXT}}` and `{{AGAMI_LOGO_LIGHT_TEXT}}` respectively.
3. Substitute the per-chart placeholders.
4. Write the result via the **Write tool** to `~/.agami/charts/<ts>.html`.

Surface the file path. On hosts that support inline artifacts, also embed as a Claude artifact block.

#### 4c.v — CSV export (`--csv` or "export this")

Even with the HTML report, the user might still want a flat CSV. If they pass `--csv` or say "export this":

```bash
ts=$(date +%Y%m%d-%H%M%S)
mkdir -p ~/.agami/exports
# write header + rows, RFC 4180 escaping
```

Surface the CSV path.

### 4d — follow-up suggestions

After a successful query, offer 2-3 follow-ups via AskUserQuestion when natural:
- "Drill into <top result>"
- "Compare to last month"
- "Save this as a correction" (only if the user expressed dissatisfaction — don't preempt)

Don't suggest "render a chart" anymore — chart is always rendered (4c). Don't always show follow-ups — only when the question has natural ones.

---

## Phase 5: Log

Append one line to `~/.agami/query_log.jsonl`:

```json
{
  "ts": "2026-05-06T15:14:00Z",
  "question": "<NL question>",
  "sql": "<executed SQL>",
  "row_count": 5,
  "execution_ms": 250,
  "tier": "cli",
  "risk": "LOW",
  "error_kind": null,
  "feedback": null
}
```

**Local-only** — never sent. The user owns it.

If the user takes a positive follow-up action (drill-down, export, chart), set `feedback: "good"` retroactively on the previous entry. If they rephrase the same question, set `feedback: "bad"`.

---

## Phase 6: Post-install opt-in (one time, after first successful query)

If `~/.agami/.optins` doesn't exist AND the query just succeeded:

> Quick one — first query worked. **Want occasional updates from us about agami?**
>
> Just product news (~once a month). Not on a sales list. Skip if you'd rather not.

AskUserQuestion: `Email me updates` (capture email next, POST to HubSpot form) / `Skip (Recommended)`.

If email: POST to `https://api.hsforms.com/submissions/v3/integration/submit/<HUB_ID>/<FORM_GUID>` with `email`, `utm_source: skill_install`, `host_preference`, `signup_timestamp`. Surface "Thanks — we'll be in touch occasionally."

Write `~/.agami/.optins` regardless of choice (existence is the never-re-prompt gate).

> **HUB_ID and FORM_GUID values get baked in before launch. Until then, this is a placeholder.**

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
| `~/.agami/<profile>.yaml` missing | Invoke `connect` |
| Model file fails to parse as YAML | Surface error, suggest `connect reintrospect` |
| `version` ≠ `"0.1.1"` | Warn but proceed; suggest `connect reintrospect` to upgrade |
| Credentials chmod wrong | Refuse, offer `chmod 600` |
| Cached tier broken | Re-detect, update `.config` |
| SQL has DDL/DML | Refuse, regenerate |
| Type mismatch (numeric aggregate on string field) | Refuse, regenerate |
| Auto-retry exhausted (2 tries) | Stop. Show all 3 attempts and their error kinds. |
| HIGH-risk query without filter | Block, AskUserQuestion |
| Chart for empty result | Skip the chart, just show empty-result message |
| Telemetry POST fails | Silent — keep events in queue, retry next flush |
| HubSpot POST fails | Tell user "Thanks" anyway, save consent locally |
