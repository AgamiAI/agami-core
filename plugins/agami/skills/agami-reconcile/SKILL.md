---
name: agami-reconcile
description: "Reconciles a CSV of (label, expected_value) pairs from an existing dashboard against agami's answers. For each row, the skill generates a matching NL question, runs it through the active profile's semantic model, diffs actual vs expected, and surfaces matches in green and mismatches in red with drill-down receipts. The strongest onboarding demo for a skeptical data engineer — either we agree with their numbers (trust earned via evidence) or we surface a real definitional disagreement (trust earned via transparency)."
when_to_use: "Use when the user says 'reconcile against this dashboard', 'do these numbers match?', 'validate against my Tableau export', '/agami-reconcile <csv>', or pastes a CSV / table of known numbers and asks agami to reproduce them. Requires agami-connect to have been run first (need an OSI model + examples library). Three independent customer asks (Sourav + Intuit + Asana data teams) anchor this as the highest-leverage validation surface for early adopters."
argument-hint: "<path-to-csv>"
---

# agami reconcile

You are running the reconciliation harness. Goal: take a CSV of labeled numbers from an existing dashboard (Tableau / Looker / Mode / Metabase / spreadsheet) and prove agami can reproduce each number. When numbers match, that's evidence the semantic model is right. When they don't, the receipt drill-down explains why — typically a definitional disagreement (gross vs net, refunds in vs out, FX rate at booking vs reporting date) — which is exactly the trust signal that makes a DE relax.

This skill orchestrates:

1. **Parse** the CSV into a list of `(label, expected_value)` pairs.
2. **Generate a matching NL question** for each label.
3. **Run** each question through the same NL→SQL→execute pipeline as agami-query-database.
4. **Diff** actual vs expected with a tolerance.
5. **Present** a markdown table with per-row status; for mismatches, render the full receipt as a drill-down so the user can find the definitional disagreement.

Spec for the deterministic helpers: [`scripts/reconcile.py`](../../scripts/reconcile.py) (CSV parser + number normalization + diff with tolerance).

## Conversation style

- **Tight loops.** This skill is a tool, not a tutorial. One question per turn, max two sentences of prose between phases.
- **Surface mismatches loud.** A reconcile run with 9/12 matches and 3 mismatches is a SUCCESSFUL run — the mismatches are the value. Lead with what didn't match.
- **Don't paste raw SQL in chat.** The receipt has it. Same hard rule as agami-query-database.

---

## Phase 0: Preflight

Same checks as agami-query-database / agami-connect:

1. **Plan-mode check** per [`shared/plan-mode-check.md`](../../shared/plan-mode-check.md). This skill needs Bash + Read + Write — refuse if locked in plan mode. **DO NOT write a plan file. DO NOT call `ExitPlanMode`.** Refusal text: *"I can't reconcile in plan mode — each row runs a live query and writes a receipt. Switch to Default or Auto-accept (Shift+Tab) and re-invoke me with the CSV path."*
2. **Credentials present** — read `~/.agami/credentials` for the active profile. If missing, invoke `/agami-connect` to set up first; this skill needs a working DB connection.
3. **OSI model present** — `<artifacts_dir>/<profile>/index.yaml` must exist. If not, invoke `/agami-connect`. This skill needs an introspected model to generate questions against.
4. **Argument** — `$ARGUMENTS` should be a path to a CSV file. If not provided, ask once: *"Paste the path to a CSV with two columns: `label,value`. (Or paste the CSV inline as a code block and I'll extract it.)"* Accept inline-pasted CSV — write it to `/tmp/agami-reconcile-<ts>.csv` and proceed.
5. **Validate the file exists**. If not, surface error and stop.

---

## Phase 1: Parse the CSV

```bash
python3 "$AGAMI_PLUGIN_ROOT/scripts/reconcile.py" parse --csv "<csv_path>" \
  > /tmp/agami-reconcile-rows-<ts>.json
```

