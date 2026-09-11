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
   --sql-file statement.sql`. **Never `--no-safety`.** stdout goes to `statement.csv`; the exit code
   and stderr go into `run.json`.
5. **A refusal is a finding, not a crash.** `execute_sql` exits `1` with one JSON line on stderr,
   `{"refusal": {"reason", "rule", "detail", "remediation"}}`. Write `run.json` with
   `status: "refused"` and that `rule`. `table_scope` and `column_scope` become a `scope: model_gap`
   in the ledger: the person wanted a table or column the semantic model does not expose.
   `select_star` becomes `runs: query_defect`. Never rewrite the statement and never retry: a
   regenerated statement is one the person never wrote.
6. **Other failures** go through [`db_error_classifier.md`](db_error_classifier.md). `auth`, `dsn`,
   `network` and `permission` stop the whole run, as Phase 3b stops it; write the `kind` and move on.
7. **`sm receipt`** whenever the statement parsed: `bash "$AGAMI_PLUGIN_ROOT/scripts/sm" receipt
   "$ROOT" --sql-file statement.sql > statement-receipt.json`.
8. **Probes** go through step 4 only, each to its own CSV named as `part-ledger.md` lists:
   `sm join-probes --sql-file statement.sql > join-probes.json`, then each join's
   `probes.overlap[i].sql` to `<join id>.overlap.<i>.csv` and each entry of the top-level
   `cardinality` map to `cardinality.<table>.<column>.csv` (skip a null entry: the semantic model
   already says that column is unique); `sm filter-values plan --sql-file statement.sql >
   filter-values.plan.json`, then each `columns[<key>].distinct` to `<key>.distinct.csv`, each
   literal's `probes.exists` to `<literal id>.exists.csv`, and `probes.exists_folded` to
   `<literal id>.exists_folded.csv` only when `exists` returned 0; then `sm filter-values judge
   --plan filter-values.plan.json --results . > filter-values.judge.json`.
   A probe the tier refuses or fails leaves an empty file; leave it, the ledger reads it as a probe
   that failed.
9. **Nothing in these steps writes `query_log.jsonl`.** `agami-save-correction` reads that log's last
   successful line as the question to correct, and a probe there would be corrected instead of the
   answer. The AI's own run logs as `agami-query` Phase 5 always has.

Then `python3 "$AGAMI_PLUGIN_ROOT/scripts/reconcile.py" ledger --row-dir .` grades what was found,
and again with `--with-claims` once `sm claims` has compared the two statements.

**The person's statement is never run with weaker guards than the AI's.** Every gate that refuses a
generated statement refuses a supplied one, and the refusal is written down as a grade rather than
treated as the run breaking.
