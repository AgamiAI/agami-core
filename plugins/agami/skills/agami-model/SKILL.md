---
name: agami-model
description: "The semantic model in the chat: what needs sign-off and the subject areas with their tables, then a card — review what needs sign-off (approve / reject the AI-proposed metrics, entities and guessed joins; Rule 1 — a query using an unsigned metric still answers but carries a warning until it's approved), hide tables or columns from queries, look up a table or metric, or show the full view (the model explorer page: browse with live search, edit descriptions/metrics/entities/joins, add metrics, edit datasource.md). Every change is applied through the curation engine and gated by the validator before it touches the YAML. (This skill absorbed the former `/agami-review`.)"
when_to_use: "Use for BOTH model curation and trust review. Curation: 'open the model explorer', 'show me the model', 'browse my tables', 'exclude a table', 'remove this column', 'I don't want PII columns', 'add a metric', 'edit datasource.md', '/agami-model'. Review / sign-off: 'open the review dashboard', 'review my model', 'what needs review', 'sign off the metrics', 'approve the metrics', 'walk the review queue', '/agami-review' (now folded in here) — or after agami-connect's Phase 4 sign-off gate or Phase 7 summary prompts to review or inspect the model. Also use when the user replies to a previously-served full view with its back-channel block (exclude tables: … / curate-ops: … / new-metrics: … / done)."
argument-hint: "[review | preseed | rule1] — go straight to the sign-off review; no arg shows the overview"
---

# agami model

You are running the **model + trust** surface: see the live semantic model, curate it (hide / edit / add), and sign off the trust layer (approve / reject metrics, entities, joins). The chat is the surface: a short overview, then question cards. The model explorer page is the optional full view.

Conventions for every card here: [`shared/question-cards.md`](../../shared/question-cards.md). The review uses the same pattern and wording as [`agami-connect` Phase 4](../agami-connect/SKILL.md#phase-4-curate-before-examples--exclude--sign-off-what-seeds-depend-on), so the two reviews read as one product.

The flow:

1. **Overview** (Phase 1) — read the model with `sm review-items` and `sm model-tree`, show what needs sign-off and the subject areas with their table counts, then one card: **Review what needs sign-off** / **Hide tables or columns** / **Look up a table or metric** / **Show the full view**.
2. **Act on the pick** (Phases 2–5) — cards, a table or two, and `sm curate` for every write.
3. **Back to the card** — after each action, one line on what changed and the overview card again, until the person is done.

Trust-spine semantics — three actions on the same `review_state` field:
- **Exclude / Reject** → `rejected`. The loader drops rejected tables, columns, metrics, entities, and relationships entirely (`include_rejected=False` at runtime) — never in prompts, never joined, never aggregated. ("Hide" / "Exclude" is the verb for tables/columns; "Reject" for metrics/entities/joins — same op.)
- **Include** → back to `unreviewed`.
- **Approve** → `approved` + a sign-off stamp (`signed_off_by`/`_at`/`_role`). **Rule 1** (metrics) — a query that uses an unsigned metric still answers, and the metric arrives on the answer's receipt carrying its `review_state`, which is what puts a warning on the answer until it's approved; **Rule 2** (entities, inferred joins) is usable while unreviewed and self-approves through use, with an unreviewed join flagged on the answer. Only `rejected` entries are dropped from the runtime entirely. Approving requires the curator's email + role (the identity card, Phase 0) — the validator rejects an approved entry with no sign-off stamp.
- **Edit of an approved item → `stale`.** An edit that changes what an approved metric, entity or join computes or connects (a binding or calculation, unit, source tables, filters, an entity's mappings, a join's columns, `on:` or cardinality — any field but the cosmetic ones) sends it back to the review queue as `stale`; until someone re-approves it, it is not signed off, and the receipt says so. Only `description`, `other_names`, a metric's `business_question`, a metric's or entity's `caveats`, an entity's `plural`, and a join's `for_questions_about` are cosmetic and keep the approval; an edit that sets a field to the value it already has changes nothing. The old `signed_off_*` stay on a stale item as a record of the approval it lost. A batch that edits an item and then approves it ends approved by the approver (approvals go after edits); an approve followed by an edit ends `stale`. `sm curate` lists the items an edit sent back to review in `stale[]` — tell the person they need re-approving. `sm review-items`, `sm review-queue` and `sm approve-queue` show each stale item's `changes_since_approval` from the curation log (e.g. *binding (sqlite) changed from SUM(amount) to SUM(amount) - SUM(discount)*), or *changes not recorded* when the log doesn't go back that far (*some changes not recorded* at the end when a line after the edit can't be read).

## Conversation style