The helper handles:
- Header detection (with-or-without first-row column names)
- 2-column or 3+ column inputs (3rd onward are appended to the label as context)
- Currency symbols / magnitude suffixes / accounting parens / percent (`$4.2M`, `₹2.16Cr`, `(123.45)`, `42%`)
- Null sentinels (`n/a`, `—`, blank)

Read the JSON. Each row is `{label, expected_value, raw_value}`. Discard rows where `expected_value` is null (unparseable) — surface a one-liner: *"Skipped 2 rows where the value couldn't be parsed: 'X', 'Y'."*

Surface to the user:
> Parsed `<N>` rows from `<csv_path>`. Reconciling now — typically `<N> × 5–15s` per row depending on query latency.

---

## Phase 2: Generate questions + execute

For each row in the parsed list:

### 2a — Generate the NL question

Use the LLM to translate `label` (+ context if present) into the most natural English question whose answer should be `expected_value`. Examples:

| label | question |
|---|---|
| `Q3 2025 Revenue` | "What was total revenue in Q3 2025?" |
| `Active customers (Apr 2026)` | "How many active customers did we have in April 2026?" |
| `Pipeline value (open opps)` | "What's the total pipeline value across open opportunities?" |
| `Mean order size last 30 days` | "What's the average order size over the last 30 days?" |

The OSI model + examples library are loaded; let the LLM pick the right datasets / metrics / named filters that resolve the labeled term to a concrete query.

### 2b — Run via the agami-query-database pipeline

Invoke the same SQL-generation + execution path agami-query-database uses (Phases 2 + 3 of that skill — see [`agami-query-database/SKILL.md`](../agami-query-database/SKILL.md)). Capture:

- The generated SQL
- The result (should be a single scalar, or a single row)
- The full chart-template HTML report (so the user can drill in for mismatches)
- The trust receipt (with confidence, signed-off-by, etc.)

If the SQL fails OR the result isn't a single scalar (e.g., the LLM-generated question returned a multi-row table), capture an error: `Could not extract a single scalar from the result.` These rows show up as `error` status in the report.

### 2c — Diff

```bash
python3 "$AGAMI_PLUGIN_ROOT/scripts/reconcile.py" diff \
  --expected "<expected_value>" \
  --actual "<actual_value_from_query>" \
  --tolerance 0.01
```

Default tolerance: ±1%. The user can override with `tolerance=N%` in their original ask (e.g., "reconcile with 5% tolerance"). Tolerance applies to numeric comparisons; for text values (rare), use exact match.

Capture: `match` (bool), `delta`, `delta_pct`.

### 2d — Build the row record

Per row:

```json
{
  "label":        "<from CSV>",
  "question":     "<LLM-generated NL question>",
  "expected":     <number>,
  "actual":       <number or null if errored>,
  "delta_pct":    <signed fraction or null>,
  "match":        true | false,
  "status":       "match" | "mismatch" | "error",
  "report_path":  "~/.agami/charts/<ts>.html",  // the full chart report for this query
  "error":        "<message if status=error, else null>"
}
```

Append all records to `/tmp/agami-reconcile-results-<ts>.jsonl` so the user can inspect later.

---

## Phase 3: Present

### 3a — Summary line first

```
Reconciled <N> numbers: <M> match (within ±1%), <K> mismatch, <E> error.
```

### 3b — Mismatches table (lead with what didn't match)

Render the mismatches as a markdown table BEFORE the matches:

```markdown
### Mismatches

| Label | Expected | Got | Δ | Drill-down |
|---|---:|---:|---:|---|
| Q3 2025 Revenue          | $4,200,000 | $3,890,000 | -7.4% | ~/.agami/charts/...html |
| Active customers (Apr)   | 12,450     | 11,920     | -4.3% | ~/.agami/charts/...html |
```

