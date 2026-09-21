# Checking a statement a person supplied

The person's statement runs through the guard agami's own SQL runs through, and it runs there in
code. **It is never run on a command-line tier**: not psql, mysql, snowsql, sqlite3 or DuckDB,
whatever tier the profile queries on. Those tools have no read-only gate, no scope gate and no row
or time bound, so a statement that writes, or a probe, would reach the database with only the
database role in its way. One script does every step below that touches the database, and it sends
each statement through `execute_sql`'s guarded chokepoint with the built-in executor, never with
`--no-safety`. The session never runs the statement or a probe itself. The files it writes are the
ones [`part-ledger.md`](part-ledger.md) reads.

Work in the row's directory, `<artifacts_dir>/local/reconcile/<ts>/rows/<n>/`. Write
`statement.sql` first, verbatim, with the Write tool. Then check the row, once:

```bash
"$PY" "$AGAMI_PLUGIN_ROOT/scripts/check_statement.py" --profile <profile> --area <area> \
  --row-dir "<artifacts_dir>/local/reconcile/<ts>/rows/<n>"
```

`$PY` is the interpreter `sm` resolves ([`connection-reference.md`](connection-reference.md)); it
has agami-core and the database driver. `<area>` is the subject area the scope gates check against,
taken from the profile's subject areas. Stdout is one JSON line: the statement's outcome and how
many probes ran. It never carries SQL.

To check a row again, say after the person rewords its statement, make the same call. It first
clears every file the last check wrote, so nothing from the earlier statement is graded as this
one's.

- **Exit `0`**: the row was checked, whatever the statement's outcome. A refusal is a finding.
- **Exit `3`**: the database cannot be queried as configured. Stop the whole run, as `agami-query`
  Phase 3b stops it, and tell the person the `remediation` in `run.json`. `run.json` names the
  cause: a failure `kind` (`auth`, `dsn`, `network`, `permission` or `driver_missing`), or the
  refusal `rule` `engine_mismatch`, which means the semantic model declares an engine its
  credentials do not connect to. `driver_missing` means `$PY` lacks the Python driver for this
  database; the sentence names what to install.
- **Exit `2`**: it could not start. Either `statement.sql` is missing, or the interpreter lacks
  agami-core; run `bash "$AGAMI_PLUGIN_ROOT/scripts/sm" install` and call it with `"$PY"`.
- **Any other exit**: the script crashed. Stop the run and tell the person. Never run the statement
  or a probe yourself instead.

## What the script does, in order

1. **Read-only first, then no recon.** Two of the guard's own gates read the statement before
   anything runs, in the order every statement meets them at the chokepoint. The read-only gate
   enforces [`sql-generation-rules.md`](sql-generation-rules.md): one `SELECT` or `WITH ... SELECT`.
   The recon gate refuses a call that reads the server's own metadata, such as its version, the
   session's identity or a privilege check. A statement either gate refuses gets
   `status: "refused"` in `run.json` and the gate's `rule` (`read_only` for a statement that
   writes, `recon` for a metadata call), and nothing runs after it, not even the zero-row check.
   Mark the row `error`.
2. **Does it run at all?** It wraps the statement so it returns no rows, the way seed validation
   does, `SELECT 1 FROM (<statement>) AS _agami_check WHERE 1=0`, writes that to `zero-row.sql`, and
   runs it through the guard. The wrap's copy of the statement loses the terminating semicolon and
   any comment around it, so a statement ending `; -- done` is wrapped as one statement and not two;
   the statement itself still runs verbatim at step 4. The wrap's own outcome goes to
   `zero-row.run.json`, in `run.json`'s shape, whatever it was. A failure whose classifier kind is
   `column_not_found`, `table_not_found` or `syntax` is the person's defect: `run.json` gets
   `status: "failed"` and the `kind`, and nothing is probed. A wrap the guard refuses is **this
   check not run**, not a fault in the statement — the wrap is agami's statement, not the person's —
   so it is recorded in `zero-row.run.json` and the statement is still checked on its own.
3. **`sm prepare`**, to `statement-prepare.json`: `aggregates`, `findings` and `unchecked`. It
   describes and never refuses.
