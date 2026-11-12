# agami-connect · Phase 7–8.5: summary and next steps

Part of [agami-connect](../SKILL.md). `<plugin_root>` in this file is the plugin's root directory: the absolute path agami-connect's SKILL.md shows in its own commands. Write that path into a command, quoted; never paste `<plugin_root>` or an unset variable into Bash.

## Phase 7: Post-introspect summary (MANDATORY — NEVER SKIP)

Mark the step started and refresh the task list ([task-list.md](../../../shared/task-list.md)); `$ROOT` is `<artifacts_dir>/<profile>`:
```bash
bash "<plugin_root>/scripts/sm" steps "$ROOT" --mark first_question=in_progress
```

**Sequencing gate (read first):** Phases 7–8 run **only after the user has finished validating** — i.e. after every Phase 6 example has an answer on a card (or, in the full view, they sent `done`), and after the Phase 4 cards were answered. **Never print this summary or the Phase 8 closing while a review or examples card is still unanswered** (a full view page being open doesn't hold it: the page is read-only and its card is asked again) — wait for the person, don't summarize over them.

Runs on **every** invocation that produces or refreshes a model — even if Phase 4 found nothing and all entries auto-approved. Lead with the **must-do** count, break out optional polish separately.

Scan the model; count by `confidence`/`review_state`/type:

```
agami-connect just ran. Here's what we found:

  ✓  <N> tables, <M> columns across <A> subject areas   (structure)
  ✓  <C>% of columns described                           (from `sm coverage` — never 0%)
  ✓  <K> relationships with join cardinality              (<E> confirmed from declared FKs)
  ⚠  <R1> inferred/probed relationships                   (review — confirm the join)
  ⚠  <R2> proposed metrics                                (sign-off — Rule 1)
  ✓  <S> sensitive columns flagged (never extracted)

  <R2 + stale> items need your sign-off to start querying.
  <R1> low-confidence joins can wait — they surface as warnings on the answers
  that use them and self-approve as you query.
```
(Omit any zero line. The closing two lines are mandatory — they tell the user "you can ship now; the tail is optional.")

**Then, where the host can draw a visual in the chat, the overview card** ([`shared/overview-card.md`](../../../shared/overview-card.md)): one tile per subject area and one for what needs sign-off, every number from `sm overview`. It comes after the block above (which stays: it is the record in the transcript), and a click on a tile carries on in the chat. Where the host can't draw one, the block alone is the summary.

When 2f drafted the company description, show it too, in one line: *"Company (draft): <description>. Correct it in /agami-model."* After the block, state the datasource's one-line description from `datasource.yaml` — *"Agents will pick this datasource by: "<description>" — edit it any time with `sm set-description`."* If you generated it in 2f, say it is a draft. If it is empty (they skipped 1.4 C), say so in one line: an organization with several datasources routes questions by this line.

Then **AskUserQuestion**: `Review what needs sign-off` (→ `/agami-model review` — approve or reject the pending metrics/entities on cards; when that review ends, come back here to Phase 8, the starter questions, rather than agami-model's overview card, so onboarding still ends on a first question) / `See the model` (→ `/agami-model` — the overview in the chat, then hide tables or columns) / `Show the full view` (→ `/agami-model`'s Phase 5: the model explorer page served from localhost; give the URL, then this card again) / `Skip — I'll review later` (default). If a sibling skill isn't built yet, omit that option — don't error.

---

## Phase 8: Follow-up suggestions

(No telemetry — agami has none; don't surface anything about it.)

**8a — gate on Rule 1 status:** count metrics/named-filters with `review_state != approved`. If > 0, use the **in-progress** framing (8b); else the **fully-set-up** framing (8c). Unsigned Rule 1 metrics don't *block* queries — agami still answers — but an answer that uses one carries the metric on its receipt with `review_state: unreviewed`, which raises the report's approve/change banner until you sign it off, so reviewing them is still worth doing.

**8b — in-progress:**
```
✓ <artifacts_dir>/<profile>/ — semantic model (<A> subject areas, validated)
✓ prompt_examples/ — <N> NL→SQL examples
⚠ <rule1_unreviewed> metric proposal(s) not signed off yet:
   - <M> metric proposal(s)
You can ask anything now — answers that use an unsigned metric just come with a
"not signed off yet" note on the receipt until you approve it.

Five things you could ask:
1.–5. <count / top-N / time-bucket / breakdown / recency — grounded in real tables>
Pick a number, or keep going:
• /agami-model review — sign off the pending metrics (+ review joins/entities) to clear the warnings.
• /agami-model — see the model in the chat and refine it (hide personal-data or staging tables, edit descriptions).
• Ask questions — if an answer's off, say "save this as a correction" and I'll teach the model.
```

**8c — fully set up:**
```
✓ <artifacts_dir>/<profile>/ — semantic model (<A> subject areas, validated)
✓ prompt_examples/ — <N> NL→SQL examples
✓ All metrics signed off

Now that <profile> is set up, here are five things you could ask:
1.–5. <count / top-N / trend / breakdown / narrative — grounded in the schema's distinctive tables>
Reply with a number, or ask anything else.

The model keeps improving as you use it:
• Just ask questions — and if an answer looks off, say "save this as a correction"
  (or paste the right SQL) and I'll teach the model so next time is right.
• /agami-model — the model in the chat: sign off metrics/joins/entities, hide tables
  or columns you don't want queried, add metrics, and edit descriptions.
```
Once the summary and these question choices are posted, before ending the turn, mark the step done and refresh the task list ([task-list.md](../../../shared/task-list.md)):
```bash
bash "<plugin_root>/scripts/sm" steps "$ROOT" --mark first_question=done
```

End the turn. Picking a number routes the question into query-database. Keep each suggestion under 80 chars and grounded in real tables.

---

## Phase 8.5: Cross-profile link offer (ONLY when ≥2 profiles now exist)

**Gate:** run this **only** when, after this onboarding, the user has **two or more** onboarded profiles (count distinct onboarded profiles — `<artifacts_dir>/*/datasource.yaml`, or `[section]`s in `<artifacts_dir>/local/credentials`). Skip entirely for the first/only profile, and skip on a plain `reintrospect` of a single-profile setup. This is the **cross-datasource** case — different databases, joined at query time via federation (Phase 2b.federation in agami-query), declared in `<artifacts_dir>/local/cross_profile_relationships.yaml`. Unlike cross-*schema* joins (same DB, auto-detected — Case 1), cross-*profile* links are **never** auto-detected at introspection: each profile is a separate connection, so there's nothing to confirm by overlap.

**Ask (opt-in, AskUserQuestion):**
> You now have <N> databases connected (`<profileA>`, `<profileB>`, …). Want me to look for likely links between them — so you can ask questions that span both (e.g. join `<profileA>`'s data to `<profileB>`'s)?

Options (no `(Recommended)` — it's the user's call): `Yes — look for links` / `Not now` (default).

**On "Not now":** one line — *"No problem. Say 'link my databases' anytime, or hand-edit `<artifacts_dir>/local/cross_profile_relationships.yaml`."* End.

**On "Yes":**
1. **Propose candidates by name+type** — compare the just-onboarded profile's columns against the other profile(s)' columns (read each model's tables/columns; no DB access needed). A candidate is a column pair with the **same or obviously-equivalent name** (`dept_id` ↔ `department_id`, `customer_id` ↔ `cust_id`) **and a compatible type**, where one side is a key/grain column. Rank by name closeness.
2. **Be honest about confidence.** Say plainly: *"These are name/type guesses — I can't sample-join across two separate databases to confirm them, so they're lower-confidence than the joins inside one DB. They'll prove out the first time a federated query uses them."* Never present them as confirmed.
3. **Confirm each, never auto-write.** Show the candidates as a table (`<profileA>.<schema>.<table>.<col>  →  <profileB>.<schema>.<table>.<col>`, at most 15 rows, saying how many more), then one multiSelect AskUserQuestion, header `Links`, with at most 4 options: up to 3 candidates, best-ranked first, one option each, and **None of these**. With more than 3 candidates, the third option is **All <n> candidates** instead, and "Other" takes the numbers or columns of the ones to keep; past that, ask the next 3 on another card.
4. **Write the confirmed ones** to `<artifacts_dir>/local/cross_profile_relationships.yaml` in the format [agami-query expects](../../agami-query/SKILL.md) — **merge**, never clobber an existing file (load it, append new entries, dedup by the `from_profile/from_dataset/to_profile/to_dataset` tuple), `chmod 600`:
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
5. **Confirm where it landed** — *"Saved <K> cross-database link(s) to `<artifacts_dir>/local/cross_profile_relationships.yaml`. A question that spans both DBs will now use them (federated via DuckDB). I'll flag low confidence on the first answer that does."* Don't block; this is additive.

---