Cell formatting:
- Numbers carry the same currency / magnitude suffix as the input where unambiguous (echo the user's `raw_value` for `Expected`, format `Got` with the same shape).
- `Δ` is the signed percent (red wins emphasized in chat by ✗ prefix if Markdown rendering allows; otherwise plain text).
- `Drill-down` links to the chart-template HTML for that query. Open these to see the full receipt — that's where the definitional disagreement lives.

For each mismatch row, surface a one-line interpretation under the table:

> **Q3 2025 Revenue** — agami reports $3.89M, your dashboard says $4.2M (-7.4%). Open the receipt; the metric `revenue` here is *gross of refunds in USD at invoice date*. If your dashboard nets refunds, that's the gap.

This is where the trust win lands. The DE doesn't have to chase the disagreement — the receipt + your interpretation does it for them.

### 3c — Errors block (if any)

```markdown
### Errors

| Label | What went wrong |
|---|---|
| Pipeline value (open opps) | Could not extract a single scalar — the question returned 47 rows. Try rephrasing or check that the metric exists in the model. |
```

### 3d — Matches summary (last, compact)

```markdown
### Matches (within ±1%)

7 numbers reproduced cleanly: Q3 2025 Orders, MoM growth, Avg order size, Top customer, Customer count by region, Refund rate, Pipeline count.
```

Don't dump every match's drill-down — they're not interesting. The matches build the case; the mismatches drive the conversation.

### 3e — Closing prompt

```
Re-run with `tolerance=5%` to see softer matches, or open any drill-down to find the definitional gap.
```

End the turn. The user typically:
- Opens a mismatch's drill-down, finds the definitional gap, says *"the dashboard is gross-of-refunds; can we update the metric?"* — chain into `/agami-save-correction` to update the metric definition.
- Asks `tolerance=5%` to widen the matches.
- Asks for a different CSV.

---

## Hard rules

1. **No automatic question generation for ambiguous labels.** If the label is too short or too vague (e.g., `Total`, `Number`, `Value`), surface to the user: *"Row 5's label is just 'Total' — too ambiguous to translate to a question. Skipping. Add more context to the CSV (e.g., `Total Revenue Q3` instead of `Total`) and re-run."* Don't guess.
2. **Receipt is non-optional.** Every per-row run MUST produce a chart-template HTML report with the trust receipt — that's what the drill-down link points at, and it's what makes mismatches actionable. If the underlying query path can't produce a receipt (legacy pre-trust-layer model), refuse with: *"This profile pre-dates the trust-layer launch. Re-run `/agami-connect` to enable receipts, then retry."*
3. **Don't write to the OSI model from this skill.** Reconcile reads + diffs; it never mutates. If a definitional disagreement surfaces and the user wants to update the metric, route them through `/agami-save-correction`.
4. **CSV stays local.** Don't upload, don't summarize-and-send. The reconcile run produces local artifacts (`/tmp/agami-reconcile-results-*.jsonl` + the per-query chart HTML) and nothing leaves the machine.

---

## Error handling cheat sheet

| Symptom | Action |
|---|---|
| `<csv>` doesn't exist | Refuse with one line: "File not found: `<path>`." |
| CSV has 0 parseable rows | Refuse: "No rows with parseable numeric values. Common cause: the value column has formatting like `$1,234.56 (USD)` — try simplifying to `1234.56`." |
| Every row errors out | Surface a meta-error: "All <N> rows errored — likely a model-coverage problem (the questions don't map to your schema). Run `/agami-connect reintrospect` if your schema changed; check the OSI model has the relevant tables." |
| Single mismatch but huge delta (> 100%) | Note in the interpretation: "The delta is large enough to suggest a unit mismatch (cents vs dollars, count vs percentage) rather than a definition gap. Check `agami.unit` on the relevant field." |
| User pastes inline CSV instead of a path | Accept it. Write to `/tmp/agami-reconcile-pasted-<ts>.csv` and proceed. |

---

## Roadmap (not in v1)

- **Vision input** — extract `(label, value)` pairs from a screenshot of a dashboard via Claude's vision capability. Currently CSV-only.
- **Tableau JSON export support** — parse `.twb` / `.twbx` exports directly.
- **Looker JSON / Mode export** — same.
- **Recurring reconcile runs** — wire into `agami test` so the golden-test suite includes reconciliation against a pinned dashboard CSV.
