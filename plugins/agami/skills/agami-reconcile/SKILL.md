---
name: agami-reconcile
description: "Reconciles known (label, expected_value) numbers from an existing dashboard against agami's answers. Input can be a SCREENSHOT of a Metabase / Power BI / Tableau / Looker dashboard (Claude's vision extracts the pairs), a CSV, or numbers pasted inline — the user doesn't need to know which; they can just ask. For each pair, the skill generates a matching NL question, runs it through the active profile's semantic model, diffs actual vs expected, and surfaces matches in green and mismatches in red with drill-down receipts. The strongest onboarding demo for a skeptical data engineer — either we agree with their numbers (trust earned via evidence) or we surface a real definitional disagreement (trust earned via transparency)."
when_to_use: "Use when the user says 'reconcile against this dashboard', 'do these numbers match?', 'validate against my Tableau export', '/agami-reconcile <csv>', drops a screenshot of a BI dashboard (Metabase/Power BI/Tableau/Looker/spreadsheet) and asks agami to reproduce the numbers, or pastes a CSV / table of known numbers. Also use when they hand over SQL they trust ('here is the SQL behind each tile', 'validate this query part by part', 'audit this SQL', 'here is the query we trust, check it') or a list of questions with no answers ('check these questions'); the person's query is graded part by part against the semantic model and the warehouse, never taken as the answer. Also use after a run, when the user says 'keep these as golden questions' or 'promote these to a golden dataset' — the rows that agreed are split between the examples agami reads when answering and the answer key later runs are scored against. Requires agami-connect to have been run first (need a semantic model + examples library). A high-leverage validation surface for a skeptical data team — reproduce their dashboard numbers, or surface the definitional gap."
argument-hint: "<screenshot | path-to-csv | pasted numbers>"
---

# agami reconcile

You are running the reconciliation harness. Goal: take the evidence a person brings and prove agami can reproduce each answer. That evidence is labeled numbers from an existing dashboard (Tableau / Looker / Mode / Metabase / Power BI / spreadsheet), most often a **screenshot**, sometimes a CSV or pasted list; or the SQL they trust; or a list of questions. When numbers match and every part of the person's statement checks out, that's evidence the semantic model is right. When they don't, the receipt drill-down and the part ledger explain why: a definitional disagreement (gross vs net, refunds in vs out, FX rate at booking vs reporting date), a join the semantic model is missing, or a mistake in the person's own query. That is exactly the trust signal that makes a DE relax.

**The person's query is evidence, never the answer.** Every part of it is graded against the semantic model and the warehouse before anything is compared or kept.

This skill orchestrates:

1. **Extract** the evidence rows from the input — a dashboard screenshot (via vision, confirmed with the user), a CSV, a pasted list, SQL the person trusts, or a list of questions. Number parsing is always deterministic (`reconcile.py`).
2. **Grade a supplied statement** part by part: run it the way agami runs its own SQL, then grade its joins, typed values, required filters and aggregates (Phase 1.5).
3. **Generate a matching NL question** for each label that has none.
4. **Run** each question through the same NL→SQL→execute pipeline as agami-query.
5. **Compare** actual vs expected with a tolerance, as one number or as a table, and name the part that differs.
6. **Present** a markdown table with per-row status; for mismatches, render the full receipt as a drill-down; for the person's statements, the parts that fell short. Write the findings to disk.

Spec for the deterministic helpers: [`scripts/reconcile.py`](../../scripts/reconcile.py) (input reader, number normalization, diff with tolerance, the part ledger and the findings). The shared procedure lives in [`shared/evidence-row.md`](../../shared/evidence-row.md), [`shared/part-ledger.md`](../../shared/part-ledger.md) and [`shared/statement-check.md`](../../shared/statement-check.md).

## Conversation style

- **Tight loops.** This skill is a tool, not a tutorial. One question per turn, max two sentences of prose between phases.
- **Surface mismatches loud.** A reconcile run with 9/12 matches and 3 mismatches is a SUCCESSFUL run — the mismatches are the value. Lead with what didn't match.
- **Don't paste raw SQL in chat.** The receipt has it. Same hard rule as agami-query — with one exception, Phase 3e's promotion offer, where the statement is shown because it is the thing being accepted into an answer key and cannot be hidden behind a receipt link at the moment somebody agrees to replay it.

---

## Phase 0: Preflight

Same checks as agami-query / agami-connect:

1. **Plan-mode check** per [`shared/plan-mode-check.md`](../../shared/plan-mode-check.md). This skill needs Bash + Read + Write — refuse if locked in plan mode. **DO NOT write a plan file. DO NOT call `ExitPlanMode`.** Refusal text: *"I can't reconcile in plan mode — each row runs a live query and writes a receipt. Switch to **Auto** or **Edit Automatically** mode (Shift+Tab to cycle) and re-invoke me with the same input."*
2. **Credentials present** — read `<artifacts_dir>/local/credentials` for the active profile. If missing, invoke `/agami-connect` to set up first; this skill needs a working DB connection.
3. **Model present** — `<artifacts_dir>/<profile>/datasource.yaml` must exist. If not, invoke `/agami-connect`. This skill needs an introspected model to generate questions against.
4. **Input — accept any of four shapes, or a mix; the user needn't know which.** Detect what they gave:
   - **A screenshot / image** of a dashboard (Metabase, Power BI, Tableau, Looker, a spreadsheet) — the common case. Go to Phase 1's **vision branch**.
   - **A CSV** — a path in `$ARGUMENTS`, or pasted inline (write inline CSV to `/tmp/agami-reconcile-<ts>.csv`). Go to Phase 1's **CSV branch**. A third column holding SQL is the statement behind each tile: Phase 1n reads it as a statement, never as part of the label.
   - **Numbers pasted inline** as a list/table — treat as inline CSV.
   - **SQL the person trusts** — one or more statements, pasted or in a `.sql` file, alone or beside the question each answers. Go to Phase 1's **statement branch**. The statement is evidence, never the answer: Phase 1.5 grades every part of it before it is compared with anything.
   - **A list of questions** with no answers — one per line. Go to Phase 1's **questions branch**.
   A screenshot and the SQL behind its tiles may arrive together; Phase 1n joins them by label. If they gave nothing (or just asked "can you check my dashboard?"), ask once, welcoming all four: *"Show me what you want to check against — easiest is a **screenshot of your dashboard** (Metabase, Power BI, Tableau, a spreadsheet — whatever you have), but a CSV, a pasted list of `label: value`, the SQL you trust, or a list of questions works too."* Don't make them figure out an export format.