- **Tables and cards.** Every result is a short markdown table (at most 15 rows, saying how many more); every decision is a card. No page opens unless the person picks **Show the full view**.
- **Plain voice.** Say "database", not "profile". A successful change gets a one-line ack with its counts.
- **Qualified names everywhere.** Tables are `<area>.<table>`. Columns are `<area>.<table>.<column>` (area = subject area). No bare names.
- **"Other" you can't act on is re-asked, never guessed.** A question ("show me the SQL", "why?") is answered first, then the same card asked again ([question-cards rule 5a](../../shared/question-cards.md#5a-a-question-typed-in-other-is-answered-then-the-same-card-is-asked-again)). Otherwise say in one line what didn't match and ask the same card again.
- **Where AskUserQuestion is unavailable, ask the same as a short numbered list** ([`shared/question-cards.md`](../../shared/question-cards.md) rule 7); the options and their order stay the same.

---

## Phase 0: Preflight

- **Plan-mode check** — this skill writes YAMLs. If plan mode is active, refuse: *"I can't apply model edits in plan mode — switch to **Auto** or **Edit Automatically** mode (Shift+Tab to cycle) and re-invoke."* **Do NOT write a plan file. Do NOT call `ExitPlanMode`.**
- **Resolve `<profile>` and `<artifacts_dir>`** via the standard chain (`AGAMI_PROFILE` → `<artifacts_dir>/local/.config.active_profile` → `default`; `AGAMI_ARTIFACTS_DIR` → `.config.artifacts_dir` → `~/agami-artifacts`). `ROOT="<artifacts_dir>/<profile>"`.
  - **A pasted full-view block names its own target.** Its first line is `profile: <name>` — that page was rendered for THAT model, so **use it and override the active-profile default.** It prevents applying one page's approvals to whatever happens to be active (a *different* model). If `<artifacts_dir>/<profile>/datasource.yaml` exists for the named model, target it directly — don't fall back to `active_profile` or hunt with `find`.
