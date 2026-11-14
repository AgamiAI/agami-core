# agami-connect · Phase 7–8.5: the closing message and next steps

Part of [agami-connect](../SKILL.md). `<plugin_root>` in this file is the plugin's root directory: the absolute path agami-connect's SKILL.md shows in its own commands. Write that path into a command, quoted; never paste `<plugin_root>` or an unset variable into Bash. `<root>` is the database's model folder, `<artifacts_dir>/<profile>`, written out in full the same way. Each command below is its own Bash call, with no variable, pipe, `;`, `&&` or redirect on the line ([invocation-conventions.md](../../../shared/invocation-conventions.md)).

## Phase 7: The closing message (MANDATORY — NEVER SKIP)

**Sequencing gate (read first):** Phase 7 runs **only after the user has finished validating**: every Phase 4 card answered, the examples run (Phase 6) and any bad number settled, and Phase 8.5 asked when it applies. **Never post the closing message while a card is still unanswered**, and never ask a card after it: it ends onboarding's last turn.

Runs on **every** invocation that produces or refreshes a model — even if Phase 4 found nothing and all entries auto-approved.

**The closing message comes from the engine, never assembled by hand: it is the `say` of the step's start mark** ([engine-says.md](../../../shared/engine-says.md)). It holds the summary (the totals, the overview table, what waits for sign-off, how long setup took: *"Set up in 6 min 40 s"*, measured from the step marks), the table of the example questions kept in the model with what each returned (and the ones that failed their run, left out), how to drop or change one, and up to four questions to start with, numbered, ending with *"Reply with a number, or ask your own question."* **Every other call of this step comes first**: Phase 8.5, the overview card's tiles below, anything else. Then mark the step started ([task-list.md](../../../shared/task-list.md)):
```bash
bash "<plugin_root>/scripts/sm" steps "<root>" --mark first_question=in_progress
```
Its `then` says to update the task list from its `list` and then post its `say` exactly as written as your last message. Do exactly that: the task-list update is the last call, and the `say` is the whole of the message after it, with no closing sentence of your own before it or in its place and no card or tool call after it. Long text before a tool call does not reach the person; the last message of a turn does. Never count, time or round anything yourself. If its `say` does not contain the summary table (a problem line in its place, or no `say` at all), deal with what it says, then run `sm steps "<root>" --mark first_question=in_progress` again.

**Where the host can draw a visual in the chat, the overview card too** ([`shared/overview-card.md`](../../../shared/overview-card.md); how it is drawn, and its fallback: [`shared/native-elements.md`](../../../shared/native-elements.md)): one tile per subject area and one for what needs sign-off, every number from `sm overview`, drawn **before** the mark above. The tiles are drawn as well as the closing message, not instead of it (the closing message is the record in the transcript).

---

## Phase 8: The replies to the closing message

End the turn with the step still in progress: "Summary and first question" is done when the first question is answered, not when it is offered. The answer comes in a later turn, from agami-query, and its Phase 4f marks the step done then ([task-list.md](../../../shared/task-list.md)), however the question came:
```bash
bash "<plugin_root>/scripts/sm" steps "<root>" --mark first_question=done
```
A live run ticked the step while the first question was still unanswered. The person's reply is one of these; for a drop or a change, invoke agami-connect again with `resume` first (as SKILL.md says before its preflight steps), so its commands stay pre-approved:

| Reply | Do |
|---|---|
| A number from the list | That question, word for word as listed: hand it to [agami-query](../../agami-query/SKILL.md). |
| A question of their own | Hand it to agami-query. |
| Drop an example ("drop the refunds example") | Find it in the closing table (ask in one line which, when two match); write `<area>/<question>`, the question exactly as stored, with the Write tool to `<artifacts_dir>/local/tmp/agami-reject.txt`, then `bash "<plugin_root>/scripts/sm" remove-example "<root>" --question-file "<artifacts_dir>/local/tmp/agami-reject.txt"`. It is marked rejected and kept for audit, not deleted: say so in one line. |
| Change an example ("the revenue one should only count paid orders") | Rewrite its SQL, write it with `sm add-example` (same question, so it replaces), re-run its area with `sm seed-validate "<root>" --profile <profile> --area <area>`, and end the turn on the new result in one short table. A fix that touches a column or metric beyond this one example is Phase 6a's. |
| "Review the model", sign off, hide columns, "show the full view" | [agami-model](../../agami-model/SKILL.md) (`review` when they asked to sign off). |