5. **If they gave a file path, validate it exists.** If not, surface the error and stop.

---

## Phase 1: Extract the (label, value) pairs

Whatever the input shape, the goal is the same normalized rows JSON. **Number parsing is always deterministic — it goes through `reconcile.py`, never the LLM eyeballing a value** (a misread expected number would manufacture a false mismatch on a verification surface).

### Vision branch — a dashboard screenshot

1. **Read the image** and extract every labeled number you can see — KPI tiles, table cells, chart value labels — as `(label, raw_value)` pairs. Keep the label the user would recognize ("Total Revenue", "Active Users — Apr"), and the value **exactly as shown, verbatim** (`$4.2M`, `₹2.16Cr`, `42%`, `1,234`) — don't convert it; the normalizer does that.
2. **Write the pairs as a 2-column CSV** (Write tool — never a heredoc/`python3 -c`) to `/tmp/agami-reconcile-<ts>.csv`, then run the SAME normalizer as the CSV branch (below) so value parsing stays deterministic.
3. **Confirm before reconciling — vision can misread.** Show the extracted pairs as a small table and ask the user to fix any misread label/number: *"I read these N numbers off your screenshot — correct anything I got wrong, then I'll reconcile."* This confirm step is **mandatory**: a wrong expected-value isn't a model bug but it reads like one. If a tile is ambiguous or partly cut off, say so and skip it rather than guess.

### CSV branch — a CSV path or inline-pasted CSV

```bash
python3 "$AGAMI_PLUGIN_ROOT/scripts/reconcile.py" parse --csv "<csv_path>" \
  > /tmp/agami-reconcile-rows-<ts>.json
```

The helper (used by **both** branches) handles:
- Header detection (with-or-without first-row column names)
- 2-column or 3+ column inputs (3rd onward are appended to the label as context)
- Currency symbols / magnitude suffixes / accounting parens / percent (`$4.2M`, `₹2.16Cr`, `(123.45)`, `42%`)
- Null sentinels (`n/a`, `—`, blank)

Read the JSON. Each row is `{label, expected_value, raw_value}`. Discard rows where `expected_value` is null (unparseable) — surface a one-liner: *"Skipped 2 rows where the value couldn't be parsed: 'X', 'Y'."*

### Statement branch — SQL the person trusts

If the SQL was pasted, write it to `/tmp/agami-reconcile-<ts>.sql` with the Write tool (several statements separated by `;`); if it came as `question,sql` pairs, write those as a CSV. Take a file path as given. A statement that arrives without a question needs one before agami can be asked the same thing: derive the question the statement answers, show it, and let the person correct the wording. **Do not run the statement here.** It is evidence, and Phase 1.5 is where its parts are graded.

### Questions branch — a list of questions and no answers

Write the lines to `/tmp/agami-reconcile-<ts>.txt` with the Write tool, one question per line. These rows carry no expected value: Phase 2 asks agami each question, and until the person grades the answers there is nothing to compare against. Say so up front: *"No answers to compare with, so I'll ask agami each one and you grade what comes back."*

### 1n — Normalize every input into evidence rows

```bash
python3 "$AGAMI_PLUGIN_ROOT/scripts/reconcile.py" intake --file <path> [--file <path> ...] \
  --source "<the person's own words for where this came from>" > /tmp/agami-reconcile-rows-<ts>.json
```

`parse` stays the number-only reader; `intake` reads everything `parse` reads and the other shapes too, so a run with any SQL or questions in it goes through `intake` for all of its files, the vision CSV included. One row per thing to check, `{label, question, statement, expected, raw_value, provenance}`, with any of the three evidence fields missing. Several files merge by label under a case-and-whitespace fold, so the SQL behind a tile joins the tile's row. Exit `2` is a file that could not be opened; exit `4` is an input with no question, statement or number anywhere. The shapes, the detection rules and the row are in [`shared/evidence-row.md`](../../shared/evidence-row.md).

Create the run directory `<artifacts_dir>/local/reconcile/<ts>/`, and `rows/<n>/` under it for every row that carries a statement.

Surface to the user:
> Parsed `<N>` rows from `<the screenshot / csv_path / the statements / the questions>`. Reconciling now — typically `<N> × 5–15s` per row depending on query latency.

---

## Phase 1.5: Grade a supplied statement, part by part

For every row that carries a `statement`. Skip this phase for a row that does not.

**The person's statement runs on the road agami's own SQL runs, with the same guards.** [`shared/statement-check.md`](../../shared/statement-check.md) is the procedure, step by step, and [`shared/part-ledger.md`](../../shared/part-ledger.md) is what each step's output means. In short, working in `rows/<n>/` with `statement.sql` written first:

- **1.5a — Is it one read-only SELECT?** Per [`shared/sql-generation-rules.md`](../../shared/sql-generation-rules.md). Anything else is refused: write `run.json` with `status: "not_run"`, mark the row `error`, and probe nothing.
- **1.5b — Run it the way agami runs its own.** `sm prepare` first, then the profile's tier exactly as `agami-query` Phase 1e tabulates it (psql, mysql, snowsql, sqlite3, DuckDB, or `execute_sql`), never `--no-safety`, and always with the statement passed **by file**, never inline in a shell string. Write stdout to `statement.csv`, and into `run.json` the `status`, `exit`, classifier `kind`, guard `rule` and `remediation`, never the raw stderr. **A refusal is a finding, not a crash**: a `table_scope` or `column_scope` refusal grades `scope: model_gap`, because the person wanted a table or column the semantic model does not expose; `select_star` grades `runs: query_defect`. Never rewrite the statement and never retry. Other failures go through [`shared/db_error_classifier.md`](../../shared/db_error_classifier.md); `auth`, `dsn`, `network` and `permission` stop the run as `agami-query` Phase 3b stops it. Nothing in this phase writes `query_log.jsonl`: `agami-save-correction` reads that log's last successful line as the question to correct, and a probe there would be corrected instead of the answer. The phase keeps its own record instead: `run.json` for the statement and a `.run.json` beside every probe's CSV, so every execution and every refusal here is written down.
- **1.5c — Its receipt.** `sm receipt "$ROOT" --sql-file statement.sql > statement-receipt.json` whenever the statement parsed.
- **1.5d — Probes.** `sm join-probes` and `sm filter-values plan` emit SQL; write each emitted probe to its own `.sql` file and run it through the same tier by path, each to the CSV `part-ledger.md` names with a `.run.json` beside it, then `sm filter-values judge`. A probe the tier refuses or fails leaves an empty CSV; leave it there, the ledger reads it as a probe that failed. A file the ledger expects and does not find, or finds empty, is a part it grades `unresolved`, never clean.
- **1.5e — The ledger.** `python3 "$AGAMI_PLUGIN_ROOT/scripts/reconcile.py" ledger --row-dir rows/<n>` writes `ledger.json`: one grade per part, `confirmed`, `model_gap`, `query_defect` or `unresolved`, and the weakest grade as the row's `ledger_verdict`; a part may also be `noted`, a fact the run states and never judges, which never decides the verdict. A part reaches `model_gap` only by measurement; the statement asserting something is never the evidence for it.
- **1.5f — Its result is the expected value.** For a row that came with no number, `expected` is the single cell `statement.csv` returned (its text folded to a number by `reconcile.py diff`, which reads `$4.2M` and `47,238,221.00` alike), or the table's shape when it returned several rows. For a row that came with a tile number too, run `reconcile.py diff` between the tile and the statement's own result: a disagreement means the statement is not the tile's statement, or the data moved; flag the row in Phase 3b.5 and keep the tile's number as `expected`.

---

## Phase 2: Generate questions + execute

For each row in the parsed list:

### 2a — Generate the NL question

A row that already carries a `question` (a `question,sql` pair, or a statement whose question the person confirmed in Phase 1) uses it as written; do not generate a second one. Otherwise use the LLM to translate `label` (+ context if present) into the most natural English question whose answer should be `expected`. Examples:

| label | question |
|---|---|
| `Q3 2025 Revenue` | "What was total revenue in Q3 2025?" |
| `Active customers (Apr 2026)` | "How many active customers did we have in April 2026?" |
| `Pipeline value (open opps)` | "What's the total pipeline value across open opportunities?" |
| `Mean order size last 30 days` | "What's the average order size over the last 30 days?" |

The semantic model + examples library are loaded; let the LLM pick the right subject areas / entities / metrics that resolve the labeled term to a concrete query.

### 2b — Run via the agami-query pipeline

Invoke the same SQL-generation + execution path agami-query uses (Phases 2 + 3 of that skill — see [`agami-query/SKILL.md`](../agami-query/SKILL.md)). Capture:

- The generated SQL
- The result (should be a single scalar, or a single row)
- The full chart-template HTML report (so the user can drill in for mismatches)
- The trust receipt (with confidence, signed-off-by, etc.)

The SQL you capture here is the one that lands in the row record (Phase 2d) — keep it verbatim. It is the only place the statement survives the run: the chart report at `report_path` is HTML, and nothing re-derives the statement from it afterwards.

If the SQL fails OR the result isn't a single scalar (e.g., the LLM-generated question returned a multi-row table), capture an error: `Could not extract a single scalar from the result.` These rows show up as `error` status in the report. One case is different: a row that carries the person's `statement` may legitimately return a table. Keep `recorded` as `{"columns": [...], "rows": []}` for it (the columns, never the rows), write the result CSV to `rows/<n>/actual.csv`, and let Phase 2e compare it as a table.

### 2c — Diff

```bash
python3 "$AGAMI_PLUGIN_ROOT/scripts/reconcile.py" diff \
  --expected "<expected_value>" \
  --actual "<actual_value_from_query>" \
  --tolerance 0.01
```

Default tolerance: ±1%. The user can override with `tolerance=N%` in their original ask (e.g., "reconcile with 5% tolerance"). Tolerance applies to numeric comparisons; for text values (rare), use exact match.

