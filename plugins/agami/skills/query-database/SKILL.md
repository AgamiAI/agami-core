---
name: query-database
description: "Answers natural-language questions about the user's database. Loads the semantic model and few-shot examples from the .agami home directory, generates SQL, executes it locally via the user's chosen execution tier (psql/mysql native CLI, DuckDB binary, or Python driver), returns results as a markdown table with optional CSV export, and renders Chart.js HTML charts on request. All execution is local — no data leaves the machine."
when_to_use: "Use when the user asks 'how many', 'show me', 'top N', 'trend over time', 'compare', 'breakdown by', 'group by', 'average', or any other data question against their configured database. Also use for CSV export ('export this'), chart rendering ('make that a bar chart'), or to follow up on a previous result ('drill into the EU region')."
argument-hint: "[question] [--csv] [--chart bar|line|pie|doughnut|scatter]"
---

# agami query-database

You are answering the user's natural-language question about their database. Goal: generate correct SQL, execute it locally, return rows + an insight, and offer a chart / export when appropriate. The whole flow runs on the user's machine — no data leaves it.

This skill is the main user-facing surface. It orchestrates:

1. **Setup** (once per session) — load the semantic model + examples, verify the execution tier still works.
2. **Generate SQL** — assemble the prompt (model + examples + question), produce one SQL statement, run safety checks.
3. **Execute** — run via the chosen tier (CLI / DuckDB / Python). Auto-retry on classified errors. Risk-assess large-table queries.
4. **Present** — markdown table by default; CSV via `--csv`; Chart.js HTML via `--chart` or "make that a chart".
5. **Log + post-install opt-in + telemetry** — write `~/.agami/query_log.jsonl`, prompt for email opt-in once after first success, flush telemetry queue.

For SQL safety: [`shared/sql-generation-rules.md`](../../shared/sql-generation-rules.md).
For dialect-specific syntax: [`shared/dialect-rules.md`](../../shared/dialect-rules.md).
For connection / tier execution: [`shared/connection-reference.md`](../../shared/connection-reference.md).
For DB error classification: [`shared/db_error_classifier.md`](../../shared/db_error_classifier.md).
For semantic model format: [`shared/schema-reference.md`](../../shared/schema-reference.md).
For chart template: [`shared/chart-template.html`](../../shared/chart-template.html).
For telemetry payload allowlist: [`shared/telemetry-payload.md`](../../shared/telemetry-payload.md).

## Conversation style

- **One question per turn unless they're truly bundled.**
- **Combine acknowledge + next question** — don't waste turns on "Got it!"
- **Use AskUserQuestion for every multi-choice prompt** (chart type, save correction, post-install opt-in).
- **Insights, not narration** — when surfacing results, lead with the answer ("Carol Chen has the highest spend at $148.95"), not the SQL or the process.
- **Round numbers in prose** ("about 150"), keep exact values in the table.

---

## Phase 1: Setup (runs once per session)

### Step 1a — find or create the semantic model

Resolve the profile (default: `default`, override with `AGAMI_PROFILE`). Look for `~/.agami/<dbname>.yaml`.

If missing: invoke the `connect` skill to introspect first, then continue.

If present: read it via the Read tool. Cache in working memory for the rest of the session.

### Step 1b — load the examples library

Read `~/.agami/<dbname>-examples.yaml`. Take the **most recent 50** entries (newest `created_at` first) — that's the cap to keep prompt context bounded.

If the file is missing or empty: warn the user "I don't have any few-shot examples yet — answers may be lower quality. You can run `connect` to seed them, or save corrections as you go."

### Step 1c — verify the execution tier

Look up the cached tier from `~/.agami/.config`. Run a `SELECT 1` probe via that tier. If it fails, route the error through [`shared/db_error_classifier.md`](../../shared/db_error_classifier.md). Common cases:

- `auth` / `dsn` → tell the user their credentials may have rotated; point at `~/.agami/credentials`.
- `network` → check VPN, DB endpoint reachability.
- `driver_missing` → Python tier failed; offer to fall through to DuckDB (tier 2) if the DB type supports it.

If the cached tier doesn't work, re-run tier detection per [`init/SKILL.md → Phase 3`](../init/SKILL.md#phase-3-tier-detection) and update `~/.agami/.config`.