A question about the closing message is answered first ([question-cards rule 5a](../../../shared/question-cards.md#5a-a-question-typed-in-other-is-answered-then-the-same-card-is-asked-again)); a reply you can't place is asked about in one line, never guessed.

---

## Phase 8.5: Cross-profile link offer (ONLY when ≥2 profiles now exist)

**Runs before Phase 7's closing message**, so onboarding still ends on it. **Gate:** run this **only** when, after this onboarding, the user has **two or more** onboarded profiles (count distinct onboarded profiles — `<artifacts_dir>/*/datasource.yaml`, or `[section]`s in `<artifacts_dir>/local/credentials`). Skip entirely for the first/only profile, and skip on a plain `reintrospect` of a single-profile setup. This is the **cross-datasource** case — different databases, joined at query time via federation (Phase 2b.federation in agami-query), declared in `<artifacts_dir>/local/cross_profile_relationships.yaml`. Unlike cross-*schema* joins (same DB, auto-detected — Case 1), cross-*profile* links are **never** auto-detected at introspection: each profile is a separate connection, so there's nothing to confirm by overlap.

**Ask (opt-in, AskUserQuestion):**
> You now have <N> databases connected (`<profileA>`, `<profileB>`, …). Want me to look for likely links between them — so you can ask questions that span both (e.g. join `<profileA>`'s data to `<profileB>`'s)?

Options (no `(Recommended)` — it's the user's call): `Yes — look for links` / `Not now` (default).

**On "Not now":** one line — *"No problem. Say 'link my databases' anytime, or hand-edit `<artifacts_dir>/local/cross_profile_relationships.yaml`."* Then Phase 7.

**On "Yes":**
1. **Propose candidates by name+type** — compare the just-onboarded profile's columns against the other profile(s)' columns (read each model's tables/columns; no DB access needed). A candidate is a column pair with the **same or obviously-equivalent name** (`dept_id` ↔ `department_id`, `customer_id` ↔ `cust_id`) **and a compatible type**, where one side is a key/grain column. Rank by name closeness.
2. **Be honest about confidence.** Say plainly: *"These are name/type guesses — I can't sample-join across two separate databases to confirm them, so they're lower-confidence than the joins inside one DB. They'll prove out the first time a federated query uses them."* Never present them as confirmed.
3. **Confirm each, never auto-write.** Say in one line how many candidates there are, then one multiSelect AskUserQuestion, header `Links`, with at most 4 options: up to 3 candidates, best-ranked first, one option each, its `preview` the pair (`<profileA>.<schema>.<table>.<col>  →  <profileB>.<schema>.<table>.<col>`), and **None of these**. With more than 3 candidates, the third option is **All <n> candidates** instead, its `preview` every pair, one per line (up to 15, then "and N more"), and "Other" takes the columns of the ones to keep; past that, ask the next 3 on another card. No table of them goes above the card: long text before a card does not reach the person ([engine-says.md](../../../shared/engine-says.md)).
4. **Write the confirmed ones** to `<artifacts_dir>/local/cross_profile_relationships.yaml` in the format [agami-query expects](../../agami-query/SKILL.md) — **merge**, never clobber an existing file (load it, append new entries, dedup by the `from_profile/from_dataset/to_profile/to_dataset` tuple); it holds no secret, so it needs no `chmod`:
   ```yaml
   version: "0.1.1"
   relationships:
     - name: <profileA>_<tableA>_to_<profileB>_<tableB>
       from_profile: <profileA>
       from_dataset: <schema>.<table>
       from_columns: [<col>]
       to_profile: <profileB>
       to_dataset: <schema>.<table>
       to_columns: [<col>]
       description: <one line — what the link means, in the user's terms>
   ```
5. **Confirm where it landed** — *"Saved <K> cross-database link(s) to `<artifacts_dir>/local/cross_profile_relationships.yaml`. A question that spans both DBs will now use them (federated via DuckDB). I'll flag low confidence on the first answer that does."* Don't block; this is additive. Then Phase 7.

---