Capture: `match` (bool), `delta`, `delta_pct`. When `expected` came from running the person's statement (Phase 1.5f), diff against it exactly the same way; the statement's result is the expected value, and its grades say how far to trust it.

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
  "status":       "match" | "match_unverified" | "mismatch" | "expected_doubtful" | "error",
  "report_path":  "<artifacts_dir>/local/charts/<profile>/<ts>.html",  // the full chart report for this query
  "sql":          "<the statement that produced actual, or null on an error row>",
  "recorded":     {"columns": ["<column name>"], "rows": [[<value>]]},  // what the query actually returned
  "error":        "<message if status=error, else null>",
  "provenance":   {"shape": "a|b|c|d", "source": "<the person's words>", "file": "...", "line": 2, "graded": null},
  "statement":    "<the person's SQL, verbatim, or null>",
  "statement_recorded":     {"columns": ["..."], "rows": [[<one cell>]]} | {"columns": ["..."], "row_count": 12} | null,
  "statement_receipt_path": "<rows/<n>/statement-receipt.json, or null>",
  "receipt_path": "<the receipt of agami's own statement>",
  "ledger":       [<the graded parts, from reconcile.py ledger>] | null,
  "ledger_verdict": "confirmed" | "model_gap" | "query_defect" | "unresolved" | null,
  "comparison":   {"scalar": <the diff>} | {"result_set": <the compare-results score>} | null,
  "claims":       <the sm claims diff between the two statements, or null>,
  "finding_keys": ["<keys of the findings this row contributed to>"]
}
```

The keys from `provenance` down are appended after `error` and every earlier key keeps its meaning; a reader that only knows the older shape keeps working. `status` gains two values beside the three it had. Their rules, applied by code and never by feel:

```bash
python3 "$AGAMI_PLUGIN_ROOT/scripts/reconcile.py" status --match <true|false|none> --ledger-verdict <verdict|none>
```

| Status | When |
|---|---|
| `match` | the numbers agree, and every graded part is `confirmed` (or there was no statement to grade) |
| `match_unverified` | the numbers agree, but a part of the person's statement is not `confirmed`. Phase 3e never sees it: a match nobody could verify may be luck |
| `mismatch` | the numbers differ and the person's statement has no `query_defect`, so agami is the likelier culprit |
| `expected_doubtful` | the numbers differ and the person's statement has a `query_defect`, so the expected value itself is in doubt. Kept out of the mismatch tally |
| `error` | the row could not run |

`sql` is the statement captured in Phase 2b, written down verbatim. `recorded` is the result it returned, shaped as `columns` + `rows` — the same two keys the golden-dataset receipt uses — so whoever picks this row up later forwards it as-is instead of rebuilding it from a number and guessing at a column name.

**On a `status: "error"` row both `sql` and `recorded` are `null`.** There is no statement to keep: either none was generated, or the one that was didn't produce a scalar anyone read. An error row therefore carries nothing a later reader could mistake for a verified answer.

Append all records to `/tmp/agami-reconcile-results-<ts>.jsonl` so the user can inspect later. The two keys are additive — a reader that only knows the older shape keeps working.

### 2e — Compare, as a number or as a table, and name the part that differs

For a row whose `expected` is one number, Phase 2c's diff is the comparison. For a row whose statement returned a table, compare the two result CSVs through the golden comparator, so a table-shaped answer is judged the way an answer key is:

```bash
bash "$AGAMI_PLUGIN_ROOT/scripts/sm" compare-results "$ROOT" \
  --golden-csv rows/<n>/statement.csv --generated-csv rows/<n>/actual.csv \
  --match values --golden-sql-file rows/<n>/statement.sql > rows/<n>/comparison.json
```

`accuracy` of `1.0` is a match. The person's statement is handed over as `--golden-sql-file` for one reason only: whether it ordered its rows.

When the row carries both statements, name where they differ before anyone reads two receipts side by side:

```bash
bash "$AGAMI_PLUGIN_ROOT/scripts/sm" claims "$ROOT" --sql-file rows/<n>/agami.sql --against-sql-file rows/<n>/statement.sql > rows/<n>/claims.json
python3 "$AGAMI_PLUGIN_ROOT/scripts/reconcile.py" ledger --row-dir rows/<n> --with-claims
```

The seven claims (tables, filter predicates, date window, group keys, join keys, ordering, limit) say which part differs; they never say who is right. Then set the row's `status` with `reconcile.py status`, from the diff's `match` and the ledger's verdict. For a table there is no `diff`: pass `--match true` when `compare-results` reports `accuracy` of `1.0`, `false` otherwise, and `none` when it could not score.

### 2f — Write the findings

Write every row record to `<artifacts_dir>/local/reconcile/<ts>/rows.jsonl` as well as the `/tmp` file, then:

```bash
python3 "$AGAMI_PLUGIN_ROOT/scripts/reconcile.py" findings --run-dir "<artifacts_dir>/local/reconcile/<ts>"
```

It writes `findings.json` (one entry per place the semantic model was shown to be missing or wrong, with every row that showed it), `query_defects.json` (the parts of the person's statements the data proved wrong, listed apart so nothing about the semantic model is proposed from them), and `ledger.json`. Question text and SQL only, never result rows beyond the one recorded cell. This is reconcile's own record for the person; nothing reads it but them.

---

## Phase 3: Present

### 3a — Summary line first

```
Reconciled <N> numbers: <M> match (within ±1%), <K> mismatch, <E> error.
```

When any row carried a statement, add one more line, counting the two statuses that belong to neither `<M>` nor `<K>`:

```
<U> agreed but a part of your statement could not be confirmed (match_unverified); <D> expected values are in doubt because your statement had a defect (expected_doubtful).
```

### 3b — Mismatches table (lead with what didn't match)

Render the mismatches as a markdown table BEFORE the matches:

```markdown
### Mismatches