---

## Phase 2: Generate SQL

### Step 2a — classify the input

If `$ARGUMENTS` looks like:

- **A question** (contains `?` or starts with how/what/show/list/which/count/give/get/find/total/average/top/which) → treat as the user's data question. Save it.
- **An empty argument** → ask the user what they'd like to know. Suggest 2-3 questions from the model's `upfront_queries` field if present, or inferred from the schema.
- **A flag-only argument** like `--csv` → re-run the previous query with the flag applied.
- **A follow-up like "make that a chart"** → see Phase 4d (charts).

### Step 2b — assemble the prompt

Build the SQL-generation prompt in this order:

1. **System** — "You are a SQL generator. Write one valid SQL statement for `<DB_TYPE>` that answers the user's question. Output ONLY the SQL, no commentary."
2. **Semantic model** — the entire `~/.agami/<dbname>.yaml` (≤ 50 tables for v1; the cap is implicit because the model is single-file).
3. **Few-shot examples** — the (up to 50) `(question, sql)` pairs from the examples library.
4. **User question** — the question from Step 2a.

Generate one SQL statement. If the model produces multiple statements separated by `;`, take only the first.

### Step 2c — safety checks

Apply [`shared/sql-generation-rules.md`](../../shared/sql-generation-rules.md):

- **No DDL/DML.** If the SQL contains `DROP`, `DELETE`, `INSERT`, `UPDATE`, `ALTER`, `TRUNCATE`, `CREATE`, `GRANT`, `REVOKE` — refuse, regenerate with explicit "SELECT only" framing.
- **No system-table queries.** If the SQL queries `pg_catalog`, `information_schema`, `mysql.*`, `sys.*` — refuse unless the user explicitly asked about schema metadata.
- **NULL-safe division** via `NULLIF(denominator, 0)`.

### Step 2d — risk assessment for large tables

If any table touched by the SQL has `performance_hints.estimated_row_count > 1_000_000` and the SQL has no `WHERE` clause matching a `recommended_filter`:

- Surface a **HIGH risk** banner: "This query scans `<table>` (~10M rows) without a date filter. Add a date range, or proceed anyway?"
- AskUserQuestion: `Add a filter` / `Proceed anyway (Recommended only if you trust the table size)` / `Cancel`.

For tables in the 100k-1M range with no recommended filter: **MEDIUM risk** — note in the response footer but proceed.

For tables under 100k or queries with recommended filters: **LOW risk** — proceed silently.

---

## Phase 3: Execute

### Step 3a — run the SQL via the chosen tier