4. **The statement itself**, through the guard, its result to `statement.csv` (absent or empty when
   it did not run). `run.json` records `status` (`ok`, `failed`, `refused`), `exit`, the
   classifier's `kind`, the guard's `rule`, `detail` and `remediation`. **Never the raw error
   text**, which can carry the statement and the engine's own words. The guard's fields are
   value-free by contract.
5. **A refusal is a finding, not a crash.** `table_scope` and `column_scope` become a
   `scope: model_gap` in the ledger: the person wanted a table or column the semantic model does not
   expose. `select_star` becomes `runs: query_defect`. The script never rewrites the statement and
   never retries, and neither does the session: a regenerated statement is one the person never
   wrote.
6. **Other failures** carry the kind [`db_error_classifier.md`](db_error_classifier.md) names.
   `auth`, `dsn`, `network`, `permission` and `driver_missing` end the script with exit `3`, and so
   does an `engine_mismatch` refusal. A failure of kind `other` says in `run.json` which side
   broke. Either agami's own code raised an error, named by its type alone, which the script also
   prints on stderr; or the database failed with an error agami could not classify. The error's
   text is never kept.
7. **`sm receipt`** to `statement-receipt.json`, and beside it **`sm mentions`** to `mentions.json`:
   the semantic model's own words (descriptions, caveats, glossary, narrative, prompt examples) about
   every table and column the statement reads, for the ledger to put beside a part that falls short.
8. **Probes.** `sm join-probes` to `join-probes.json` and `sm filter-values plan` to
   `filter-values.plan.json` emit the probe SQL. Each probe is written to its own `.sql` file, and
   each result goes to its own CSV named as `part-ledger.md` lists: each join's
   `probes.overlap[i].sql` to `<join id>.overlap.<i>.csv`, each non-null entry of the top-level
   `cardinality` map to `cardinality.<table>.<column>.csv` (a null entry means the semantic model
   already says that column is unique), each join's `dropped_rows_probe.sql` to
   `<join id>.dropped_rows.csv`, each `columns[<key>].distinct` to `<key>.distinct.csv`, and each
   literal's `probes.exists` to `<literal id>.exists.csv`. All of them run as one plan,
   `probes.plan.json`, through `execute_sql`'s batch door: the semantic model is resolved once and
   the connection kept open, and every probe still meets the guard on its own. The door writes each
   CSV, a `<same name>.run.json` beside it (`status`, `exit`, `kind`, `rule` and `detail`, refusal
   rule included) and the manifest, `probes.plan.json.manifest.json`. Then each literal's
   `probes.exists_folded` runs to `<literal id>.exists_folded.csv`, **only when `exists` returned 0**,
   as a second plan, `probes.folded.plan.json`. Last, `sm filter-values judge` reads the CSVs to
   `filter-values.judge.json`. A probe the guard refuses or the database fails leaves an empty CSV
   beside its `.run.json`; the ledger reads it as a probe that failed.
9. **Nothing in these steps writes `query_log.jsonl`, and nothing here runs unrecorded.**
   `agami-save-correction` reads that log's last successful line as the question to correct, and a
   probe there would be corrected instead of the answer. The record of this phase is the row
   directory itself: `run.json` for the statement, `zero-row.run.json` for its wrap and
   `<probe>.run.json` for every probe, each with its exit, rule and kind, so every execution and
   every refusal in this phase is written down. The
   AI's own run logs as `agami-query` Phase 5 always has.

## What stays with the session

10. **Does the statement answer the question?** For every statement row, write `question_fit.json`;
   when the row carries a question, read the two side by side first: `{"fit": "plausible" | "doubtful" | "no_question",
   "reason": "<one sentence, or null>"}`. Doubtful when the grain differs, the measure differs, a
   filter is present the question never asked for or absent when it did, or the time window differs.
   `no_question` for a statement that came alone. This is the one step here that judges by reading;
   the ledger turns a doubtful fit into an open part, and a missing file into one too.

Then, once per row and after the comparison, `python3 "$AGAMI_PLUGIN_ROOT/scripts/reconcile.py"
ledger --row-dir . --with-claims` grades what was found, or without `--with-claims` when agami's own
statement is missing. One run: every file above is still there, so waiting loses nothing, and the same
ledger written twice is a step somebody will skip or double.

**The person's statement is never run with weaker guards than the AI's.** Every gate that refuses a
generated statement refuses a supplied one, and the refusal is written down as a grade rather than
treated as the run breaking.