| Label | Expected | Got | Δ | Drill-down |
|---|---:|---:|---:|---|
| Q3 2025 Revenue          | $4,200,000 | $3,890,000 | -7.4% | <artifacts_dir>/local/charts/&lt;profile&gt;/...html |
| Active customers (Apr)   | 12,450     | 11,920     | -4.3% | <artifacts_dir>/local/charts/&lt;profile&gt;/...html |
```

Cell formatting:
- Numbers carry the same currency / magnitude suffix as the input where unambiguous (echo the user's `raw_value` for `Expected`, format `Got` with the same shape).
- `Δ` is the signed percent (red wins emphasized in chat by ✗ prefix if Markdown rendering allows; otherwise plain text).
- `Drill-down` links to the chart-template HTML for that query. Open these to see the full receipt — that's where the definitional disagreement lives.

For each mismatch row, surface a one-line interpretation under the table:

> **Q3 2025 Revenue** — agami reports $3.89M, your dashboard says $4.2M (-7.4%). Open the receipt; the metric `revenue` here is *gross of refunds in USD at invoice date*. If your dashboard nets refunds, that's the gap.

This is where the trust win lands. The DE doesn't have to chase the disagreement — the receipt + your interpretation does it for them.

### 3b.5 — Your statements (where the evidence itself fell short)

Only when a row carried a statement, and only for the parts that did not grade `confirmed`. One rule throughout: no SQL in chat; the part's name and its note are enough, and `findings.json` holds the rest. Three blocks, because the three kinds of part answer three different questions, and a reader scanning one table counts every row as a problem with the query. The table holds the grades that judge; the two blocks under it hold what was not judged.

```markdown
### Your statements

| Label | Part | Grade | What the ledger found |
|---|---|---|---|
| Delivered orders | literal: orders.status = 'Delivered' | query_defect | not one of the values the semantic model lists, and no row holds it; did you mean 'delivered' |
| Paid revenue     | join: orders-payments              | query_defect | the join is on a different key than the one the semantic model declares |
| Q3 Revenue       | default_filter: orders             | model_gap    | the semantic model declares this filter and the statement does not apply it |
| Open items       | values_declared: items.state       | model_gap    | the column holds 6 distinct values and the semantic model lists none of them |

**What couldn't be checked**
- Q3 Revenue, fan_out: SUM(total): the pre-flight could not bind this aggregate to one table: a column inside the aggregate could not be attributed to one table