Invoke the tier-specific command from [`shared/connection-reference.md → CLI Connection Commands`](../../shared/connection-reference.md#cli-connection-commands). Wrap in `time` (or use `date +%s%N` before/after) to measure latency in ms.

Capture: stdout (rows as CSV), stderr (errors), exit code.

### Step 3b — error handling + auto-retry

If exit code is non-zero, route stderr through [`shared/db_error_classifier.md`](../../shared/db_error_classifier.md) → one of nine `error_kind` values.

Behavior per kind:

| `error_kind` | Behavior |
|---|---|
| `auth`, `dsn`, `network` | Stop. Surface the one-line remediation. Do not retry. |
| `driver_missing` | Fall through to the next tier (CLI → DuckDB → Python) if available. Otherwise stop with the install hint. |
| `permission` | Stop. Tell the user the DB user lacks SELECT on the touched table. |
| `column_not_found`, `table_not_found`, `syntax` | Auto-retry up to **2** times. Pass the error back to the SQL generator with: "The previous SQL failed with `<one-line classifier message>`. Regenerate using only schema elements that exist in the model above." |
| `timeout` | Stop. Surface latency + suggest adding a filter. |
| `other` | Stop. Surface the raw error truncated to 200 chars. |

After 2 retries with no success, stop. Don't loop.

### Step 3c — parse rows

Parse the CSV stdout. Header row = column names. Body rows = data. Skip empty trailing lines.

If row count > 1000:
- Truncate to 1000 in display.
- Add a footer: "Showing 1000 of <total> rows. Reply 'show all' or 'export csv' to see more."

If row count == 0:
- "No rows matched. The query was: ..." (show the SQL).
- Suggest a relaxation if applicable ("try removing the date filter").

---

## Phase 4: Present

### Step 4a — insight

Lead with one sentence stating the answer. Examples:

- "**Carol Chen** is the top spender at **$148.95**."
- "**6 orders** were placed in May, up from **2 in April**."
- "Three statuses dominate: shipped (45%), delivered (32%), pending (15%)."

Don't restate the SQL or the question — the user just asked it.

### Step 4b — markdown table

Render the rows as a GitHub-flavored markdown table. Right-align numeric columns. Format numbers per [`shared/sql-generation-rules.md`](../../shared/sql-generation-rules.md) (commas in thousands, 2 decimals for currency).

For wide tables (> 8 columns or > 60 chars per row): switch to a vertical layout (`column: value` per line, blank line between rows) and warn the user the table didn't fit.

### Step 4c — CSV export (`--csv` or "export this")

Write the full result (not truncated) to `~/.agami/exports/<ts>.csv` via Bash:

```bash
ts=$(date +%Y%m%d-%H%M%S)
mkdir -p ~/.agami/exports
# write header row + body rows (CSV-escaping commas/quotes per RFC 4180)
```

Surface: `✓ Exported to ~/.agami/exports/<ts>.csv (<n> rows)`.

### Step 4d — Chart.js chart (`--chart` or "make that a chart")

When the user asks for a chart (or passes `--chart <type>`):

1. **Pick the chart type.** Inferred from the result shape:
   - 1 categorical column + 1 numeric column → `bar` (default) or `pie` if ≤ 6 categories
   - 1 date/time column + 1+ numeric columns → `line`
   - 2 numeric columns → `scatter`
   - 1 categorical + multiple numeric → grouped `bar`
   - User can override with `--chart pie|line|doughnut|scatter`

2. **Build labels + datasets** as JSON:
   ```js
   labels = [<x-axis values from the categorical / date column>]
   datasets = [{ "label": "<numeric column header>", "data": [<numeric values>] }]
   ```

3. **Render the HTML** by reading [`shared/chart-template.html`](../../shared/chart-template.html), substituting the placeholders, and writing to `~/.agami/charts/<ts>.html` via the **Write tool**:
   ```
   {{TITLE}}        → the question (or a summary)
   {{CHART_TYPE}}   → bar | line | pie | doughnut | scatter
   {{LABELS}}       → JSON-encoded array
   {{DATASETS}}     → JSON-encoded array
   {{GENERATED_AT}} → ISO8601 UTC
   {{SQL}}          → the SQL used (HTML-escaped: & → &amp;, < → &lt;, > → &gt;)
   ```

4. **Surface** the file path + a Claude artifact block (inline render on hosts that support it):
   ```
   ✓ Chart saved to ~/.agami/charts/<ts>.html
   Open in any browser.
   ```

   On hosts that support inline artifacts (Claude.ai web, some IDE integrations), additionally embed the HTML as an `application/vnd.claude.code.artifact` block.

5. **Quick alternative**: there's also a sample Python implementation at [`scripts/sample_render_chart.py`](../../scripts/sample_render_chart.py) for users who want to render charts programmatically. The skill itself uses the Write-tool path; no Python needed.

### Step 4e — follow-up suggestions

After every successful query, offer 2-3 follow-ups via AskUserQuestion when natural:

- "Drill into <top result>'s details"
- "Compare this to last month"
- "Render as a chart"
- "Save this as a correction" (only if the user expressed dissatisfaction)

Don't always show — only when the question genuinely has natural follow-ups.

---

## Phase 5: Log

Append one line to `~/.agami/query_log.jsonl`:

```json
{
  "ts": "2026-05-06T15:14:00Z",
  "question": "<the NL question>",
  "sql": "<the executed SQL>",
  "row_count": 5,
  "execution_ms": 250,
  "tier": "cli",
  "risk": "LOW",
  "error_kind": null,
  "feedback": null
}
```

This log is **local-only** — it never gets sent anywhere. The user owns it.

If the user takes a follow-up action that signals satisfaction (drill-down, export, chart), set `feedback: "good"` retroactively on the previous entry. If they rephrase the same question, set `feedback: "bad"` on the previous entry.

---

## Phase 6: Post-install opt-in (one time, after first successful query)

If `~/.agami/.optins` does not exist AND the query just succeeded (Phase 3 returned rows):

```bash
[ ! -f ~/.agami/.optins ] && first_success=true
```

Show this exact prompt via AskUserQuestion (use plain English, don't paraphrase):

> Quick one — first query worked. **Want occasional updates from us about agami?**
>
> Just product news (~once a month). Not on a sales list. Skip if you'd rather not.

Options:
- `Email me updates` — capture an email address (next AskUserQuestion: free-text), POST to HubSpot form API
- `Skip (Recommended)` — write the skip choice and move on

If they pick email:

```bash
# AskUserQuestion next: "What email?" (free text, validate looks like an email)
email="<from user>"
host="<from ~/.agami/.config>"
ts=$(date -u +"%Y-%m-%dT%H:%M:%SZ")

curl -sS -m 5 -X POST https://api.hsforms.com/submissions/v3/integration/submit/<HUB_ID>/<FORM_GUID> \
  -H "Content-Type: application/json" \
  -d @- <<JSON
{
  "fields": [
    {"name": "email",            "value": "$email"},
    {"name": "utm_source",       "value": "skill_install"},
    {"name": "host_preference",  "value": "$host"},
    {"name": "signup_timestamp", "value": "$ts"}
  ]
}
JSON
```

Don't surface the HubSpot URL or response. Tell the user "Thanks — we'll be in touch occasionally."

Write `~/.agami/.optins`:

```json
{
  "schema_version": 1,
  "email_optin": true,
  "email": "<email>",
  "ts": "<ISO8601>"
}
```

(Or `email_optin: false` if they skipped.) Never re-prompt — the file's existence is the gate.

> **Note:** the HUB_ID and FORM_GUID values get baked into the SKILL.md before launch. Until then, this section is a placeholder — the email opt-in is wired but the form isn't live.

---

## Phase 7: Telemetry flush (if opted in)

If `~/.agami/.config` has `analytics_consent: true`:

1. Append a `query` event to `~/.agami/.telemetry-queue.jsonl` using ONLY the allowlisted fields per [`shared/telemetry-payload.md`](../../shared/telemetry-payload.md):
   ```json
   {"event_type": "query", "install_id": "...", "db_type": "postgres", "os": "darwin", "host": "claude-code-cli", "tier": "cli", "latency_p50_ms": 250, "latency_p95_ms": 250, "client_version": "1.0.0", "timestamp": "..."}
   ```
   Build the payload **only** from the allowlist. There is no `query_text` field. There is no `error_message` field. If you find yourself reaching for any other field, stop — there's nothing else to send.

2. Check `~/.agami/.telemetry-last-flush`. If absent or older than 24 hours, flush:
   ```bash
   batch=$(jq -s '{schema_version: 1, events: .[:100]}' ~/.agami/.telemetry-queue.jsonl)
   curl -sS -m 5 -X POST https://analytics.agami.ai/v1/events \
     -H "Content-Type: application/json" \
     -d "$batch" || true
   # On 200, truncate the queue and write the new flush timestamp
   ```

   Failure-tolerant: `|| true`. Never block on telemetry.

If errors occur during this phase, suppress them — telemetry must never affect the user's experience.

---

## Closing

End with:
- The result table (if any)
- One follow-up suggestion (chart, drill-down, save-correction) if natural
- File paths for any artifacts written (export CSV, chart HTML)

Keep the closing tight — the user came here for an answer.

---

## Error handling cheat sheet

| Symptom | Action |
|---|---|
| `~/.agami/<dbname>.yaml` missing | Invoke `connect` skill |
| Credentials chmod wrong | Refuse, offer to fix via `chmod 600` |
| Cached tier no longer works | Re-detect, update `~/.agami/.config` |
| SQL has DDL/DML | Refuse, regenerate with safe framing |
| Auto-retry exhausted (2 tries) | Stop. Show all 3 attempts and their error kinds. |
| HIGH-risk query without filter | Block, AskUserQuestion |
| Chart for empty result | Skip the chart, just show the empty-result message |
| Telemetry POST fails | Silent — keep events in queue, retry next flush |
| HubSpot POST fails | Tell user "Thanks" anyway, save consent locally — sync next time |
