---
name: agami-export
description: "Writes the active database's reviewed semantic model as the files a Snowflake agent (a semantic view, an agent and its evaluation files), a Databricks agent (metric views, a Genie space, Pages, Unity Catalog scripts) or an Apache Ossie consumer loads, through `sm export`, and reports in the chat every item it left out or carried with less, grouped by reason. Asks its questions on cards: which formats, where the tables live, which Snowflake warehouse, and which opt-in extras. Everything that would change a catalog or show content to other people stays off unless the person picks it on a card. Reads the model and writes files under the artifacts dir only: it never connects to a warehouse, never calls a vendor, never applies what it writes, and never translates SQL between engines."
when_to_use: "Use when the user says 'export my model', 'export my model to Snowflake / Databricks / Ossie', 'make a Genie space from my model', 'write the model as a Snowflake semantic view', 'give my Snowflake agent this context', 'take the model to Databricks', or '/agami-export'. Requires agami-connect to have run first (needs a semantic model). This skill writes files only; applying them is the user's act in their own account. To approve the items that stay behind as not approved, use `/agami-model`."
argument-hint: "[snowflake | databricks | ossie]"
---

# agami export

You are writing the reviewed semantic model as files another agent loads: Snowflake (a semantic view, an agent, evaluation files), Databricks (metric views, a Genie space, Pages, Unity Catalog scripts) or Apache Ossie. Goal: the person picks the formats and the few values the files need on cards, `sm export` writes them under the artifacts dir, and the chat says what each target got, what stayed behind and why, and where the files are. Applying the files is the person's own act in their own account; this skill never does it.

Conventions for every card here: [`shared/question-cards.md`](../../shared/question-cards.md). What every flag writes and risks, the order to apply the files in, and the report's fields: `docs/context-export.md` in the agami-core repository.

This skill orchestrates:

1. **Check** plan mode, then find the database's model and count what will stay behind (Phases −1 and 0).
2. **Ask** on one card: the formats, where the tables live, the Snowflake warehouse, and the opt-in extras, with one follow-up card for the extras picked (Phase 1).
3. **Confirm** the exact command on a `Run it?` card, then run `sm export` (Phase 2).
4. **Summarise** in the chat: per target the files and what they carry, what stayed behind grouped by reason, the flags left off, and where the files are (Phase 3).

## Conversation style

- **Tables and cards.** Every result is a short markdown table (at most 15 rows, saying how many more); every decision is a card. No page opens: the summary is the chat, and the files are on disk.
- **Plain voice.** Say "database", not "profile". Name a format by what it is ("a Genie space"), and a flag by what it does; the flag itself goes in an option's `preview`.
- **"Other" you can't act on is re-asked, never guessed.** A question ("what does this change?", "why do I need a location?") is answered first, then the same card asked again ([question-cards rule 5a](../../shared/question-cards.md#5a-a-question-typed-in-other-is-answered-then-the-same-card-is-asked-again)). Otherwise say in one line what didn't match and ask the same card again.
- **Where AskUserQuestion is unavailable, ask the same as a short numbered list** ([`shared/question-cards.md`](../../shared/question-cards.md) rule 7); the options and their order stay the same.

---

## Phase −1: Plan-mode preflight

Run the detection logic from [`shared/plan-mode-check.md`](../../shared/plan-mode-check.md). This skill writes the export files and runs `sm` through Bash. If plan mode is active, refuse with: *"I can't export in plan mode — it writes the export files under your artifacts folder. Switch to **Auto** or **Edit Automatically** mode (Shift+Tab to cycle) and re-invoke me."* **DO NOT** write a plan file or call `ExitPlanMode`.

---

## Phase 0: Preflight