**What this run noticed**
- Open items, dropped_rows: items-users: 3 of 8,345 items rows have no users partner and are dropped by this inner join; counted over the whole table, before the statement's own filters
```

Say the kinds apart in one sentence each: a `query_defect` is the person's to fix, and nothing about agami changes because of it; a `model_gap` is a place the data proved their statement right where the semantic model is missing or wrong, and a single fix still goes through `/agami-save-correction` (an undeclared value list is a field's `choice_field`, the `field_metadata` route). An `unresolved` part is neither: it says what could not be checked and why, and it sits in its own block so nobody counts it as a defect. A `noted` part is not a grade at all: a fact the run states and never judges, in the last block.

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

### 3e — Offer promotion

The rows that agreed are the most reusable thing this run produced: a question, the statement that answered it, and a number the user's own dashboard already vouches for. Nothing else in the product carries evidence from outside agami. So keep them — and keep them in **both** of the places they are worth keeping.

**They are worth two different things, and one row cannot be both.**

- A **golden item** tests the model: a later run tells you if that number drifts.
- A **prompt example** teaches it: questions like it get answered this way from now on.

A row written to both is a test grading its own study material — the example ranks first for its own question, the model reproduces it, and the item can only ever pass. So the batch is split rather than duplicated. They came from one dashboard, so they are the same family of question, which is exactly what makes holding some back meaningful.

**You do the splitting, not the user.** Which rows should teach depends on what the example library already covers, which is not something anybody can answer without reading it. `sm examples --query` already answers it, and its `high_confidence` flag is the product's own judgement of "we have a close example for this".

For each agreeing row, rank its question across the areas:

```bash
bash "$AGAMI_PLUGIN_ROOT/scripts/sm" examples "$ROOT" --area <area> --query "<the row's question>" --top-k 1
```

- **`high_confidence: false`** → the library does not cover this shape. **Teach with it** — it goes to the examples.
- **`high_confidence: true`** → already covered, so another example adds nothing. **Test with it** — it goes to the golden dataset.

Two adjustments, and both are about not splitting something too small to split:

- **Fewer than four agreeing rows → all of them become golden items.** A split of three leaves too little on either side to be worth the explanation.
- **Never send more than half the batch to the examples.** This is a cap, not a preference, and it is the rule that makes the split survive an empty library: with nothing curated, every row reads as novel and the base rule would teach with all of them, leaving the golden dataset empty. That is exactly the profile reconcile is aimed at, and an onboarding run that writes no test at all has failed at the thing it was asked to do. When the cap bites, keep the highest-scoring rows as examples and the rest as tests.

**Make the offer once, here, after the summary. Never per row.** A per-row prompt turns a twelve-number reconcile into twelve interruptions and buries the mismatches, which are the point of the run.

**Say what goes where. This is not optional.** Writing to the examples changes how agami answers future questions, and that must not happen silently behind a button that says "keep these numbers". One line, in what it does for them:

> Ten of these agreed — I'll keep all ten.
> **Four as examples**, so questions like them get answered this way from now on.
> **Six as tests**, with a ±<the run's tolerance> band, so you're told if any of those numbers move.
> Save all ten?

Someone who presses enter without reading gets the right outcome; someone who reads it learns the distinction by watching it happen, which is the only way anybody will. The override is one line — "let me choose" — and not twelve prompts.

**Only rows whose `status` is `match` are offered.** That is the run's own tolerance — `reconcile.diff` decided it back in Phase 2c, and it is the only notion of agreement this skill has, so nothing here re-judges a number. One predicate drops `mismatch` and `error` together, and with them the `missing_expected` and `missing_actual` rows — those are `reconcile.diff`'s own reasons rather than a row status, and a row that could not be diffed never reached `match` either. **A row with no statement is never offered**: an error row carries `sql: null` (Phase 2d), so there is nothing to replay and nothing worth promoting.

**Only a single-cell result is offered.** A row whose `recorded` carries more than one column has no single number to band, and a `bounded` item over a wider result is scored on its row count alone — it would pass forever without ever checking the number it was promoted for.

**If no row agreed, make no offer at all** — not an empty one, not "there's nothing to promote here". A run where nothing matched is having a different conversation (Phase 3b), and an offer with nothing in it interrupts it.

**Every agreeing row starts selected.** The person reviewed each row's agreement as it landed; asking them to opt each one back in asks the same question twice. They can deselect any row, and **they can edit any question before it is written** — a later run regenerates SQL from the question, so the wording *is* the item.

**Show the statement and the result, not just the number.** Per row: the question (editable), the statement that answered it, and the recorded result. What they are accepting is that this statement answers this question — that is what gets replayed — and a row accepted on its number alone is a row nobody checked the meaning of.

**Declining writes nothing.** No dataset is created, no file is touched, and the run ends exactly where Phase 3f leaves it. Say that when you ask, and say it again if they decline.

#### A question asked relative to today

The save door refuses a question asked over a window that slides when its statement is pinned to fixed dates — exit `2`, nothing written. That is the **common** case here, not the exotic one: Phase 2a's own example turns `Mean order size last 30 days` into *"What's the average order size over the last 30 days?"*, and the statement that answered it names the thirty days that were current when it ran.

Two things get past the refusal and **only one of them is right**:

- **Ask which window the question means, and rewrite the question to name it.** *"'The last 30 days' was 2–31 August 2026 when this ran — save it as '…in August 2026'?"* The statement stays exactly as it ran, the question finally names the window it always meant, and the item stays true for as long as it exists.
- **Never re-anchor the statement to `CURRENT_DATE`.** It clears the lint and it is a **trap**: the band was recorded around today's value of a window that slides, so next month the same question asks about different days, returns a different number, and fails against a band nobody moved. That is a false alarm on the one surface whose whole job is to be believed.

So the offer edits the **question**, with the person's answer to "which window?". It never edits the SQL.

**Ask which window it means in the offer, before anything is written** — not after the refusal lands. A batch is written one row at a time (below), so a refusal on the seventh row arrives with six items already on disk: **items already written stay written, and nothing is rolled back**. The cheat sheet's exit-`2` row is the fallback for a question that slips through, not the plan.

#### What gets written, per row kept as a test

Write the item with the **Write tool** — never a heredoc, never `python3 -c`, per [`shared/invocation-conventions.md`](../../shared/invocation-conventions.md) — to `/tmp/agami-golden-item-<ts>.json`:

```json
{
  "query": "What was total revenue in Q3 2025?",
  "sql": "<the row's `sql`, verbatim>",
  "match": "bounded",
  "bounds": {"min_rows": 1, "max_rows": 1, "min_value": 3851100.0, "max_value": 3928900.0},
  "tags": ["reconciled"],
  "confirmed_by": {"method": "reconciled against the finance dashboard on 2026-08-31; agreed within ±<the run's tolerance>"}
}
```

- **`id` is omitted on purpose.** The save door derives it from the question, exactly as the import door does, so a promotion lands **on** an already-imported question of the same wording rather than beside it as a second copy.
- `sql` comes from the row record (Phase 2d) as it stands, verbatim. `recorded` is omitted — the save door stamps it itself and never takes it from this payload, because the golden-datasets directory has no gitignore exclusion and this row's `recorded` is the finance dashboard's own numbers.
- `match: "bounded"` because a reconciled number is one that legitimately moves. `bounded` with no band is refused (exit `2`), which is why the band below is not optional.
- **`bounds` comes from the helper, never from arithmetic written in prose:**

  ```bash
  python3 "$AGAMI_PLUGIN_ROOT/scripts/reconcile.py" band \
    --value "<the row's actual>" --tolerance <the run's tolerance>
  ```

  Pass the same tolerance the run diffed with. The four keys it prints *are* the `bounds` block — paste them in unchanged.

Then call the save door. It is the only writer of a golden dataset anywhere in the plugin, and this skill does not become a second one:

```bash
python3 "$AGAMI_PLUGIN_ROOT/scripts/golden_author.py" save \
  --profile <profile> --dataset <stem> \
  --item /tmp/agami-golden-item-<ts>.json \
  > /tmp/agami-golden-save-<ts>.json
```

`<stem>` is the dataset's **filename stem** — one plain name (`reconciled`), never a path and never `reconciled.yaml`. Ask which dataset once, for the whole batch.

**One call per kept row.** The door takes one item, not an array — ten kept rows are ten runs of that command, each with its own item file. The dataset is asked once; the writing is a loop.

**Pass `--confirm-convention` on a promoted row.** The save door checks a statement against the profile's own examples and stops when they disagree, which is right when somebody is authoring a key by hand. Here it is redundant and wrong: the statement being saved is the one agami itself generated *from* those examples minutes ago, and a stop would ask the person to confirm a convention they never departed from.

#### What gets written, per row kept as an example

Write the pair with the **Write tool** to `/tmp/agami-reconciled-example-<ts>.json` — a JSON **array**, one entry per row going to the examples:

```json
[{"question": "What was total revenue in Q3 2025?", "sql": "<the row's `sql`, verbatim>",
  "tables": ["<from the row's statement>"], "source": "reconcile", "status": "confirmed",
  "created_at": "<ISO8601 UTC>"}]
