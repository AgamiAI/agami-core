# agami-connect · Phase 0s: the sample database

Part of [agami-connect](../SKILL.md). `<plugin_root>` in this file is the plugin's root directory: the absolute path agami-connect's SKILL.md shows in its own commands. Write that path into a command, quoted; never paste `<plugin_root>` or an unset variable into Bash.

## Phase 0s: Sample-database bootstrap (no connection)

**Runs only when the person chose `Try the sample data` on the start card (Phase 0, step 1a)** (or said "I don't have a database" / "try the sample" / ran `agami-connect sample`). This **replaces Phases 0a, 1, and 2** with a short deterministic wire-up — no credential template, no hand-off turn, no live introspection on the fast path. The committed sample dataset + its prebuilt model live at **`<plugin_root>/samples/store/`** (`seed.sql`, `build_sample.py`, `model/`). Profile is always **`agami-example`**.

If `<artifacts_dir>/agami-example/datasource.yaml` already exists, the sample is already set up → skip to the demo query (step 7), or treat `reintrospect` as the 6B rebuild.

**Todo seed (use these instead of the 9-phase introspect seed).** The **copy** path (6A) skips introspect/enrich/seed entirely (it copies a pre-built model); the **rebuild** path (6B) *does* run introspect → enrich → seed, but with the sample carve-outs spelled out in 6B (no prune/org/doc prompts). Keep labels plain and user-facing — describe the action, not how good it is; no "wow"/"magic"/sales framing:

```
1. Set up the sample database
2. Build the local database file
3. Configure the sample profile
4. Load the semantic model
5. Run a first query
```

1. **Set up `local/`** (same as 0a.1): `mkdir -p "<artifacts_dir>/local" && chmod 700 "<artifacts_dir>/local"`. Ensure `<artifacts_dir>/.gitignore` ignores `local/`. Resolve `<artifacts_dir>` per Phase 0 (first run: ask via 0a.6, default `~/agami-artifacts`).

   Mark the step started and refresh the task list ([task-list.md](../../../shared/task-list.md)); here `$ROOT` is `<artifacts_dir>/agami-example`:
   ```bash
   bash "<plugin_root>/scripts/sm" steps "$ROOT" --mark connect=in_progress
   ```
