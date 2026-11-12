# agami-connect · Phase 5–6: example questions

Part of [agami-connect](../SKILL.md). `<plugin_root>` in this file is the plugin's root directory: the absolute path agami-connect's SKILL.md shows in its own commands. Write that path into a command, quoted; never paste `<plugin_root>` or an unset variable into Bash.

## Phase 5: Seed prompt examples

Mark the step started and refresh the task list ([task-list.md](../../../shared/task-list.md)):
```bash
bash "<plugin_root>/scripts/sm" steps "$ROOT" --mark examples=in_progress
```

**Surface a progress warning first** (second-longest phase): "Generating NL→SQL seeds (a spread across your tables, metrics, entities, and joins — including cross-schema ones) and EXPLAIN-validating each against the live DB. Expect 1–3 min (longer on cloud / a multi-schema DB). I'll narrate per-example progress."

**5a — the candidates.** When Phase 4 started example writers, the candidates come from their files; you check and finish them:

1. **Collect** every area's `<artifacts_dir>/local/tmp/agami-examples-<area>.candidates.json` with `sm wait-files`, as [parallel-work.md](../../../shared/parallel-work.md) says (they have had the whole sign-off to run, so usually all are ready on the first pass). A writer given up on there: draft that area's candidates yourself, by the rules below.
2. **Bring them up to date with sign-off**: (i) drop any candidate that uses a metric or entity rejected in sign-off, or a column hidden on the personal-data card; (ii) rewrite any that uses a definition the person changed in sign-off, to the definition as it now stands (`sm review-items` or `sm context` show it); (iii) write the cross-area candidates yourself, with the whole model in view (the writers each saw one area); (iv) write the person's own questions yourself if a writer could not (its reply line says which).
3. **Write them all as one JSON array** to `<artifacts_dir>/local/tmp/agami-seeds.json` with the Write tool, each keeping its `area`, then go to 5b+5c. Nothing a writer drafted reaches the model unchecked: `seed-examples` validates every candidate and refuses until sign-off is done.

With no writers (no Agent tool), you generate the candidates yourself, as follows. The writers follow these same rules.

**Generate** candidate examples grounded in the model. Aim for **coverage and a good spread**, not a fixed count:

- **Shapes:** a count, a top-N, a time-bucketed trend, a breakdown, a recency filter. Anchor time filters to each table's `data_range` MAX (not `NOW()`) so seeds don't return 0 rows on a stale dataset.
- **Spread:** across the whole set, touch each major table/family, exercise **every approved metric at least once**, use the key **entities** by their real names/synonyms, and cover **representative joins** — intra-area *and* cross-schema.
- **Get every join from the model through `bash "<plugin_root>/scripts/sm" context "$ROOT" --tables <t1> <t2> …`** — it returns the joins touching those tables exactly as a question will be answered (area and cross-area alike), each with its role label and full condition (a `current_flag = 'Y'` included), so a seed writes the join the model declares. Never read the model's YAML to find joins.
- **Cross-schema seeds — the highest-value few-shots, include them deliberately.** The cross-schema / cross-area joins are in that same output (and in `cross_subject_area_relationships`). For the most meaningful ones, write a question a user would actually ask that **spans both schemas** — a fact/metric in one schema joined to the entity it references in another. Use the relationship's declared join (`on:` / from→to columns) and **schema-qualified** table names (it's one database — ordinary cross-schema SQL, fully EXPLAIN-able). Include **2–4** of these and make **1–2 genuinely complex** (3+ tables across two schemas, with an aggregation + a filter) — cross-schema SQL is exactly what NL→SQL gets wrong unaided, so a worked example pays off most here. (Cross-*profile* / federation seeds are out of scope — those need DuckDB ATTACH; this is cross-*schema* within one DB.)
- **Count scales with the model:** a single-schema DB is fine at ~10–12; a multi-schema one wants ~2–3 per subject area **plus** the 2–4 cross-schema seeds — don't undersample a 5-schema DB at a flat 12.