```

Then the packaged writer, which dedups by question and creates the library if it is absent:

```bash
bash "$AGAMI_PLUGIN_ROOT/scripts/sm" add-example "$ROOT" --area <area> --file /tmp/agami-reconciled-example-<ts>.json
```

`<area>` is the subject area whose tables the statement reads — the same choice `agami-save-correction` makes, and the same writer. **Do not hand-edit `examples.yaml`**, and do not invent a second way in.

`status: "confirmed"` is honest here in a way it rarely is: the number was verified against a source outside agami. That is stronger evidence than the recollection behind most curated examples.

#### `confirmed_by.method` — two shapes, kept apart

The method line is what somebody consults a year later to find out where this item's authority came from, so the two ways a number reaches a dataset from here read differently on purpose. Each names **the source, the date and the tolerance**:

- **Agreed** — the row matched and the person accepted the offer:

  > `reconciled against <source> on <date>; agreed within ±<tolerance>`

- **Resolved** — the row did *not* match, and the person judged agami right anyway:

  > `reconciled against <source> on <date>; disagreed beyond ±<tolerance>, resolved in agami's favour by the analyst`

`<source>` is what the numbers came from, as the user described it ("the finance dashboard", "the Q3 export"); `<date>` is the day the run happened.

**The resolution path is not part of the offer.** A disagreeing row is not offered and cannot be written by accepting the offer — the selection has no room for one. If the person opens a mismatch, decides their dashboard is wrong and agami is right, they have to say so explicitly, as its own step, and it writes with the *resolved* method above. **That friction is deliberate**: promoting a mismatch writes an answer key that contradicts the number the team currently believes, and that should cost a sentence rather than a checkbox.

#### If the item already exists

**Read which payload the `1` carries before you answer it — there are two, and the wrong flag clears neither.** A `needs_confirmation_convention` payload means the statement departs from the profile's own curated examples; on a promotion that is expected and not interesting, because the statement being saved is the one agami generated *from* those examples minutes ago. Pass `--confirm-convention` for it. Everything below is the other payload.

Exit `1`, a `needs_confirmation` payload, and **nothing written**. Render the `before` AND the `after` for every id — the item on disk and the one that would take its place — ask, and only on an explicit yes re-run the **same command with `--confirm-replace` appended**. **Never pass `--confirm-replace` pre-emptively**: the flag means one thing, that a person saw both sides and said yes, and passing it before that has happened makes the stop decorative.

A replacement is wholesale — the item sent is the item written — so read the `before` and carry forward whatever it holds that still applies (its `tags`, a `must_filter`) into the item JSON before you re-run. On a no, say the file is untouched and stop.

### 3f — Closing prompt

```
Re-run with `tolerance=5%` to see softer matches, or open any drill-down to find the definitional gap.
```

End the turn. The user typically:
- Opens a mismatch's drill-down, finds the definitional gap, says *"the dashboard is gross-of-refunds; can we update the metric?"* — chain into `/agami-save-correction` to update the metric definition.
- Asks `tolerance=5%` to widen the matches.
- Asks for a different CSV.
- Takes the promotion offer from Phase 3e, and the rows that agreed become a golden dataset later runs are scored against.
- Asks about a part in the "Your statements" table. Point at `<artifacts_dir>/local/reconcile/<ts>/findings.json` and `query_defects.json`; a fix to one definition goes through `/agami-save-correction`, which grades a pasted statement with the same ledger.

---

## Hard rules

1. **No automatic question generation for ambiguous labels.** If the label is too short or too vague (e.g., `Total`, `Number`, `Value`), surface to the user: *"Row 5's label is just 'Total' — too ambiguous to translate to a question. Skipping. Add more context to the CSV (e.g., `Total Revenue Q3` instead of `Total`) and re-run."* Don't guess.
2. **Receipt is non-optional.** Every per-row run MUST produce a chart-template HTML report with the trust receipt — that's what the drill-down link points at, and it's what makes mismatches actionable. If the underlying query path can't produce a receipt (legacy pre-trust-layer model), refuse with: *"This profile pre-dates the trust-layer launch. Re-run `/agami-connect` to enable receipts, then retry."*
3. **Don't write to the semantic model from this skill.** Reconcile reads + diffs; it never mutates a metric, a join, a column or any other part of the semantic model. If a definitional disagreement surfaces and the user wants to update the metric, route them through `/agami-save-correction`. **The writes this skill can make are Phase 3e's, and neither is a semantic-model write:** a golden dataset is the answer key that *tests* the semantic model, and the prompt-example library is what the AI *reads* before it writes SQL. Neither is the semantic model's own definitions, and this skill still never touches a metric, a join, a column or a default filter. Both writes go through the packaged writers — `golden_author.py save` and `sm add-example`, those doors and nothing else, never a hand-edited YAML — and both need the person's yes in front of them. A row reaches either one by exactly two routes: this run scored it as agreeing, or it scored a mismatch that the person has explicitly resolved in agami's favour. **No row goes to both**, which is the whole reason Phase 3e splits the batch rather than duplicating it. The skill now also writes a findings file under `<artifacts_dir>/local/reconcile/<ts>/`: findings for a person to read, in the ignored half of the artifacts, never a metric, a join, a column or a default filter. A `query_defect` in the person's statement never becomes a finding about the semantic model at all.
4. **CSV stays local.** Don't upload, don't summarize-and-send. The reconcile run produces local artifacts (the per-query chart HTML, and `/tmp/agami-reconcile-results-*.jsonl`, which now carries the statement behind every row as well as its numbers) and nothing leaves the machine. A promoted row stays local too: the save door writes into the profile's own `golden_datasets/` directory on this machine.

