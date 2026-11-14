# agami-connect · Phase 1.0–1.6s: context and tables

Part of [agami-connect](../SKILL.md). `<plugin_root>` in this file is the plugin's root directory: the absolute path agami-connect's SKILL.md shows in its own commands. Write that path into a command, quoted; never paste `<plugin_root>` or an unset variable into Bash. `<root>` is the database's model folder, `<artifacts_dir>/<profile>`, written out in full the same way. Each command below is its own Bash call, with no variable, pipe, `;`, `&&` or redirect on the line ([invocation-conventions.md](../../../shared/invocation-conventions.md)).

## Phase 1: Introspect → semantic model

Mark the step started and refresh the task list ([task-list.md](../../../shared/task-list.md)); `<root>` is `<artifacts_dir>/<profile>`:
```bash
bash "<plugin_root>/scripts/sm" steps "<root>" --mark context=in_progress
```

### 1.0 — Set expectations before kicking off

Introspection can take a while against cloud DBs. Tell the user **before** the first probe. Honest estimates — **don't lowball** (a user told "5 min" who waits 4 thinks "almost there"; one told "1 min" thinks "stuck").

| db_type | Typical | Why |
|---|---|---|
| sqlite / duckdb | < 5s | local file |
| postgres / mysql (local) | 5–15s | fast catalog |
| postgres / mysql (cloud) | 15–60s | network RTT per query + FK overlap checks |
| **redshift / databricks / trino** | **~30s for ≤20 tables; 10–30+ min for 50+** | unenforced FKs → the relationship phase confirms joins by value-overlap, one scan per candidate over the network, and big fact tables dominate. **Scale your estimate with table count and warn explicitly.** |
| **snowflake** | **5–15 min** | cold-warehouse spin-up dominates; per-table queries, sample scans, EXPLAIN validation. A 100-table account measured ~12 min. |
| sqlserver / oracle | 30s–5 min | network + per-table catalog |

**Set a HONEST estimate — understating it is what makes a working run read as "stuck."** For a large schema (50+ tables) on Redshift/Databricks/Trino, say so up front: e.g. *"This is ~50 tables on Redshift — the relationship phase confirms joins by value-overlap, so expect **10–30 minutes**. I'll stream progress and report when it lands."* Then **stream the heartbeat** (background + tail, per 1.7) so the user sees `columns+grain 30/52` and `relationships: declared FK 80/187` rather than silence. For `reintrospect`, prepend "Re-introspecting (about as long as initial setup)."

### 1.1 — Existing-model check

If `<artifacts_dir>/<profile>/datasource.yaml` exists and `$ARGUMENTS != reintrospect`: the profile is already onboarded. When the start card (Phase 0, step 1a) already took the person's choice, act on it and skip this menu. Otherwise offer (AskUserQuestion, no `(Recommended)` — these are equal-weight choices), capped at 4:
- **Re-introspect `<profile>`** — refresh the structure from the live DB (new/changed tables, columns, FKs) while preserving descriptions, entities, metrics, caveats, and sign-offs (the `reintrospect` path; 2e: preview, then write).
- **Review the model** — the model overview in the chat, then cards to sign off definitions, hide tables or columns, or look one up (`/agami-model`; its explorer page is the full view, only when asked).
- **Onboard another database** — set up a **different** database (a different connection), leaving `<profile>` untouched. On this choice, **first ask whether it's the same company or a different one** (see 1.1a), then start a fresh onboarding: jump to the profile-naming step (Phase 0a's naming question) → have the user name the new profile (must differ from `<profile>` and any existing `[section]` in `<artifacts_dir>/local/credentials`) and pick its DB type → write that profile's `credentials.example` → run the full flow for it. Never reuse or overwrite the current profile's credentials or model.
- **Try the sample database** — explore agami's bundled **`agami-example`** sample (retail + subscriptions, no connection needed), leaving `<profile>` untouched. Routes to [Phase 0s](sample.md#phase-0s-sample-database-bootstrap-no-connection). This MUST be a real selectable option here (not a prose aside) — a returning user with an onboarded profile has no other visible path to the sample, since plain `/agami-connect` resolves their active profile and lands on this menu.

(Cancel isn't a listed option — the modal's Esc / "Other" covers "do nothing"; the four real choices are the actions above. Keep the list at exactly these four.)