**The person's own questions come first.** When 1.5 saved `<artifacts_dir>/local/tmp/agami-asked-questions.txt`, write one candidate per question, its `question` as the person wrote it and SQL from the model, before any you make up; they go through the same validation and the same Keep / Drop cards as the rest, and Phase 6's table marks them *(yours)*. A question the model can't answer (a table that was left out, say) is not forced: list it in one line after the table with the reason.

Tag each with its `tables` (list **all** tables it touches — both schemas for a cross-area one), `columns`, and `metrics` (**every** model metric it computes, by `name`, so it is found under each; a name the model doesn't declare rejects the seed). **Store a cross-area seed under its primary/driving table's area** (tagged with both schemas' tables, so the ranker can surface it from either side). Write the candidates as a JSON array to `<artifacts_dir>/local/tmp/agami-seeds.json` — each: **required** `question`, `sql`; optional `tables`, `columns`, `metrics`.

**5b+5c — validate + write in ONE packaged call.** **Do NOT write a script to loop EXPLAIN over the seeds and don't hand-write the YAML.** `seed-examples` validates every candidate against the live DB (wraps each as a zero-row query — dialect-agnostic, scans nothing), writes the passing ones (appends, dedups by `question`, commits), and returns the rejects with their DB error. Post one progress line from its output (`totals`, or `added` and `rejected` for one area): *"✓ Wrote <added> examples; <rejected> failed the dry run."*
```bash
bash "<plugin_root>/scripts/sm" seed-examples "$ROOT" --profile <profile> --file "<artifacts_dir>/local/tmp/agami-seeds.json"
```
**Write every area's seeds in this one call:** give each seed an `area` (the subject area it is stored under) and leave out `--area`, or repeat `--area` to limit the call to those areas. A seed with no `area`, or one outside the call's areas, stops the call before anything is written. With exactly one `--area` the seeds need no `area` and the output is the single-area shape `{added, written, committed, rejected:[{question, error}]}`; with several or none it is that shape with each `rejected` entry labelled `area`, plus `areas` (one such result per area, labelled `area`) and `totals` (`areas`, `added`, `rejected`). For each `rejected`, optionally regenerate once and re-run with just those; don't block the flow on a few drops. Corrections later append via `/agami-save-correction` (same `add-example` path).

> **The command enforces Phases 2 and 4 for you.** It runs two gates before writing anything:
> - `{refused: "columns_unenriched", unenriched_tables}` → a table got no column descriptions at all (you skipped the Phase-2 column pass). Go back, run the column pass on those tables, re-run. **NOT bypassable** — naked columns degrade every answer.
> - `{refused: "preseed_review_pending", pending_count}` → metrics/entities the seeds depend on are still unreviewed (you skipped the review). **Go back to Phase 4a** and ask its Sign-off card. Bypass only via `--after-review`, and **only** on the Phase-4b path (the person has answered the Sign-off card and chose to continue with some items unreviewed). Never pass `--after-review` to force past a *fresh* refusal.

---

## Phase 6: Validate every seed example (the trust onboarding)

**Run every seed with ONE packaged call — `sm seed-validate` — never a hand-rolled "run all the seeds" script.** It executes each written seed against the live DB **through `execute_sql`** (the same path agami-query uses), so the **fan-trap / chasm-trap pre-flight and the scope/PII gates always apply** — a raw-connection driver could skip that safety and let a fan-out scan the whole table. It emits the examples-validation items (`{n, question, sql, row_headers, row_preview, row_count, state}`); a seed the pre-flight refuses or that errors comes back with its `error`, not a faked result:

```bash
bash "<plugin_root>/scripts/sm" seed-validate "$ROOT" --profile <profile> > "<artifacts_dir>/local/tmp/agami-examples-items.json"
```

Without `--area` it runs every area's seeds (each through `execute_sql` scoped to its own area, with the same per-statement limits) and prints one object: `areas` (`{area, examples, errored}` each), `items` (the items above, each labelled `area`) and `totals` (`areas`, `examples`, `errored`). Repeat `--area` to run only those areas. One `--area` prints the bare items array, as before. An area the model doesn't have is refused with `error: unknown_area` and the `valid_areas`.

(Heads-up before you run it: on a large warehouse this executes every seed for real — some may be full-scan aggregates that take a few minutes and cost warehouse compute. It's a one-time onboarding step; surface a one-liner so the wait is expected.)

**On a server database, run it in the background**, as the build is (1.7): Bash with `run_in_background: true` and a `description` in the person's words, *"Run the example questions (11)"*, and if you end your turn while it runs, say it's still running. It prints nothing until it is done, so post no bar; one line when it starts (*"Running the 11 example questions against your database…"*) and the result when it exits. SQLite and DuckDB run in the foreground.

**Show what ran, then one card** (conventions: [`shared/question-cards.md`](../../../shared/question-cards.md)). No page opens on this path, and no reply block is waited for. Post one progress line from `totals` (or the bare array's length and its items with an `error`): *"✓ Ran <examples> examples: <examples − errored> returned rows, <errored> failed."* Then a markdown table from the items, failing ones first, at most 15 rows, then *"Showing 15 of <n>; <n−15> more."* when there are more:

| # | Question | Rows | Result |
|---|---|---|---|
| 3 | Revenue by month this year | 12 | 2024-01 $41,200 · 2024-02 $39,850 · … |
| 7 | Top 5 customers by orders | — | failed: `column "cust_id" does not exist` |

`#` is the item's `n`, `Rows` its `row_count`, `Result` the first row or two of `row_preview` under `row_headers` (already unit-formatted), or `failed:` and its `error`. A result that looks wrong though it ran (a ratio far outside its range, a count of 0, a trend sorted by name) goes in one sentence under the table: that is 6a's case.

Then call AskUserQuestion, one question (header `Examples`), *"What should I do with these examples?"*:

- **Keep all that ran (Recommended)**: sign off every example that returned rows. Drop the (Recommended) and put **Review one by one** first when a result that ran looks wrong (6a).
- **Review one by one**: every example, 4 per card, the failing ones first.
- **Drop the failing ones**: mark every failed example rejected. Leave it out when none failed.
- **Show the full view**: the examples-validation page, every result with its SQL, served from localhost (below); then this card again.

| Answer | Do |
|---|---|
| Keep all that ran | The identity card from Phase 4a if `reviewer_email`/`reviewer_role` aren't saved yet; here **Don't sign anything yet** leaves every example written but unsigned and goes on to Phase 7 (not 4b). Then, per area, write the examples that ran back with their own `question` and `sql` (same SQL, so it is a sign-off, not a change; their tags are kept): `bash "<plugin_root>/scripts/sm" add-example "$ROOT" --area <area> --file "<artifacts_dir>/local/tmp/agami-keep-examples.json" --signer "<reviewer_email>" --role "<reviewer_role>"`. The sign-off is recorded in `curation_log.jsonl`, not on the example. Failed ones are left as they are; say how many. |
| Drop the failing ones | `bash "<plugin_root>/scripts/sm" remove-example "$ROOT" --question "<area>/<question>" …` (one `--question` per failed item, `--signer`/`--role` when known). They are marked rejected and kept for audit, not deleted: say so. |
| Review one by one | The one-by-one cards over every item, failed ones first; **Keep all remaining** on each card ends the review once the failures are dealt with. |
| Show the full view | The full view below, then this card again. |

**One by one.** Ask up to 4 examples per card, one question each, header `Example 3/9` (at most 12 characters; past 9/99 use `Ex 12/40`). The question is the example's question and what it returned, *"Revenue by month this year: 12 rows, 2024-01 $41,200"* (or *"failed: <error>"*). **The SQL goes in the `Keep` option's `preview`**, as a metric's does on its sign-off card: the item's `sql`, laid out over lines with a short comment on each filter, then the first rows of `row_preview` under `row_headers` (or the `error` for a failed one), so the person checks the statement without asking for it ([question-cards rule 3a](../../../shared/question-cards.md#3a-detail-goes-in-an-options-preview-not-the-question)). Options: **Keep** (the `add-example` sign-off above, for that one), **Drop** (`remove-example` for that one), **Keep all remaining** (keep this one and every remaining one that ran), and **Show the full view** (the page below; nothing is decided, and the same card is asked again after the URL; on a card with several questions, on the first question only). "Other" describes a fix in the person's words ("should only count paid orders"): rewrite that example's SQL, write it with `sm add-example` (same question, so it replaces), re-run its area with `sm seed-validate "$ROOT" --profile <profile> --area <area>`, and ask about it again showing the new result. A fix that touches a column or metric beyond this one example is 6a's: fix it everywhere, as 6a says. A question ("show me the SQL", "show me the rows") is answered first, then the same card asked again ([question-cards rule 5a](../../../shared/question-cards.md#5a-a-question-typed-in-other-is-answered-then-the-same-card-is-asked-again)). **An "Other" you can't act on is re-asked, never guessed**: an empty fix, or one naming a column the model doesn't have, is said back and you ask the same card again.

When every example has an answer, go to Phase 7.

**Show the full view** (an option on the Examples and one-by-one cards, or asked for in words). The examples-validation page, with every result and its SQL. Render it from the items and serve it from localhost, giving the person the URL:

```bash
bash "<plugin_root>/scripts/py" render_examples_validation.py \
  --title "Seed examples · <profile>" --profile <profile> \
  --items-file "<artifacts_dir>/local/tmp/agami-examples-items.json" \
  --read-only --out "<artifacts_dir>/local/examples-validation/<profile>/<ts>.html"
bash "<plugin_root>/scripts/py" serve_page.py "<artifacts_dir>/local/examples-validation/<profile>/<ts>.html" --minutes 30 --open
```

Run `serve_page.py` with `run_in_background` and read its first line, `{"url": …}`, for the URL to give the person.

It takes the combined object or a single area's array; repeat `--items-file` to put several files on one page, labelling a single area's array with its area as `--items-file <area>=<path>` (an unlabelled one is refused there, since two areas can share a question; so is the same file twice). Each card shows its area.

The page is read-only; decisions stay on the cards, so after the URL ask the card it was picked on again. A block pasted from an older, editable page is still accepted: `validate N` / `reject N` / `edit N` / `add example` / `note N` / `note all`. Apply each through the **packaged commands**, never by hand-rewriting `examples.yaml`. **`N` is the page's global example number (1..N across every area, assigned in render order).** `render_examples_validation.py` normalizes the items file to this same numbering, so resolve `N` → its example by reading that file back:
- **`validate N`** → the example is a trusted anchor; if the block carries a `by <email>` clause, record the sign-off via `sm add-example` (re-write the same example with `--signer`/`--role`). It reports `applied` (one line per example re-written).
- **`reject N`** → **`sm remove-example "$ROOT" --area <area> --question "<that example's exact question>"`** (`--signer`/`--role` to record who). To reject several across areas in one call, leave out `--area` and name each as `--question "<area>/<question>"` (looked up in that area only); a bare question is matched in whichever area has it, and one that more than one area has is refused (`refused: ambiguous_question`, with the areas) and nothing is rejected. That call adds `areas` and `totals` to the output. It marks the example `status: rejected`: kept in `examples.yaml` for audit (not deleted), dropped from the runtime ranker. Tell the person it's rejected and kept, not deleted.
- **`edit N`** that fixes *that example's* SQL → re-write it with `sm add-example` (dedups by question, so it replaces). Re-run the new SQL to capture its number.

When the user gives a **cross-cutting display/formatting rule** (currency, units, number formatting — applies to many examples, not one), classify it like any correction (see [`agami-save-correction`](../../agami-save-correction/SKILL.md) → "DISPLAY / FORMATTING preference"): a currency/unit fact attaches to the **column** (a `caveat`/`value_transform` in the shared model — org-wide by construction); a cross-cutting presentation convention → `datasource.md`; a personal tic → `USER_MEMORY.md`. Don't reflexively file to USER_MEMORY, and don't reflexively ask — only ask if personal-vs-org is genuinely unclear. **Display rounding is a convention, not SQL** — a "show fewer decimals" rule → `datasource.md`, never `ROUND()` baked into the example's SQL (that corrupts the exact-number verification anchor; the system formats numbers in full via `units.py`).

**`note all >>>…<<<` — the model-wide note. Apply it ONCE; the user never repeats themselves.** The full view page has a "note for the whole model" box (separate from per-example `note N`). Its content arrives as a single **`note all >>>…<<<`** block. Treat it as one correction that applies across the board: classify it like any other (see [`agami-save-correction`](../../agami-save-correction/SKILL.md)) and write it to the **right place, once** — a column `unit`/`caveat`/`value_transform` (a data fact: "amounts are in INR", "TOTAL can be negative"), a cross-cutting presentation convention → `datasource.md`, a personal display tic → `USER_MEMORY.md`, or a filter/business rule the user wants applied → the relevant column/example. Then **re-render so every affected example reflects it**, and name where it landed in one line — *"Got it — set `<col>`'s unit to INR model-wide; it'll show on every example now."* If the user is still typing the same thing into per-example `note N` boxes, that's a smell: lift it to a single model-wide change and tell them they don't need to repeat it. (Formatting is just the common case — the rule is generic: any cross-cutting fact is stated once, written once, applied everywhere.) Note the result preview is already unit-formatted — `sm seed-validate` runs numbers through the same `units.py` as the live query path — so a money column showing a **bare** number means a missing column `unit`, not a per-example note.

### 6a — A bad number is the highest-value catch: fix it COMPLETELY, and ask in PLAIN language

Validation often surfaces a result that's *obviously wrong* — a bounded ratio averaging to a huge negative number, a "count" of 0, an alphabetically-sorted "trend." This almost always means a **data-quality** problem: sentinel/junk values in a column (encoded nulls / "not computed" markers stored as extreme numbers like ±1e9), a mis-typed column (a date stored as a string), or similar. This is the most valuable thing onboarding can find — handle it deliberately:

1. **Diagnose the cause** with a quick probe — `MIN`/`MAX`, and a small histogram (`COUNT` per coarse bucket) so you can SEE where the junk sits (sentinels show up as a sharp spike at an absurd value, separated by a gap from the real data). Confirm what's actually wrong before proposing a fix.

2. **Exclude the SENTINELS — do NOT clip to a "textbook" range.** The bug is junk values, not real-but-extreme data, and the two are easy to confuse. Don't reflexively clamp to the range you *expect*: many columns legitimately pass their "obvious" bounds — a ratio can exceed 1 when its numerator really can exceed its denominator, an age can be 0, a balance can be negative — and clipping to the expected range silently drops real (often the most important) records and biases the result. Cut **only** the implausible sentinels (the absurd spike), and **when the line between "junk" and "legitimate-but-extreme" is unclear, ask the user for the real valid range — that's the user's domain knowledge, not something to guess.** The same holds for a unit or a label: values that look implausible for the stated unit are a question for the user, never a reason to reinterpret the column (2d).

3. **Fix it at EVERY level the bad data reaches — not just the seed.** A contaminated column poisons *three* paths, and fixing one leaves the others broken:
   - the **seed example** itself → correct its SQL;
   - any **metric** whose `bindings` SQL touches that column → guard the binding (wrap the aggregate in a `CASE WHEN <col> BETWEEN <lo> AND <hi> THEN <col> END`, with bounds drawn from the distribution + the user's domain input — never a hard-coded "textbook" range), and update its `calculation` prose to match;
   - **ad-hoc questions** that aggregate the column without naming the metric → add a **caveat** on the column (state the sentinel values to exclude, plus any non-obvious valid range the user confirmed) so the SQL generator guards them too.
   Use a `caveat` for this (advisory steer), NOT a `default_filter` (that wrongly drops the whole row from every query) and NOT a `value_transform` (you can't cleanly sanitize a sentinel). Apply all of it in **one `sm curate` batch**. A signed-off metric whose binding or calculation this changes goes back to review as `stale` (its old sign-off covered the old definition); when the user approves the corrected definition, add an `approve` op for it **after** the edits in the same batch, with the user's `--signer`/`--role`, which records the new sign-off.

4. **Ask ONE plain-language question — about the NUMBER, never the model's vocabulary.** A first-time user does not know what "bindings" or "caveats" are and cannot be asked to choose between them. **Never surface those words in the question.** Frame it by the wrong number and the consequence, in the user's own domain terms, and make the **complete fix the recommended default**. The skill decides *what* to edit; the user only confirms *whether* to fix (and, when the valid range is ambiguous, confirms that range). The pattern below is the *shape* — fill it with the user's actual column, value, and domain, not these placeholders:
   > The average <column> came out as **<impossible value>**, which can't be right — some rows hold junk values (e.g. <absurd sentinel>) that look like an encoded "missing", not a real <column>. I'd leave those out. One thing to confirm: can <column> legitimately go past <expected bound> in your data? If so I'll keep those — they look like the records that matter most.

   Say that in the chat, then call AskUserQuestion, one question, header `Bad number`, *"Fix the <column> values?"*:
   - **Fix it everywhere (Recommended)**: agami leaves the junk values out whenever it works with <column> (this metric and any question that uses it).
   - **Just this example**: fix this one example and leave everything else as it is.
   - **Leave it for now**: note it; it can be fixed later.

   "Other" takes the valid range in the person's words ("it can go up to 1.5"); use it for the bounds and treat the answer as **Fix it everywhere**.

   On **Fix it everywhere** → apply the seed + metric-binding + column-caveat edits as one batch. Do **not** present a "patch the bindings vs add a caveat" choice — that's the skill's call, not the user's. (If the user is technical and asks for the detail, then show it.)

When every example has its answer on a card (and any bad number above is settled), mark the step done and refresh the task list ([task-list.md](../../../shared/task-list.md)):
```bash
bash "<plugin_root>/scripts/sm" steps "$ROOT" --mark examples=done
```

---

### 6b — Check the reports the person shared (only when 1.5 saved some)

When 1.5 saved nothing, mark the step skipped and refresh the task list ([task-list.md](../../../shared/task-list.md)):
```bash
bash "<plugin_root>/scripts/sm" steps "$ROOT" --mark reports=skipped
```

When it saved some, mark the step started and refresh the task list:
```bash
bash "<plugin_root>/scripts/sm" steps "$ROOT" --mark reports=in_progress
```

When `<artifacts_dir>/local/reconcile/inbox/` has files (or a screenshot path) from 1.5, say in one line what was shared, then AskUserQuestion, one question, header `Reports`: *"Check your <n> reports against the new model now?"* Options: **Check them now (Recommended)** / **Later** (say they can run `/agami-reconcile` any time; the files stay in the inbox). **Check them now** hands the files to [agami-reconcile](../../agami-reconcile/SKILL.md), which grades any SQL part by part, compares the numbers, and asks about each mismatch on cards; come back to Phase 7 when it is done.

When agami-reconcile is done, mark the step done and refresh the task list (on **Later**, mark it `reports=skipped` instead):
```bash
bash "<plugin_root>/scripts/sm" steps "$ROOT" --mark reports=done
```

