# The part ledger

How a statement a person supplied is graded, one part at a time. Shared by `agami-reconcile`
(Phase 1.5) and `agami-save-correction` (Phase 1d, for a pasted statement). The person's query is
evidence, never the answer: a part is graded against the semantic model and the warehouse, and the
query saying something is never proof of it.

## Four grades and a note, and only measurement earns a `model_gap`

| Grade | Means | What follows |
|---|---|---|
| `confirmed` | the statement and the semantic model agree on this part, and the data backs it | nothing |
| `model_gap` | the data proves the statement right where the semantic model is missing it or has it wrong | a finding, for a person to act on |
| `query_defect` | the data proves the statement wrong on this part | reported to the person; nothing about the semantic model changes |
| `unresolved` | the part could not be checked, and the note says why | nothing is written; a row with one, or a row never graded at all, is never kept as an example |
| `noted` | not a grade: a fact the run states and never judges (rows an inner join dropped, a wide column nobody would list) | shown in its own block; never decides the row's verdict and never blocks an example |

**A check that could not run is never a pass.** And a failed measurement upstream never lends a grade
downstream: a join that could not be graded leaves the fan-out check on its aggregate `unresolved`.

## The parts, and what grades each one

| Part id | Read from | Rule |
|---|---|---|
| `runs` | `run.json` | `ok` → confirmed. Failed with `column_not_found`, `table_not_found` or `syntax` → query_defect. Refused for `select_star` → query_defect. Refused for `table_scope` or `column_scope` → unresolved here, and see `scope`. Any other failure → unresolved. No record → unresolved. The evidence carries the classifier's `kind` and `remediation`, never the engine's error text |
| `scope` | `run.json` | a scope refusal → model_gap of kind `scope`: the statement names a table or column the semantic model does not expose. A clean run → confirmed |
| `join:<a>-<b>` | `join-probes.json` + overlap CSVs | the verb's `status` decides: `declared` → confirmed; `wrong_key` (a different key between two tables with a declared relationship) → query_defect, whatever the probe says; `undeclarable` or `undetermined` (a CTE or derived table, a `USING`, a comma join, a declared `on:` nobody could read) → unresolved, with the verb's reason. `undeclared`: keys overlap → model_gap of kind `relationship`; keys never meet → query_defect; not probed, or one overlap probe's file empty → unresolved. A second join between the same two tables is its own part, `join:<a>-<b>#2` |
| `join_key:<a>-<b>` | overlap CSVs | sampled keys from one side exist on the other → confirmed; none do, every probe having answered → query_defect; a result missing or a probe file empty → unresolved. Emitted whenever overlap probes were planned or answered, which a declared join never plans |
| `cardinality:<a>-<b>` | `cardinality.<table>.<column>.csv`, or the semantic model | one side unique (a declared key, or distinct = total − nulls) → confirmed, naming the side; both sides repeat → query_defect, the join multiplies rows; a side missing → unresolved |
| `fan_out:<aggregate>` | `statement-prepare.json` | a `join:` part it depends on is not confirmed → unresolved. `multiplied` with only `fan_out_invariant` → confirmed. `multiplied` otherwise → query_defect, naming the risk. `not_multiplied` → confirmed. `undetermined` with `join-probes.json` readable and `joins_written` zero → confirmed (a statement that writes no join has nothing to multiply); `undetermined` with every written join listed, confirmed, and bringing in one row at most (its right endpoint the one side of the declared relationship the statement wrote, or its written column unique by the semantic model) → confirmed, "every join the statement writes brings in one row at most"; `undetermined` otherwise → unresolved, the note repeating the pre-flight's `reason` (the aggregate names no column; a column attributed to no single table; a name bound to a computed relation; a table the semantic model does not declare). Pre-flight `unchecked` → one `fan_out:*` row, unresolved |
| `aggregation:<aggregate>` | `statement-prepare.json` | a `bad_aggregation` or `semi_additive` risk → query_defect; else confirmed |
| `default_filter:<table>:<expr>` | `statement-receipt.json` `tables.items[].filters` | `applied` → confirmed; `omitted` → model_gap of kind `filter`; `undetermined` → unresolved |
| `metric:<output column>` | `statement-receipt.json` `columns.items[]` | `matched` → confirmed; `unmatched` → model_gap of kind `metric`, except when every aggregate in the statement is a bare `count(*)`, which matches no metric by design; `undetermined` (the receipt could not tell) → unresolved, because a failure to read is never a gap; `matched` to a metric whose `source_tables` the statement never reads → unresolved, the match being by shape alone |
| `literal:<t>.<c>=<v>` | `filter-values.judge.json` | the judge's grade, as it stands; a `model_gap` is of kind `description`, the column's list of values being stale. Every grade carries `declared` (`populated`, `empty`, `absent`), and a grade the warehouse decided over an undeclared column says so in its note |
| `values_declared:<t>.<c>` | `filter-values.judge.json` `columns` | one per filtered column. `populated` → confirmed; `absent` or `empty` with the distinct probe `listed` (under 26 values) → model_gap of kind `description`, the same finding family as a stale list; `overflow` → noted, no list is expected of a wide column; `empty` → noted; `failed` or `not_run` → unresolved; a sensitive column → noted |
| `dropped_rows:<a>-<b>` | `<join id>.dropped_rows.csv` | noted, never a grade: `<dropped> of <total> <left> rows have no <right> partner`, counted over the whole table before the statement's own filters; a probe planned but not run → noted, nothing claimed; no probe planned → no part |
| `question_fit` | `question_fit.json`, the skill's Phase 1.5g reading of whether the statement answers its question | `plausible` → confirmed, by reading, and the note says so; `doubtful` → unresolved with the reason, so the row grades `match_unverified` at best and never reaches the keep-offer; `no_question` → no part; absent after a run that succeeded → unresolved, the fit was not checked. The one part graded by judgment: it can withhold a row and never proves anything about the semantic model |
| `predicates`, `date_window` | `claims.json`, only with `--with-claims` | `agrees` → confirmed; `differs` → unresolved, with both sides named; `unknown` → unresolved, except a `date_window` that is `null` on both sides when `unreadable` says both statements parsed and `temporal_predicates` is zero on both sides, which is confirmed (neither writes a date filter, so there is nothing to disagree about); a count above zero is a window written in a shape the reader does not fold, and stays open. A difference is reported, never judged here |

