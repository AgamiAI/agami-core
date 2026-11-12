---
name: agami-connect
description: "End-to-end database connection for agami: sets up credentials on first run (DB-type picker → writes <artifacts_dir>/local/credentials.example for the user to fill in), then introspects the live DB directly into the agami semantic model (subject areas, tables, columns, relationships with join cardinality, deep-table column groups, sensitive-column flags) under <artifacts_dir>/<profile>/. The structural model is built deterministically by the agami-core semantic_model package (catalog mode, or a probe-mode fallback when the catalog is locked down); the skill then layers LLM enrichment (descriptions, entities, metrics) and seeds EXPLAIN-validated NL→SQL examples. Every model write is gated by the semantic-model validator — no breaking model is ever persisted."
when_to_use: "Run when the user installs the plugin for the first time, asks 'how do I set up agami' / 'connect to my database' / 'introspect my database' / 'introspect the schema' / 'reload schema' / 'add a new database', wants to try agami WITHOUT a database ('I don't have a database', 'try the sample', 'use the sample data', 'demo data'), or after the user changes their schema and wants the model refreshed. Also auto-invoked by agami-query the first time it runs (when the semantic model is missing). This skill handles credential setup, introspection, enrichment, and seed-example validation — one entry point for everything before the user can query. The sample-database path (Phase 0s) needs no connection and lands a queryable model in under a minute."
argument-hint: "[sample | reintrospect | profile NAME]"
allowed-tools:
  - Bash(bash "${CLAUDE_PLUGIN_ROOT}/scripts/sm" *)
  - Bash(bash ${CLAUDE_PLUGIN_ROOT}/scripts/sm *)
  - Bash("${CLAUDE_PLUGIN_ROOT}/scripts/sm" *)
  - Bash(${CLAUDE_PLUGIN_ROOT}/scripts/sm *)
  - Bash(AGAMI_SQL_MAX_ROWS=* bash "${CLAUDE_PLUGIN_ROOT}/scripts/sm" *)
  - Bash(AGAMI_SQL_MAX_ROWS=* bash ${CLAUDE_PLUGIN_ROOT}/scripts/sm *)
  - Bash(bash "${CLAUDE_PLUGIN_ROOT}/scripts/py" *)
  - Bash(bash ${CLAUDE_PLUGIN_ROOT}/scripts/py *)
  - Bash(AGAMI_PROFILE=* bash "${CLAUDE_PLUGIN_ROOT}/scripts/py" *)
  - Read(/${CLAUDE_PLUGIN_ROOT}/**)
---

# agami connect

**Before suggesting any slash command in chat, read [`shared/invocation-conventions.md`](../../shared/invocation-conventions.md).** Agami slash commands: `/agami-connect`, `/agami-query`, `/agami-model`, `/agami-save-correction`, `/agami-reconcile`. (`/agami-model` is also the trust-review surface — its Review tab absorbed the former `/agami-review`.) Never write the un-prefixed forms (`/init`, `/connect`, etc.) or colon forms (`/agami:connect`) — those don't exist. For chat replies, prefer natural language ("say 'reload the schema'", "say 'introspect my database'") — the `when_to_use` matcher routes correctly without an explicit slash command.

You are setting up the agami **semantic model** for the user's database. Goal: by the end there is a validated semantic model at `<artifacts_dir>/<profile>/` (`datasource.yaml` + `subject_areas/<area>/…` + `datasources/<connection>/storage.yaml`), a seeded examples library at `<artifacts_dir>/<profile>/prompt_examples/<area>/examples.yaml`, an `datasource.md` the user can edit, and the user has seen one demo query execute end-to-end.

**The structural model is built by a deterministic engine, not hand-authored.** `bash "${CLAUDE_PLUGIN_ROOT}/scripts/sm" introspect` introspects the live DB across all supported dialects — **PostgreSQL (incl. Supabase / Redshift), MySQL/MariaDB, Snowflake, BigQuery, SQL Server, Oracle, Databricks, Trino/Presto, DuckDB, SQLite** — into the model: storage connection, proposed subject areas, tables, columns + types, primary-key grain, foreign-key relationships **with join cardinality**, `column_groups` on wide tables, and `sensitive` flags on PII. When the catalog (`information_schema` / PRAGMA / data-dictionary) is reachable it runs in **catalog mode**; when a locked-down role denies the catalog it falls back **per-capability to probe mode** (describe via a zero-row header, infer types from a value sample, grain from uniqueness probes, FKs from name+overlap) and everything inferred lands `unreviewed` for sign-off. Your job is the layer the engine can't do: **enrichment** (prose descriptions, entities, metrics, caveats) and **curation** (subject-area boundaries, trust review).

For the model format: [`semantic_model/__init__.py`](../../../../packages/agami-core/src/semantic_model/__init__.py) (layout) and the Pydantic models in `packages/agami-core/src/semantic_model/models.py`.
For credentials: [`shared/credentials-format.md`](../../shared/credentials-format.md).
For connection method + local execution: [`shared/connection-reference.md`](../../shared/connection-reference.md).
For DB error classification: [`shared/db_error_classifier.md`](../../shared/db_error_classifier.md).
For every decision card and result table: [`shared/question-cards.md`](../../shared/question-cards.md).

## Conversation style

- **Combine acknowledge + next question** — don't waste turns on "Got it!"
- **Use AskUserQuestion for every Yes/No/Skip** — never inline-bullet options. Where AskUserQuestion is unavailable, ask the same as a short numbered list ([`shared/question-cards.md`](../../shared/question-cards.md) rule 7). Use `(Recommended)` only when there's a genuine recommendation. For fact-of-environment questions ("which database type?", "which schemas?"), don't mark any option Recommended — the user picks what they have.
- **Every multiSelect needs an explicit "none / continue" option** — `AskUserQuestion` cannot be submitted with zero boxes checked, so any multiSelect where "pick nothing" is a valid answer MUST offer a selectable "Nothing — continue" (or "Keep everything") option. Never phrase the prompt as "leave all unchecked" — that's unsubmittable and traps the user.
- **Keep the user oriented** — print one-line progress markers between phases (`✓ Introspected 12 tables`, `✓ Validator passed`, `✓ Generated 10 examples`).
- **Plain voice — state what happened, never how impressive it is.** Progress markers, todo labels, and narration describe the action and the result; they do NOT editorialize. Banned: "wow moment", "magic", "watch this", "the exciting part", "you'll love this", exclamation hype. The reader is a data professional — a query running correctly speaks for itself. Say *"Running the first query"*, not *"Now the wow moment"*.

## Progress tracking — the onboarding steps, shown the whole way

This is a multi-phase skill that often takes 5–15 minutes, with cards in between. **The very first action on every invocation is to show the onboarding steps as a task list, and to keep it current**, so the person always knows where they are and what's left. Once the database's folder exists, the list is drawn from `sm steps`, which prints every step with its status and its numbers read from disk; how and when to run it, and where the list goes, is in [shared/task-list.md](../../shared/task-list.md). Each phase file marks its own step when it starts and ends.

The same steps every run, in the person's words (no "introspect", "enrich" or "NL→SQL"):

```
1. Connect your database
2. Tell agami about your data
3. Pick the tables
4. Read the tables and joins
5. Describe columns and add metrics
6. Sign off definitions and hide personal data
7. Write and check example questions
8. Check your reports
9. Summary and first question
```

Leave out a step that won't run (8 when no reports were shared in 1.5; 3 with a model spec workbook, which names the tables); never add your own working steps to this list. **When a step finishes, add its numbers to its label**, so the list reads as a running summary: *"Read the tables and joins — 21 tables, 19 joins"*, *"Describe columns and add metrics — 93% described, 7 metrics"*, *"Sign off definitions — 5 of 7 signed"*, *"Write and check example questions — 10 of 11 ran"*. The label with its numbers is each step's `text` in `sm steps`' output, copied as printed, never typed by hand and never an estimate; a step with nothing to count keeps its plain label. **While a long step runs, its label carries the live count too** (*"Read the tables and joins — 30 of 52 tables"*), from the same `text`, refreshed after every `sm progress --wait`, so the list shows movement during a wait and not only at the end. **Mark each step in progress when it starts and done the moment it ends**, exactly one in progress at a time, and keep it in progress while a card for that step is waiting, so the list shows where the person is while they decide.

**Don't seed it again if the list already holds these steps** (the skill is resuming after a hand-off, a password page or a file wait): run `sm steps` first and show what it prints ([shared/task-list.md](../../shared/task-list.md), resuming). When `$ARGUMENTS == reintrospect`, the list restarts at the first step this run marks started (step 2, when Phase 1 begins): `sm steps` reopens that step and every step after it, and keeps the steps before it done.

## What runs at the same time

Subagents read and draft in the background while the person answers cards, so nobody waits on one thing while another could run. How a batch is started, collected and given up on is in [shared/parallel-work.md](../../shared/parallel-work.md); the phase files say when. Subagents only read and return data: you write the model, run every command and ask every question.

| While the person is on | These run in the background |
|---|---|
| The context and table cards (1.5–1.6) | One `agami-doc-reader` per shared document, up to 4 |
| The build (1.7) | Document readers still running; the build itself is a background task |
| The join cards after the build (1.7) | `agami-enricher`s: one per chunk of up to 12 tables |
| The sign-off cards (4) | `agami-example-writer`s: one per subject area, drafting example questions |
| The wait for the example run (6) | `sm seed-validate` as a background task |

With no Agent tool the same work is done inline, in this order.

---

## Phase −1: Plan-mode check

Run the detection + ask logic from [`shared/plan-mode-check.md`](../../shared/plan-mode-check.md). agami-connect needs Bash (introspection) and Write (model files) — both blocked in plan mode.

**If plan mode is active and the user stays in plan mode** (or the skill is invoked under plan mode with no prompt): refuse with the one-liner below and **end the turn**. DO NOT write a plan file. DO NOT call `ExitPlanMode`.

> I can't introspect in plan mode — switch to **Auto** or **Edit Automatically** mode (Shift+Tab to cycle) and re-invoke me. Introspection, enrichment, and the demo query all need write access to `<artifacts_dir>/<profile>/`.

If plan mode is not active, skip silently.

---

## Phase 0: Preflight

### HARD RULES — read before doing anything

Non-negotiable. They override every other instruction here when they conflict.

1. **Connect ONLY to the host/port/database/user/password in `<artifacts_dir>/local/credentials`.** That file is the sole credential source — there is no env-var bypass. Never connect to anything else. Never probe `localhost` unless the credentials say so. Never substitute defaults for missing fields.
2. **Never ask for a password in chat.** Nor any other secret: a password, an access token, a key (or a key file's contents), or a DSN that contains a password. Not even temporarily, and never echo one back. The non-secret fields (host, port, database, user, account, warehouse, a file path) ARE asked in chat, on the connection cards in 0a.4c: they are not secret, they already appear in the credentials file in plain text, and a card is faster than editing a file. The secret goes only through a path that keeps it out of the chat, the transcript, and your context: on macOS and Windows the one-time local page of **0a.4k** (it goes straight to the OS credential store), on Linux (and for a Databricks token anywhere) the `credentials.example` file of **0a.4** that the person fills in themselves. If the person types a secret into the chat anyway, don't use or repeat it: say it should not be shared in chat, suggest they change it, and carry on with the page or the file.
3. **Never scan or guess.** No `pgrep`, `ps`, `lsof`, `find /`, `ls /Applications`, no port-listener scans, no testing connections to common hostnames. The only acceptable Bash probes here are `which <tool>` and `bash "${CLAUDE_PLUGIN_ROOT}/scripts/py" -c 'import <module>'`.
4. **If credentials are missing for the active profile, run Phase 0a.** It ends connected (the password page, 0a.4k) or with a card that waits for the filled-in file (0a.9). A person who closes the chat instead can come back later with any data question — `agami-query` auto-invokes us and 0a.10 picks the file up.
5. **NEVER put a credential on a Bash command line** — no `export PGPASSWORD=…`, no `psql -W <pw>`, no heredoc that interpolates a secret. Hosts render Bash calls in chat; anything on the line leaks. Runtime queries use the auth files from `scripts/setup_pgauth.py` (psql/mysql) or `python -m execute_sql` (every driver, reads `<artifacts_dir>/local/credentials` itself). See [`shared/connection-reference.md → HARD RULES`](../../shared/connection-reference.md).

If you reach for a command that doesn't fit, stop and re-read this section.

### Preflight steps

0. **Sample-path short-circuit (check FIRST, before resolving any profile).** If the user explicitly asked for the sample — `$ARGUMENTS` is `sample`, or they said *"try the sample"* / *"I don't have a database"* / *"use the sample (or example) data"* / *"demo data"* — then bind `$PROFILE_NAME = agami-example`, `$DB_TYPE = sample`, and **go straight to [Phase 0s](references/sample.md#phase-0s-sample-database-bootstrap-no-connection)**. Do this **regardless of any existing `active_profile` or onboarded profiles** — the sample is a deliberate, explicit choice and must not be intercepted by another profile's credential/existing-model checks. (A returning person also gets "Try the sample data" on the start card in step 1a; this short-circuit covers the request that already says so.) If `agami-example` is already set up, Phase 0s sends them to the demo. Only fall through to step 1 when there's **no** sample signal.
1. **Resolve the environment** — `bash "${CLAUDE_PLUGIN_ROOT}/scripts/py" connect_resolve.py [--db-type <t>] [--profile <p>]` prints `{data, anomalies}` (self-describing JSON): profile, artifacts_dir, the `[<profile>]` credentials section + chmod, the cached config, the **scored** `interpreter.python3` (the one with model deps + driver — use it everywhere; never a Python missing a dep), native `tools`, and a `next` decision. Branch on `data.next`: **`ready`** → the start card for a returning person (step 1a); a reload or re-introspect request skips the card and goes straight to Phase 1 with `reintrospect`; **`promote`** → run 0a.10 to promote the filled template, then continue; **`bootstrap`** → the first-run start card (step 1a), then Phase 0a. On a first-time bootstrap `data.profile` is **`null`** (`profile_source: "default"`) — there's no profile yet, so narrate it **without a name**: *"No credentials yet — first-time setup."* (Never say "profile `main`" — the user names their profile in 0a.3.) Only when `data.profile` is non-null (a `bootstrap` for an explicitly-named/env profile) may you name it. Surface `anomalies` (e.g. `credentials_world_readable` → offer `chmod 600`; `library_older_than_plugin` → offer `bash "${CLAUDE_PLUGIN_ROOT}/scripts/sm" install`, or the `AGAMI_PYTHON` fix it names; `agami_python_cannot_be_brought_current` → the `$AGAMI_PYTHON` the user set has no pip and an old or missing agami-core, so say so; `env_profile_has_no_model` → `AGAMI_PROFILE` names a database with no model while others have one: say its `detail` in one line before the start card, and offer the databases it lists rather than a new connection under that name, unless the person asked to connect one). `interpreter.chosen_because` says why that Python was picked, and a candidate passed over carries a `reason`; never substitute a `missing_fields` value — surface "missing field X for profile Y" and stop.
1a. **The start card** (conventions: [`shared/question-cards.md`](../../shared/question-cards.md)). One AskUserQuestion, one question, header `Start`. Skip it when the request already names the action: `$ARGUMENTS` is `reintrospect` or `profile NAME`, the person asked to reload or re-introspect, to add a new database (go to Phase 0a), or `agami-query` invoked this skill because a model is missing (go to Phase 0a).
   - **First run** (`data.next` is `bootstrap`, nothing connected yet). Question: *"How do you want to start?"* No option is marked Recommended: whether there is a database to connect is a fact about the person's setup.

     | label | description | then |
     |---|---|---|
     | `Connect a database` | Your own database; the password never goes through the chat | Phase 0a |
     | `Try the sample data` | A small demo store, no connection needed | Phase 0s |

   - **Returning** (`data.next` is `ready`: a database is already connected). Question: *"What would you like to do?"*

     | label | description | then |
     |---|---|---|
     | `Ask a question (Recommended)` | Ask `<active database>` something | Hand to [agami-query](../agami-query/SKILL.md). A question typed in "Other" goes straight there. Picked with no text: offer a card of up to 4 questions taken from that database's seed examples (`prompt_examples/`), with "Other" for their own. |
     | `Reload the schema` | Pick up new or changed tables, keeping descriptions, metrics and sign-offs | Phase 1 as `reintrospect` (the 1.1 Re-introspect path; 2e previews, then writes). |
     | `Connect another database` | Add a different database alongside this one | 1.1a first (same company or a different one), then Phase 0a (the new database gets its own name in 0a.3). |
     | `Try the sample data` | A small demo store, no connection needed | Phase 0s |

     **An empty database gets no recommendation to ask it.** When every table in the active database's `sm model-tree` has `rows` of 0, the first option is `Ask a question` without "(Recommended)", its description says *"<database> has no rows, so answers will be empty"*, and `Connect another database` is marked **(Recommended)** instead.

     Route "Other" text by what it asks for. An action goes to that action: re-introspect, reload or refresh the schema → `Reload the schema`; add a schema that lives in the same database → `Reload the schema` with the schema selection in 1.3 widened to the old and new schemas (see the note under 1.1: same database, another schema is a re-introspect, never a new database); review or sign off the model, edit descriptions, exclude tables → [agami-model](../agami-model/SKILL.md); add or connect a different database → `Connect another database`. Only a question goes to agami-query. When the text could be either (a schema name alone, say), ask which with a card rather than guess.

     This card replaces the 1.1 menu for a returning person; don't ask 1.1 again after it.

   Say "database", never "profile", in the card and around it.
2. **Update-check (best-effort).** Run the probe from [`shared/version-check.md`](../../shared/version-check.md); surface a one-liner if a newer version exists. Never block on network failure.
3. If `$ARGUMENTS` is `reintrospect`: re-introspect from scratch, but **preserve hand-edits** (descriptions, entities, metrics, caveats, trust sign-offs). The engine writes the structural skeleton; merge it over the existing enrichment rather than discarding it (see Phase 2's reintrospect note).

---

## Phases: read each file before you start it

The phases live in their own files, so a run loads only the ones it reaches. **Before starting a
phase, `Read` its file; never work a phase from memory of an earlier run or of this list.** Those
files write the plugin root as `<plugin_root>`: it is `${CLAUDE_PLUGIN_ROOT}`, the path to write
into their commands.

| Phase | When | File |
|---|---|---|
| 0a: connect a database | No credentials for the profile | [`references/credentials.md`](references/credentials.md) (`${CLAUDE_PLUGIN_ROOT}/skills/agami-connect/references/credentials.md`) |
| 0s: the sample database | `sample`, or "try the sample" | [`references/sample.md`](references/sample.md) (`${CLAUDE_PLUGIN_ROOT}/skills/agami-connect/references/sample.md`) |
| 1.0–1.6s: context and tables | Connected; before the build | [`references/context-and-tables.md`](references/context-and-tables.md) (`${CLAUDE_PLUGIN_ROOT}/skills/agami-connect/references/context-and-tables.md`) |
| 1.7–1.8: build the model | Tables picked | [`references/build.md`](references/build.md) (`${CLAUDE_PLUGIN_ROOT}/skills/agami-connect/references/build.md`) |
| 2: describe and add metrics | The model is built | [`references/enrich.md`](references/enrich.md) (`${CLAUDE_PLUGIN_ROOT}/skills/agami-connect/references/enrich.md`) |
| 3–4: areas and sign-off | Described | [`references/review.md`](references/review.md) (`${CLAUDE_PLUGIN_ROOT}/skills/agami-connect/references/review.md`) |
| 5–6: example questions | Signed off | [`references/examples.md`](references/examples.md) (`${CLAUDE_PLUGIN_ROOT}/skills/agami-connect/references/examples.md`) |
| 7–8.5: summary and next steps | Examples checked | [`references/summary.md`](references/summary.md) (`${CLAUDE_PLUGIN_ROOT}/skills/agami-connect/references/summary.md`) |

---

## Error handling

| Symptom | Action |
|---|---|
| Credentials chmod wrong | Refuse, offer to `chmod 600` |
| Cached connection tool no longer works | Re-detect, update `<artifacts_dir>/local/.config` |
| Catalog denied (no `information_schema`/PRAGMA/dict access) | Engine falls back to probe mode; if even table enumeration is denied, ask for the table allowlist (Phase 1.2) |
| Introspection SQL fails | Route through `db_error_classifier.md`; surface the one-line remediation |
| **Validator fails** | **Model is NOT persisted. Show errors verbatim, fix, re-validate.** |
| EXPLAIN fails for a seed | Auto-fix once → else move to `<artifacts_dir>/local/.rejected/`. Don't block. |
| Reintrospect would lose hand-edits | Phase 2e — preserve descriptions, entities, metrics, caveats, sign-offs |
| Legacy (v1) model at the profile root | Engine backs it up to `.legacy_backup/` before writing; surface a one-liner |
| Unsupported engine (MongoDB, Cassandra, …) | "Not supported yet — supported: Postgres/Redshift/Supabase, MySQL, Snowflake, BigQuery, SQL Server, Oracle, Databricks, Trino, DuckDB, SQLite." |