**Same DB, another *schema*? That's the Re-introspect path, not "Onboard another database."** If the user wants to add a schema that lives in the **same database** they already onboarded (e.g. they did `public`, now they want `billing` too), choose **Re-introspect** and **expand the schema selection** in Phase 1.3 to include both the old and the new schemas. The engine scans them together in one pass, so any relationship between the original and the new schema is detected as a first-class **cross-schema** join (Case 1) and surfaced for review. Picking "Onboard another database" instead would split the two schemas into separate models and demote any link between them to manual cross-profile glue (Phase 2b federation) — wrong for one DB. If you're unsure which the user means, ask: *"Is `billing` in the same database connection as `<profile>`, or a different server/database?"* — same connection → Re-introspect + expand schemas; different → new profile.

The engine **auto-backs-up any legacy (v1) model** (`index.yaml` + per-schema `_schema.yaml`) it finds at the profile root into `.legacy_backup/` before writing — so a first run over an old profile is safe and reversible; surface a one-liner when that happens.

### 1.1a — Same company vs different company

When the user chooses **Onboard another database**, ask (AskUserQuestion, header `Same org`) whether the new database belongs to the **same company** or a **different** one. A deployment (one artifacts dir) holds exactly **one** company/organization with many datasources under it; a genuinely different company is a **different folder**.

- **Same company (the common case)** → the new profile **attaches under the deployment org**: it lands in the **same** `<artifacts_dir>`, shares the same `org_id` from the root `organization.yaml`, and **shares the company context** authored earlier. Do **not** re-ask the company description (1.4 detects the already-authored company context and skips it); the per-database note is still asked. Narrate it: *"This attaches under your **`<company name>`** organization, sharing the company context you set earlier — its per-database notes stay its own."*
- **Different company** → set up a **new deployment folder**. Pick/confirm a new artifacts dir path, create it, and **repoint** the pointer to it (the same steps Phase 0a.1 uses for a non-default dir): `mkdir -m 700 -p "<new_artifacts_dir>/local"`, one call that makes the folder and its private `local/`; then the Write tool writes the folder's absolute path as the only line of `<home>/.config/agami/path` (your home folder written out; the Write tool makes the folder); then `<new_artifacts_dir>/.gitignore` gets a `local/` line (Read it first with the Read tool, never `cat` in Bash: a missing `.gitignore` is not an error, it means there is none yet, so write the file with that one line with the Write tool; when it exists, add the line with the Edit tool).
  The current org's files are **untouched**; the first onboarding in the new folder mints a **fresh** `org_id` + its own company record (1.4 authors it there). This is the only way to run two companies from one machine — one folder each.

### 1.2 — Scope: schemas, and the no-catalog case

Run `cli areas`/probe is not needed yet — schema discovery happens inside the engine. But **decide scope first**:

- **Catalog reachable (common):** after the engine lists schemas, it introspects all of them. If the DB has many schemas (Snowflake with 50+), 1.6's summary shows them with their table counts and its **Only some schemas** answer narrows them; there is no separate schema question on a first onboard.
  - **Supabase is handled automatically** — the engine detects it by signature and drops its system schemas (`auth`, `storage`, `vault`, `realtime`, `extensions`, …) so only the user's app schemas (e.g. `public`) are modeled (it surfaces a note saying which it skipped). **Don't hand-build a `--tables` allowlist for Supabase** to dodge the system tables — that's already done, and a manual allowlist is fragile (shell word-splitting, missed tables). Only allowlist when the user genuinely wants a subset of their *own* tables.
- **Catalog denied (locked-down role):** if a quick probe shows the catalog isn't readable, the engine **cannot enumerate tables**, so ask for them on a card. Say first, in one line: *"Your role can read the data but not the catalog, so I can't list the tables myself; I'll describe each one from the data."* Then call AskUserQuestion, one question, header `Tables`, *"Which tables should I model? Type them in Other, e.g. `sales.orders, sales.customers`."*:
  - The names go in "Other" (the question says so; there is no option for typing them, since one would be clicked with nothing typed): comma- or line-separated, `schema.table` or a bare table name.
  - **Show the grant to ask for**: this database's read-only grant from [`shared/readonly-grants.md`](../../../shared/readonly-grants.md) in a fenced block, for the person to give their admin, then this card again.
  - **Stop for now**: nothing is introspected; say they can connect again once the role can read the catalog or they have the list.

  Write the names, one per line, with the Write tool to `<artifacts_dir>/local/tmp/agami-names.txt`. Where an inventory exists (a re-introspect, or a discover pass that listed some tables), check them first with `bash "<plugin_root>/scripts/py" inventory_summary.py --inventory "<inventory_path>" --keep names:<artifacts_dir>/local/tmp/agami-names.txt --tables-out "<artifacts_dir>/local/tmp/agami-keep.txt"`: an `unmatched` or `ambiguous` name is said back and the same card asked again, never guessed. With no inventory to check against, pass the file as it is. Then run the engine with `--tables-file` on that file (never an unquoted `--tables` list). Everything the engine then infers (types, grain, FKs) lands `unreviewed` for sign-off.