**The verdict is the weakest part:** `query_defect` outranks `unresolved`, which outranks
`model_gap`, which outranks `confirmed`. The counts travel with it so a reader sees what else was there.

**An input that is not there is a part that was not checked.** After a run whose `run.json` says
`ok`, the ledger expects `statement-prepare.json`, `statement-receipt.json`, `join-probes.json`,
`filter-values.judge.json` and `question_fit.json`. One that is absent, zero bytes, one JSON error line from a verb that
exited non-zero, or JSON of another shape becomes one open part, `fan_out:*`, `receipt:*`, `join:*` or
`literal:*`, whose evidence names the file and the problem. A verb that could not read the statement
(`unreadable` set) opens `join:*` or `literal:*` the same way. Grading only what happened to be there
would make a crashed verb read as a clean statement.

## The row directory

The skill writes one directory per row, `<artifacts_dir>/local/reconcile/<ts>/rows/<n>/`, with fixed
filenames, and `reconcile.py ledger --row-dir <dir> [--with-claims]` reads them. The verb is idempotent
and writes `ledger.json` beside the inputs.

| File | Written by | Holds |
|---|---|---|
| `statement.sql` | the skill | the person's statement, verbatim |
| `run.json` | the skill | `{"status": "ok" \| "refused" \| "failed" \| "not_run", "rule": ..., "kind": ..., "detail": ...}` from the tier's exit, the refusal line, or the error classifier |
| `statement.csv` | the tier | the statement's result; only its shape and one cell are ever copied onward |
| `statement-prepare.json` | `sm prepare --sql-file` | aggregates, findings, `unchecked` |
| `statement-receipt.json` | `sm receipt --sql-file` | the receipt of the person's statement |
| `join-probes.json` | `sm join-probes --sql-file` | every join written with its status, what the semantic model declares about it (`declared_cardinality`, `unique_by_model`), the probes to run, and a top-level `cardinality` map of one probe per column |
| `<join id>.overlap.<i>.csv` | the tier | the `i`-th overlap probe's `matched` count |
| `<join id>.dropped_rows.csv` | the tier | `total, dropped` for the join's left table: its rows with no partner on the right |
| `cardinality.<table>.<column>.csv` | the tier | `total, distinct_count, null_count` for one column, shared by every join that reads it; not written for a column the semantic model declares a key |
| `filter-values.plan.json` | `sm filter-values plan --sql-file` | every typed value and its probes, and a `columns` map of one distinct-values probe per column |
| `<column key>.distinct.csv` | the tier | one column's distinct values, bounded one past the enum ceiling |
| `<literal id>.exists.csv`, `<literal id>.exists_folded.csv` | the tier | one value's row count, and the folded near miss, run only when `exists` returned 0 |
| `filter-values.judge.json` | `sm filter-values judge` | one grade per typed value |
| `claims.json` | `sm claims` | the diff against the AI's own statement, once both exist |
| `question_fit.json` | the skill, Phase 1.5g | `{"fit": "plausible" \| "doubtful" \| "no_question", "reason": "<one sentence, or null>"}`: whether the statement plausibly answers the question it came with, decided by reading |
| `mentions.json` | `sm mentions --sql-file` | every description, caveat, glossary line, narrative paragraph and prompt example that mentions a table or column the statement reads, with a `values_named_differ` flag when two mentions about one column name different quoted values. Optional: absent, the ledger grades as before |
| `receipt.json` | `sm receipt` | the receipt of the AI's statement |
| `ledger.json` | `reconcile.py ledger` | the graded parts |