1. **Resolve the environment.** `bash "${CLAUDE_PLUGIN_ROOT}/scripts/py" connect_resolve.py` prints JSON; read `data.profile` and `data.artifacts_dir`. `ROOT="<artifacts_dir>/<profile>"`. An `env_profile_has_no_model` anomaly means `AGAMI_PROFILE` names a database with no model ([`shared/file-layout.md`](../../shared/file-layout.md#when-agami_profile-names-a-database-with-no-model)): say its `detail` in one line and stop.
2. **Model present.** `<ROOT>/datasource.yaml` must exist. If it doesn't, there is nothing to export: invoke `/agami-connect` and stop.
3. **The model's engine.** Read `storage_type` from `<ROOT>/datasources/<connection>/storage.yaml`. It decides whether a location is required (Phase 1) and whether a target can carry the model's SQL.
4. **Count what stays behind.** These commands read the model and write nothing, so they run before any choice is made:

```bash
bash "${CLAUDE_PLUGIN_ROOT}/scripts/sm" model-tree "$ROOT"
bash "${CLAUDE_PLUGIN_ROOT}/scripts/sm" review-items "$ROOT" --scope all
bash "${CLAUDE_PLUGIN_ROOT}/scripts/sm" sensitive "$ROOT"
```

- **Not approved:** tables and columns from `model-tree`, and metrics, entities and joins from `review-items`, whose `review_state` is neither `approved` nor `rejected`. Rejected items never travel and are not counted.
- **Withheld as sensitive:** `sensitive`'s `count`. Use the count only: never print its `columns`, here or anywhere in this skill.

Say it in one or two sentences before the card: *"14 items are not approved yet and stay behind (3 metrics, 2 joins, 9 columns); 2 columns are flagged sensitive and stay behind too. Example and golden questions not yet confirmed also stay behind, and the summary after the run counts every one."* When both counts are zero, say *"Everything in the model is approved."* These counts come from read-only commands rather than from a practice export, because that run would write files before the person chose anything. The export's own report (Phase 3) is the full count, examples and golden questions included.

---

## Phase 1: Ask

### 1a. One card

One AskUserQuestion call, up to four questions. Leave a question off when it cannot apply: `Targets` when `$ARGUMENTS` named the formats, `Location` when the named formats never need one, `Warehouse` when Snowflake is not a possible pick. No `(Recommended)` on any of these: which formats, where the tables live and which warehouse are facts about the person's accounts.

- header `Targets`, *"Which formats should I write the model in?"*, `multiSelect: true`:
  - **Snowflake**: a semantic view, an agent on it, and evaluation files when golden questions exist.
  - **Databricks**: metric views, a Genie space and Pages.
  - **Ossie**: one Apache Ossie file per subject area.
- header `Location`, *"Which database and schema hold these tables on Snowflake or Databricks? Type it in Other as database.schema (catalog.schema on Databricks)."* Required for Snowflake always (the view is created in that schema), for Databricks unless the model is written for Databricks, and for Ossie unless the model is written for Snowflake or Databricks; it is passed as `--location`. Options:
  - **Not needed**: only for a pick that runs without one, as above.
  - **Not sure yet**: nothing is written; find where the tables are loaded and ask again.
- header `Warehouse`, *"Which Snowflake warehouse should the agent run its queries on? Type its name in Other."* A name of letters, digits, `_` and `$`, not starting with a digit, all one case; it is passed as `--snowflake-warehouse`. Options:
  - **None**: no warehouse is written; the agent uses the default warehouse of whoever calls it, and does not run for someone with none.
  - **Not sure yet**: the same as None for now; the agent file's header shows the lines that add one later.
- header `Opt-in`, *"Should the files also change your catalog or show content to others? Each extra is off unless you pick it."*, `multiSelect: true`. Offer only the groups for formats that are picked or still on the card. Each group's `preview` lists every flag in it with its risk, one per line:

| Option | Description | `preview` |
|---|---|---|
| **None — keep all off** | The files change nothing until you apply them, and publish nothing | — |
| **Snowflake extras** | Certification tags, an MCP endpoint, or evaluation files without stored answers | `--snowflake-certification-tags`: marks objects certified for everyone who can see them<br>`--snowflake-mcp-server`: opens an endpoint MCP clients can call<br>`--snowflake-no-expected-answers`: keeps result rows out of your account |
| **Databricks catalog changes** | Table and column comments, keys and certification tags in Unity Catalog | `--uc-comments`: replaces existing comments<br>`--uc-constraints`: adds keys and NOT NULL; fails on a NULL<br>`--uc-certify`: marks objects certified for everyone |
| **Databricks sharing** | Published Pages, sensitive columns marked hidden in the Genie space, a page per metric | `--pages-publish`: every user of the domain sees the pages<br>`--genie-hide-sensitive`: writes sensitive column names into the space file<br>`--pages-metrics`: one more draft page per metric |

After the card:

- A location answer of **Not needed** for a pick that requires one, or **Not sure yet**, is said back in one line (*"A Snowflake export needs the database and schema its view is created in."*), and the `Location` question is asked again on its own. Never guess a location, never take one from the credentials or the model, and never fill in an example such as `demo.sales_data`.
- **None — keep all off** picked together with a group, or a group for a format not picked in `Targets`, is said back in one line and the `Opt-in` question asked again. A flag for a format the run does not write is never passed: the command refuses it.
- A warehouse answer is used only when Snowflake is picked.

### 1b. The extras, one by one

When any group was picked, one more card: one `multiSelect` question per group picked, each with **None of these** last. Every flag below stays off unless its option is picked here.

header `Snowflake`, *"Which Snowflake extras should the files carry?"*

| Option | Description | `preview` |
|---|---|---|
| **Mark approved items certified** | Tags the view and its members from their review state | `--snowflake-certification-tags`<br>Changes the catalog: the tags mark objects certified for everyone who can see them. Applying the view then needs an application role that only account admins hold by default. |
| **Add an MCP server** | A server exposing the view and the agent as tools | `--snowflake-mcp-server`<br>Opens an endpoint on the account that MCP clients can call with the grants you give. |
| **Leave expected answers out** | The evaluation file stores no result rows | `--snowflake-no-expected-answers`<br>By default, running the evaluation file stores up to 20 rows of each golden question's result in your account. This turns that off. |
| **None of these** | Keep all three off | — |

header `Catalog`, *"Which Unity Catalog changes should the files carry?"*

| Option | Description | `preview` |
|---|---|---|
| **Replace comments** | Table and column comments from the model's descriptions | `--uc-comments`<br>Changes the catalog: each statement replaces the existing comment, including one a person wrote. The undo file cannot bring the old one back. |
| **Add keys** | Informational primary and foreign keys | `--uc-constraints`<br>Changes the catalog: SET NOT NULL checks every row and fails on a NULL; adding a constraint may upgrade a table's writer protocol. |
| **Tag approved items certified** | Certification tags on approved tables and metric views | `--uc-certify`<br>Changes the catalog: marks those objects certified for everyone who can see them. |
| **None of these** | No Unity Catalog file is written | — |

header `Sharing`, *"What should the Databricks files show to others?"*

| Option | Description | `preview` |
|---|---|---|
| **Publish the pages** | Approved Pages are written as published, not drafts | `--pages-publish`<br>Makes the pages visible to every user of the domain, and the assistant there uses them in every conversation. Without it a page is a draft only its owner sees. |
| **Hide sensitive columns in the space** | The Genie space marks them hidden | `--genie-hide-sensitive`<br>Writes those columns' names into the Genie space file, the only place an export names one. Hiding is not access control: a query that names the column still reads it. |
| **Add a page per metric** | One more Page for each metric | `--pages-metrics`<br>Draft pages unless published; the metric views already carry each definition. |
| **None of these** | Keep all three off | — |

**None of these** picked with another option is said back and that question asked again.

**Only on request.** These are never on a card and never passed unless the person asks for them in their own words (in "Other" or a message): `--include-unreviewed` (carry the items not yet approved; they are never marked certified), `--include-sensitive` (sensitive column names, and the SQL and text that use them, go into every file: say so in one line before the `Run it?` card), `--area <subject area>` (one subject area only), and `--ossie-version <version>` (a format version other than the default, as the person names it).

---

## Phase 2: Confirm and run

Build the command from the answers: one `--target` per format picked, `--location` and `--snowflake-warehouse` when given, and exactly the flags picked in Phase 1. Then a card, header `Run it?`, *"Write the export files now?"*:

- **Write the files**: its `preview` is the exact command below, filled in.
- **Change an answer**: the Phase 1 card again, with the answers given so far.
- **Don't export**: nothing is written; end here.

```bash
mkdir -p "<artifacts_dir>/local/tmp"
bash "${CLAUDE_PLUGIN_ROOT}/scripts/sm" export "$ROOT" \
  --target <format> [--target <format> …] \
  --out "<artifacts_dir>/local/exports/context/<profile>" \
  [--location "<database>.<schema>"] [--snowflake-warehouse "<NAME>"] [<flags picked>] \
  > "<artifacts_dir>/local/tmp/agami-export.json"
```

The output folder is fixed: `<artifacts_dir>/local/exports/context/<profile>`, which git ignores. Each run replaces a target's folder there whole, so a re-export diffs cleanly, and writes `export-report.json` beside them. There are no dated folders.

Read the exit code, then `Read` `<artifacts_dir>/local/tmp/agami-export.json`: on `0` it is the report (the same as `export-report.json`); on `2` it is `{"error": "export_refused", "detail": "…"}`. Pass a refusal's `detail` on verbatim, with its fix from the cheat sheet below; nothing was written or removed.

---

## Phase 3: Summary

Post the summary in the chat, in this order. Every table is at most 15 rows; when there are more, say *"Showing 15 of <N>; <N−15> more in `export-report.json`."*

1. **One line.** *"Wrote <formats> for <database> to `<out>`."*
2. **What each target carries.** One row per target, from `targets.<target>.files` and `carried`: `Target | Files | Tables | Columns | Metrics | Joins | Examples | Golden`.
3. **What stayed behind or travelled with less, grouped by reason.** Group `targets.<target>.losses` by `reason`; one row per reason, in plain words, with its count per target and whether the items were left out (`dropped`) or carried with less (`degraded`): `What happened | Snowflake | Databricks | Ossie`. Never one row per item. The words for each reason:

| `reason` | In the summary |
|---|---|
| `not_approved` | Not approved yet |
| `sensitive` | A column flagged sensitive (counted, never named) |
| `depends_on_excluded` | Names something that stayed behind |
| `no_binding_for_target` | A metric with no SQL written for this engine |
| `sql_not_for_target` | SQL written for another engine |
| `unparseable_for_target` | SQL that does not parse for this engine |
| `no_home_in_target` | The format has no place for it |
| `carried_as_text` | Carried as text, not as something the agent acts on |
| `detail_lost` | Carried without some of its detail |
| `target_rule` | Would break one of the format's rules |
| `target_limit` | Past one of the format's size limits |
| `name_collision` | Its name was already taken in the file |
| `spans_subject_areas` | Belongs to more than one subject area |

4. **Metrics that did not travel.** When any loss has reason `no_binding_for_target`, call it out under the table: *"Revenue and Refund rate did not travel to Snowflake: neither has SQL written for Snowflake, and SQL is never translated."* Name the metrics from the loss's `name` in words. Adding SQL for another engine is a change to the model: send the person to `/agami-model` for it, and never write or suggest the SQL here.
5. **Counts the person may act on.** The number of `not_approved` losses: *"<n> items stayed behind because they are not approved. Say 'include the unreviewed items' if they should travel anyway."* The number of `sensitive` losses of kind `column`: *"<n> sensitive columns were left out; no file and not the report names them."* Under `--genie-hide-sensitive`, add only *"Their names are written into the Genie space file, which marks them hidden there."* Never print a name or an export id of a sensitive item.
6. **Flags left off.** For each format written, the extras from Phase 1 that were not picked, one line each on what it would add (from the `preview` above), so the person knows what they declined.
7. **What does not travel.** One line from `does_not_travel`: *"These stay in agami and never travel: pre-query checks, answer receipts, audit log rows, and who signed off an item."*
8. **Where the files are.** The path of each target's folder and of `<out>/export-report.json`. A target folder this run did not write is left from an earlier run: say so when one is there, since `export-report.json` describes this run only.

Then **what to do next**, in two sentences: the files change nothing until the person applies them in their own account, in the order `docs/context-export.md` gives under "Applying the files" (agami-core repository); and to approve what stayed behind, say "review my model" (`/agami-model`) and export again.

---

## Hard rules

1. **Never apply the files.** No running a generated `.sql` file, no deploy, no upload, no API call. Applying them is the person's act, with their own credentials.
2. **Never call a vendor or open a network connection.** `sm export` reads files and writes files; this skill runs nothing else against an account.
3. **Never pass an extra the person did not pick on a card,** and never a flag for a format the run does not write. `--include-unreviewed`, `--include-sensitive`, `--area` and `--ossie-version` are passed only when the person asks for them in their own words.
4. **Never guess a location or a warehouse.** Not from the credentials, the model, an earlier export or an example; ask on the card and re-ask when it is missing.
5. **Never print a sensitive column's name.** The report withholds them; keep it that way in every table and line, counts only. Under `--genie-hide-sensitive`, say only that the names are written into the Genie space file.
6. **Pass an `export_refused` message on verbatim,** with its fix from the cheat sheet; never retry with a changed command the person did not confirm.
7. **Never translate SQL or suggest a binding to make a metric travel.** A metric with no SQL for a target's engine is the model's to fix: send the person to `/agami-model`.
8. **One closing pointer to `/agami-model`** for approving what stayed behind, and no more.

---

## Error handling cheat sheet

Every refusal prints `{"error": "export_refused", "detail": "…"}`, exits `2`, and happens before anything is written or removed.

| `detail` starts with | Tell the person |
|---|---|
| `--location takes <database>.<schema>` | The location must be two parts, database (or catalog) and schema. Ask the `Location` question again. |
| `the snowflake target creates its semantic view in a schema` | A Snowflake export needs the schema its view is created in. Ask the `Location` question again. |
| `target <format> runs on … but the model is written for …` | That format runs on another engine, so it needs to know where the tables live there. Ask the `Location` question again. |
| `--snowflake-warehouse takes a name an unquoted identifier reaches` | The warehouse name must be letters, digits, `_` and `$`, not starting with a digit, all one case, and not a reserved word. Ask the `Warehouse` question again. |
| `<flag> belongs to --target <format>, which this export does not select` | A flag for a format not picked reached the command. Drop the flag, show the corrected command on the `Run it?` card, and run it only on a yes. |
| `unknown target` / `--target … is given more than once` | The command named a format wrongly. Fix the `--target` list to the formats picked and confirm again on the `Run it?` card. |
| `--ossie-version takes one of` | Say the versions the message lists and ask which one the person meant. |
| `no subject area` | The `--area` asked for is not in the model; the message lists the ones that are. Ask which. |
| `the semantic model at … does not load` | The model is broken, not the export. Run `sm validate` as the message says and send the person to `/agami-model` or `/agami-connect` to fix what it reports. |
| `the semantic model declares <n> storage engines` | An export writes SQL for one engine, and this model declares several. Pass the message on; the model needs one storage connection before it can be exported. |
| `subject areas <a> and <b> both export to …` | Two subject areas get the same file name in that format. Rename one through `/agami-model`, or export each alone with `--area <a>`, after the `Run it?` card. |
| `--out … lies inside …` / `… holds the semantic model` | The fixed output folder overlaps the model, which means the artifacts folder is laid out unusually. Pass the message on and stop; never pick another folder on your own. |
| `--out …: … exists and is not a directory` | A file sits where the output folder goes. Say which, and ask the person to move it. |
| Exit code other than `0` or `2` | The command failed to start (often a missing library): say so, show its last error line, and offer `bash "${CLAUDE_PLUGIN_ROOT}/scripts/sm" install`. |
