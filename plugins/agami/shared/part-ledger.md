# The part ledger

How a statement a person supplied is graded, one part at a time. Shared by `agami-reconcile`
(Phase 1.5) and `agami-save-correction` (Phase 1d, for a pasted statement). The person's query is
evidence, never the answer: a part is graded against the semantic model and the warehouse, and the
query saying something is never proof of it.

## Four grades, and only measurement earns a `model_gap`

| Grade | Means | What follows |
|---|---|---|
| `confirmed` | the statement and the semantic model agree on this part, and the data backs it | nothing |
| `model_gap` | the data proves the statement right where the semantic model is missing it or has it wrong | a finding, for a person to act on |
| `query_defect` | the data proves the statement wrong on this part | reported to the person; nothing about the semantic model changes |
| `unresolved` | the part could not be checked, and the note says why | nothing is written; a row with one is never kept as an example |

**A check that could not run is never a pass.** And a failed measurement upstream never lends a grade
downstream: a join that could not be graded leaves the fan-out check on its aggregate `unresolved`.

## The parts, and what grades each one

| Part id | Read from | Rule |
|---|---|---|
| `runs` | `run.json` | `ok` → confirmed. Failed with `column_not_found`, `table_not_found` or `syntax` → query_defect. Refused for `select_star` → query_defect. Refused for `table_scope` or `column_scope` → unresolved here, and see `scope`. Any other failure → unresolved. No record → unresolved |
| `scope` | `run.json` | a scope refusal → model_gap of kind `scope`: the statement names a table or column the semantic model does not expose. A clean run → confirmed |
| `join:<a>-<b>` | `join-probes.json` + overlap CSVs | the verb's `status` decides: `declared` → confirmed; `wrong_key` (a different key between two tables with a declared relationship) → query_defect, whatever the probe says; `undeclarable` or `undetermined` (a CTE or derived table, a `USING`, a comma join, a declared `on:` nobody could read) → unresolved, with the verb's reason. `undeclared`: keys overlap → model_gap of kind `relationship`; keys never meet → query_defect; not probed → unresolved |
| `join_key:<a>-<b>` | overlap CSVs | sampled keys from one side exist on the other → confirmed; none do → query_defect; no result → unresolved. Emitted for every undeclared join, and for a declared one only when probes ran |
| `cardinality:<a>-<b>` | `cardinality.<table>.<column>.csv`, or the semantic model | one side unique (a declared key, or distinct = total − nulls) → confirmed, naming the side; both sides repeat → query_defect, the join multiplies rows; a side missing → unresolved |
| `fan_out:<aggregate>` | `statement-prepare.json` | a `join:` part it depends on is not confirmed → unresolved. `multiplied` with only `fan_out_invariant` → confirmed. `multiplied` otherwise → query_defect, naming the risk. `not_multiplied` → confirmed. `undetermined` → unresolved. Pre-flight `unchecked` → one `fan_out:*` row, unresolved |
| `aggregation:<aggregate>` | `statement-prepare.json` | a `bad_aggregation` or `semi_additive` risk → query_defect; else confirmed |
| `default_filter:<table>:<expr>` | `statement-receipt.json` `tables.items[].filters` | `applied` → confirmed; `omitted` → model_gap of kind `filter`; `undetermined` → unresolved |
| `metric:<output column>` | `statement-receipt.json` `columns.items[]` | `matched` → confirmed; `unmatched` → model_gap of kind `metric`, except when every aggregate in the statement is a bare `count(*)`, which matches no metric by design |
| `literal:<t>.<c>=<v>` | `filter-values.judge.json` | the judge's grade, as it stands; a `model_gap` is of kind `description`, the column's list of values being stale |
| `predicates`, `date_window` | `claims.json`, only with `--with-claims` | `agrees` → confirmed; `differs` or `unknown` → unresolved, with both sides named. A difference is reported, never judged here |

**The verdict is the weakest part:** `query_defect` outranks `unresolved`, which outranks
`model_gap`, which outranks `confirmed`. The counts travel with it so a reader sees what else was there.

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
| `join-probes.json` | `sm join-probes --sql-file` | every join written with its status, the probes to run, and a top-level `cardinality` map of one probe per column |
| `<join id>.overlap.<i>.csv` | the tier | the `i`-th overlap probe's `matched` count |
| `cardinality.<table>.<column>.csv` | the tier | `total, distinct_count, null_count` for one column, shared by every join that reads it; not written for a column the semantic model declares a key |
| `filter-values.plan.json` | `sm filter-values plan --sql-file` | every typed value and its probes, and a `columns` map of one distinct-values probe per column |
| `<column key>.distinct.csv` | the tier | one column's distinct values, bounded one past the enum ceiling |
| `<literal id>.exists.csv`, `<literal id>.exists_folded.csv` | the tier | one value's row count, and the folded near miss, run only when `exists` returned 0 |
| `filter-values.judge.json` | `sm filter-values judge` | one grade per typed value |
| `claims.json` | `sm claims` | the diff against the AI's own statement, once both exist |
| `receipt.json` | `sm receipt` | the receipt of the AI's statement |
| `ledger.json` | `reconcile.py ledger` | the graded parts |

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
metric gap. A row whose status is `mismatch` while every part of the person's statement held is a
finding of kind `example`: the AI answered differently from a statement that checks out, and the fix
is a worked example rather than a change to a definition.

`query_defects.json` lists `{row, part, note}` for every `query_defect`, apart from the findings, so
nothing about the semantic model is ever proposed from a part the data proved wrong.