**The semantic model's words ride on the parts that fell short.** When `mentions.json` is present,
every part that is not `confirmed` or `noted` and names a table or column (`literal:`,
`values_declared:`, `default_filter:`, `join:` and its siblings) carries `evidence.prose`: the
mentions about that column and its table, at most twenty. A `values_named_differ` flag on the column
lands in `evidence.prose_flags` and the note says two descriptions disagree. Never a grade: the words
that shaped the SQL sit beside the number that went wrong, for a person to read.

**A zero-byte probe CSV is a probe that failed**, because the tier writes CSV to stdout only on
success. The ledger reads it as unresolved and says so. A header-only CSV is a probe that ran and
found nothing.

## The findings file

`reconcile.py findings --run-dir <dir>` reads `rows.jsonl` and every row directory, and writes three
files at the run's root: `ledger.json` (every row's ledger, by row number), `findings.json`, and
`query_defects.json`.

A finding is one place the semantic model was shown to be missing or wrong, with every row that
showed it:

```json
{"key": "relationship:customers-orders", "kind": "relationship",
 "evidence": [{"row": 1, "part": "join:customers-orders", "question": "...", "statement": "...",
               "expected": 4200000.0, "note": "...", "ledger": {...}, "claims": null}],
 "words": "what the person wrote, when a grade came with a note"}
```

Kinds: `relationship`, `filter`, `metric`, `scope`, `description`, `example`. Keys sort table pairs
and fold expressions to lowercase single-spaced text, with the kind as prefix, so the same missing
join seen from two statements in either order is one finding and a filter gap never collides with a
metric gap. A row whose status is `mismatch` while every part of the person's own statement is `confirmed` (the
`predicates` and `date_window` parts compare it with agami's statement, so they are the finding's
evidence rather than its bar) is a finding of kind `example`: the AI answered differently from a statement that checks out, and the fix
is a worked example rather than a change to a definition. A part left `unresolved`, or a row that was
never graded, is not a statement that held, and makes no example. Filter keys drop the statement's
alias, so `o.status` and `orders.status` over one declared filter are one finding.

`query_defects.json` lists `{row, part, note}` for every `query_defect`, apart from the findings, so
nothing about the semantic model is ever proposed from a part the data proved wrong.
