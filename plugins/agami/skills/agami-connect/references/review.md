# agami-connect · Phase 3–4: areas and sign-off

Part of [agami-connect](../SKILL.md). `<plugin_root>` in this file is the plugin's root directory: the absolute path agami-connect's SKILL.md shows in its own commands. Write that path into a command, quoted; never paste `<plugin_root>` or an unset variable into Bash. `<root>` is the database's model folder, `<artifacts_dir>/<profile>`, written out in full the same way. Each command below is its own Bash call, with no variable, pipe, `;`, `&&` or redirect on the line ([invocation-conventions.md](../../../shared/invocation-conventions.md)).

## Phase 3: Review the subject-area split

Mark the step started and refresh the task list ([task-list.md](../../../shared/task-list.md)):
```bash
bash "<plugin_root>/scripts/sm" steps "<root>" --mark signoff=in_progress
```

The engine **proposes** the split (one area for small DBs; for large single-schema ones, tables grouped where they join each other most densely — no target number of areas, none past the size limit, a hub table doesn't pull everything into one area — with tables that have no joins grouped by their whole first name word; each table owned once, cross-area joins as `cross_subject_area_relationships`). Area names keep the table's plural (`suppliers`, `status`). For a multi-area split, **surface it for the user to adjust** — boundaries are a curation decision, not a fact:

```
I split <N> tables into <A> subject areas:
  • <area1> — <t1, t2, …>
  • <area2> — <…>
<C> joins span areas (kept as cross-area relationships).
```

Show the split as above (at most 15 areas; say how many more). Its question (header `Areas`: `Looks good (Recommended)` / `Adjust`, with "Other" taking the change in the person's words, "move `refunds` to `payments`") is asked on Phase 4's first card, beside the sign-off and personal-data questions, so the split and the sign-off are one card round; nothing about the split is decided before then. Skip it silently for a single-area small DB. A change that names an area or table the model doesn't have is said back and the same card asked again; never guess which one they meant.

If they adjust (on that card), move tables with the curate `move` op — never by editing the `subject_areas/` tree: `{"op":"move","kind":"table","area":"<from>","name":"<table>","to_area":"<to>"}` (add `"schema"` when two schemas share the table name; a `to_area` the model doesn't have yet is created). Its joins follow it — a join that now spans two areas becomes a cross-area join and the reverse — and metrics and entities move with it once every table they read is in the new area; the result's `notes` list what it moved and what stayed because it also reads another table. The whole batch is validated and undone on failure (sizing warns at 25 tables, errors at 30).

**Replace each area's auto-proposed description with a real one** — the engine seeds `"Auto-proposed subject area covering: <tables>"`, which is boilerplate, but the **subject-area description is load-bearing**: it's what the MCP shows in its first pass so the LLM can ROUTE a question to the right area (`get_datasource_schema` returns name + description per area). A blank/boilerplate description means the router is guessing. So write one business line per area — what domain it covers and when you'd query it — grounded in its tables/columns (NOT product memory); after an adjustment, rewrite the lines of the areas it changed. Apply through the validated path, one batch (no generator script):
The ops file holds one `{op:edit, kind:subject_area, area:<name>, name:<name>, field:description, value:"<line>"}` per area:
```bash
bash "<plugin_root>/scripts/sm" curate "<root>" --ops-file "<artifacts_dir>/local/tmp/agami-area-desc.json"
```

---

## Phase 4: Curate before examples — exclude + sign off what seeds depend on

Seeds reference **columns, tables, metrics, and entities** — so settle those *before* generating examples (a seed that uses a column you'd later exclude breaks at query time, and a seed built on an unreviewed metric bakes in a guessed definition). **Relationships are NOT gated here** — they stay lazy: FK joins are already auto-approved by the engine (the DB declared them), and inferred joins self-approve as you query, and an unreviewed one raises the trust banner on the answer's report. So you're not asked to rubber-stamp database-declared foreign keys.

**4a — What needs a decision, asked on cards.** One call returns the counts (turn-boundary-safe — same answer on a fresh run or a resume):

```bash
bash "<plugin_root>/scripts/sm" curate-gate "<root>"
```
→ `{pii_count, preseed_count, should_open_explorer}`. **PII count** = columns flagged `sensitive` still queryable (an excluded column, or any column under an excluded table, isn't counted — so once the person hides them it drops to 0). **preseed count** = metrics + named-filters + entities needing sign-off (relationships are NOT gated — FK joins are engine-approved, inferred joins self-approve as you query). `should_open_explorer` only means "something to decide": nothing opens on this path.

> **PII is advisory, not a seed blocker.** A non-zero `pii_count` is a *review nudge* (the person's call to hide or keep), **not** a gate on seed generation. Only a non-zero `preseed_count` (unreviewed metrics/entities) blocks `seed-examples`. Don't tell the person personal data is stopping their seeds; it isn't.

Also run `bash "<plugin_root>/scripts/sm" sensitive "<root>"` every time: besides the flagged columns it prints `suspected` and `suspected_count`, columns that look personal but aren't flagged (`pii_count` doesn't count them).

**Guessed joins: one line, no card.** Read them with `bash "<plugin_root>/scripts/sm" review-items "<root>" --scope rule2` and keep the items whose `kind` is `relationship` (that scope also returns entities, which the Sign-off step covers). When there are any, post: *"<N> joins were guessed from names and data; they're used as you query and flagged on the answer until confirmed. Review them any time in agami-model."* With 5 or fewer, a short table under it (`Join | Kind`, the item's `title` and `cardinality`); more, just the line. They are not put on a card.

**Start the example writers now, before the first card** (on a resumed run too, with a new batch stamp), so example questions are drafted while the person signs off and hides columns ([parallel-work.md](../../../shared/parallel-work.md); Phase 5a collects them). With the Agent tool, every model gets them, one area or many: they run while the cards are answered.

1. **Stamp the batch, then prepare each area's input**: run `bash "<plugin_root>/scripts/sm" batch-id` once and use its `batch` in every file name below ([parallel-work.md](../../../shared/parallel-work.md)); then one pre-approved call per area (`sm areas "<root>"` lists them), the area's bundle with its tables, columns, metrics, entities and joins:
   ```bash
   bash "<plugin_root>/scripts/sm" bundle "<root>" --area <area> --out "<artifacts_dir>/local/tmp/agami-examples-<batch>-<area>.input.json"
   ```
2. **Launch one `agami-example-writer` per area in one message**, in the background as [parallel-work.md](../../../shared/parallel-work.md) says (`subagent_type` `agami-core:agami-example-writer`, `run_in_background: true` where the Agent tool has that parameter, at most 4 running at once), each described in words, *"Draft example questions for Sales"*. Each prompt names `area`, `input_path` (the file above), `result_path` (`<artifacts_dir>/local/tmp/agami-examples-<batch>-<area>.candidates.json`), `plugin_root` (the absolute path), `dialect` (the database type), and `asked_questions`: the path of `<artifacts_dir>/local/tmp/agami-asked-questions.txt` if 1.5 saved one, else "none". Where the Agent tool doesn't list it, use a general-purpose subagent told to *"Read `<plugin_root>/agents/agami-example-writer.md` and follow it for this input: …"*.

Then go on with the cards below; the turn stays open while the writers run. With no Agent tool, nothing starts here: Phase 5a drafts the examples inline.

**If `pii_count`, `preseed_count` and `suspected_count` are all 0 → say "Nothing to review before examples; continuing."** (after asking the Areas question on a card of its own, when Phase 3 has one) and go to Phase 5 (it collects the writers just started).

Otherwise run the two steps below that apply (conventions: [`shared/question-cards.md`](../../../shared/question-cards.md)). The Areas question, the Sign-off question and the personal-data question that apply go on one card: up to three questions on one AskUserQuestion call, asked once the split, the definitions table and the personal-data table are all posted. None of them depends on another's answer. Apply the answers in this order: the Areas change first (it moves tables, and their metrics with them), then the sign-off, then the columns to hide. The identity card and the one-by-one cards that an answer leads to come after it.

**Sign-offs (when `preseed_count` > 0).** Before the queue is read and the table and cards are put together, post one line (*"Gathering the definitions that need your sign-off…"*). Then read the queue from the engine:

```bash
bash "<plugin_root>/scripts/sm" review-items "<root>" --scope preseed
```

Each item is `{kind, area, name, extra_lines:[{label,text}], bindings, review_state, …}`. A metric's meaning is its `extra_lines` entry labelled `Definition`; an entity's is its `maps_to` (and `source_signal` when set). An item with `changes_since_approval` was signed off before and has changed since: say so on its row. Show a markdown table, `Kind | Name | What it means | SQL`, one short line per item taken from those fields (never a meaning you made up), at most 15 rows, then *"Showing 15 of <P>; <P−15> more."* when there are more. The **SQL** column is what the item maps to, so the person checks the formula and not only the words: a metric's binding for this database's engine from `bindings`, with its `source_tables`; a join's condition; an entity's `maps_to` column. Show it in backticks, cut at about 80 characters with `…` (the whole of it is answered on request, question-cards rule 5a).

Then the Sign-off question, on that one card (header `Sign-off`), *"What should I do with the <P> definitions waiting for sign-off?"*:

- **Approve all**: sign off every item as listed. Mark it **(Recommended)** and put it first only when the list is short and confident: every item fits in the table above (15 or fewer), none has `changes_since_approval`, and every metric has a `Definition`. Otherwise put **Review one by one** first and mark that one (Recommended).
- **Review one by one**: up to 4 items per card (below).
- **Skip for now**: nothing is signed; they still work, and answers that use one carry a warning until it is signed off.
- **Show the full view**: the model explorer page with every definition and its SQL, served from localhost (below); then this card again.

| Answer | Do |
|---|---|
| Approve all | The identity card first if needed (below), then one batch: `{"op":"approve","kind":"<kind>","area":"<area>","name":"<name>"}` per item (leave `area` out when the item's `area` is null, a cross-area metric), applied with `bash "<plugin_root>/scripts/sm" curate "<root>" --ops-file "<artifacts_dir>/local/tmp/agami-signoff.json" --signer "<reviewer_email>" --role "<reviewer_role>"`. Say how many were signed off, from its `applied`, then run `sm steps "<root>"` and refresh the task list. |
| Review one by one | The one-by-one cards below. |
| Skip for now | Nothing is written. Go to 4b. |
| Show the full view | The full view below, then this card again. |

**One by one.** Up to 4 items per card, one question each. `header` is `Metric 3/9` or `Entity 3/9` (at most 12 characters; drop the word when the numbers are long). The question is only the decision, with the item's `plain_name` from `review-items`: *"Sign off Revenue?"*, *"Sign off Orders to Customers by Customer ID?"* (never the identifier, `ela_proficiency_rate`, which goes in the preview; [question-cards rule 3b](../../../shared/question-cards.md#3b-things-are-named-in-plain-words)). The meaning and any real value go in the `Approve` option's description, kept apart from the title: *"Sum of paid order totals, refunds excluded. 2024-25: $4.2M"*. **The SQL goes in the `Approve` option's `preview`**, the monospace pane a card shows beside a focused option, laid out over lines with a short comment on each filter (`AND status = 'paid'  -- paid orders`), the table it reads, and, for an item that changed since its approval, what changed. Never put the SQL in the question: a card question is plain text and a long formula there is unreadable. Options:

- **Approve**: sign it off as written.
- **Reject**: drop it from the model (`{"op":"reject",…}`).
- **Approve all remaining**: sign off this item and every item not yet shown. Every card carries it, so a long queue finishes in one press. Answers given on the same card to the other questions still stand.
- **Show the full view**: the model explorer page, served from localhost; nothing about this item is decided, and the same card is asked again after the URL. On a card with several questions, offer it on the first question only.

After each card, apply its answers in one `sm curate` batch (approvals with `--signer`/`--role`), run `sm steps "<root>"` and refresh the task list ([task-list.md](../../../shared/task-list.md)), then ask the next card. Every approval goes through that one command with the person's identity; never mark an item approved any other way.

**"Other" on an item is a change, in the person's words** ("revenue should exclude cancelled orders"). Turn it into `sm curate` edit ops on the field the change names (`calculation`, `bindings`, `description` or `other_names` on a metric; `maps_to`, `description` or `other_names` on an entity), e.g. `{"op":"edit","kind":"metric","area":"<area>","name":"<name>","field":"calculation","value":"<new definition>"}` plus the matching `bindings` edit when the SQL changes, and apply them with `bash "<plugin_root>/scripts/sm" curate "<root>" --ops-file "<artifacts_dir>/local/tmp/agami-edit.json"`. Never hand-edit the YAML and never a generator script. Then ask about that item again, on the next card, showing its new meaning: an edit is not a sign-off.

**An "Other" that asks to stop is a stop, not a change.** When the words on an item card ask to stop, pause or finish later ("stop here", "I'll do the rest later", "pause") rather than change that item, apply the card's other answers, approve or change nothing more, and go to 4b's Continue / Pause card.

**Any other "Other" you can't act on is re-asked, never guessed.** A question ("show me the SQL", "why?") is answered first, then the same card asked again ([question-cards rule 5a](../../../shared/question-cards.md#5a-a-question-typed-in-other-is-answered-then-the-same-card-is-asked-again)). An empty change, a column or table the model doesn't have, or words you can't turn into one field's new value: say in one line what didn't match, and ask the same card again. The same when `sm curate` returns `errors` or `skipped` for the edit: show them, and ask the same card again.

**Sign-off identity: asked once, on the first approval.** Before the first `approve` op of the run (from Approve all, Approve, or Approve all remaining), read `reviewer_email` and `reviewer_role` from `<artifacts_dir>/local/.config` (the keys `/agami-model` uses). When both are there, use them and don't ask. When either is missing, ask one card with two questions:

- header `Email`, *"Which email should sign-offs be recorded under? Type it in Other below."*: options **Don't sign anything yet** (nothing is approved; the items stay as they are and you go to 4b) and **Go back** (ask again the card that led here, with nothing approved). The email is typed in "Other", e.g. `you@example.com`; it is asked for in the question, never offered as an option, because an option like "Type it in Other" gets clicked with nothing typed ([question-cards rule 5](../../../shared/question-cards.md#5-free-text-goes-in-other)).
- header `Role`, *"What is your role?"*: options **Data Lead**, **Engineer**, **Analyst**; "Other" takes any other role (≤ 40 characters).

The identity is typed by the person: **never infer it** from git config, the environment, the credentials, or the Claude login, and don't pre-fill a domain. An email that doesn't match `\S+@\S+\.\S+` is said back and the same card asked again. Merge the two keys into `<artifacts_dir>/local/.config` (keep every other key), so neither this run nor `/agami-model` asks again.

**Personal-data columns (when `pii_count` or `suspected_count` > 0).** From the `sm sensitive` output above, `{count, columns:[{area, table, column, label}], suspected, suspected_count}`: `columns` are flagged personal, `suspected` look personal but weren't flagged (a phone-like or birth-date-like sample, a name like `ip_address`). Show both in one table, `Table | Columns`, marking each suspected column "(suspected)", e.g. `customers | email, phone, ip_address (suspected)` (a table in the chat may show identifiers; the card names them in words); at most 15 rows and *"and N more"*. Then the personal-data question, on that same card: multiSelect (header `Personal`), **"Hide these from queries"**: *"These columns look like personal data. Which should queries never read?"*

Flagged and suspected columns are picked the same way:

- 3 columns or fewer in all: one option per column, named by its `label`, **Email on Customers**, or **IP address on Customers (suspected)**; the description says in plain words what kind of data it looks like (*"An email address"*, *"Looks like an IP address, not flagged"*), and the identifier (`customers.email`) goes in the option's `preview`, never in the label or the description ([question-cards rule 3b](../../../shared/question-cards.md#3b-things-are-named-in-plain-words)).
- More: grouped by table, one option per table with its count, the table and columns in words, **Customers (3: Email, Phone, IP address (suspected))**; the option's `preview` lists each column's identifier, one per line; picking a table hides every listed column in it, suspected ones included. With more than 3 tables, the options are **All <n> listed columns** and the 2 tables with the most listed columns.
- Always last: **Keep them all queryable**.
- "Other" takes `table.column` names to hide, flagged, suspected or any other column in the model.

Hide the picks in one batch, `{"op":"exclude","kind":"table","area":"<area>","name":"<table>","column":"<column>"}` per column, with `bash "<plugin_root>/scripts/sm" curate "<root>" --ops-file "<artifacts_dir>/local/tmp/agami-hide.json"`, then say how many columns are hidden and how many personal-data columns stay queryable (`sm sensitive`'s new `count`). **Keep them all queryable** writes nothing. A name in "Other" that isn't a column in the model, or **Keep them all queryable** picked together with columns to hide, is said back and the same card asked again; never guess which column was meant.

**Show the full view** (an option on the Sign-off and item cards, or asked for in words). The model explorer, with every metric, entity, join and column: render it the way `/agami-model preseed` does (`render_model_explorer.py … --initial-tab review --read-only`) and serve it from localhost, giving the person the URL:

```bash
bash "<plugin_root>/scripts/py" serve_page.py "<explorer.html>" --minutes 30 --open
```

Run it with `run_in_background` and read its first line, `{"url": …}`, for the URL to give the person.

The page is read-only; decisions stay on the cards, so after the URL ask the card it was picked on again. A "Generate feedback for Claude" block from an older, editable page is still accepted: apply it as `/agami-model` does, then continue at 4b.

**4b — after the cards:** **recount the sign-offs** with `bash "<plugin_root>/scripts/sm" review-items "<root>" --scope preseed` (`review-items`, not `review-queue`, which takes no scope). If **0** → Phase 5 (the seed command runs clean). If **> 0**:

- the person picked **Skip for now** or **Don't sign anything yet** → that is their decision to continue: say in one line that <n> definitions are unsigned and answers using them carry a warning, and go to Phase 5.
- they stopped part-way (an "Other" on an item card asked to stop, pause or finish later) → AskUserQuestion: `Continue (Recommended)` (seeds run against the current state; answers warn) / `Pause — I'll finish review first` (stop here; resume via `/agami-connect`). **At a Pause, with example writers running:** leave them; don't wait for them or draft their examples yourself ([parallel-work.md](../../../shared/parallel-work.md) names this as the one turn that closes with subagents running). **On resume**, the writers are always started again with a new batch stamp when Phase 4 is reached, so their bundles reflect the sign-off so far; files from before the Pause are not used.

This is the **only** place "continue to examples with items still unreviewed" is offered — and the **only** place you pass `seed-examples --after-review` (otherwise the preseed-review refusal is the engine telling you Phase 4 hasn't happened). **Personal data left queryable does NOT block seeds** — keeping a sensitive column queryable is the person's call; only unreviewed *sign-offs* gate the seeds.

On `reintrospect` with nothing flagged sensitive and nothing unreviewed, skip silently.

When the sign-off cards are finished (items left for later included, and when 4a found nothing to review; not on Pause), mark the step done and refresh the task list ([task-list.md](../../../shared/task-list.md)):
```bash
bash "<plugin_root>/scripts/sm" steps "<root>" --mark signoff=done
```

---