### 1.3 — Schema picker (re-introspect only)

**On a first onboard, skip this step.** 1.6's summary shows every schema with its table count, and its **Only some schemas** answer narrows them, so a schema question here would ask the same thing twice.

**On re-introspect / an existing model, ask which schemas to rescan, and make "add" vs "replace" explicit.** Read the schemas already in `<profile>`'s model (the distinct `schema` of its existing tables). Call AskUserQuestion, one multiSelect question, header `Schemas`, *"Which schemas should I rescan?"*, with at most 4 options:

- **The <n> schemas already in your model (Recommended)**: rescans exactly those (name them in the description). Picking only this is "nothing new, continue".
- Then the schemas not yet modeled: one option each when there are 2 or fewer; more are grouped by name prefix (as `groups.schema_groups` does in 1.6), one option per group naming its schema count, at most 2 such options.
- **All schemas**: every schema the catalog lists, when the options above don't cover them all.

"Other" takes schema names to add, or `drop <schema>` to take one out. Record `selected_schemas` as the union of what was picked; the engine scopes to these. Tell the user plainly:

> The engine re-scans exactly the schemas you check here and rebuilds the structure from them. To **add** a schema, leave the existing ones checked and tick the new one — relationships between them get found in the same pass. **Unchecking a schema removes its tables from the model** (its hand-edits are preserved only if you keep it checked).

This is the union-rescan that makes Case 2 work: adding `billing` to a model that already has `public` must scan `public` ∪ `billing` together — scanning only `billing` would build the new schema's internal joins but **miss every join back to `public`** (a cross-schema edge needs both endpoints in one introspection pass). Never silently re-scan just the delta.

**Reconcile what you'll scan vs what's actually there — NEVER silently shrink the scope.** When the user picks `All schemas` (or you narrow the set yourself), do NOT quietly hand the engine a smaller list. Count what the catalog holds (schemas + tables) and **state plainly what you're INCLUDING and what you're LEAVING OUT, with a reason for each exclusion**, then confirm. Three kinds get excluded and each must be named, not dropped silently:
- **System/internal schemas** — `pg_auto_copy`, `pg_*`, `information_schema`, Supabase `auth`/`storage`/… (the engine already drops these; still *report* them).
- **A schema that looks like a DIFFERENT dataset** — e.g. a `public` full of Salesforce objects (`accounts`, `opportunities`, `leads`, `contacts`, `pricebooks`) sitting next to ServiceNow module schemas. Don't fold a foreign domain into this model; **name it and offer it as its own profile**.
- **Tables you allowlist away** — any explicit `--tables`/`--tables-file` subset.

Example to say BEFORE introspecting: *"Found 70 tables across 18 schemas. I'll model the **52** in the 16 ServiceNow module schemas. **Excluding:** `public` (17 — `accounts`/`opportunities`/`leads`… looks like a separate Salesforce dataset; onboard it as its own profile if you want it) and `pg_auto_copy` (1 — Redshift system). Good?"* A user who said "all" must SEE the delta up front — never discover later that 19 tables never made it in. This is mandatory whenever discovered-count ≠ scanned-count.

### 1.4 — Datasource context (MANDATORY — ALWAYS ASK)