2. **Resolve `$PY` + the agami-core package** (trimmed 0a.5/0a.5b): SQLite needs **no DB driver** (stdlib), so only ensure the **agami-core** package is importable in `$PY` — confirm via AskUserQuestion, then `bash "<plugin_root>/scripts/sm" install` (the one, one-time install via the `sm` launcher; brings pydantic/sqlglot/pyyaml and works with no `packages/` dir). Detect the `sqlite3` CLI with `which sqlite3` → `tier = cli` if present else `python`.
3. **Build the `.db`** into the gitignored `local/` (it's regenerable machine state, not committed):
   ```bash
   "$PY" "<plugin_root>/samples/store/build_sample.py" --out "<artifacts_dir>/local/samples/store.db"
   ```
   Uses the `sqlite3` CLI if present, else the stdlib builder. ~seconds; prints the size.
4. **Write the `[agami-example]` credential by reusing the deterministic promoter** — don't hand-roll the INI. Write a `credentials.example` whose `[agami-example]` section has the **resolved absolute** path from step 3, then promote it:
   ```ini
   [agami-example]
   type = sqlite
   path = <resolved abs path to local/samples/store.db>
   ```
   ```bash
   bash "<plugin_root>/scripts/py" promote_credentials.py
   ```
   `SECURED`/`APPENDED` → chmod 600, collision-safe. (No placeholders → never trips `PLACEHOLDERS_REMAIN`.)
5. **Write `<artifacts_dir>/local/.config`** (same shape as 0a.7): `active_profile: agami-example`, `artifacts_dir`, `tool_paths.python3 = $PY` (+ `sqlite3` if found), `tier`. `chmod 600`.

   The sample database is ready: mark the step done and refresh the task list ([task-list.md](../../../shared/task-list.md)):
   ```bash
   bash "<plugin_root>/scripts/sm" steps "$ROOT" --mark connect=done
   ```
6. **Fork — copy the ready-made model, or rebuild it live? (AskUserQuestion).** Ask once, before touching `<artifacts_dir>/agami-example/`:
   > The sample comes with a ready-made semantic model. Want to query it right away, or watch agami build that model from the data first?

   | option | branch |
   |---|---|
   | `Just let me query it (Recommended)` | **6A — copy** |
   | `Build the model from scratch so I can see it work` | **6B — rebuild live (~5–10 min)** |

   - **6A (copy, < 1 min):** `mkdir -p "<artifacts_dir>/agami-example"` then `cp -R "<plugin_root>/samples/store/model/." "<artifacts_dir>/agami-example/"`. **Validate it loads here**: `bash "<plugin_root>/scripts/sm" validate "<artifacts_dir>/agami-example"`. If it fails, surface the errors and stop — never leave a half-wired profile. Then **mint the deployment `org_id`** (a *copy* carries no id — introspect mints inline, a copy bypasses it): `bash "<plugin_root>/scripts/sm" ensure-org-id "<artifacts_dir>/agami-example"` — idempotent, writes a locally-minted uuid into `datasource.yaml` so the sample deployment has a portable identity like any other. Then **stamp a model_version** (a *copy* doesn't go through introspect/curate, so nothing auto-stamps it): `bash "<plugin_root>/scripts/sm" snapshot "<artifacts_dir>/agami-example"` — best-effort, so the answer receipt shows a version rather than `null`. (We stamp at copy time instead of committing a static `.snapshots/` so it always matches the model's actual content; 6B gets one automatically from introspect.)

     Then refresh the task list ([task-list.md](../../../shared/task-list.md)); the copied model finishes the steps it covers:
     ```bash
     bash "<plugin_root>/scripts/sm" steps "$ROOT"
     ```
   - **6B (rebuild live — "watch it build"):** ignore the committed `model/` and run the **normal Phases 1→2** against the `agami-example` profile (`--db-type sqlite`) — the same introspect → enrich → seed pipeline a real onboarding uses, just pointed at the sample SQLite file. It takes a few minutes (the non-default option). **Don't mention tokens, cost, or billing** — surface time (~5–10 min), not scary money words.
     - **Sample carve-outs — the dataset is small + curated, so DON'T prompt (build silently over ALL tables):** skip the [Phase 1.6](context-and-tables.md#16--discover--prune-the-table-list-cheap-first-pass) **table** card, skip the **org-description** prompt (Phase 2f / 0a), and skip the **doc/metrics intake** (Phase 1's "do you have a data dictionary / dbt repo?"). These prompts exist for a real unknown DB; for the sample they're noise. Introspect + enrich every sample table without asking.
     - **Clear the pre-seed gate before seeding (the silent build has no human to sign off).** Enrichment lands the sample's metrics/entities `unreviewed`, so `seed-examples` would refuse with `preseed_review_pending`. Because the sample is curated and trusted, **auto-approve the queue as a system signer** right before seeding — the silent path clears its own gate:
       ```bash
       bash "<plugin_root>/scripts/sm" approve-queue "<artifacts_dir>/agami-example" --signer system --role system
       ```
       (This is only sanctioned for the curated sample; a real database keeps the human sign-off gate.)
     - **When the model validates, show what was built in the chat**, the overview [`/agami-model`](../../agami-model/SKILL.md#phase-1-overview-and-the-card) posts (its Phase 1: read the model with `sm model-tree` and `sm review-items`, never the YAML): one sentence on what needs sign-off (after the system approval above, usually nothing) and the `Subject area | Tables | Hidden` table. This is the whole point of "watch it build"; a wrap with no numbers would defeat it. Post it *together with* step 7's short dataset description + starter questions (so don't cede the turn). No page opens: the model explorer is the full view, rendered and served from localhost by `/agami-model` only when the person asks for it. (Do **not** render the NL→SQL examples-validation page either.)

   **6A** → **step 7** (describe + stop). **6B** → the model overview above, then step 7's description + starter questions. Both end with a validated `<artifacts_dir>/agami-example/` model.
7. **Wrap up — describe the dataset, offer questions, then STOP. Do NOT auto-run a query.** This is the entire closing for the sample path: **skip the rest of Phases 3–8** (no introspect summary, no "re-introspect `<profile>`" / "when you want the real thing" framing — that pushes the user off the sample they just picked and can surface another profile's name). The user asked to *query* the sample, not watch a scripted demo — so hand them the keys, don't drive. **(6B adds the model overview in the chat, see 6B. Neither path opens a page.)**
   - **Short description (2–3 lines, plain):** what the dataset is, drawn from `<artifacts_dir>/agami-example/datasource.md` — Acme Store, a retailer with one-time **commerce** (customers, products by category, orders, line items, payments, refunds) and recurring **subscriptions** (plans, subscriptions, invoices). ~500 customers, ~4,000 orders over ~2 years. State it; don't sell it.
   - **A card of starter questions — and DO NOT answer any until one is picked.** AskUserQuestion, one question, header `Ask`, *"What would you like to ask? Pick one or type your own in Other."*, no `(Recommended)`. Four options, **at least two of them multi-table joins** so the person sees agami handle real joins, not just single-table counts, each with the tables it joins as its description:

     | label | description |
     |---|---|
     | `Revenue by product category` | Line items joined to products and categories |
     | `Top categories per channel` | Which categories drive the most revenue in each sales channel |
     | `Top 5 customers by spend` | Orders joined to customers |
     | `Active subscriptions and MRR` | Subscriptions joined to plans |

     (The first two are the multi-table joins. All four are backed by EXPLAIN-validated seed examples in the shipped model, so they answer reliably. "Other" takes any question, a monthly trend or "customers with both an order and a subscription" among them.) Where AskUserQuestion is unavailable, list the same four as a numbered list ([question-cards rule 7](../../../shared/question-cards.md#7-where-askuserquestion-is-unavailable)).
   - Then **stop and wait.** Nothing about agami-serve / agami-model / corrections yet — let them experience one real query in Claude Code first. When the user picks a question or asks anything, hand to [agami-query](../../agami-query/SKILL.md) — it answers AND runs its own first-query flow (the one-time GitHub-star ask + `/agami-serve` pointer, gated by `local/.optins`). **Do NOT pre-narrate fan/chasm traps or "what agami caught."** If their question hits a trap (questions 1–3 can), agami-query surfaces it in the answer's receipt — let it happen on a real question instead of scripting it.
8. **The "where to go deeper" footer (corrections / `/agami-model` / `/agami-serve`) fires from agami-query, not here.** When the user asks their first sample question, agami-query owns the turn — so its Phase 4f surfaces a one-time orientation footer for the `agami-example` profile (see [agami-query SKILL.md → Phase 4f](../../agami-query/SKILL.md)). Don't try to print it from Phase 0s — a footer placed here never executes once agami-query has taken over. Phase 0s ends at step 7 (describe + questions + stop).

---

