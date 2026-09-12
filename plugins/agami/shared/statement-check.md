# Running a statement a person supplied

The person's statement takes the road the AI's SQL takes. There is no second road. `agami-query`
Phase 1e says how the profile's tier is invoked, Phase 3a says the two steps every statement goes
through, Phase 3b says what an error means, and Phase 4e.iii.5 says how a receipt is assembled. This
page only says what to do at each step with a statement you did not write, and which file to write it
to. The files are the ones [`part-ledger.md`](part-ledger.md) reads.

Work in the row's directory, `<artifacts_dir>/local/reconcile/<ts>/rows/<n>/`. Write
`statement.sql` first, verbatim.

1. **Read-only first.** One `SELECT` or `WITH ... SELECT` per
   [`sql-generation-rules.md`](sql-generation-rules.md). Anything else is refused here: write
   `run.json` with `status: "not_run"`, mark the row `error`, and probe nothing.
2. **Does it run at all?** Wrap it so it returns no rows, the way seed validation does:
   `SELECT 1 FROM (<statement>) AS _agami_check WHERE 1=0`, through steps 3 and 4. A failure whose
   classifier kind is `column_not_found`, `table_not_found` or `syntax` is the person's defect: write
   `run.json` with `status: "failed"` and the `kind`, and stop probing.
3. **`sm prepare` on every tier.** `bash "$AGAMI_PLUGIN_ROOT/scripts/sm" prepare "$ROOT" --area <area>
   --sql-file statement.sql > statement-prepare.json`. Keep `aggregates`, `findings` and `unchecked`.
   It describes and never refuses.
4. **The tier's own tool**, exactly as `agami-query` Phase 1e tabulates it for this profile: psql,
   mysql, snowsql, sqlite3, DuckDB, or `"$PY" -m execute_sql --profile <profile> --area <area>
   --sql-file statement.sql`. **Never `--no-safety`.** Always pass the statement **by file** (the
   tool's `-f` or `--sql-file` form), never inline in a shell string: a literal such as `'$(id)'` is
   legal SQL and the shell would expand it. stdout goes to `statement.csv`. `run.json` records
   `status` (`ok`, `failed`, `refused`, `not_run`), `exit`, the classifier's `kind`, the guard's
   `rule`, and its `remediation`; **never the raw stderr**, which can carry the statement and the
   engine's error text.
5. **A refusal is a finding, not a crash.** `execute_sql` exits `1` with one JSON line on stderr,
   `{"refusal": {"reason", "rule", "detail", "remediation"}}`. Write `run.json` with
   `status: "refused"` and that `rule`. `table_scope` and `column_scope` become a `scope: model_gap`
   in the ledger: the person wanted a table or column the semantic model does not expose.
   `select_star` becomes `runs: query_defect`. Never rewrite the statement and never retry: a
   regenerated statement is one the person never wrote.
6. **Other failures** go through [`db_error_classifier.md`](db_error_classifier.md). `auth`, `dsn`,
   `network` and `permission` stop the whole run, as Phase 3b stops it; write the `kind` and its
   `remediation` and move on.
7. **`sm receipt`** whenever the statement parsed: `bash "$AGAMI_PLUGIN_ROOT/scripts/sm" receipt
   "$ROOT" --sql-file statement.sql > statement-receipt.json`. Beside it, `bash
   "$AGAMI_PLUGIN_ROOT/scripts/sm" mentions "$ROOT" --sql-file statement.sql > mentions.json`: the
   semantic model's own words (descriptions, caveats, glossary, narrative, prompt examples) about every
   table and column the statement reads, for the ledger to put beside a part that falls short.
8. **Probes** go through step 4 only, each written to its own `.sql` file first and passed by path,
   each result to its own CSV named as `part-ledger.md` lists:
   `bash "$AGAMI_PLUGIN_ROOT/scripts/sm" join-probes "$ROOT" --sql-file statement.sql >
   join-probes.json`, then each join's `probes.overlap[i].sql` to `<join id>.overlap.<i>.csv` and
   each entry of the top-level `cardinality` map to `cardinality.<table>.<column>.csv` (skip a null
   entry: the semantic model already says that column is unique), and each join's `dropped_rows_probe.sql` to
   `<join id>.dropped_rows.csv` (skip a null probe); `bash "$AGAMI_PLUGIN_ROOT/scripts/sm"
   filter-values plan "$ROOT" --sql-file statement.sql > filter-values.plan.json`, then each
   `columns[<key>].distinct` to `<key>.distinct.csv`, each literal's `probes.exists` to
   `<literal id>.exists.csv`, and `probes.exists_folded` to `<literal id>.exists_folded.csv` only when
   `exists` returned 0; then `bash "$AGAMI_PLUGIN_ROOT/scripts/sm" filter-values judge "$ROOT" --plan
   filter-values.plan.json --results . > filter-values.judge.json`.
   A probe the tier refuses or fails leaves an empty CSV; leave it, the ledger reads it as a probe
   that failed. Beside every probe's CSV write `<same name>.run.json` with the same fields as step
   4's `run.json`, refusal `rule` included: that file is the record of the probe having run or having
   been refused.
9. **Nothing in these steps writes `query_log.jsonl`, and nothing here runs unrecorded.**
   `agami-save-correction` reads that log's last successful line as the question to correct, and a
   probe there would be corrected instead of the answer. The record of this phase is the row
   directory itself: `run.json` for the statement and `<probe>.run.json` for every probe, each with
   its exit, rule and kind, so every execution and every refusal in this phase is written down. The
   AI's own run logs as `agami-query` Phase 5 always has.

10. **Does the statement answer the question?** For a row that carries a question too, read the two
   side by side and write `question_fit.json`: `{"fit": "plausible" | "doubtful" | "no_question",
   "reason": "<one sentence, or null>"}`. Doubtful when the grain differs, the measure differs, a
   filter is present the question never asked for or absent when it did, or the time window differs.
   `no_question` for a statement that came alone. This is the one step here that judges by reading;
   the ledger turns a doubtful fit into an open part, and a missing file into one too.

Then `python3 "$AGAMI_PLUGIN_ROOT/scripts/reconcile.py" ledger --row-dir .` grades what was found,
and again with `--with-claims` once `sm claims` has compared the two statements.

**The person's statement is never run with weaker guards than the AI's.** Every gate that refuses a
generated statement refuses a supplied one, and the refusal is written down as a grade rather than
treated as the run breaking.