One card, asked right after the connection works and before tables are listed, with up to three questions on one AskUserQuestion call: the company (once per deployment), what the person can share (1.5), and this database's one line. None of the three depends on another's answer, so they are never asked one card at a time (a live run that did took three card rounds where one had done); act on the answers after the card, in the order A, B, C. Only the follow-ups that an answer leads to (1.5's follow-up cards, a `Replace it` below) come on cards of their own. They are required state-gathering: "don't ask clarifying questions" does NOT cancel them, and the skill never decides skip for the person. Conventions: [`shared/question-cards.md`](../../../shared/question-cards.md). The sample path skips all of them (its model is ready-made).

**A. Company question — ONCE, on the first onboard into this deployment.** Decide "already authored" by content, not by file existence: done only if the root `<artifacts_dir>/organization.md` exists **or** `<artifacts_dir>/organization.yaml` has a non-empty `name`/`description`. Otherwise the card carries this question, header `Company`:

> *"What's your company called, and what does it do? Type a sentence or two in Other, name first. It's shared by every database you connect here."*

Options: **Skip for now** (no company context; say it can be added later in `/agami-model`) and **Draft it from the database** (after the build, Phase 2f drafts a short description from the model and Phase 7 shows it as a draft to correct). Text in "Other" is the answer. Write it as both of these (the record write is the only thing that fills `organization.yaml`):
1. The name and the one-line summary, each written exactly as the person gave it with the Write tool to its own file, `<artifacts_dir>/local/tmp/agami-org-name.txt` and `<artifacts_dir>/local/tmp/agami-org-description.txt`, then `bash "<plugin_root>/scripts/sm" set-org "<artifacts_dir>" --name-file "<artifacts_dir>/local/tmp/agami-org-name.txt" --description-file "<artifacts_dir>/local/tmp/agami-org-description.txt"`. The person's text never goes on the command line, where the shell would read its quotes and symbols
2. The full text to the **root** `<artifacts_dir>/organization.md` under `# About this company` (**narrative prose ONLY** — no model facts).

Then confirm both landed. On a **later** onboard (company context already there) don't ask: say *"Sharing the **`<company>`** context you set earlier."*

**B. Context question — every onboard.** This is 1.5 below, the card's next question after A.

**C. About-it question — every onboard.** The one line `list_datasources` shows an agent so it can pick the right datasource (an organization with several refuses a query that names none), and, when the person writes more, this database's narrative. The card's last question, header `About it`:

> *"In one line, what does this database hold, and when would someone query it? Type it in Other, e.g. "Invoices, payments and products: use for revenue questions." Add more sentences if a term means something specific here."*

Options: **Generate one for me (Recommended)** / **Skip**.

**Nothing is written at this step** — `datasource.yaml` does not exist until introspection (1.7) creates it. Bind the answer and act on it after 1.7:
- **Text in "Other"** → its first sentence is `$DATASOURCE_DESCRIPTION`, written right after 1.7: the sentence, exactly as the person wrote it, with the Write tool to `<artifacts_dir>/local/tmp/agami-description.txt`, then `bash "<plugin_root>/scripts/sm" set-description "<root>" --description-file "<artifacts_dir>/local/tmp/agami-description.txt"`. The person's text never goes on the command line. When it has more than one sentence, the whole text also goes to `<artifacts_dir>/<profile>/datasource.md` under `# About this database`.
- **Generate one for me** → `$DATASOURCE_DESCRIPTION = generate`. **Phase 2f** writes it once every table is enriched, and the Phase 7 summary shows it as a draft they can edit.
- **Skip** → leave it empty. Say plainly that `model_deploy` will warn about it, and that an agent picking between several datasources will be guessing from the name.

If `datasource.yaml` already has a non-empty `description` (a re-onboard), show it and ask `Keep it (Recommended)` / `Replace it` instead. **Keep it** → `$DATASOURCE_DESCRIPTION = keep`; nothing is written. **Replace it** → ask the card above and bind from that answer. Without a narrative from this card, Phase 2f writes a short per-database starter from the enriched model.

These are **non-secret model files** and must stay readable (e.g. by the deploy container / a teammate reading a checked-in copy): the Write tool leaves them readable under the usual umask, so no `chmod` is needed; never `chmod 600` them (that's for `local/` secrets only). See [`shared/organization-context-format.md`](../../../shared/organization-context-format.md) for the content-routing rule (company-wide → root; per-database → the profile; per-column units → the structured model; personal → `USER_MEMORY.md`).

### 1.5 — Existing data model / semantic layer (MANDATORY — ALWAYS ASK)

The context question (1.4 B), on the intake card. Same "required state-gathering" rule. It has header `Context` and is **multiSelect**:

> *"What can you share so answers match how your team works? Pick any that you have."*

| label | description | then |
|---|---|---|
| `Definitions or a semantic layer` | A data dictionary, metrics list, ERD, spec workbook, or a dbt / LookML / Cube repo | A follow-up card: *"Type the file or folder path in Other, or attach the file to your next message."*, options **Skip this one** / **I'll attach it** (wait for the attachment, then carry on; if a reader is already running, say once that a permission prompt may appear for agami's own commands, since the turn ended while it ran). Then read it as the sections below say: a doc, a column dictionary, a model spec workbook, or a semantic-layer repo by what the path holds. |
| `Questions people ask` | Real questions from your team; they become checked examples | A follow-up card: *"Type them in Other, one per line, or a file path."*, options **Skip this one** / **I'll attach a file**. Save them with the Write tool to `<artifacts_dir>/local/tmp/agami-asked-questions.txt`, one per line; Phase 5 writes SQL for each and checks them with the rest. |
| `Report SQL or dashboards` | SQL you trust, an export, or a screenshot; checked against the model after the build | A follow-up card: *"Type a path to a .sql or .csv file in Other, or attach a screenshot to your next message."*, options **Skip this one** / **I'll attach it**. Copy files or pasted SQL into `<artifacts_dir>/local/reconcile/inbox/` (SQL as `<ts>.sql`, a CSV as is; note a screenshot's path). Phase 6b hands them to `/agami-reconcile`. |
| `Nothing to share` | Build from the database alone | Go to 1.6. Picked with another option: say it back and ask that question again. |