- **If `<artifacts_dir>/<profile>/datasource.yaml` doesn't exist**, invoke `agami-connect` and stop — there's no model yet. First rule out an `AGAMI_PROFILE` that names a database with no model ([`shared/file-layout.md`](../../shared/file-layout.md#when-agami_profile-names-a-database-with-no-model)): when it does, say so and stop instead.
- **Verify Python + PyYAML are importable**: `bash "${CLAUDE_PLUGIN_ROOT}/scripts/py" -c 'import yaml'`. If not, surface the install hint and stop.

**Scope.** Look at `$ARGUMENTS`:
- `review` → skip the overview card and go straight to Phase 2 with every item waiting for sign-off.
- `preseed` or `rule1` (from `/agami-connect`) → Phase 2 with `--scope preseed` / `--scope rule1`.
- otherwise → Phase 1.

**The identity card (sign-off identity: asked once, on the first approval).** Needed to stamp sign-off on an approve, and recorded on `curation_log.jsonl` and commits. It is resolved lazily: only when a batch holds an `approve`. Before the first `approve` op of the session, read `reviewer_email` and `reviewer_role` from `<artifacts_dir>/local/.config` (the keys `/agami-connect` uses too); a pasted full-view block's `signed-off-by:` line also supplies them. When both are there, use them and don't ask. When either is missing, ask one card with two questions, the same card as agami-connect Phase 4:

- header `Email`, *"Which email should sign-offs be recorded under? Type it in Other below."*: options **Don't sign anything yet** (nothing is approved; the items stay as they are and you return to the overview card) and **Go back** (ask again the card that led here, with nothing approved). The email is typed in "Other", e.g. `you@example.com`; it is asked for in the question, never offered as an option, because an option like "Type it in Other" gets clicked with nothing typed ([question-cards rule 5](../../shared/question-cards.md#5-free-text-goes-in-other)).
- header `Role`, *"What is your role?"*: options **Data Lead**, **Engineer**, **Analyst**; "Other" takes any other role (≤ 40 characters).

The identity is typed by the person: **never infer it** from git config, the environment, the credentials, or the Claude login / session email (the host exposes it, but using it produces a silently-wrong audit trail), and don't pre-fill a domain. An email that doesn't match `\S+@\S+\.\S+` is said back and the same card asked again. Merge the two keys into `<artifacts_dir>/local/.config` (keep every other key), then `chmod 600 <artifacts_dir>/local/.config`, so neither this session nor `/agami-connect` asks again. Hide/edit-only sessions never need it.

---

## Phase 1: Overview and the card

Read the model from the engine; never walk the YAML yourself:

```bash
bash "${CLAUDE_PLUGIN_ROOT}/scripts/sm" review-items "$ROOT" --scope all
bash "${CLAUDE_PLUGIN_ROOT}/scripts/sm" model-tree "$ROOT"
bash "${CLAUDE_PLUGIN_ROOT}/scripts/sm" sensitive "$ROOT"
```

- `review-items` is a list of `{kind, area, name, title, tab, extra_lines, changes_since_approval?, …}`. What needs sign-off is every item whose `tab` is `review`; count them by `kind` (`metric`, `entity`, `relationship`), and count the ones carrying `changes_since_approval` (signed off before, changed since).
- `model-tree` is `{datasource, subject_areas:[{area, description, tables:[{table, review_state, columns:[…]}]}]}`. Per subject area, count its tables, and the hidden ones (`review_state: rejected`) apart.
- `sensitive` gives `count` (personal-data columns still queryable) and `suspected_count`.

Post the overview, in this order:

1. One sentence on sign-off, from the counts: *"4 metrics, 2 entities and 3 guessed joins need sign-off; 1 of them changed since it was approved."* or *"Nothing needs sign-off."*
2. A table of the subject areas, `Subject area | Tables | Hidden`, largest first, at most 15 rows, then *"Showing 15 of <A> subject areas; <A−15> more."* when there are more.
3. When `count` or `suspected_count` > 0, one line: *"<n> columns look like personal data and are still queryable."*

Then call AskUserQuestion, one question (header `Model`), *"What would you like to do with the model?"*:

- **Review what needs sign-off**: approve or reject the definitions waiting (Phase 2). Mark it **(Recommended)** and put it first when anything needs sign-off; leave it out when nothing does.
- **Hide tables or columns**: keep them out of every query (Phase 3).
- **Look up a table or metric**: its description, columns and caveats, in the chat (Phase 4).
- **Show the full view**: the model explorer page, served from localhost (Phase 5).

"Other" takes a request in the person's words: a name to look up ("orders"), something to hide ("hide sales.stg_leads"), an edit ("add a metric: refund rate = refunds / captured payments", "describe orders.status as …"), or "done". Route it to the phase it asks for; an edit becomes `sm curate` edit ops, or `sm add --kind metric` for a new metric, applied as Phase 6 says, then shown back in one line; "done" goes to Phase 7. A question ("show me the SQL", "why?") is answered first, then the same card asked again ([question-cards rule 5a](../../shared/question-cards.md#5a-a-question-typed-in-other-is-answered-then-the-same-card-is-asked-again)). Anything else you can't place is re-asked, never guessed.

**After each action** (Phases 2–4), post one line on what changed and ask this card again, with the counts recomputed. Dismissing the card, or "done" in "Other", ends the session (Phase 7).

---

## Phase 2: Review what needs sign-off

The same pattern and wording as agami-connect Phase 4 (sign-offs, the personal-data question, the identity card); only where it goes afterwards differs. Read the queue from the engine:

```bash
bash "${CLAUDE_PLUGIN_ROOT}/scripts/sm" review-items "$ROOT" --scope all
```

(`--scope preseed` or `--scope rule1` when `$ARGUMENTS` asked for it.) Keep the items whose `tab` is `review`. A metric's meaning is its `extra_lines` entry labelled `Definition`; an entity's is its `maps_to` (and `source_signal` when set); a guessed join's is its `title` and `cardinality`. An item with `changes_since_approval` was signed off before and has changed since: say so on its row, with what changed. Show a markdown table, `Kind | Name | What it means | SQL`, one short line per item taken from those fields (never a meaning you made up), at most 15 rows, then *"Showing 15 of <P>; <P−15> more."* when there are more. The **SQL** column is what the item maps to, so the person checks the formula and not only the words: a metric's binding for this database's engine from `bindings`, with its `source_tables`; a join's condition; an entity's `maps_to` column. Show it in backticks, cut at about 80 characters with `…` (the whole of it is answered on request, question-cards rule 5a).

Then call AskUserQuestion, one question (header `Sign-off`), *"What should I do with the <P> definitions waiting for sign-off?"*:

- **Approve all**: sign off every item as listed. Mark it **(Recommended)** and put it first only when the list is short and confident: every item fits in the table above (15 or fewer), none has `changes_since_approval`, and every metric has a `Definition`. Otherwise put **Review one by one** first and mark that one (Recommended).
- **Review one by one**: up to 4 items per card (below).
- **Skip for now**: nothing is signed; they still work, and answers that use one carry a warning until it is signed off.

| Answer | Do |
|---|---|
| Approve all | The identity card first if needed (Phase 0), then one batch: `{"op":"approve","kind":"<kind>","area":"<area>","name":"<name>"}` per item (leave `area` out when the item's `area` is null, a cross-area metric or join), applied with `bash "${CLAUDE_PLUGIN_ROOT}/scripts/sm" curate "$ROOT" --ops-file "<artifacts_dir>/local/tmp/agami-signoff.json" --signer "<reviewer_email>" --role "<reviewer_role>"`. Say how many were signed off, from its `applied`. |
| Review one by one | The one-by-one cards below. |
| Skip for now | Nothing is written. Back to the overview card. |

**Rule 1 guard.** A metric with an empty `Definition` can't be approved as it stands: on its card, ask for the definition in "Other" and apply it as an edit first.

**One by one.** Up to 4 items per card, one question each. `header` is `Metric 3/9`, `Entity 3/9` or `Join 3/9` (at most 12 characters; drop the word when the numbers are long). The question is short: the item's name and one plain sentence, with a real value when there is one, *"Approve revenue? Sum of paid order totals, refunds excluded."* **The SQL goes in the `Approve` option's `preview`**, the monospace pane a card shows beside a focused option, laid out over lines with a short comment on each filter (`AND status = 'paid'  -- paid orders`), the table it reads, and, for an item that changed since its approval, what changed. Never put the SQL in the question: a card question is plain text and a long formula there is unreadable. Options:

- **Approve**: sign it off as written.
- **Reject**: drop it from the model (`{"op":"reject",…}`).
- **Approve all remaining**: sign off this item and every item not yet shown. Every card carries it, so a long queue finishes in one press. Answers given on the same card to the other questions still stand.

After each card, apply its answers in one `sm curate` batch (approvals with `--signer`/`--role`), then ask the next card. Every approval goes through that one command with the person's identity; never mark an item approved any other way.

**"Other" on an item is a change, in the person's words** ("revenue should exclude cancelled orders"). Turn it into `sm curate` edit ops on the field the change names (`calculation`, `bindings`, `description` or `other_names` on a metric; `maps_to`, `description` or `other_names` on an entity; `cardinality` or the join columns on a join), e.g. `{"op":"edit","kind":"metric","area":"<area>","name":"<name>","field":"calculation","value":"<new definition>"}` plus the matching `bindings` edit when the SQL changes, and apply them with `bash "${CLAUDE_PLUGIN_ROOT}/scripts/sm" curate "$ROOT" --ops-file "<artifacts_dir>/local/tmp/agami-edit.json"`. Never hand-edit the YAML and never a generator script. Then ask about that item again, on the next card, showing its new meaning: an edit is not a sign-off.

**An "Other" that asks to stop is a stop, not a change.** When the words on an item card ask to stop, pause or finish later ("stop here", "I'll do the rest later", "pause") rather than change that item, apply the card's other answers, approve or change nothing more, say how many are still unsigned, and go back to the overview card.

**Any other "Other" you can't act on is re-asked, never guessed.** A question ("show me the SQL", "why?") is answered first, then the same card asked again ([question-cards rule 5a](../../shared/question-cards.md#5a-a-question-typed-in-other-is-answered-then-the-same-card-is-asked-again)). An empty change, a column or table the model doesn't have, or words you can't turn into one field's new value: say in one line what didn't match, and ask the same card again. The same when `sm curate` returns `errors` or `skipped` for the edit: show them, and ask the same card again.

**Personal-data columns (when `sm sensitive` reports `count` or `suspected_count` > 0).** Run `bash "${CLAUDE_PLUGIN_ROOT}/scripts/sm" sensitive "$ROOT"`: `{count, columns:[{area, table, column}], suspected, suspected_count}`. `columns` are flagged personal, `suspected` look personal but weren't flagged (a phone-like or birth-date-like sample, a name like `ip_address`). This question can share the sign-off card (two questions on one AskUserQuestion call). Show both in one table, `Table | Columns`, marking each suspected column "(suspected)", e.g. `customers | email, phone, ip_address (suspected)`; at most 15 rows and *"and N more"*. Then one multiSelect question (header `Personal`), **"Hide these from queries"**: *"These columns look like personal data. Which should queries never read?"*

Flagged and suspected columns are picked the same way:

- 3 columns or fewer in all: one option per column, `customers.email`, or `customers.ip_address (suspected)`.
- More: grouped by table, one option per table with its count, `customers (3: email, phone, ip_address (suspected))`; picking a table hides every listed column in it, suspected ones included. With more than 3 tables, the options are **All <n> listed columns** and the 2 tables with the most listed columns.
- Always last: **Keep them all queryable**.
- "Other" takes `table.column` names to hide, flagged, suspected or any other column in the model.

Hide the picks in one batch, `{"op":"exclude","kind":"table","area":"<area>","name":"<table>","column":"<column>"}` per column, with `bash "${CLAUDE_PLUGIN_ROOT}/scripts/sm" curate "$ROOT" --ops-file "<artifacts_dir>/local/tmp/agami-hide.json"`, then say how many columns are hidden and how many personal-data columns stay queryable (`sm sensitive`'s new `count`). **Keep them all queryable** writes nothing. A name in "Other" that isn't a column in the model, or **Keep them all queryable** picked together with columns to hide, is said back and the same card asked again; never guess which column was meant.

When the queue is done (or skipped), recount with `sm review-items` and go back to the overview card. **When agami-connect sent the person here** (its Phase 7 `Review what needs sign-off`), go back to agami-connect Phase 8 instead, the starter questions, so onboarding still ends on a first question.

---

## Phase 3: Hide tables or columns

Hidden tables and columns are never read by a query (`review_state: rejected`); they stay in the YAML, so they can be brought back with an `include`. Ask about groups, not one long list.

From the `model-tree` output, build the groups that apply, skipping anything already hidden:

- **Staging and backup tables**: names with a staging, temporary or copy marker (`stg_`, `_stg`, `staging`, `tmp`, `temp`, `backup`, `_bak`, `_old`, `_copy`, `test`).
- **Personal-data columns**: `sm sensitive`'s `columns` and `suspected` together.
- **Tables by subject area**: every visible table, grouped by its subject area.

Show a table of the groups, `Group | Count | First few`, the first 5 names in each and *"and N more"*. Then call AskUserQuestion, one multiSelect question (header `Hide`), *"Which of these should queries never read?"*, at most 4 options, the groups that apply in the order above:

- **Staging and backup tables (<n>)**: hides every table in the group.
- **Personal-data columns (<n>)**: asks the Phase 2 "Hide these from queries" question next, so the person picks which.
- **Pick by subject area**: a follow-up card (below).
- **Nothing for now**: writes nothing.

"Other" takes names in the person's words: `<area>.<table>` or `<area>.<table>.<column>`, comma-separated, to hide along with the picked groups; `keep <name>` keeps one member of a picked group out of it.

**Pick by subject area.** One multiSelect question (header `Area`), *"Which subject area's tables do you want to choose from?"*: the 3 subject areas with the most visible tables, one option each with its count; "Other" takes any other area's name. Then, per picked area, one multiSelect question (header `Tables`, at most 4 questions on a card), *"Which tables in <area> should queries never read?"*: with 3 tables or fewer, one option per table; with more, grouped by the name's first word (the part before the first `_`), the 3 largest groups as options with their counts, `orders (5: orders, orders_items, …)`; always last, **None of these**. "Other" takes table names in that area.

Build one ops batch from every pick: `{"op":"exclude","kind":"table","area":"<area>","name":"<table>"}` per table, `{"op":"exclude","kind":"table","area":"<area>","name":"<table>","column":"<column>"}` per column, written with the Write tool and applied with `bash "${CLAUDE_PLUGIN_ROOT}/scripts/sm" curate "$ROOT" --ops-file "<artifacts_dir>/local/tmp/agami-hide.json"` (Phase 6). Say how many tables and columns are now hidden, from its `applied`, and list any `skipped[]`. A name in "Other" that isn't in the model, or **Nothing for now** / **None of these** picked together with something to hide, is said back and the same card asked again; never guess which table or column was meant.

To bring something back, the person says so ("show sales.stg_leads again"): one `{"op":"include",…}` op with the same shape.

---

## Phase 4: Look up a table or metric

Call AskUserQuestion, one question (header `Look up`), *"Which table or metric? Type its name in Other."*: up to 3 likely names as options (the metrics waiting for sign-off first, then the tables of the largest subject area), at least 2. A name already given (in the overview card's "Other", or in the request) skips this card.

Match the name, case-insensitive, against the `model-tree` tables (`<table>` or `<area>.<table>`) and the `review-items` metrics and entities (`name`):

- **One match** → print it (below).
- **Several** (the same table name in two areas, a metric and a table with one name) → a card with one option per match, up to 4; more than 4, ask for the qualified `<area>.<name>` in "Other".
- **None** → say so, list up to 3 close names from the model, and ask the same card again. Never guess.

**A table.** Read it with `bash "${CLAUDE_PLUGIN_ROOT}/scripts/sm" context "$ROOT" --area <area> --tables <table>` and print:

- its description and grain, one or two sentences;
- its columns, `Column | Type | Meaning`, at most 15 rows and *"Showing 15 of <C> columns; <C−15> more."*, marking hidden columns "(hidden)" and personal-data ones "(personal)";
- its caveats and default filters, one line each, or *"No caveats."*;
- its joins, `Joins to | Cardinality`, when it has any.

A hidden table isn't in `sm context` (the runtime drops it): print its description and columns from `model-tree` and say it is hidden.

**A metric or entity.** From its `review-items` item: the `Definition` (a metric) or `maps_to` (an entity), each `Caveat` line, its `other_names`, and its sign-off state (`approved` by whom and when, or waiting, or changed since approval with what changed). Show the definition in words; leave the binding SQL out of the chat.

Then back to the overview card.

---

## Phase 5: Show the full view

The model explorer page, with every subject area, table, field, metric, entity, join and example, live search, the Review tab, edits, new metrics, and datasource.md. It is the only path that opens a page, and it is served from localhost, never as a `file://` page.

### 5a — Render and serve

```bash
ts=$(date -u +%Y%m%d-%H%M%S)
# Per-model subdir so a person with several databases can tell renders apart.
mkdir -p <artifacts_dir>/local/model/"$profile"
out="<artifacts_dir>/local/model/$profile/$ts.html"
bash "${CLAUDE_PLUGIN_ROOT}/scripts/py" render_model_explorer.py \
  --profile "$profile" \
  --artifacts-dir "$artifacts_dir" \
  ${initial_tab:+--initial-tab "$initial_tab"} \
  --read-only --out "$out"
bash "${CLAUDE_PLUGIN_ROOT}/scripts/py" serve_page.py "$out" --minutes 30
```

Run `serve_page.py` with `run_in_background` and read its first line, `{"url": "http://localhost:<port>/<name>"}`. Set `initial_tab=review` when the person came from the review (or `$ARGUMENTS` was `review`/`preseed`/`rule1`); leave it empty otherwise, and the renderer's `auto` default opens on **Review** when anything needs sign-off and on **Tables** when the model is clean.

Post the link and what the page does, in a few lines:

```
Full view: http://localhost:<port>/<name> (open for 30 minutes)
A read-only view of every table, metric, entity and join. Make changes on the card here:
approve, hide, or type an edit in Other ("add a metric …", "describe orders.status as …").
```

Then go back to the overview card. The page is read-only; a block pasted from an older, editable page is still applied (5b).

### 5b — The pasted block

A "Generate feedback for Claude" block from an older, editable page (rendered without `--read-only`) is still accepted. **Don't hand-parse it** — pipe it to the parser, which handles the whole grammar (the `profile:` line, the `exclude/include tables|columns:` lists, and the `curate-ops` / `new-metrics` / `new-examples` / `example-edits` / `key-terminology` / `datasource-md` JSON blocks) and translates the exclude/include lists into curate ops:

```bash
bash "${CLAUDE_PLUGIN_ROOT}/scripts/py" parse_model_feedback.py --block-file <pasted>
```

It prints `{data, anomalies, needs_judgment}`:
- `data.profile` — the page's target model. **Use it, overriding the active-profile default** (a block was rendered for THAT model; never apply it to whatever's active).
- `data.ops` — the merged curate ops array (exclude/include for tables/columns, translated, **plus** the `curate-ops` approve/reject/edit/move verbatim, with `move` ops last). Apply in one `sm curate "$ROOT" --ops-file <json>` call (Phase 6).
- `data.new_metrics_by_area` — `{area: [metric…]}` → one `sm add "$ROOT" --kind metric --area <area> --file <json>` per area.
- `data.examples_by_area` — edited + new NL→SQL examples `{area: [example…]}` → one `sm add-example "$ROOT" --area <area> --file <json>` per area (dedups by question).
- An example the person wants dropped rather than edited → `sm remove-example "$ROOT" --question "<area>/<question>"` (repeat `--question`; with no `--area` one call covers every area, and a bare question found in more than one area is refused, not rejected everywhere).
- `data.key_terminology` — the complete glossary object → `sm set-terminology "$ROOT" --file <json> --replace`.
- `data.datasource_md` — the full datasource.md text (already decoded) → **Write** it to `<artifacts_dir>/<profile>/datasource.md` (overwrite; human narrative only).
- `data.signer` / `data.role` — the curator identity for `--signer`/`--role` (recorded on every op; required on an approve). **Persist** them into `<artifacts_dir>/local/.config` (`reviewer_email`/`reviewer_role`; preserve other keys, `chmod 600`) so future sessions don't re-ask; if absent on an approve-bearing batch, fall back to `.config`, then the identity card (Phase 0).

**If `needs_judgment` is set, stop and ask** — `malformed_targets` (a table without its `<area>.` prefix → *"Tables need a subject-area prefix — `sales.STG_LEADS`, not `STG_LEADS`. Did you mean that?"*) or `unparseable_json` (re-copy that block from the page). Don't apply a partial block.

**Rule 1 guard (judgment):** before applying an `approve` on a metric, confirm its `calculation` is non-empty — for an introspected metric with an empty calculation, ask the person to fill it via an `edit` first. Show a one-line summary of what each block changed before applying.

### 5c — Re-render after a pasted block

Every successful apply of a pasted block renders a new page at a new timestamp and serves it again (5a), so the open page is never out of date without saying so. **Delete the previous timestamped file first** (`rm -f "<artifacts_dir>/local/model/$profile/$prev_ts.html"`); track `$prev_ts` across re-renders. Post the new link: *"Applied: 2 tables hidden, 2 columns hidden. Updated full view: http://localhost:<port>/<name> (the previous one can be closed)."*

---

## Phase 6: Applying changes with `sm curate`


Every write in this skill is one `sm curate` batch: the ops Claude builds from a card answer (Phases 2–4), or `data.ops` from a pasted full-view block (5b), which is already the merged ops array — exclude/include translated + the `curate-ops` approve/reject/edit verbatim. The shape, for reference:

```json
[
  {"op": "exclude", "kind": "table", "area": "sales", "name": "STG_LEADS"},
  {"op": "exclude", "kind": "table", "area": "sales", "name": "CUSTOMERS", "column": "EMAIL"},
  {"op": "approve", "kind": "metric", "area": "sales", "name": "total_revenue", "at": "2026-06-10T17:51:41Z"},
  {"op": "reject",  "kind": "entity", "area": "sales", "name": "region"},
  {"op": "exclude", "kind": "metric", "area": "sales", "name": "avg_discount", "reason": "not a real metric"},
  {"op": "approve", "kind": "relationship", "area": "sales", "name": "orders->customers", "at": "2026-06-10T17:51:41Z"},
  {"op": "move",    "kind": "table", "area": "sales", "name": "shipments", "to_area": "fulfilment"}
]
```

A `move` (the explorer's **Move to area**) gives the table a new owning area, created if the model doesn't have it yet. Its joins follow it (a join that now spans two areas becomes a cross-area join, and the reverse), and the metrics and entities on it move too once every table they read is in the new area. Ones that also read a table outside it stay; the result's `notes` lists them (*"left in sales: metric units_per_order also reads orders"*). A join the new area already holds is kept once: a person's sign-off over the engine's approval, and that over an unreviewed copy; two copies a person signed off that disagree refuse the move until one is approved or rejected to match. Pass `notes` on to the user verbatim; a batch that was undone reports none.

(Column exclusions use `kind: table` + a `column` field — the engine flips the column inside that table's YAML.) **Write the ops array to the file with the Write tool — never a heredoc, a shell variable (`printf "$OPS"`), or `python3 -c` (JSON quotes/`null` break those).** Then apply the whole batch atomically — the engine flips `review_state` / stamps sign-off, **validates the model, logs to `curation_log.jsonl`, commits (when the profile folder is its own git repository), and reverts everything on validation failure**:

```bash
bash "${CLAUDE_PLUGIN_ROOT}/scripts/sm" curate "$ROOT" \
  --ops-file "<artifacts_dir>/local/tmp/agami-model-ops.json" \
  --signer "${reviewer_email}" --role "${reviewer_role}"
```

`ROOT="<artifacts_dir>/<profile>"`. `--signer`/`--role` come from Phase 0. They are recorded in `curation_log.jsonl` on every op the batch applies (exclude, include, approve, reject and edit); an approve also stamps them on the item as its sign-off, while an edit that changes what an approved item computes or connects sends it back to review as `stale` instead of re-stamping it. An edit never sets a sign-off field (`review_state`, `signed_off_*`, `confidence`, `migrated_from`): `sm curate` skips it, so change a state with approve / reject / include. Resolve them before applying if the batch has any `approve` op — the validator rejects an approved entry with no sign-off stamp. Stdout is JSON: `{applied, skipped, errors, validated, committed}`, plus `commit_note` when nothing was committed (see the edge-case table) and `stale` when an edit sent approved items back to review.

**Success** (`validated: true`):
- Ack with the counts that apply, e.g. *"✓ Applied: approved 4, rejected 1, 2 columns excluded, 1 edit. Re-rendering…"*
- List any `skipped[]` (bad target / not found) on their own lines.
- Back to the overview card (Phase 1); after a pasted block, re-render the full view first (5c).

**Failure** (`validated: false`):
- The engine already reverted via git. Surface: *"⚠ Validator rejected the batch — all changes reverted. No files were modified."* Show `errors` verbatim. Stop; don't re-render.
- The same happens when an op with no `area` names a join, entity or metric that isn't in the model, or one that matches more than one entry (e.g. two joins between the same tables): the error names the op and the candidates. Re-send the op with `from_column` / `to_column` (or `area` for an entity or metric), never drop it. An op without `area` is searched for in every subject area and in the cross-area joins, entities and metrics (an entity or metric whose declared name equals the op's `name` exactly wins over others that only share its slug); an op that names an area and misses its target is listed in `skipped[]` as before.

Excluded entries vanish from the runtime model (the loader drops `review_state: rejected`) but stay in `sm model-tree` and the full view (both load with `include_rejected=True`) so the person can bring them back with an `include`.

---

---

## Phase 7: Closing

When the person says `done` (in a card's "Other" or as a message) or dismisses the overview card, post the final state from `sm model-tree` and `sm review-items`:

```
Done. <X> tables and <Y> columns are hidden; <P> definitions still need sign-off.
Hidden tables and columns are never read by a query. To bring one back, run /agami-model and
say which.
```

The skill is one-shot per invocation; re-enter via the slash command or a natural-language request.

---

## Edge cases

| Case | Behavior |
|---|---|
| User types `exclude tables: schema.table` for a table that's already excluded | The applier still runs (idempotent flip to rejected with the curator's name on the curation log). No-op for the YAML, but the audit trail records the re-confirm. |
| User types `include tables: schema.table` for a table that's already unreviewed | Same — idempotent flip; entry stays unreviewed; curator name recorded. |
| User types `exclude columns: schema.table.col` and the column doesn't exist | The applier returns it in `skipped[]` with reason "field not found on dataset". Surface the skip; continue with the rest. |
| User types `exclude tables: schema.table.col` (3 segments — looks like a column) | The grammar is positional; agami treats the `exclude tables:` prefix as authoritative. The applier will fail to find a table with that 3-segment name and return `skipped` with reason "table yaml not found". Surface clearly. |
| Validator fails on a partially-applied batch | The applier restores every file it touched from its own backups (no git needed) before returning. `applied` counts are zeroed in the response (defense against partial-apply confusion). The user sees `validator_output` verbatim and can fix + retry. |
| `committed: false` with a `commit_note` | Nothing went wrong — the change is written and validated; it just wasn't committed. Pass the note on in one line, in your own words: the model isn't in git (the change is still in `curation_log.jsonl` — `sm history <root>` lists what changed, newest first; `git init` in its folder keeps whole file versions too), or it's inside a larger repository (agami never commits there — tell them which folder changed so they commit it themselves). |
| The model has 5000+ fields | The renderer handles it (JSON manifest is ~hundreds of KB at the high end). Client-side search is instant up to the millions of DOM nodes mark. If it gets sluggish, file an issue. |

---

## Sign-off without cards (`sm approve-queue`)

The cards are the default; where there are no cards and no person to answer them (a script, or a host without AskUserQuestion that would rather not walk the queue as a numbered list), sign off the whole pending queue from the CLI instead. `sm approve-queue` reads the same queue Phase 2 shows (Rule 1 metrics/named-filters + Rule 2 joins/entities), stamps each item, and applies it in one call:

```bash
ROOT="<artifacts_dir>/<profile>"
bash "${CLAUDE_PLUGIN_ROOT}/scripts/sm" approve-queue "$ROOT" --signer you@example.com --role owner
# --kind metric|entity|relationship  → narrow to one type (repeatable)
# --dry-run                          → print the approve ops without applying
```

- A **stale** item (approved, then edited) is in this queue too; the output's `changed_since_approval` lists what changed on each since its last approval (or *changes not recorded*). Show it to the user before approving — re-approving records the new signer and time.
- **`--signer` and `--role` are required** — an approve must record *who* signed off (the validator rejects an approved entry with no sign-off stamp). The command self-stamps the `at` timestamp, so you don't build ops by hand.
- Afterward `sm review-queue "$ROOT"` shows `counts.total: 0` and `sm curate-gate "$ROOT"` drops `preseed_count` to 0.
- **PII does not block seeding.** `curate-gate` may still report `should_open_explorer: true` purely because sensitive columns remain queryable — that's **advisory** (your call to exclude or keep them), **not** a gate on seed generation. Only unreviewed **pre-seed** items (metrics/entities) block `seed-examples`; approving the queue clears that.

---

## What the runtime does with `rejected` entries

The trust spine has always read `agami.review_state`. The model loader in [`plugins/agami/skills/agami-query/SKILL.md → Phase 1c`](../agami-query/SKILL.md#1c--index-the-model-for-fast-access) filters entries with `review_state: rejected` out of `datasets_by_name`, `datasets_by_qname`, `fields_by_qname`, and `relationships_by_endpoints`. Rejected entries:

- **Never appear in the schema context** the SQL generator sees (Phase 2b).
- **Are not joinable** — the join-path picker skips relationships whose endpoints reference a rejected dataset.
- **Dropped from the runtime** — a *rejected* metric or named filter is excluded entirely; a query that would have referenced one gets a "metric not found" path, not an error. (An *unreviewed* metric is different — it's still used, and arrives on the receipt carrying `review_state: unreviewed`, which is what puts it in the report's approve/change banner.)
- **Stay in the YAML** for audit. The user can `include` them later without re-introspect.

Reject (on a review card, or the full view's Review tab) and Hide / Exclude (on tables/columns) are the same `rejected` operation — this skill is the one surface for both the bulk tables-as-units curation and the per-entry sign-off.

---

## Examples

### Hiding personal-data columns

```
You: I don't want PII columns in any query

agami: 3 metrics need sign-off.
       | Subject area | Tables | Hidden |
       |--------------|--------|--------|
       | sales        | 12     | 0      |
       | support      | 4      | 0      |
       Showing 2 of 2 subject areas.
       5 columns look like personal data and are still queryable.

[card "Model": Review what needs sign-off (Recommended) / Hide tables or columns /
 Look up a table or metric / Show the full view]  → Hide tables or columns

[card "Hide": Personal-data columns (5) / Pick by subject area / Nothing for now]
 → Personal-data columns (5)

[card "Personal": customers (3: email, phone, ip_address (suspected)) /
 leads (2: email, ssn) / Keep them all queryable]  → both tables

agami: Hidden 5 columns; 0 personal-data columns are still queryable.
```

### Removing staging tables

```
You: I never want agami to use the staging tables

[card "Hide": Staging and backup tables (2) / Pick by subject area / Nothing for now]
 → Staging and backup tables (2)   (the table above the card listed sales.stg_leads,
                                    sales.customer_score_stg)

agami: Hidden 2 tables.
```