---

## Error handling cheat sheet

| Symptom | Action |
|---|---|
| `<csv>` doesn't exist | Refuse with one line: "File not found: `<path>`." |
| CSV has 0 parseable rows | Refuse: "No rows with parseable numeric values. Common cause: the value column has formatting like `$1,234.56 (USD)` — try simplifying to `1234.56`." |
| Every row errors out | Surface a meta-error: "All <N> rows errored — likely a model-coverage problem (the questions don't map to your schema). Run `/agami-connect reintrospect` if your schema changed; check the model has the relevant tables." |
| Single mismatch but huge delta (> 100%) | Note in the interpretation: "The delta is large enough to suggest a unit mismatch (cents vs dollars, count vs percentage) rather than a definition gap. Check `agami.unit` on the relevant field." |
| User pastes inline CSV instead of a path | Accept it. Write to `/tmp/agami-reconcile-pasted-<ts>.csv` and proceed. |
| Screenshot is blurry / a value is cut off / can't read a tile | Don't guess the number. Extract what's legible, and tell the user which tiles you skipped: "Couldn't read 'Pipeline value' clearly — re-snip it or type that one in." |
| User says "reconcile my dashboard" but attaches nothing | Ask for the screenshot (or CSV / pasted numbers / the SQL they trust / a list of questions) per Phase 0.4 — don't proceed without something to check. |
| `reconcile.py intake` exits `4` | Nothing usable: no question, statement or number anywhere in the input. Say so in one line and ask again per Phase 0.4. |
| The person's statement is refused by the scope gate (`table_scope`, `column_scope`) | Not a crash. Write `run.json` with `status: "refused"` and the rule; the ledger grades `scope: model_gap`. Tell the person the semantic model does not expose that table or column. Never rewrite or retry the statement. |
| The person's statement fails the zero-row check (`table_not_found`, `column_not_found`, `syntax`) | The person's defect. `runs: query_defect`; nothing else is probed for that row. Show the one-line classifier remediation. |
| `sm filter-values plan` marks a value `sensitive` or unquotable | No probe is emitted for it, by design. The value is checked against the semantic model's list only; the grade is `unresolved` if the list cannot answer. Say why in the "Your statements" table. |
| A probe's CSV is empty | The tier refused or failed it. Leave the file; the ledger grades that part `unresolved` and says the probe likely failed. Do not delete it and do not re-run with weaker guards. |
| A promotion exits `0` | Written. Report `added` / `replaced` and the path, and say the dataset can be run with "run the evals". |
| A promotion exits `1` with `needs_confirmation` | Nothing was written. Render the `before` and the `after` for every id, carry forward the `tags` / `must_filter` the `before` holds, ask, and re-run with `--confirm-replace` only on an explicit yes. On a no, the file is untouched. |
| A promotion exits `1` with `needs_confirmation_convention` | The statement departs from the profile's own examples. On a promotion this is expected — the statement is the one agami generated from those examples — so re-run with `--confirm-convention`. Do not render it as a decision for the user; it is a decision this skill has already made. |
| A promotion exits `2` | Cannot start. The `agami-save-golden:` line on stderr names the cause. Nothing was written; a rolled-back write left the previous bytes exactly as they were. |
| A promotion exits `2` saying the question moves with time and the answer key doesn't | **Rename the question, don't re-anchor the statement.** Ask which window it meant, rewrite the question to name it ("…in August 2026"), and re-run with the SQL exactly as it ran. Anchoring the SQL to `CURRENT_DATE` clears the lint and bands a sliding window around one day's value — a false alarm at the next run. |

---

## Hard rule for screenshots

The screenshot is an **image of numbers**, and a misread expected value reads exactly like a model bug. So: (1) the value is parsed by `reconcile.py`, never by eyeballing; (2) the extracted `(label, value)` table is **always confirmed with the user before any query runs** (Phase 1 vision branch). The image stays local — same as the CSV (Hard rule #4); it's never uploaded or summarized off-machine.

---

## Hard rule for a statement the person supplies

**The person's query is not ground truth.** It is evidence about one part at a time. Every part is graded against the semantic model and the warehouse (Phase 1.5), and the query asserting something is never the evidence for it; a part reaches `model_gap` only by measurement. The worked case: `WHERE state IN ('pending', 'open', 'work in progress', 'Hold')` over a column whose list of values reads `Pending`, `Open`, `Work in Progress`, `Hold`. Three of the four members are the person's mistake and grade `query_defect`; `'Hold'` is right; and the join beside them may still be a gap in the semantic model. A whole-query verdict would hide all three facts, so there is none. The statement runs on the road agami's own SQL runs, with the same guards, and a refusal is written down as a grade rather than routed around. A row whose statement has any part not `confirmed` never reaches Phase 3e's offer: its status is `match_unverified`, and a match nobody could verify may be luck.

---

## Roadmap (not in v1)

- **Tableau / Looker / Mode export parsing** — parse `.twb` / `.twbx` / JSON exports directly (today a screenshot of any of them already works via the vision branch).
- **Recurring reconcile runs** — re-run a reconcile against a pinned dashboard on a schedule, rather than by asking each time.