**"Other" on the context question** takes a named product ("it's ServiceNow", "this is our Salesforce") → the published-product-schema path below; or any of the above typed straight in (a path, questions, SQL), routed by what it is. A question about the options ("what's a semantic layer?") is answered first and the card asked again ([question-cards rule 5a](../../../shared/question-cards.md#5a-a-question-typed-in-other-is-answered-then-the-same-card-is-asked-again)).

Ask the follow-up cards one at a time, in the table's order. Nothing here is required: every follow-up's **Skip this one** goes on to the next.

**A model spec workbook is recognised by its sheets, whatever the user called it:** an `.xlsx` with `Subject areas`, `Tables` and `Joins` sheets is one, attached under the first option like any doc. Take the [model-spec path](build.md#18--apply-a-model-spec-only-when-one-was-attached) for it — **never `Read` it as a doc**: the parser reads every row, and a doc read is trimmed and paraphrased.

**Each document gets its own reader, started the moment its path is known.** A doc, a metrics list, a spec, or a dbt / LookML / Cube project (a project is one document: its files refer to each other) goes to the `agami-core:agami-doc-reader` subagent, one per document, launched in the background as [parallel-work.md](../../../shared/parallel-work.md) says, so it reads while the cards go on and the database is read (1.6, 1.7). At most 4 run at once; start the next when one finishes (after the card the person is on). The readers of this run are one batch: run `bash "<plugin_root>/scripts/sm" batch-id` when the first document's path is known and use its `batch` for every reader. Its description is the document in the person's words, *"Read the metrics spec"*; its prompt names `document` (the absolute path), `tables` ("not yet known" at this point) and `result_path`, `<artifacts_dir>/local/tmp/agami-doc-<batch>-<n>.result.json`, where `<n>` is the document's number in the order shared. Say once: *"Reading your spec in the background while we go on."*
- **Not given to a reader:** a model spec workbook (the model-spec path below) and a one-row-per-column dictionary (`$DICTIONARY_FILE`, below), which exact parsers apply; and a few lines typed into the card, which you read yourself.
- **Where the Agent tool doesn't list `agami-core:agami-doc-reader`**, launch a general-purpose subagent whose prompt is: *"Read `<plugin_root>/agents/agami-doc-reader.md` and follow it for this input: …"*. Where there is no Agent tool, read the document yourself as below.
- **Collect them before the enrichers start**: after the build in 1.7, or at the start of Phase 2 when the enrichers start there, with `sm wait-files` on their result files ([parallel-work.md](../../../shared/parallel-work.md); they don't block the cards or the build). Each file holds one JSON object: `descriptions`, `metrics`, `joins`, `terms`, each with its `source`, plus `questions` and `notes`. A reader not ready within the give-up limit, failed, or whose file isn't that object: say so in one line, read that document yourself as below, and go on.
- **Merge them into `$DATA_MODEL_DOC_TEXT`**: everything the readers found, grouped by table, with each item's document and `source`. Where two documents define the same thing differently (two meanings of "active customer"), ask one card per conflict, the two definitions as options with their sources, before Phase 2 uses either. A reader's `questions` are asked the same way, on cards, after the build.

**If a doc, read yourself** (no Agent tool, or a reader failed): `Read` the path (handles PDF/image/md/text/CSV natively; trim huge files to first 20 pages / 50 rows). Any other `.xlsx`/`.docx` → ask for PDF, proceed without if not.

**A column data dictionary is not read as a doc.** A CSV or JSON with one row per column (`table`, `column`, `description`, optionally `area` / `schema`) is the customer's own text for each column: note its path as `$DICTIONARY_FILE` and apply it after introspection with `sm describe-file --source metadata` ([2a.0](enrich.md#customer-dictionary-file)), which applies every row one to one. Don't `Read` it into the prompt (the read is trimmed and paraphrased) and don't convert it into curate ops yourself. A dictionary in another shape (PDF, prose, an `.xlsx`) → ask for a CSV export; if there is none, read it as a doc as above.

**If a published product schema (or the user names one — "use the ServiceNow data model online"):** the user wants the standard fields grounded in the authoritative reference, not your training memory.

1. **Prefer the in-DB metadata.** Most metadata-driven platforms carry their own dictionary *in the database* (ServiceNow `sys_dictionary`/`sys_choice`, SAP `DD03L`, …). That's authoritative AND machine-readable — far better than scraping docs. So the real answer here is usually **Phase 2a.0's `sm enrich-metadata`**, not a web fetch. Note that intent now; it runs during enrichment.
2. **Web reference as fallback / supplement.** When there's no in-DB dictionary, **`WebSearch`/`WebFetch`** the official developer/data-dictionary docs for the named product + the tables you discovered in 1.6. If you chose this path, the fetch is **mandatory, not optional** — do it once, upfront, and stash into `$DATA_MODEL_DOC_TEXT` (never written to disk). Note that vendor doc portals are often **JS-rendered and return only nav skeletons** (ServiceNow's does); when a fetch comes back empty, fall back to WebSearch result snippets / community pages and say so — don't silently substitute training knowledge.
3. **Propagate it.** When enrichment runs in subagents ([2a](enrich.md#2a--descriptions-describe-coded-columns-leave-only-self-evident-ones-empty)), **every enricher's prompt MUST include `$DATA_MODEL_DOC_TEXT`** with the instruction: *describe standard fields from this reference; if a field isn't in the reference or the data, mark it `ai_unknown` — do NOT fill it from general product knowledge.* (This is the hole that let subagents drift to "from ServiceNow domain knowledge.")

Stay anchored to the tables you actually introspected; don't invent fields the live DB doesn't have.

**If a semantic-layer repo, read yourself** (only where no reader could): use the directory from the follow-up card (a local clone / monorepo path — no upload needed since it's git-backed). Glob the **definition** files and `Read` them up to a budget (~30 files / ~250 KB total; if larger, prefer metric/model definitions and tell the user what you sampled). **Skip compiled SQL and data files** — you want the declared metrics/joins, not the warehouse output:
> | Layer | Read these | Carries |
> |---|---|---|
> | **LookML** | `*.view.lkml`, `*.explore.lkml`, `*.model.lkml` | dimensions, **measures** (→ metrics), **joins** (→ relationships), `sql_table_name` |
> | **dbt** | `models/**/*.yml` (esp. `schema.yml`), `semantic_models/**`, `metrics/**`, `dbt_project.yml` | column descriptions, `relationships` tests (→ FKs), MetricFlow metrics/measures |
> | **Cube** | `model/**/*.{yml,js}` (or `schema/**`) | `measures`, `dimensions`, `joins` |

Stash everything gathered (the readers' merged findings, or doc text + repo definitions you read yourself) as `$DATA_MODEL_DOC_TEXT` for enrichment — give entities/metrics/relationships found here **`confidence: inferred`** (a declared metric is a strong signal but still wants a human sign-off; FK-derived joins stay as the engine set them). **Never written to disk** — lives only in the enrichment prompt, then discarded. `Skip` → proceed.

When 1.5 saved no report SQL, export or screenshot, mark the report check skipped now, so it leaves the list:
```bash
bash "<plugin_root>/scripts/sm" steps "<root>" --mark reports=skipped
```

The context is gathered: mark the step done and refresh the task list ([task-list.md](../../../shared/task-list.md)):
```bash
bash "<plugin_root>/scripts/sm" steps "<root>" --mark context=done
```

### 1.6 — Discover & prune the table list (cheap first pass)

Mark the table step started and refresh the task list ([task-list.md](../../../shared/task-list.md)):
```bash
bash "<plugin_root>/scripts/sm" steps "<root>" --mark tables=in_progress
```

**Before the full, expensive introspection, summarise the tables in the chat and ask which to keep** — so staging/backup/scratch tables and dated snapshots can be dropped. The full grain/FK/description work then runs on **only the kept tables**, which on a big DB (hundreds of staging/snapshot tables) is the difference between minutes and hours.

**Run the discover pass** — it lists tables + columns only (no grain, FK, or row-count probes, so it's fast even over a tunnel):

```bash
AGAMI_SQL_MAX_ROWS=200000 bash "<plugin_root>/scripts/sm" discover --profile <profile> --db-type <db_type> --artifacts "<artifacts_dir>" --schemas <schema> <schema>
```

`--schemas` is the 1.3 pick; leave it out when you didn't narrow the schemas.

> **<a id="raised-cap"></a>Why the `AGAMI_SQL_MAX_ROWS` prefix — and why it must stay a prefix.** The executor **refuses** (never truncates) any result over the row cap, default 1000. That bound is sized for a *user query*, where a runaway is the thing it exists to stop. A **catalog read** blows past it on schema size alone — `information_schema.columns` for a 500-table catalog is tens of thousands of rows — and the engine **swallows that refusal silently**, degrading to one round-trip per table, or reporting "catalog enumeration denied" when the catalog was in fact readable. So the three commands that read catalog or dictionary tables — `discover` (1.6), `introspect` (1.7), `enrich-metadata` (2a.0) — run with a raised cap.
>
> - **Inline on the command, never `export`.** An exported value is still set when Phase 6 runs the demo *user* query, quietly raising the bound for exactly the caller it exists to hold. The inline form dies with the process.
> - **Never on `seed-examples` (Phase 5).** That command validates SQL which later runs as a real user query; validating under a raised cap green-lights examples that get refused in production.
> - **Tell the user you did it**, once, with the real numbers the commands print — and say their query-time cap is unchanged. See 2a.0 for the wording.

It writes the inventory to `<artifacts_dir>/<profile>/.introspect/inventory.json` and prints JSON with `table_count` and `inventory_path`. No page is written: everything about picking tables happens in the chat.

**Summarise it in the chat, then ask one card** (conventions: [`shared/question-cards.md`](../../../shared/question-cards.md)). No page opens and nothing waits on a pasted block.

```bash
bash "<plugin_root>/scripts/py" inventory_summary.py --inventory "<inventory_path>"
```

It prints `{tables, schemas, schemas_more, likely_noise, noise_all_copies, groups}`. Show:

1. One sentence: *"<N> tables in <K> schemas; <M> look like staging or backups: `stg_*`, `*_bak`, `*_2023`."* (name the real patterns from `groups.noise_by_pattern`; leave the second clause out when `likely_noise` is empty).
2. A markdown table, one row per entry of `schemas` (`Schema | Tables`), at most 15 rows, then the line *"and `schemas_more.schemas` more schemas (`schemas_more.tables` tables)"* when `schemas_more` is not null. Never list tables one by one.
3. The first 5 names in `likely_noise` with their `why` and, when it has one, the table it copies (`copy_of`), then "and N more", so the person sees what "Drop staging and backups" would remove (a real table that only looks like a copy shows up here before they press it). If they ask for the whole list, show up to 15 rows and "and N more".

Then call AskUserQuestion, one question (header `Tables`):

- **Keep everything**: model all `<N>` tables.
- **Drop staging and backups**: skip the `<M>` likely-noise tables. Offer it only when `likely_noise` is not empty and does not cover every table. Mark it **(Recommended)** and put it first only when `noise_all_copies` is true (every flagged table has a same-named counterpart, so dropping them loses no data); otherwise list it after **Keep everything** without "(Recommended)", since a flagged table with no counterpart may be real data. With no noise, or when it covers every table, leave this option out; in the second case say that everything looks like staging or backups and ask what to keep.
- **Only some schemas**: pick which schemas to model. Leave it out when there is a single schema.
- **Let me name them**: "Other" takes the table names. Names typed straight into "Other" count as this answer. If the option is picked with no names, ask a second card ("Which tables?", options "Keep everything" and "Drop staging and backups") whose "Other" takes the names.

Add one line before the card: *"Ask to see any schema's tables or any table's columns, or type columns to leave out in Other (`schema.table.column`)."*

Act on the answer, writing the kept set to `<artifacts_dir>/local/tmp/agami-keep.txt`:

| Answer | Do |
|---|---|
| Keep everything | Skip the file. Run 1.7 without `--tables-file`; columns typed in "Other" still go to `--exclude-columns` (below). |
| Drop staging and backups | `inventory_summary.py --inventory "<inventory_path>" --keep drop_noise --tables-out "<artifacts_dir>/local/tmp/agami-keep.txt"` |
| Only some schemas | **When the person shared questions or report SQL in 1.5, lead with the schemas they use**: run the summary again with `--used-by <each shared file>` (`agami-asked-questions.txt`, the files in `local/reconcile/inbox/`, a semantic-layer folder's SQL), and when `used_schemas` is not empty make the first option `The <k> schemas your SQL uses (Recommended)`, naming them in its description; tell the person in one line before the card how much of their material a pick would leave out when it drops one of those schemas. Then ask a follow-up AskUserQuestion, multiSelect. Up to 4 schemas: one option each. More: the options are `groups.schema_groups` (label, schema count and table count), and a picked group keeps all its schemas; "Other" takes schema names to add or to treat on their own. Name each group option's `schema_count` and `tables`. Add a "None of these" option only when that leaves fewer than 4 options; at 4, "Other" is the way out (a card holds 2 to 4 options). Then `--keep schemas:<a,b,c>`. |
| Let me name them | Write the names (one per line, `schema.table` or a bare table name) with the Write tool to `<artifacts_dir>/local/tmp/agami-names.txt`, then `--keep names:<artifacts_dir>/local/tmp/agami-names.txt`. |

`--keep` prints `{"tables_file", "kept"}`; say how many tables are kept and pass `--tables-file "<tables_file>"` to 1.7. **`{"error": "no tables kept"}` (exit 1, nothing written) means the choice left no tables: say so and ask the same card again.** **A name that matches no table, or a bare name that matches several, comes back as `{"unmatched": [...], "ambiguous": {...}}` with exit 1 and nothing written: tell the person which ones, and ask the same card again. Never guess.** Column exclusion is not asked here: personal-data columns are handled in review.

If the person asks to skip this ("just introspect everything"), proceed to 1.7 unscoped.

**Showing what is there (when asked).** "What's in sales?", "show me the columns of orders": list it in the chat from the inventory, then ask the same card again.

```bash
bash "<plugin_root>/scripts/py" inventory_summary.py --inventory "<inventory_path>" --show "<schema | schema.table | table>"
```

A schema prints its tables with their column counts; a table prints its columns with their types. Show them as a markdown table (`Table | Columns` or `Column | Type`), at most 15 rows, and *"and `more` more"* when `more` is not 0. `{"unmatched"}` or `{"ambiguous"}` (exit 1): say which name didn't match, or list the tables it could mean, and ask the same card again.

**Columns to leave out, typed in "Other".** Names of the form `schema.table.column` are columns to exclude from the build, whatever the table answer (Keep everything included) or on their own. Write them one per line with the Write tool to `<artifacts_dir>/local/tmp/agami-drop-columns.txt` and check them:

```bash
bash "<plugin_root>/scripts/py" inventory_summary.py --inventory "<inventory_path>" --check-columns "<artifacts_dir>/local/tmp/agami-drop-columns.txt"
```

`{"exclude_columns": [...]}` → pass them to 1.7 as `--exclude-columns <names…>` and say how many columns are left out. `{"unmatched": [...]}` (exit 1) → say which names are not columns and ask the same card again; never guess. Personal-data columns are asked about again on their own card after the build (Phase 4), and any column can be hidden later from `/agami-model`, so nobody has to find every column now.

Once the kept set is settled (or the person chose to model everything), mark the step done and refresh the task list ([task-list.md](../../../shared/task-list.md)):
```bash
bash "<plugin_root>/scripts/sm" steps "<root>" --mark tables=done
```

### 1.6s — With a model spec: the spec IS the kept set

The spec names the tables, so there is no table pick: mark the step skipped and refresh the task list ([task-list.md](../../../shared/task-list.md)):
```bash
bash "<plugin_root>/scripts/sm" steps "<root>" --mark tables=skipped
```

When a model spec workbook was attached in 1.5, convert it FIRST and skip the 1.6 table card — the spec already names every table to model. Its outputs go under `local/`, outside the profile, so moving a partial model aside (1.8) never takes them with it (`mkdir -p "<artifacts_dir>/local/model-spec/<profile>"` first, as its own call):

```bash
bash "<plugin_root>/scripts/py" model_spec_workbook.py parse --file "<workbook.xlsx>" --out "<artifacts_dir>/local/model-spec/<profile>/spec.json" --tables-out "<artifacts_dir>/local/model-spec/<profile>/spec-tables.txt"
```

It prints the counts it read (`subject_areas`, `tables`, `joins`, `joins_approved`, `joins_with_condition`, `metrics`, `sensitive_columns`, `rules`). **Quote those numbers verbatim — never count a sheet by reading it.** Exit 1 prints `errors` naming the missing sheet or column: show them and stop, the person fixes the workbook. Then run 1.7 with `--tables-file` set to `spec-tables.txt` as **ONE call — the single background call + progress tail (1.7 option 2), never `--append` batches**, however many tables the spec names. The spec is applied once, over the whole set, so read the whole set in one go.

