# agami-connect · Phase 1.7–1.8: build the model

Part of [agami-connect](../SKILL.md). `<plugin_root>` in this file is the plugin's root directory: the absolute path agami-connect's SKILL.md shows in its own commands. Write that path into a command, quoted; never paste `<plugin_root>` or an unset variable into Bash.

### 1.7 — Run the introspection engine (on the kept tables)

Mark the step started and refresh the task list ([task-list.md](../../../shared/task-list.md)):
```bash
bash "<plugin_root>/scripts/sm" steps "$ROOT" --mark build=in_progress
```

This is the deterministic core — it replaces hand-authoring tables/columns/FK SQL/confidence formulas. Run it exactly as written, with the full quoted path (never `cd` into the scripts folder, never a shell variable for the path): that form is the one agami pre-approves, so it doesn't stop for permission.

```bash
# write the kept allowlist to a file first (zsh-safe — no word-splitting to get wrong)
printf '%s\n' incident problem change_request sys_user ... > "<artifacts_dir>/local/tmp/agami-keep.txt"
AGAMI_SQL_MAX_ROWS=200000 bash "<plugin_root>/scripts/sm" introspect \
  --profile <profile> --db-type <db_type> \
  --artifacts "<artifacts_dir>" \
  [--tables-file "<artifacts_dir>/local/tmp/agami-keep.txt"] \
  [--exclude-columns <schema.table.column list from 1.6>]
```

(The raised cap is the same one 1.6 explains — [why](context-and-tables.md#raised-cap). Without it a large schema's `sql_tables` / foreign-key reads are refused, the engine swallows it, and you get a model with no join graph and no error to show for it.)

`--tables-file` is the prune step's **kept set** (it also covers the no-catalog/probe-only case from 1.2). `--exclude-columns` marks the dropped columns excluded during the build — one validated write, no follow-up curate call. Omit both only when the user kept everything or skipped pruning. (If a table name is bogus / can't be described, the engine now drops it with a note and — if *nothing* describes — errors clearly instead of writing a partial model.)

**On a server database the build takes from seconds to many minutes, and the command prints nothing until it returns, which reads as hung.** So on every server database (anything but SQLite and DuckDB, which return in seconds) run it **in the background and show progress from its log**:

1. **One background call.** Bash with `run_in_background: true`, and a `description` the person can read in the app's Background tasks pane, in their words: *"Read the tables (Snowflake, 84 tables)"*.
2. **Say once where to watch**, the first time a background step starts in a run: *"Reading your tables in the background. In the Desktop app, ⋮ → Background tasks shows it live."* (Leave out the second sentence outside the Desktop app.)
3. **Keep the turn open and post progress with `sm progress`**, in a loop until it says `done` or `failed`, or `sm steps` (below) prints a `problem`:
   ```bash
   bash "<plugin_root>/scripts/sm" progress "<artifacts_dir>/<profile>" --wait 20
   ```
   It returns as soon as the build's log moves on (or after 20 seconds) with `line`, ready to post: *"Reading columns ▰▰▰▱▱▱▱▱ 12/34 tables"*, the bar drawn from the log's own counts and rounded down (never an estimate), and `done` once the build has finished. Post `line` when `changed` is true. Use this, not `tail` or a watcher of your own: it is pre-approved, and a turn that ends mid-onboarding loses the skill's pre-approvals, so every later command would stop for permission. If you do end your turn while it runs, say so in that message (*"Still reading your tables; I'll carry on as soon as it finishes."*).

   After every `sm progress --wait` call, refresh the task list ([task-list.md](../../../shared/task-list.md)):
   ```bash
   bash "<plugin_root>/scripts/sm" steps "$ROOT"
   ```

   When `failed` is true, or `sm steps` prints a `problem` that is not null (a killed build writes no `failed` line; `sm steps` calls it stopped after 30 silent minutes), the build stopped: say the `problem` to the person in one sentence, and handle it as the paragraph "The validator gates the write" below says (surface the errors and stop).
4. **The onboarding step's label** is the build step's `text` from that `sm steps` output, with the live count while it runs (*"Read the tables and joins — 12 of 34 tables"*) and the final numbers when it ends; never type the count yourself.
5. **When it exits**, read the command's own output (the report, and the validator's verdict) as below.

**Where background commands aren't available, a batched build** keeps it legible instead:

1. **Batched build (30+ tables) — `--append`.** Split the kept allowlist into batches of ~10–15 tables and introspect **one batch per call**, each a quick *foreground* command that returns in tens of seconds. Every call MERGES into the existing model (prior tables are loaded from disk, never re-queried), so you end with the full union — and you report `batch 3/5 (+12 tables)` between calls, natural progress with **no background monitor to babysit**:
   ```bash
   split -l 12 "<artifacts_dir>/local/tmp/agami-keep.txt" "<artifacts_dir>/local/tmp/agami-batch-"      # batch files
   for b in "<artifacts_dir>/local/tmp/agami-batch-"*; do
     AGAMI_SQL_MAX_ROWS=200000 bash "<plugin_root>/scripts/sm" introspect \
       --profile <p> --db-type <t> \
       --artifacts "<artifacts_dir>" --tables-file "$b" --append
   done
   ```
   (`--append` on the first batch just creates the model — there's nothing to merge yet. The relationship pass runs each batch across the union, deduped, so the final batch yields the complete join graph.)
(`--progress <file>` moves the log elsewhere. SQLite and DuckDB run in the foreground: they return in seconds.)

It builds + **validates** + writes the model at `<artifacts_dir>/<profile>/`: storage connection, **proposed subject areas**, per-table columns + types (catalog or value-inferred), PK→`grain`, other unique keys→`unique_keys`, FK→`relationships` with **inferred cardinality** (`many_to_one`/`one_to_many`/`one_to_one`, one-to-one when the join is on a whole key on both sides; a join only the data shows is unique keeps its label and lands `unreviewed` with a "looks one-to-one, confirm" note), `column_groups` on deep tables (≥30 cols), `sensitive` flags on PII (by whole-word column name, email-valued columns, and personal keys inside JSON values, named in a column caveat), cross-area edges, and a report. Relationships from **unenforced-FK** dialects (Redshift/Databricks/Trino) and everything from probe mode are confirmed-by-overlap or `unreviewed`. The report prints the **capability mode per step** (catalog vs probe) — surface that to the user so they know what was read vs inferred.

**Join candidates (tested, not written).** After the joins are built, introspection tests every column no join covers against each other table's one-column key (primary or unique) when the column's name matches the key or its type does — `orders.cust_ref` against `customers.id`, say (a column with a value list, or one column of a key over several, needs its name to point at the table) — and also when the database declares some foreign keys, for the columns they don't cover. Each test is one read-only `SELECT` over a sample of at most 1,000 of the column's distinct values; a run makes **at most 300** of them, and a table over 3,000,000 rows is not probed, so a billed warehouse sees a bounded number of small queries. A column flagged sensitive, or whose sampled values look personal, is never tested at either end, and the report and the file carry only names and counts. None of these joins is written to the model. The report's `## join candidates` section lists each with its match rate (`+ orders.cust_ref->customers.id: 100.0% of sampled orders.cust_ref values found in customers.id`), and the near misses with a `-`; it says so when the 300-probe cap stopped the scan. The same list is in `<artifacts_dir>/<profile>/.introspect/join_candidates.json`, each candidate a ready `add` op with its evidence. Show the user the candidates and ask which are real joins; add each one they confirm with one call (written `unreviewed`, for sign-off in `/agami-model`).

**Show every join before asking about any of them.** A card that offered "Add all 16" and "Only 1–8" without the joins themselves left the person nothing to judge by. First a table in the chat, one row per candidate from `candidates`, highest match first, numbered so a range on the card points at rows the person can see ([question-cards rule 3](../../../shared/question-cards.md#3-a-result-is-chat-output-before-the-card-that-acts-on-it)); a chat table may show the identifiers:

```
| # | Join                                     | Match              |
|---|------------------------------------------|--------------------|
| 1 | leads.converted_contact_id → contacts.id | 100% (412 of 412)  |
| 2 | orders.cust_ref → customers.id           | 97.5% (390 of 400) |
```

The join is the candidate's `id` without its schemas, `->` drawn as `→`; the match is `evidence.match_rate` with `matched of sampled`. At most 15 rows, then *"and N more"*; say how many near misses (`low_overlap`) were left out, without listing them. Then the card:

- **Up to 4 candidates:** one question per candidate (header `Join 1/3`), the question in plain words (*"Join Leads to Contacts by Converted contact ID?"*, [rule 3b](../../../shared/question-cards.md#3b-things-are-named-in-plain-words)), options **Add it** (description: the match, *"100% of 412 sampled values match"*; preview: the join as in the table, then the `ON` condition, `leads.converted_contact_id = contacts.id`) and **Leave it out**.
- **More than 4:** one question, with group options, and **each option's `preview` lists every join that option adds**, one per line as in the table, so the person sees exactly what a press adds: **Add the <k> full matches (Recommended)** when some match 100% (the others stay out), **Choose one by one** (cards of up to 4 as above, each with **Add all remaining**), **Add none**. Never offer a numbered range ("1–8") as an option on its own: a range is only a label for joins its preview names.

Add each confirmed join with:

```bash
bash "<plugin_root>/scripts/sm" curate "<artifacts_dir>/<profile>" \
  --ops-file "<artifacts_dir>/<profile>/.introspect/join_candidates.json" --candidate "<id>"
```

Never accept them all unasked: a column can match a key's values by chance (two counters that both run 1, 2, 3 …). The name-guessed joins (a `dealer_id` beside a `dealers` table) are still written `unreviewed` as before and aren't listed again.

The validator gates the write — **if it fails, the model is not persisted.** Surface the errors verbatim and stop (this should be rare; the engine emits valid models).

**Post one progress line, with numbers from the engine's output only.** No card here: the build asks nothing. Every number comes from the report it printed (`tables:`, `relationships:`, `subject_areas (<A>)`, `capability modes:`, `deep tables:`, `sensitive columns:`); never estimate, round up or fill a number the output didn't give. For how many joins came from declared foreign keys, count the relationship items with `origin: "fk"` in `bash "<plugin_root>/scripts/sm" review-items "$ROOT"`; if you didn't run it, leave that clause out.

`✓ Read <N> tables into <A> subject area(s) (<catalog|probe> mode); <R> joins, <F> from declared foreign keys; <S> columns flagged personal; <D> wide tables.`

Add **one finding** on the next line when the report has one worth a person's attention, quoted from it: a `suspected personal data (review):` line, a `large table` line, or a note that dropped a table. One, not a list; none when there is none. Batched builds post `batch 3/5 (+12 tables)` between calls, then this line once at the end.

**Empty tables — say so before going on.** Read `rows` per table from `bash "<plugin_root>/scripts/sm" model-tree "$ROOT"` (measured during the build; `null` when it wasn't). When some tables have 0 rows, add to the progress line *"<k> of <N> tables are empty"* and name up to 5. **When every measured table is empty**, the build can't find joins or keys from the data, and every example will return nothing, so stop and say it plainly: *"These <N> tables have no rows: the database holds the structure only."* Then AskUserQuestion, header `Empty`: **Connect a different database (Recommended)** (Phase 0a, a new name) / **Continue with the structure only** (descriptions, metrics and sign-off still work; Phase 6's examples will show 0 rows, so say that again there) / **Stop here**. A live run built a whole model on an empty demo database before anyone noticed.

**Backstop reconciliation — if `<N>` is fewer than the catalog held, say what's NOT in the model and why** (the 1.3 guard should have covered this up front; repeat it here so it can't slip). E.g. *"Modeled 52 of 70 tables — left out `public` (17, separate Salesforce dataset) and `pg_auto_copy` (1, system). Say the word if you want any of them."* A user who picked "all" should never have to diff the catalog themselves to notice a schema is missing.

### 1.8 — Apply a model spec (ONLY when one was attached)

Introspection proposed areas by name and joins only where the catalog or `*_id` names showed them. The spec replaces both, in one validated step that writes nothing unless the whole result validates and restores the previous model if anything fails:

1. **Dry run** and show the result:
   ```bash
   bash "<plugin_root>/scripts/sm" apply-spec "<artifacts_dir>/<profile>" \
     --file "<artifacts_dir>/local/model-spec/<profile>/spec.json" --dry-run
   ```
   `errors` lists every problem at once (a table the build didn't find, an area a table names that the spec doesn't declare, a join column that doesn't exist) — show them all and stop; the person fixes the workbook and you re-run 1.6s.

   **A table "not in the introspected model" that IS in the database** means its columns couldn't be read during the build (introspect's notes say `dropped … no readable columns`; a dropped connection is the usual cause). A second introspect into the same profile can't add it — once a model exists, its queries are scoped to the model's own tables — so move the partial model aside and rebuild once: `mv "<artifacts_dir>/<profile>" "<artifacts_dir>/<profile>.partial-<timestamp>"` (never delete it), then re-run 1.7 with the same `--tables-file`. If the same tables are missing again, stop and name them: the connecting role probably can't read them.
2. **If the spec approves anything** (`joins_approved` > 0, or a metric marked approved), ask ONE question: *"The spec marks <n> joins and <m> metrics approved. Sign them off as you (<email>)?"* — `Yes` → pass `--signer <email> --role owner`; `No` → re-run without `--signer` after the person clears the Approved column. Never sign off in someone's name without asking.
3. **Apply**: the same command without `--dry-run`. Each join lands where the runtime serves it — in the area that owns both its tables, else as a cross-area join between the two owners — so the counts are `joins` = area joins + `cross_area_joins`.
4. Surface the counts from the JSON, verbatim: *"Applied the spec: <A> subject areas (<per-area owns / lists>), <J> joins (<approved> approved, <with condition> with a condition, <cross> between areas), <M> metrics, <S> sensitive columns, <R> rules; dropped <D> introspected tables the spec doesn't name; replaced <I> joins introspection had guessed."*

Surface any `notes` too — in particular a metric flagged as a plain aggregate: ask once whether to drop it and put its name in the column description instead (re-run 1.6s after they edit the workbook), or keep it.

**After a spec, the model's structure is the person's, not yours.** Through the rest of the run: don't propose, add or re-infer joins; don't re-split subject areas (Phase 3 shows the spec's areas, it doesn't propose new ones); don't add metrics the spec didn't name (2c — the spec's metrics are already in); carry its rules as written (they are in `datasource.md` already). Enrichment still describes tables and the columns the spec's Columns sheet left out — the spec's own descriptions are `human` and a generated one is refused over them. **Run 2b (entities) as usual:** entities are the model's vocabulary (the business nouns questions are routed by), not structure, and a spec doesn't declare them. **Regroup wide tables as usual too** (the step at the end of 2a), even when the spec described every column: a spec doesn't declare column groups, so they're still the engine's name-prefix buckets.

When the model has validated (and the spec, if one was attached, is applied), mark the step done and refresh the task list ([task-list.md](../../../shared/task-list.md)):
```bash
bash "<plugin_root>/scripts/sm" steps "$ROOT" --mark build=done
```

---

