---
name: agami-connect
description: "End-to-end database connection for agami: sets up credentials on first run (DB-type picker → writes <artifacts_dir>/local/credentials.example for the user to fill in), then introspects the live DB directly into the agami semantic model (subject areas, tables, columns, relationships with join cardinality, deep-table column groups, sensitive-column flags) under <artifacts_dir>/<profile>/. The structural model is built deterministically by the agami-core semantic_model package (catalog mode, or a probe-mode fallback when the catalog is locked down); the skill then layers LLM enrichment (descriptions, entities, metrics) and seeds EXPLAIN-validated NL→SQL examples. Every model write is gated by the semantic-model validator — no breaking model is ever persisted."
when_to_use: "Run when the user installs the plugin for the first time, asks 'how do I set up agami' / 'connect to my database' / 'introspect my database' / 'introspect the schema' / 'reload schema' / 'add a new database', wants to try agami WITHOUT a database ('I don't have a database', 'try the sample', 'use the sample data', 'demo data'), or after the user changes their schema and wants the model refreshed. Also auto-invoked by agami-query the first time it runs (when the semantic model is missing). This skill handles credential setup, introspection, enrichment, and seed-example validation — one entry point for everything before the user can query. The sample-database path (Phase 0s) needs no connection and lands a queryable model in under a minute."
argument-hint: "[sample | reintrospect | profile NAME]"
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

This is a multi-phase skill that often takes 5–15 minutes, with cards in between. **The very first action on every invocation is to show the onboarding steps as a task list, and to keep it current**, so the person always knows where they are and what's left. Use the task-list tool the host gives you (`TodoWrite`, `TaskCreate`, or the app's own task list, such as the one shown under your message in the Desktop app); where there is none, a short numbered list in your first message, repeated with the current step marked whenever a step finishes.

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

Leave out a step that won't run (8 when no reports were shared in 1.5; 3 with a model spec workbook, which names the tables); never add your own working steps to this list. **Mark each step in progress when it starts and done the moment it ends**, exactly one in progress at a time, and keep it in progress while a card for that step is waiting, so the list shows where the person is while they decide. The sample path uses its own short list (Phase 0s).

**Don't seed it again if the list already holds these steps** (the skill is resuming after a hand-off, a password page or a file wait). When `$ARGUMENTS == reintrospect`, the list starts at step 3.

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

0. **Sample-path short-circuit (check FIRST, before resolving any profile).** If the user explicitly asked for the sample — `$ARGUMENTS` is `sample`, or they said *"try the sample"* / *"I don't have a database"* / *"use the sample (or example) data"* / *"demo data"* — then bind `$PROFILE_NAME = agami-example`, `$DB_TYPE = sample`, and **go straight to [Phase 0s](#phase-0s-sample-database-bootstrap-no-connection)**. Do this **regardless of any existing `active_profile` or onboarded profiles** — the sample is a deliberate, explicit choice and must not be intercepted by another profile's credential/existing-model checks. (A returning person also gets "Try the sample data" on the start card in step 1a; this short-circuit covers the request that already says so.) If `agami-example` is already set up, Phase 0s sends them to the demo. Only fall through to step 1 when there's **no** sample signal.
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

## Phase 0a: First-time credential bootstrap

**Runs when preflight step 1 returns `next: bootstrap` (no credentials for the profile) and the person picked "Connect a database", or when they picked "Connect another database".** If `<artifacts_dir>/local/credentials` already has the `[<profile>]` section for the database being connected, **skip Phase 0a entirely.**

**Two paths, chosen by the operating system** (`uname -s`: `Darwin` is macOS; `MINGW*` / `MSYS*` / `CYGWIN*`, or `$OS` = `Windows_NT`, is Windows):

- **macOS and Windows: the password page.** 0a.1 → 0a.2 → **0a.4c** (connection fields card) → 0a.3 (the name, offering the database's own name) → 0a.5 → 0a.5b → 0a.6 → 0a.7 → 0a.8 → **0a.4k** (the password page) → Phase 1. No file to edit and nothing to paste. The page needs the interpreter, agami-core (with `keyring`) and the artifacts folder in place, which is why 0a.5–0a.8 run before it.
- **Linux and anything else, and Databricks on every system** (its secret is a `token`, and the page stores a `password`): the file. 0a.1 → 0a.2 → 0a.3 → **0a.4** (`credentials.example`) → 0a.5 → 0a.5b → 0a.6 → 0a.7 → 0a.8 → **0a.9** (a card waits for the filled-in file) → 0a.10 → Phase 1.

### 0a.1 — Set up `<artifacts_dir>/local/`
```bash
mkdir -p "<artifacts_dir>/local" && chmod 700 "<artifacts_dir>/local"
```
If the user chose a non-default `<artifacts_dir>`, **persist the pointer** so future sessions find it, and gitignore `local/`:
```bash
mkdir -p ~/.config/agami && printf '%s\n' "<artifacts_dir>" > ~/.config/agami/path
grep -qxF 'local/' "<artifacts_dir>/.gitignore" 2>/dev/null || printf 'local/\n' >> "<artifacts_dir>/.gitignore"
```

### 0a.2 — Ask the database type

**AskUserQuestion**, one question, header `Database`, no `(Recommended)` — fact-of-environment. The sample data is on the start card (1a), not here.

**Name the Other-only engines in the QUESTION PROMPT** so they're visibly supported — the auto-provided "Other" field can't carry its own description/examples, so the examples must live in the prompt. End the prompt with a line like:
> *Something else — BigQuery, SQL Server, Oracle, Databricks, Trino/Presto, DuckDB, or SQLite? Choose **Other** and type it (a name or a file path).*

| label | description |
|---|---|
| `PostgreSQL` | Postgres + compatible: Supabase, Neon, RDS, Aurora, Cloud SQL, Timescale, and **Amazon Redshift** (port 5439, SSL by default). |
| `MySQL` | MySQL, MariaDB, RDS MySQL, PlanetScale. |
| `Snowflake` | Snowflake. Account identifier instead of host. |
| `Other (Other field)` | **BigQuery, SQL Server, Oracle, Databricks, Trino/Presto, DuckDB, SQLite**. |

Bind `$DB_TYPE` ∈ `postgres | mysql | snowflake | bigquery | sqlserver | oracle | databricks | trino | duckdb | sqlite | dsn`.

**Routing:**
- **"sample" typed in Other** (or *"I don't have a database"*) → Phase 0s, as on the start card.
- `PostgreSQL` → `postgres`; if the user later enters port `5439` or a `*.redshift.*.amazonaws.com` host, transparently re-bind to `redshift`. A `*.pooler.supabase.com` host stays `postgres` (Supabase is hosted Postgres).
- `MySQL`/`Snowflake` → pass-through. `BigQuery` lives under **Other** now (or the user types it) → `bigquery`.
- `Other` → parse the free-form input: a DSN scheme → derive `db_type` (if it carries a password, follow HARD RULE 2: don't use or repeat it, ask the fields on 0a.4c instead); `.db`/`.sqlite`/`.duckdb` suffix or absolute file path → SQLite or DuckDB; a named DB (`bigquery`, `sqlserver`/`mssql`, `oracle`, `databricks`, `trino`/`presto`, `duckdb`) → that dialect. Only refuse with "not supported yet" for engines outside the supported set above (e.g. MongoDB, Cassandra, ClickHouse).

### 0a.3 — Name the database (the person's choice)

Ask the user to **name** this connection — don't pick for them. The name is how they'll switch databases later (`AGAMI_PROFILE=<name>`) and it names the model folder (`<artifacts_dir>/<name>/`), so a name that means something to them — their database, product, team, or environment — beats a generic default.

**On the password-page path this card comes after the connection fields (0a.4c)**, because the fields card asks for the database's own name and a separate "what should I call this database?" before it reads as the same question twice. Here the name is for the *connection*, and the database name the person just gave is the natural answer. **AskUserQuestion**, header `Name`:
> *"What should agami call this connection? It's how you'll pick it when you connect more than one. Type your own in Other."*

Options: `<database> (Recommended)`, the database name from 0a.4c made valid (lowercased, any other character turned into `-`, trimmed to 32), and `<database>-<where>`, with `<where>` = `local` for `localhost`/`127.0.0.1` or the first label of the host otherwise (`<database>-<account>` on Snowflake, `<project>` alone on BigQuery, the file's stem for SQLite and DuckDB). Leave out an option that would equal the other or fail the rules below.

**On the file path** (no fields card), ask it here, before the file: *"What should agami call this database? It's how you'll pick it when you connect more than one, so your database or product name works well. Type it in Other."* with `prod`, `staging` and `analytics` as examples. **Don't present a `main` default that nudges them past the choice.**

Bind `$PROFILE_NAME` to their answer (the Other text, or a picked option). Validate: lowercase letters/digits/dashes/underscores, 1–32 chars; **and not already a `[section]` in `<artifacts_dir>/local/credentials`** (a profile name is a unique key — reusing one would clash). If it fails either rule, show the reason and re-ask. (The 0a.10 promote helper enforces the uniqueness backstop too — it returns `COLLISION` rather than overwrite — but catch it here so the user isn't surprised later. On the password-page path the helper checks again, and also refuses a name the OS store already holds a password under.) Say "database", not "profile", to the person.

### 0a.4c — The connection fields as one card (macOS and Windows)

**Only on the password-page path** (see the top of Phase 0a). Linux, and Databricks on any system, use the file in 0a.4 instead.

One AskUserQuestion, header per question, **one question per non-secret field** (at most 4). **The password is never on this card**: it goes on the page in 0a.4k. Every question takes the real value in "Other"; the options are the obvious defaults and common values, so the person presses one when it fits and types otherwise. No option is marked Recommended (these are facts about their setup). Put a line in the first question's prompt: *"Type yours in Other when the options don't fit, for example `your-cluster.example.com` (never a password or a URL that holds one: the password goes on a separate page)."*

| `$DB_TYPE` | questions (header: options) | spec `fields` keys | `auth` |
|---|---|---|---|
| postgres / redshift | `Host`: `localhost` (this computer), `127.0.0.1` · `Port`: `5432` (PostgreSQL default), `5439` (Amazon Redshift), `6543` (Supabase pooler) · `Database`: `postgres` (the default database), `analytics` · `User`: `readonly` (a read-only user, the safest), `postgres` | `host`, `port`, `database`, `user` | `password` |
| mysql | `Host`: as above · `Port`: `3306` (MySQL default), `3307` · `Database`: `analytics`, `app` · `User`: `readonly`, `root` | same | `password` |
| sqlserver | `Host`: as above · `Port`: `1433` (default), `1434` · `Database`: `master`, `analytics` · `User`: `readonly`, `sa` | same | `password` |
| oracle | `Host`: as above · `Port`: `1521` (default), `1522` · `Service`: `ORCLPDB1`, `XEPDB1` · `User`: `readonly`, `system` | `host`, `port`, `service_name`, `user` | `password` |
| snowflake | `Account`: `acme-xy12345` (org-account form), `xy12345.us-east-1` (locator form) · `User`: `readonly`, `analyst` · `Warehouse`: `COMPUTE_WH` (Snowflake's default), `ANALYTICS_WH` · `Sign-in`: `Password` (typed on a one-time page), `Browser sign-in (SSO)` (your company's sign-in, no password) | `account`, `user`, `warehouse` (+ `authenticator = externalbrowser` for SSO) | `password`, or `sso` for browser sign-in |
| trino | `Host`: as above · `Port`: `8080` (HTTP default), `443` (HTTPS) · `User`: `readonly`, `admin` · `Catalog`: `hive`, `iceberg`; then a second card, `Password`: `No password`, `Password (HTTPS)` | `host`, `port`, `user`, `catalog` | `sso` for no password, else `password` |
| bigquery | `Project`: `acme-analytics`, `acme-prod` · `Sign-in`: `Google sign-in on this computer` (gcloud application-default login), `A service-account key file` (its path in Other; the path, never the file's contents) | `project` (+ `service_account_path`) | `sso` |
| sqlite / duckdb | `File`: the absolute path in Other; options `~/data/app.db`, `~/data/app.duckdb` (examples) | `path` | `sso` |

Snowflake's database, schema and role, and Postgres' `sslmode`, are optional: ask them on a second card only when the person mentions them or the connection fails without them.

Read the answers into the spec's `fields`, as strings. Expand `~` to an absolute path. **When an answer can't be used, say why in one line and ask that question again** (an empty answer; a port that isn't a number; an example value such as `acme-xy12345` or one containing `example` that is plainly not theirs; a value with `%`, or starting with `#` or `;`, which the credentials file can't hold). A host that is a full URL with a password in it: don't use or repeat it (HARD RULE 2), and ask for the host alone.

Then run 0a.5 → 0a.8 (interpreter, agami-core, the artifacts folder, `.config`), and go to 0a.4k.

### 0a.4k — the password page (macOS and Windows)

The password goes from the person's browser straight into the OS credential store (Keychain on macOS, Credential Manager on Windows). It is never in the chat, on a command line, in a file, or in your context: you never see it, so never ask for it, and if it is typed into the chat anyway, follow HARD RULE 2.

**1. Write the spec** with the Write tool to `<artifacts_dir>/local/tmp/agami-connect-spec.json` (non-secret fields only; there is no password key):

```json
{"profile": "<$PROFILE_NAME>", "db_type": "<$DB_TYPE>",
 "fields": {"host": "<host>", "port": "<port>", "database": "<database>", "user": "<user>"}}
```

For a connection with no secret to type (Snowflake browser sign-in, BigQuery, Trino with no password, SQLite, DuckDB) add `"auth": "sso"`. **The `sso` path has no link and no page:** the helper writes the section, tests the connection, and prints only its final line, so skip step 3 and read that line in step 4. Its wait is longer than the password path's (step 2).

**2. Start the helper in the background.** Call Bash with `run_in_background: true`:

```bash
echo "agami-helper-pid $$" >&2; exec bash "${CLAUDE_PLUGIN_ROOT}/scripts/py" connect_helper.py --spec "<artifacts_dir>/local/tmp/agami-connect-spec.json"
```

The `exec` chain (this shell, then `scripts/py`) keeps one PID, so the `agami-helper-pid` line on stderr is the helper's own PID, for stopping it where TaskStop isn't available. It is not an error line; ignore it when looking for the last stderr line.

Stdout is JSON lines and nothing else. **Wait for the helper's first line with the Monitor tool**: the Bash result names the task and its output file, so use the Monitor tool to watch the helper's output until its first line, or the task's exit (an until-loop on that output file that stops on the first JSON line, which the `agami-helper-pid` line is not, or when the task has exited). Never run a foreground `sleep`: the Desktop Code tab blocks it. As a fallback where Monitor isn't available, re-read the output file at spaced intervals, each read its own tool call, never a tight loop. Stop waiting when one of these happens:

- **The first line has a `status`**: it is the final line. Every check that runs before the page (`missing_driver`, `name_taken`, `no_store`, `stale_install`, and the other refusals), and the whole `sso` path, prints that one line and exits. Skip step 3 and go to step 4 with it.
- **The first line has a `url`** (`{"url": "http://127.0.0.1:<port>/<token>/", "expires_in": 300}`): the page is listening, usually within a second. Go to step 3.
- **The background task has exited with no line, or, on the password path, about 15 seconds pass with no line**: the helper failed before it could print (an import error, say, which goes to stderr only). Stop the task if it is still running (below), show the last stderr line in one sentence, and ask a card, header `Next step`: `Try again` (step 2 again) / `Use a file instead (manual)` (0a.4).

**With `"auth": "sso"` (`auth: sso`) there is no 15-second limit.** The helper tests the connection before it prints anything, and that test can open a browser sign-in (Snowflake's SSO, say) that waits for the person. So tell the person once, when you start the helper, that a browser sign-in may open and that you are waiting for it to finish, then wait until the helper exits, up to 5 minutes, with the same Monitor watch. Don't repeat the line while waiting. Past 5 minutes with no exit, stop the task (below) and ask the `Next step` card above.

Don't run the helper in the foreground: it would hold the turn until the page is submitted, and the link would never reach the person. Where background commands aren't available, use the file path (0a.4) instead.

**3. Post the link** as a clickable markdown link, with this sentence and how long it lasts:

> [Enter your password](http://127.0.0.1:<port>/<token>/). Enter the password on this page; it goes to your keychain, not the chat. The link expires after 5 minutes.

(On Windows say "Windows Credential Manager" in place of "your keychain".) **Then a card, so the link isn't missed** (a line of chat is easy to scroll past; a live run showed exactly that). AskUserQuestion, one question, header `Password`: *"Enter the password on the page at http://127.0.0.1:<port>/<token>/ (open for 5 minutes), then come back here."* Options: **I've saved it** / **New link** / **Use a file instead (manual)**.

- **I've saved it** → read the helper's output: a final line is there → step 4. None yet → say the page hasn't been submitted, and ask the card again.
- **New link** → stop the running helper (below) and start step 2 again with the same spec.
- **Use a file instead (manual)** → stop the helper, then 0a.4.

The helper exits when the page is submitted or the 5 minutes pass, and that exit also brings you back if the card is still open: go to step 4 and don't ask the card again. Never wait silently: if the person writes before the helper is done, answer, and remind them the link is still open. If they want to stop, stop the background task with TaskStop and its task id, or, where TaskStop isn't available, `kill <PID>` with the PID from the `agami-helper-pid` line (`kill` sends SIGTERM, and the helper then takes back whatever it stored), and show the database-type card again.

**4. Read the final line** of the output when the helper exits, and act on it. The exit is what you wait for after the link is posted: the Monitor tool watching the task until it exits (or, as the fallback, spaced re-reads of the output file), never a foreground `sleep`; the background task's exit notice also brings you back. Exit code is 0 for every outcome; `status` (and a refusal's `reason`) says what happened, and `detail` is the one line to show the person. Each outcome gets one sentence plus the next card:

| final line | say | next |
|---|---|---|
| `connected` | *"Connected to `<name>`. The password is in your keychain."* (or the Windows store) | Run `setup_pgauth.py --all` (as in 0a.10) and continue to Phase 1. |
| `connect_failed` | `detail` (why it failed). Nothing was kept: the helper removed the section and the stored password. | Card `Next step`: `Try again` (the same fields, a new link; the person may have mistyped the password) / `Change the connection details` (0a.4c again) / `Use a file instead (manual)` (0a.4). |
| `expired` | *"The link expired before the page was submitted; nothing was saved."* | Card `Next step`: `New link` (step 2 again, same spec) / `Use a file instead (manual)`. |
| `missing_driver` | `detail`, which names `package`. | Card `Driver`: `Install <package>` (with the real package names) / `Not now`. On the press, install into the interpreter `scripts/py` uses: `bash "${CLAUDE_PLUGIN_ROOT}/scripts/py" -m pip install --user <package>` (unquoted: `package` can hold two names, `duckdb pytz`), then step 2 again. If pip fails, show its last line and offer `Try again` / `Not now`. `Not now`: stop with *"Connect again when you're ready to install it."* |
| `refused`, `no_store` | `detail` (agami's keyring library isn't set up for the OS store yet). | Card `Secure store`: `Set up secure storage` / `Use a file instead (manual)`. The first runs `bash "${CLAUDE_PLUGIN_ROOT}/scripts/sm" install` (it adds `keyring` on macOS and Windows), then step 2 again. |
| `refused`, `stale_install` | `detail` (the agami-core this Python has is too old for the store). | Card `Update`: `Update agami` (runs `bash "${CLAUDE_PLUGIN_ROOT}/scripts/sm" install`, then step 2 again) / `Use a file instead (manual)`. |
| `refused`, `name_taken` or `saved_password_exists` | `detail` (the name is in use here, or the OS store already holds a password under it). | The name card (0a.3) again, then a new spec and step 2. |
| `refused`, `bad_name` | `detail`. | The name card (0a.3) again. |
| `refused`, `bad_field` | `detail` (a field the credentials file can't hold). | The connection fields card (0a.4c) again. |
| `refused`, `bad_spec` | Nothing to the person: the spec file you wrote is unreadable. | Rewrite it (step 1) and run step 2 again, once. A second `bad_spec`: say connecting failed and offer `Use a file instead (manual)`. |
| `refused`, `unreadable_credentials` | `detail` (the credentials file under `<artifacts_dir>/local/` can't be parsed). | Card `Next step`: `I've fixed it` (step 2 again) / `Stop`. Never open or print the file: it can hold passwords. |
| `refused`, `unsupported_os` | `detail`. | The file path: 0a.4, then 0a.9. |
| `refused`, `store_error` or `write_failed` | `detail` (the store or the credentials file refused the write; nothing was kept). | Card `Next step`: `Try again` (step 2) / `Use a file instead (manual)`. |

`Use a file instead (manual)` always means 0a.4 → 0a.9 → 0a.10, the same as Linux. A line that is not one of these, or no final line at all: show the last line of output and offer `Try again` / `Use a file instead (manual)`. Delete the spec file once the helper has exited.

### 0a.4 — Write `<artifacts_dir>/local/credentials.example`

**The file path:** Linux and any system without a secure store, Databricks on every system, and whoever picks `Use a file instead (manual)` in 0a.4k. The person types the password into the file themselves, so it never passes through the chat.

Use the **Write tool**. Shared header first, then the `$DB_TYPE` body with `[$PROFILE_NAME]` as the section.

**Header:**
```ini
# <artifacts_dir>/local/credentials.example
# Fill in your values below and save, then press "I've filled it in" in the chat.
# agami moves this file to <artifacts_dir>/local/credentials and chmod-600s it for you — no
# manual save or chmod needed. (Don't rename it yourself.)
# agami only runs read-only SELECT queries, so a read-only database user is all it needs
# (and the safest thing to connect). Copy-paste GRANT SQL for your database:
# plugins/agami/shared/readonly-grants.md — or ask agami for "the read-only grant".
# Format reference: plugins/agami/shared/credentials-format.md
# Switch profiles with AGAMI_PROFILE=<name>.
```

Bodies — `postgres`, `redshift`, `snowflake`, `mysql`, `bigquery`, `sqlite` are unchanged from [`shared/credentials-format.md`](../../shared/credentials-format.md) (URL-form first for Postgres/MySQL/Redshift; account fields for Snowflake; `project`+`service_account_path` for BigQuery; `path` for SQLite). The new dialects:

```ini
# SQL Server / Azure SQL.
[$PROFILE_NAME]
type = sqlserver
host = your-server.database.windows.net
port = 1433
database = your-database
user = your-username
password = your-password
```
```ini
# Oracle.
[$PROFILE_NAME]
type = oracle
host = your-host.example.com
port = 1521
service_name = ORCLPDB1
user = your-username
password = your-password
# OR a full DSN:  dsn = host:1521/ORCLPDB1
```
```ini
# Databricks SQL warehouse.
[$PROFILE_NAME]
type = databricks
host = your-workspace.cloud.databricks.com
http_path = /sql/1.0/warehouses/abc123
token = dapiXXXXXXXXXXXX
# catalog = main      # optional Unity Catalog
```
```ini
# Trino / Presto.
[$PROFILE_NAME]
type = trino
host = your-coordinator.example.com
port = 8080
user = your-username
catalog = your_catalog
schema = your_schema
# password = ...       # uncomment for HTTPS + basic auth
```
```ini
# DuckDB (local file or in-memory).
[$PROFILE_NAME]
type = duckdb
path = /absolute/path/to/your.duckdb
```

Always finish with the additional-profiles hint (one commented block):
```ini

# Add more profiles by appending another [section]. Switch with AGAMI_PROFILE=<name>.
# [staging]
# type = postgres
# url  = postgresql://readonly:pass@staging-db.example.com:5432/mydb
```

For BigQuery / Databricks / any key-or-token file: remind the user to `chmod 600` it.

### 0a.5 — Resolve the agami interpreter + detect tools

**`$PY` = `data.interpreter.python3` from the Phase-0 `connect_resolve.py` call** — the ONE Python agami uses for the model *and* DB connections (introspection always runs `python -m execute_sql` under it on *every* tier, so the DB driver must live in it). It's already the **scored** pick — the candidate that has `pydantic`+`sqlglot`+`pyyaml` AND the `$DB_TYPE` driver — so there's no guessing and the user sets nothing. **Re-run `connect_resolve.py --db-type $DB_TYPE` now** that the DB type is known, so the interpreter is scored against the right driver and native tools are refreshed. It records as `tool_paths.python3` in 0a.7. (`AGAMI_PYTHON` is honored as a first-priority override but is never required.)

Native CLIs (optional fast path for *queries* — introspection doesn't use them) are in `data.tools` (`psql`/`mysql`/`snowsql`/`sqlite3`/`duckdb`/`bq`, `null` if absent), detected with `which` only. **Forbidden** elsewhere: `pgrep`/`ps`/`lsof`/`find /`/`ls /Applications`/port scans.

**If `data.interpreter.has_driver` is `false`** the `$DB_TYPE` driver isn't in `$PY` yet — confirm via AskUserQuestion, then `"$PY" -m pip install --user <package>` from the table (probe was already done in `$PY` by `connect_resolve.py`):

| `$DB_TYPE` | probe (`"$PY" -c '…'`) | pip package |
|---|---|---|
| postgres / redshift | `import psycopg2` | `psycopg2-binary` |
| mysql | `import pymysql` | `pymysql` |
| snowflake | `import snowflake.connector` | `snowflake-connector-python` |
| bigquery | `import google.cloud.bigquery` | `google-cloud-bigquery` |
| sqlserver | `import pymssql` | `pymssql` |
| oracle | `import oracledb` | `oracledb` |
| databricks | `from databricks import sql` | `databricks-sql-connector` |
| trino | `import trino` | `trino` |
| duckdb | `import duckdb, pytz` | `duckdb pytz` |
| sqlite | stdlib — always present | — |

> **duckdb needs `pytz`** to materialize `TIMESTAMP WITH TIME ZONE` values into Python — without it a query that selects a timestamptz column fails at runtime (`Required module 'pytz' failed to import`), so it's part of the driver install, not optional.

If the driver is missing, **confirm via AskUserQuestion**, then `"$PY" -m pip install --user <package>` (plain `pip install` fallback). Same "never install silently" convention as the model deps (0a.5b). Do this for `$PY` so `sm introspect` connects on the first try.

### 0a.5b — Ensure the semantic-model dependencies

The model (introspection, validation, traversal, curation — everything the `sm` wrapper drives) needs **`pydantic` + `sqlglot` + `pyyaml`** in the interpreter agami uses. Check the resolved interpreter (`$AGAMI_PYTHON` → `.config` `tool_paths.python3` → `python3`):

```bash
"$PY" -c 'import pydantic, sqlglot, yaml' 2>/dev/null && echo "model deps OK"
```

If they're present, continue. If missing, **confirm via AskUserQuestion before installing** (same convention as the DB-driver install above — agami never installs silently):
> agami needs the **agami-core** package (which pulls `pydantic`, `sqlglot`, `pyyaml`) to build and read the semantic model. Install it now? (one-time, user-site — `pip install --user`)

On **Yes**: `bash "${CLAUDE_PLUGIN_ROOT}/scripts/sm" install` — the `sm` launcher is the single place that installs the agami-core library into the resolved interpreter (editable from a dev checkout, else the published package via PyPI or pinned git — so it works in a marketplace install with no `packages/` dir), and everything (`python -m execute_sql` / `python -m semantic_model.cli` / `sm`) then resolves it. On **No**: stop with *"Can't build the model without it — re-run when you're ready to install."* — don't proceed to introspect.

(The `sm` wrapper also self-installs these on first use as a safety net, but doing it here makes it explicit, confirmed, and at a predictable moment rather than mid-introspection.)

### 0a.6 — Ask for `<artifacts_dir>` (first run only)

**The folder is chosen once, on the very first onboarding, and reused for every profile thereafter.** Before asking, check whether it's already set: if the pointer `~/.config/agami/path` exists (non-empty) **or** `AGAMI_ARTIFACTS_DIR` is set **or** `<artifacts_dir>/local/.config` already records `artifacts_dir`, **skip this question entirely** — reuse that path and continue to 0a.7. Only a brand-new install (none of those present) reaches the question below. This guarantees a second/third database lands as a sibling inside the same folder rather than re-prompting (and risking a fragmented, multi-folder setup).

Detect the OS once so the options are platform-native — `uname -s` (`Darwin` = macOS, `Linux` = Linux) or treat `$OS == Windows_NT` / a `MINGW*`/`MSYS*` uname as Windows. Then **AskUserQuestion** with the two defaults for that OS as named options (Recommended first). The auto-provided **Other** lets the user type any absolute path — so this both gives sensible options *and* allows a full custom path:

> Where should agami save your semantic model, examples, and preferences? This is the **parent** for ALL profiles — each lands in `<artifacts_dir>/<profile>/`. It's non-secret (no credentials) — point it inside a git repo to share the tuned model with your team. Credentials stay in `<artifacts_dir>/local/` regardless.

| OS | Option 1 — Recommended | Option 2 |
|---|---|---|
| macOS | `~/agami-artifacts` | `~/Documents/agami-artifacts` |
| Linux | `~/agami-artifacts` | `~/Documents/agami-artifacts` |
| Windows | `%USERPROFILE%\agami-artifacts` | `%USERPROFILE%\Documents\agami-artifacts` |

(For Other, suggest a team repo path as the example, e.g. `~/code/acme-data/agami`.) Expand `~` / `%USERPROFILE%` to an absolute path. Validate: absolute, not inside `<artifacts_dir>/local/`, parent creatable. Store the **resolved absolute path** in `.config.artifacts_dir`.

### 0a.7 — Write `<artifacts_dir>/local/.config`
```json
{
  "schema_version": 1,
  "tier": "<cli | duckdb | python>",
  "active_profile": "$PROFILE_NAME",
  "artifacts_dir": "<resolved absolute path>",
  "tool_paths": { "psql": "...", "python3": "$PY" },
  "detected_at": "<ISO8601 UTC from `date -u +%Y-%m-%dT%H:%M:%SZ`>"
}
```
`python3` MUST be the `$PY` resolved in 0a.5 (the interpreter that has both the model deps and the DB driver) — `sm` and the introspection engine read it from here, so recording the wrong one reintroduces the interpreter mismatch. `chmod 600 <artifacts_dir>/local/.config`.

### 0a.8 — Seed `<artifacts_dir>/USER_MEMORY.md` if missing
Create the parent (`mkdir -p && chmod 755`) and write the default seed (per [`shared/user-memory-format.md`](../../shared/user-memory-format.md)), `chmod 644`. Don't overwrite. Migrate a v1.1 `<artifacts_dir>/local/USER_MEMORY.md` if present.

### 0a.9 — Hand-off, then a card that waits for the file

Show the summary, then the card. Don't ask the person to type a phrase.
```
✓ <artifacts_dir>/local/ ready (chmod 700)
✓ Credentials template → <artifacts_dir>/local/credentials.example
✓ Tool detected: <tool> (<tier>)
✓ Artifacts dir: <resolved path>

Open <artifacts_dir>/local/credentials.example, fill in your connection details and save it
(keep the filename as-is — don't rename it). I'll secure the file and run the full
introspect → enrich → seed flow.

Tip: agami only runs read-only queries, so a read-only database user is all it needs
(and safest). Want the exact GRANT SQL for your database? Just ask.

Heads-up: a cold cloud warehouse (Snowflake especially) makes introspect the slow
step — ~5–15 min for a sizable account. Postgres / MySQL are seconds.
```
Then **AskUserQuestion**, one question, header `Credentials`: *"Tell me when the file is filled in and saved."*

| label | description | then |
|---|---|---|
| `I've filled it in` | Secure the file and connect | 0a.10 |
| `Show me the file again` | Where the file is and what to fill in | Print the path and the field names of the `[$PROFILE_NAME]` section from the template you wrote (from your own copy, never by reading the file: once filled in it holds the password), then the same card again. |

The card is the wait: don't continue to Phase 1 until `I've filled it in` is pressed (or, in a later session, until the person comes back with any data question and preflight returns `promote`).

Show the read-only **Tip** line only for dialects with a user/role concept (`postgres`,
`redshift`, `mysql`, `snowflake`, `sqlserver`, `oracle`, `databricks`, `trino`); **omit** it for
`sqlite` / `duckdb` (file-based, no user) and when BigQuery auth is ADC. If the user asks for the
grant, read [`shared/readonly-grants.md`](../../shared/readonly-grants.md), pick the `$DB_TYPE`
block, and fill in whatever `<…>` placeholders it uses (e.g. `<db>` / `<schema>` / `<warehouse>` /
`<catalog>` / `<project>` / `<dataset>`) from the values they entered.

### 0a.10 — On re-entry: promote the filled-in template, then continue
The person pressed `I've filled it in` on the 0a.9 card (or came back later with a data question, and preflight returned `promote`). **Promote it deterministically** with the helper — do NOT hand-roll an `mv`/append, and do NOT assume "no file yet." The script handles all four cases (first profile → move; Nth profile → append; name clash → refuse; placeholders → refuse), so the second-profile and `[main]`/`[main]` cases can't silently corrupt the file:

```bash
bash "${CLAUDE_PLUGIN_ROOT}/scripts/py" promote_credentials.py
```

Read the first token of stdout and act:
- `SECURED <profile>` → credentials created from the template (chmod 600, `.example` consumed). Continue.
- `APPENDED <profile>` → the new profile was appended to an existing credentials file (other profiles untouched, chmod 600). Continue.
- `COLLISION <profile>` → a profile by that name **already exists**; nothing was changed. Tell the user *"You already have a profile named `<profile>` — pick a different name for this database,"* go back to **0a.3** to rename, re-write the template in 0a.4 under the new name, and show the 0a.9 card again. Never overwrite or duplicate an existing profile.
- `PLACEHOLDERS_REMAIN <fields>` → the template still holds template values; tell the person which fields to fill and show the 0a.9 card again (never introspect against a template).
- `NOTHING` → no `credentials.example` to promote (already promoted, or never written) — fall back to the preflight decision.

(`${CLAUDE_PLUGIN_ROOT}` is the plugin root, filled in with its absolute path when this skill loads; `promote_credentials.py` is stdlib-only and needs no special interpreter.)

**Run `setup_pgauth.py --all`** before the first native-CLI query (writes `.pgpass` / `.mysql.cnf` so passwords never hit the command line). Idempotent. Then continue to Phase 1.

---

## Phase 0s: Sample-database bootstrap (no connection)

**Runs only when the person chose `Try the sample data` on the start card (Phase 0, step 1a)** (or said "I don't have a database" / "try the sample" / ran `agami-connect sample`). This **replaces Phases 0a, 1, and 2** with a short deterministic wire-up — no credential template, no hand-off turn, no live introspection on the fast path. The committed sample dataset + its prebuilt model live at **`${CLAUDE_PLUGIN_ROOT}/samples/store/`** (`seed.sql`, `build_sample.py`, `model/`). Profile is always **`agami-example`**.

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
2. **Resolve `$PY` + the agami-core package** (trimmed 0a.5/0a.5b): SQLite needs **no DB driver** (stdlib), so only ensure the **agami-core** package is importable in `$PY` — confirm via AskUserQuestion, then `bash "${CLAUDE_PLUGIN_ROOT}/scripts/sm" install` (the one, one-time install via the `sm` launcher; brings pydantic/sqlglot/pyyaml and works with no `packages/` dir). Detect the `sqlite3` CLI with `which sqlite3` → `tier = cli` if present else `python`.
3. **Build the `.db`** into the gitignored `local/` (it's regenerable machine state, not committed):
   ```bash
   "$PY" "${CLAUDE_PLUGIN_ROOT}/samples/store/build_sample.py" --out "<artifacts_dir>/local/samples/store.db"
   ```
   Uses the `sqlite3` CLI if present, else the stdlib builder. ~seconds; prints the size.
4. **Write the `[agami-example]` credential by reusing the deterministic promoter** — don't hand-roll the INI. Write a `credentials.example` whose `[agami-example]` section has the **resolved absolute** path from step 3, then promote it:
   ```ini
   [agami-example]
   type = sqlite
   path = <resolved abs path to local/samples/store.db>
   ```
   ```bash
   bash "${CLAUDE_PLUGIN_ROOT}/scripts/py" promote_credentials.py
   ```
   `SECURED`/`APPENDED` → chmod 600, collision-safe. (No placeholders → never trips `PLACEHOLDERS_REMAIN`.)
5. **Write `<artifacts_dir>/local/.config`** (same shape as 0a.7): `active_profile: agami-example`, `artifacts_dir`, `tool_paths.python3 = $PY` (+ `sqlite3` if found), `tier`. `chmod 600`.
6. **Fork — copy the ready-made model, or rebuild it live? (AskUserQuestion).** Ask once, before touching `<artifacts_dir>/agami-example/`:
   > The sample comes with a ready-made semantic model. Want to query it right away, or watch agami build that model from the data first?

   | option | branch |
   |---|---|
   | `Just let me query it (Recommended)` | **6A — copy** |
   | `Build the model from scratch so I can see it work` | **6B — rebuild live (~5–10 min)** |

   - **6A (copy, < 1 min):** `mkdir -p "<artifacts_dir>/agami-example"` then `cp -R "${CLAUDE_PLUGIN_ROOT}/samples/store/model/." "<artifacts_dir>/agami-example/"`. **Validate it loads here**: `bash "${CLAUDE_PLUGIN_ROOT}/scripts/sm" validate "<artifacts_dir>/agami-example"`. If it fails, surface the errors and stop — never leave a half-wired profile. Then **mint the deployment `org_id`** (a *copy* carries no id — introspect mints inline, a copy bypasses it): `bash "${CLAUDE_PLUGIN_ROOT}/scripts/sm" ensure-org-id "<artifacts_dir>/agami-example"` — idempotent, writes a locally-minted uuid into `datasource.yaml` so the sample deployment has a portable identity like any other. Then **stamp a model_version** (a *copy* doesn't go through introspect/curate, so nothing auto-stamps it): `bash "${CLAUDE_PLUGIN_ROOT}/scripts/sm" snapshot "<artifacts_dir>/agami-example"` — best-effort, so the answer receipt shows a version rather than `null`. (We stamp at copy time instead of committing a static `.snapshots/` so it always matches the model's actual content; 6B gets one automatically from introspect.)
   - **6B (rebuild live — "watch it build"):** ignore the committed `model/` and run the **normal Phases 1→2** against the `agami-example` profile (`--db-type sqlite`) — the same introspect → enrich → seed pipeline a real onboarding uses, just pointed at the sample SQLite file. It takes a few minutes (the non-default option). **Don't mention tokens, cost, or billing** — surface time (~5–10 min), not scary money words.
     - **Sample carve-outs — the dataset is small + curated, so DON'T prompt (build silently over ALL tables):** skip the [Phase 1.6](#16--discover--prune-the-table-list-cheap-first-pass) **table** card, skip the **org-description** prompt (Phase 2f / 0a), and skip the **doc/metrics intake** (Phase 1's "do you have a data dictionary / dbt repo?"). These prompts exist for a real unknown DB; for the sample they're noise. Introspect + enrich every sample table without asking.
     - **Clear the pre-seed gate before seeding (the silent build has no human to sign off).** Enrichment lands the sample's metrics/entities `unreviewed`, so `seed-examples` would refuse with `preseed_review_pending`. Because the sample is curated and trusted, **auto-approve the queue as a system signer** right before seeding — the silent path clears its own gate:
       ```bash
       bash "${CLAUDE_PLUGIN_ROOT}/scripts/sm" approve-queue "<artifacts_dir>/agami-example" --signer system --role system
       ```
       (This is only sanctioned for the curated sample; a real database keeps the human sign-off gate.)
     - **When the model validates, show what was built in the chat**, the overview [`/agami-model`](../agami-model/SKILL.md#phase-1-overview-and-the-card) posts (its Phase 1: read the model with `sm model-tree` and `sm review-items`, never the YAML): one sentence on what needs sign-off (after the system approval above, usually nothing) and the `Subject area | Tables | Hidden` table. This is the whole point of "watch it build"; a wrap with no numbers would defeat it. Post it *together with* step 7's short dataset description + starter questions (so don't cede the turn). No page opens: the model explorer is the full view, rendered and served from localhost by `/agami-model` only when the person asks for it. (Do **not** render the NL→SQL examples-validation page either.)

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

     (The first two are the multi-table joins. All four are backed by EXPLAIN-validated seed examples in the shipped model, so they answer reliably. "Other" takes any question, a monthly trend or "customers with both an order and a subscription" among them.) Where AskUserQuestion is unavailable, list the same four as a numbered list ([question-cards rule 7](../../shared/question-cards.md#7-where-askuserquestion-is-unavailable)).
   - Then **stop and wait.** Nothing about agami-serve / agami-model / corrections yet — let them experience one real query in Claude Code first. When the user picks a question or asks anything, hand to [agami-query](../agami-query/SKILL.md) — it answers AND runs its own first-query flow (the one-time GitHub-star ask + `/agami-serve` pointer, gated by `local/.optins`). **Do NOT pre-narrate fan/chasm traps or "what agami caught."** If their question hits a trap (questions 1–3 can), agami-query surfaces it in the answer's receipt — let it happen on a real question instead of scripting it.
8. **The "where to go deeper" footer (corrections / `/agami-model` / `/agami-serve`) fires from agami-query, not here.** When the user asks their first sample question, agami-query owns the turn — so its Phase 4f surfaces a one-time orientation footer for the `agami-example` profile (see [agami-query SKILL.md → Phase 4f](../agami-query/SKILL.md)). Don't try to print it from Phase 0s — a footer placed here never executes once agami-query has taken over. Phase 0s ends at step 7 (describe + questions + stop).

---

## Phase 1: Introspect → semantic model

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
- **Try the sample database** — explore agami's bundled **`agami-example`** sample (retail + subscriptions, no connection needed), leaving `<profile>` untouched. Routes to [Phase 0s](#phase-0s-sample-database-bootstrap-no-connection). This MUST be a real selectable option here (not a prose aside) — a returning user with an onboarded profile has no other visible path to the sample, since plain `/agami-connect` resolves their active profile and lands on this menu.

(Cancel isn't a listed option — the modal's Esc / "Other" covers "do nothing"; the four real choices are the actions above. Keep the list at exactly these four.)

**Same DB, another *schema*? That's the Re-introspect path, not "Onboard another database."** If the user wants to add a schema that lives in the **same database** they already onboarded (e.g. they did `public`, now they want `billing` too), choose **Re-introspect** and **expand the schema selection** in Phase 1.3 to include both the old and the new schemas. The engine scans them together in one pass, so any relationship between the original and the new schema is detected as a first-class **cross-schema** join (Case 1) and surfaced for review. Picking "Onboard another database" instead would split the two schemas into separate models and demote any link between them to manual cross-profile glue (Phase 2b federation) — wrong for one DB. If you're unsure which the user means, ask: *"Is `billing` in the same database connection as `<profile>`, or a different server/database?"* — same connection → Re-introspect + expand schemas; different → new profile.

The engine **auto-backs-up any legacy (v1) model** (`index.yaml` + per-schema `_schema.yaml`) it finds at the profile root into `.legacy_backup/` before writing — so a first run over an old profile is safe and reversible; surface a one-liner when that happens.

### 1.1a — Same company vs different company

When the user chooses **Onboard another database**, ask (AskUserQuestion) whether the new database belongs to the **same company** or a **different** one. A deployment (one artifacts dir) holds exactly **one** company/organization with many datasources under it; a genuinely different company is a **different folder**.

- **Same company (the common case)** → the new profile **attaches under the deployment org**: it lands in the **same** `<artifacts_dir>`, shares the same `org_id` from the root `organization.yaml`, and **shares the company context** authored earlier. Do **not** re-ask the company description (1.4 detects the already-authored company context and skips it); the per-database note is still asked. Narrate it: *"This attaches under your **`<company name>`** organization, sharing the company context you set earlier — its per-database notes stay its own."*
- **Different company** → set up a **new deployment folder**. Pick/confirm a new artifacts dir path, create it, and **repoint** the pointer to it (the same idiom Phase 0a.1 uses for a non-default dir):
  ```bash
  mkdir -p "<new_artifacts_dir>" && mkdir -p ~/.config/agami && printf '%s\n' "<new_artifacts_dir>" > ~/.config/agami/path
  grep -qxF 'local/' "<new_artifacts_dir>/.gitignore" 2>/dev/null || printf 'local/\n' >> "<new_artifacts_dir>/.gitignore"
  ```
  The current org's files are **untouched**; the first onboarding in the new folder mints a **fresh** `org_id` + its own company record (1.4 authors it there). This is the only way to run two companies from one machine — one folder each.

### 1.2 — Scope: schemas, and the no-catalog case

Run `cli areas`/probe is not needed yet — schema discovery happens inside the engine. But **decide scope first**:

- **Catalog reachable (common):** after the engine lists schemas, it introspects all of them. If the DB has many schemas (Snowflake with 50+), 1.6's summary shows them with their table counts and its **Only some schemas** answer narrows them; there is no separate schema question on a first onboard.
  - **Supabase is handled automatically** — the engine detects it by signature and drops its system schemas (`auth`, `storage`, `vault`, `realtime`, `extensions`, …) so only the user's app schemas (e.g. `public`) are modeled (it surfaces a note saying which it skipped). **Don't hand-build a `--tables` allowlist for Supabase** to dodge the system tables — that's already done, and a manual allowlist is fragile (shell word-splitting, missed tables). Only allowlist when the user genuinely wants a subset of their *own* tables.
- **Catalog denied (locked-down role):** if a quick probe shows the catalog isn't readable, the engine **cannot enumerate tables**, so ask for them on a card. Say first, in one line: *"Your role can read the data but not the catalog, so I can't list the tables myself; I'll describe each one from the data."* Then call AskUserQuestion, one question, header `Tables`, *"Which tables should I model? Type them in Other, e.g. `sales.orders, sales.customers`."*:
  - The names go in "Other" (the question says so; there is no option for typing them, since one would be clicked with nothing typed): comma- or line-separated, `schema.table` or a bare table name.
  - **Show the grant to ask for**: this database's read-only grant from [`shared/readonly-grants.md`](../../shared/readonly-grants.md) in a fenced block, for the person to give their admin, then this card again.
  - **Stop for now**: nothing is introspected; say they can connect again once the role can read the catalog or they have the list.

  Write the names, one per line, with the Write tool to `<artifacts_dir>/local/tmp/agami-names.txt`. Where an inventory exists (a re-introspect, or a discover pass that listed some tables), check them first with `bash "${CLAUDE_PLUGIN_ROOT}/scripts/py" inventory_summary.py --inventory "<inventory_path>" --keep names:<artifacts_dir>/local/tmp/agami-names.txt --tables-out "<artifacts_dir>/local/tmp/agami-keep.txt"`: an `unmatched` or `ambiguous` name is said back and the same card asked again, never guessed. With no inventory to check against, pass the file as it is. Then run the engine with `--tables-file` on that file (never an unquoted `--tables` list). Everything the engine then infers (types, grain, FKs) lands `unreviewed` for sign-off.

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

Three cards, asked right after the connection works and before tables are listed: the company (once per deployment), what the person can share (1.5), and this database's one line. They are required state-gathering: "don't ask clarifying questions" does NOT cancel them, and the skill never decides skip for the person. Conventions: [`shared/question-cards.md`](../../shared/question-cards.md). The sample path skips all three (its model is ready-made).

**A. Company card — ONCE, on the first onboard into this deployment.** Decide "already authored" by content, not by file existence: done only if the root `<artifacts_dir>/organization.md` exists **or** `<artifacts_dir>/organization.yaml` has a non-empty `name`/`description`. Otherwise AskUserQuestion, one question, header `Company`:

> *"What's your company called, and what does it do? Type a sentence or two in Other, name first. It's shared by every database you connect here."*

Options: **Skip for now** (no company context; say it can be added later in `/agami-model`) and **Draft it from the database** (after the build, Phase 2f drafts a short description from the model and Phase 7 shows it as a draft to correct). Text in "Other" is the answer. Write it as both of these (the record write is the only thing that fills `organization.yaml`):
1. `bash "${CLAUDE_PLUGIN_ROOT}/scripts/sm" set-org "<artifacts_dir>" --name "<company name>" --description "<one-line summary>"`
2. The full text to the **root** `<artifacts_dir>/organization.md` under `# About this company` (**narrative prose ONLY** — no model facts).

Then confirm both landed. On a **later** onboard (company context already there) don't ask: say *"Sharing the **`<company>`** context you set earlier."*

**B. Context card — every onboard.** This is 1.5 below; ask it after A.

**C. Database card — every onboard.** The one line `list_datasources` shows an agent so it can pick the right datasource (an organization with several refuses a query that names none), and, when the person writes more, this database's narrative. AskUserQuestion, one question, header `Database`:

> *"In one line, what does this database hold, and when would someone query it? Type it in Other, e.g. "Invoices, payments and products: use for revenue questions." Add more sentences if a term means something specific here."*

Options: **Generate one for me (Recommended)** / **Skip**.

**Nothing is written at this step** — `datasource.yaml` does not exist until introspection (1.7) creates it. Bind the answer and act on it after 1.7:
- **Text in "Other"** → its first sentence is `$DATASOURCE_DESCRIPTION`, written right after 1.7: `bash "${CLAUDE_PLUGIN_ROOT}/scripts/sm" set-description "$ROOT" --description "$DATASOURCE_DESCRIPTION"`. When it has more than one sentence, the whole text also goes to `<artifacts_dir>/<profile>/datasource.md` under `# About this database`.
- **Generate one for me** → `$DATASOURCE_DESCRIPTION = generate`. **Phase 2f** writes it once every table is enriched, and the Phase 7 summary shows it as a draft they can edit.
- **Skip** → leave it empty. Say plainly that `model_deploy` will warn about it, and that an agent picking between several datasources will be guessing from the name.

If `datasource.yaml` already has a non-empty `description` (a re-onboard), show it and ask `Keep it (Recommended)` / `Replace it` instead. **Keep it** → `$DATASOURCE_DESCRIPTION = keep`; nothing is written. **Replace it** → ask the card above and bind from that answer. Without a narrative from this card, Phase 2f writes a short per-database starter from the enriched model.

`chmod 644` whatever you write — these are **non-secret model files** and must stay readable (e.g. by the deploy container / a teammate reading a checked-in copy); never `chmod 600` them (that's for `local/` secrets only). See [`shared/organization-context-format.md`](../../shared/organization-context-format.md) for the content-routing rule (company-wide → root; per-database → the profile; per-column units → the structured model; personal → `USER_MEMORY.md`).

### 1.5 — Existing data model / semantic layer (MANDATORY — ALWAYS ASK)

The context card (1.4 B). Same "required state-gathering" rule. AskUserQuestion, one question, header `Context`, **multiSelect**:

> *"What can you share so answers match how your team works? Pick any that you have."*

| label | description | then |
|---|---|---|
| `Definitions or a semantic layer` | A data dictionary, metrics list, ERD, spec workbook, or a dbt / LookML / Cube repo | A follow-up card: *"Type the file or folder path in Other, or attach the file to your next message."*, options **Skip this one** / **I'll attach it** (wait for the attachment). Then read it as the sections below say: a doc, a column dictionary, a model spec workbook, or a semantic-layer repo by what the path holds. |
| `Questions people ask` | Real questions from your team; they become checked examples | A follow-up card: *"Type them in Other, one per line, or a file path."*, options **Skip this one** / **I'll attach a file**. Save them with the Write tool to `<artifacts_dir>/local/tmp/agami-asked-questions.txt`, one per line; Phase 5 writes SQL for each and checks them with the rest. |
| `Report SQL or dashboards` | SQL you trust, an export, or a screenshot; checked against the model after the build | A follow-up card: *"Type a path to a .sql or .csv file in Other, or attach a screenshot to your next message."*, options **Skip this one** / **I'll attach it**. Copy files or pasted SQL into `<artifacts_dir>/local/reconcile/inbox/` (SQL as `<ts>.sql`, a CSV as is; note a screenshot's path). Phase 6b hands them to `/agami-reconcile`. |
| `Nothing to share` | Build from the database alone | Go to 1.6. Picked with another option: say it back and ask the card again. |

**"Other" on the context card** takes a named product ("it's ServiceNow", "this is our Salesforce") → the published-product-schema path below; or any of the above typed straight in (a path, questions, SQL), routed by what it is. A question about the options ("what's a semantic layer?") is answered first and the card asked again ([question-cards rule 5a](../../shared/question-cards.md#5a-a-question-typed-in-other-is-answered-then-the-same-card-is-asked-again)).

Ask the follow-up cards one at a time, in the table's order. Nothing here is required: every follow-up's **Skip this one** goes on to the next.

**A model spec workbook is recognised by its sheets, whatever the user called it:** an `.xlsx` with `Subject areas`, `Tables` and `Joins` sheets is one, attached under the first option like any doc. Take the [model-spec path](#18--apply-a-model-spec-only-when-one-was-attached) for it — **never `Read` it as a doc**: the parser reads every row, and a doc read is trimmed and paraphrased.

**If a doc:** `Read` the path (handles PDF/image/md/text/CSV natively; trim huge files to first 20 pages / 50 rows). Any other `.xlsx`/`.docx` → ask for PDF, proceed without if not.

**A column data dictionary is not read as a doc.** A CSV or JSON with one row per column (`table`, `column`, `description`, optionally `area` / `schema`) is the customer's own text for each column: note its path as `$DICTIONARY_FILE` and apply it after introspection with `sm describe-file --source metadata` ([2a.0](#customer-dictionary-file)), which applies every row one to one. Don't `Read` it into the prompt (the read is trimmed and paraphrased) and don't convert it into curate ops yourself. A dictionary in another shape (PDF, prose, an `.xlsx`) → ask for a CSV export; if there is none, read it as a doc as above.

**If a published product schema (or the user names one — "use the ServiceNow data model online"):** the user wants the standard fields grounded in the authoritative reference, not your training memory.

1. **Prefer the in-DB metadata.** Most metadata-driven platforms carry their own dictionary *in the database* (ServiceNow `sys_dictionary`/`sys_choice`, SAP `DD03L`, …). That's authoritative AND machine-readable — far better than scraping docs. So the real answer here is usually **Phase 2a.0's `sm enrich-metadata`**, not a web fetch. Note that intent now; it runs during enrichment.
2. **Web reference as fallback / supplement.** When there's no in-DB dictionary, **`WebSearch`/`WebFetch`** the official developer/data-dictionary docs for the named product + the tables you discovered in 1.6. If you chose this path, the fetch is **mandatory, not optional** — do it once, upfront, and stash into `$DATA_MODEL_DOC_TEXT` (never written to disk). Note that vendor doc portals are often **JS-rendered and return only nav skeletons** (ServiceNow's does); when a fetch comes back empty, fall back to WebSearch result snippets / community pages and say so — don't silently substitute training knowledge.
3. **Propagate it.** If you fan enrichment out to subagents, **every subagent prompt MUST include `$DATA_MODEL_DOC_TEXT`** with the instruction: *describe standard fields from this reference; if a field isn't in the reference or the data, mark it `ai_unknown` — do NOT fill it from general product knowledge.* (This is the hole that let subagents drift to "from ServiceNow domain knowledge.")

Stay anchored to the tables you actually introspected; don't invent fields the live DB doesn't have.

**If a semantic-layer repo:** use the directory from the follow-up card (a local clone / monorepo path — no upload needed since it's git-backed). Glob the **definition** files and `Read` them up to a budget (~30 files / ~250 KB total; if larger, prefer metric/model definitions and tell the user what you sampled). **Skip compiled SQL and data files** — you want the declared metrics/joins, not the warehouse output:
> | Layer | Read these | Carries |
> |---|---|---|
> | **LookML** | `*.view.lkml`, `*.explore.lkml`, `*.model.lkml` | dimensions, **measures** (→ metrics), **joins** (→ relationships), `sql_table_name` |
> | **dbt** | `models/**/*.yml` (esp. `schema.yml`), `semantic_models/**`, `metrics/**`, `dbt_project.yml` | column descriptions, `relationships` tests (→ FKs), MetricFlow metrics/measures |
> | **Cube** | `model/**/*.{yml,js}` (or `schema/**`) | `measures`, `dimensions`, `joins` |

Stash everything gathered (doc text + repo definitions) as `$DATA_MODEL_DOC_TEXT` for enrichment — give entities/metrics/relationships found here **`confidence: inferred`** (a declared metric is a strong signal but still wants a human sign-off; FK-derived joins stay as the engine set them). **Never written to disk** — lives only in the enrichment prompt, then discarded. `Skip` → proceed.

### 1.6 — Discover & prune the table list (cheap first pass)

**Before the full, expensive introspection, summarise the tables in the chat and ask which to keep** — so staging/backup/scratch tables and dated snapshots can be dropped. The full grain/FK/description work then runs on **only the kept tables**, which on a big DB (hundreds of staging/snapshot tables) is the difference between minutes and hours.

**Run the discover pass** — it lists tables + columns only (no grain, FK, or row-count probes, so it's fast even over a tunnel):

```bash
ts=$(date -u +%Y%m%d-%H%M%S)
AGAMI_SQL_MAX_ROWS=200000 bash "${CLAUDE_PLUGIN_ROOT}/scripts/sm" discover \
  --profile <profile> --db-type <db_type> \
  --artifacts "<artifacts_dir>" \
  [--schemas <selected_schemas…>]   # the 1.3 pick, if you narrowed schemas
```

> **<a id="raised-cap"></a>Why the `AGAMI_SQL_MAX_ROWS` prefix — and why it must stay a prefix.** The executor **refuses** (never truncates) any result over the row cap, default 1000. That bound is sized for a *user query*, where a runaway is the thing it exists to stop. A **catalog read** blows past it on schema size alone — `information_schema.columns` for a 500-table catalog is tens of thousands of rows — and the engine **swallows that refusal silently**, degrading to one round-trip per table, or reporting "catalog enumeration denied" when the catalog was in fact readable. So the three commands that read catalog or dictionary tables — `discover` (1.6), `introspect` (1.7), `enrich-metadata` (2a.0) — run with a raised cap.
>
> - **Inline on the command, never `export`.** An exported value is still set when Phase 6 runs the demo *user* query, quietly raising the bound for exactly the caller it exists to hold. The inline form dies with the process.
> - **Never on `seed-examples` (Phase 5).** That command validates SQL which later runs as a real user query; validating under a raised cap green-lights examples that get refused in production.
> - **Tell the user you did it**, once, with the real numbers the commands print — and say their query-time cap is unchanged. See 2a.0 for the wording.

It writes the inventory to `<artifacts_dir>/<profile>/.introspect/inventory.json` and prints JSON with `table_count` and `inventory_path`. No page is written: everything about picking tables happens in the chat.

**Summarise it in the chat, then ask one card** (conventions: [`shared/question-cards.md`](../../shared/question-cards.md)). No page opens and nothing waits on a pasted block.

```bash
bash "${CLAUDE_PLUGIN_ROOT}/scripts/py" inventory_summary.py --inventory "<inventory_path>"
```

It prints `{tables, schemas, schemas_more, likely_noise, groups}`. Show:

1. One sentence: *"<N> tables in <K> schemas; <M> look like staging or backups: `stg_*`, `*_bak`, `*_2023`."* (name the real patterns from `groups.noise_by_pattern`; leave the second clause out when `likely_noise` is empty).
2. A markdown table, one row per entry of `schemas` (`Schema | Tables`), at most 15 rows, then the line *"and `schemas_more.schemas` more schemas (`schemas_more.tables` tables)"* when `schemas_more` is not null. Never list tables one by one.
3. The first 5 names in `likely_noise` with their `why`, then "and N more", so the person sees what "Drop staging and backups" would remove (a real table such as `test_results` shows up here before they press it). If they ask for the whole list, show up to 15 rows and "and N more".

Then call AskUserQuestion, one question (header `Tables`):

- **Keep everything**: model all `<N>` tables.
- **Drop staging and backups**: skip the `<M>` likely-noise tables. Mark it **(Recommended)** and put it first only when `likely_noise` is not empty and does not cover every table. With no noise, or when it covers every table, leave this option out; in the second case say that everything looks like staging or backups and ask what to keep.
- **Only some schemas**: pick which schemas to model. Leave it out when there is a single schema.
- **Let me name them**: "Other" takes the table names. Names typed straight into "Other" count as this answer. If the option is picked with no names, ask a second card ("Which tables?", options "Keep everything" and "Drop staging and backups") whose "Other" takes the names.

Add one line before the card: *"Ask to see any schema's tables or any table's columns, or type columns to leave out in Other (`schema.table.column`)."*

Act on the answer, writing the kept set to `<artifacts_dir>/local/tmp/agami-keep.txt`:

| Answer | Do |
|---|---|
| Keep everything | Skip the file. Run 1.7 with neither `--tables-file` nor `--exclude-columns`. |
| Drop staging and backups | `inventory_summary.py --inventory "<inventory_path>" --keep drop_noise --tables-out "<artifacts_dir>/local/tmp/agami-keep.txt"` |
| Only some schemas | Ask a follow-up AskUserQuestion, multiSelect. Up to 4 schemas: one option each. More: the options are `groups.schema_groups` (label, schema count and table count), and a picked group keeps all its schemas; "Other" takes schema names to add or to treat on their own. Name each group option's `schema_count` and `tables`. Add a "None of these" option only when that leaves fewer than 4 options; at 4, "Other" is the way out (a card holds 2 to 4 options). Then `--keep schemas:<a,b,c>`. |
| Let me name them | Write the names (one per line, `schema.table` or a bare table name) with the Write tool to `<artifacts_dir>/local/tmp/agami-names.txt`, then `--keep names:<artifacts_dir>/local/tmp/agami-names.txt`. |

`--keep` prints `{"tables_file", "kept"}`; say how many tables are kept and pass `--tables-file "<tables_file>"` to 1.7. **`{"error": "no tables kept"}` (exit 1, nothing written) means the choice left no tables: say so and ask the same card again.** **A name that matches no table, or a bare name that matches several, comes back as `{"unmatched": [...], "ambiguous": {...}}` with exit 1 and nothing written: tell the person which ones, and ask the same card again. Never guess.** Column exclusion is not asked here: personal-data columns are handled in review.

If the person asks to skip this ("just introspect everything"), proceed to 1.7 unscoped.

**Showing what is there (when asked).** "What's in sales?", "show me the columns of orders": list it in the chat from the inventory, then ask the same card again.

```bash
bash "${CLAUDE_PLUGIN_ROOT}/scripts/py" inventory_summary.py --inventory "<inventory_path>" --show "<schema | schema.table | table>"
```

A schema prints its tables with their column counts; a table prints its columns with their types. Show them as a markdown table (`Table | Columns` or `Column | Type`), at most 15 rows, and *"and `more` more"* when `more` is not 0. `{"unmatched"}` or `{"ambiguous"}` (exit 1): say which name didn't match, or list the tables it could mean, and ask the same card again.

**Columns to leave out, typed in "Other".** Names of the form `schema.table.column` (alongside a table answer or on their own) are columns to exclude from the build. Write them one per line with the Write tool to `<artifacts_dir>/local/tmp/agami-drop-columns.txt` and check them:

```bash
bash "${CLAUDE_PLUGIN_ROOT}/scripts/py" inventory_summary.py --inventory "<inventory_path>" --check-columns "<artifacts_dir>/local/tmp/agami-drop-columns.txt"
```

`{"exclude_columns": [...]}` → pass them to 1.7 as `--exclude-columns <names…>` and say how many columns are left out. `{"unmatched": [...]}` (exit 1) → say which names are not columns and ask the same card again; never guess. Personal-data columns are asked about again on their own card after the build (Phase 4), and any column can be hidden later from `/agami-model`, so nobody has to find every column now.

### 1.6s — With a model spec: the spec IS the kept set

When a model spec workbook was attached in 1.5, convert it FIRST and skip the 1.6 table card — the spec already names every table to model. Its outputs go under `local/`, outside the profile, so moving a partial model aside (1.8) never takes them with it (`mkdir -p` that folder first):

```bash
"$PY" "${CLAUDE_PLUGIN_ROOT}/scripts/model_spec_workbook.py" parse --file "<workbook.xlsx>" \
  --out "<artifacts_dir>/local/model-spec/<profile>/spec.json" \
  --tables-out "<artifacts_dir>/local/model-spec/<profile>/spec-tables.txt"
```

It prints the counts it read (`subject_areas`, `tables`, `joins`, `joins_approved`, `joins_with_condition`, `metrics`, `sensitive_columns`, `rules`). **Quote those numbers verbatim — never count a sheet by reading it.** Exit 1 prints `errors` naming the missing sheet or column: show them and stop, the person fixes the workbook. Then run 1.7 with `--tables-file` set to `spec-tables.txt` as **ONE call — the single background call + progress tail (1.7 option 2), never `--append` batches**, however many tables the spec names. The spec is applied once, over the whole set, so read the whole set in one go.

### 1.7 — Run the introspection engine (on the kept tables)

This is the deterministic core — it replaces hand-authoring tables/columns/FK SQL/confidence formulas. From `plugins/agami/scripts/`:

```bash
# write the kept allowlist to a file first (zsh-safe — no word-splitting to get wrong)
printf '%s\n' incident problem change_request sys_user ... > "<artifacts_dir>/local/tmp/agami-keep.txt"
AGAMI_SQL_MAX_ROWS=200000 bash "${CLAUDE_PLUGIN_ROOT}/scripts/sm" introspect \
  --profile <profile> --db-type <db_type> \
  --artifacts "<artifacts_dir>" \
  [--tables-file "<artifacts_dir>/local/tmp/agami-keep.txt"] \
  [--exclude-columns <schema.table.column list from 1.6>]
```

(The raised cap is the same one 1.6 explains — [why](#raised-cap). Without it a large schema's `sql_tables` / foreign-key reads are refused, the engine swallows it, and you get a model with no join graph and no error to show for it.)

`--tables-file` is the prune step's **kept set** (it also covers the no-catalog/probe-only case from 1.2). `--exclude-columns` marks the dropped columns excluded during the build — one validated write, no follow-up curate call. Omit both only when the user kept everything or skipped pruning. (If a table name is bogus / can't be described, the engine now drops it with a note and — if *nothing* describes — errors clearly instead of writing a partial model.)

**On a large schema (50+ tables) over a tunnel this takes minutes and the command prints nothing until it returns — which reads as hung.** Two ways to keep it legible, in order of preference:

1. **Batched build (preferred for 30+ tables) — `--append`.** Split the kept allowlist into batches of ~10–15 tables and introspect **one batch per call**, each a quick *foreground* command that returns in tens of seconds. Every call MERGES into the existing model (prior tables are loaded from disk, never re-queried), so you end with the full union — and you report `batch 3/5 (+12 tables)` between calls, natural progress with **no background monitor to babysit**:
   ```bash
   split -l 12 "<artifacts_dir>/local/tmp/agami-keep.txt" "<artifacts_dir>/local/tmp/agami-batch-"      # batch files
   for b in "<artifacts_dir>/local/tmp/agami-batch-"*; do
     AGAMI_SQL_MAX_ROWS=200000 bash "${CLAUDE_PLUGIN_ROOT}/scripts/sm" introspect \
       --profile <p> --db-type <t> \
       --artifacts "<artifacts_dir>" --tables-file "$b" --append
   done
   ```
   (`--append` on the first batch just creates the model — there's nothing to merge yet. The relationship pass runs each batch across the union, deduped, so the final batch yields the complete join graph.)
2. **Single background call + tail.** If you'd rather one call: run it **in the background** (Bash `run_in_background: true`) and `tail` its progress log every ~15–20s, surfacing the latest line. The engine writes a flushed, **throttled** (~every 10%) per-phase log to `<artifacts_dir>/<profile>/.introspect/progress.log` (override with `--progress`): `discovered 52 tables …`, `columns+grain 30/52`, `relationships: declared FK 80/187`, `done`.

(For a small DB introspect returns in seconds — no batching or backgrounding needed.)

It builds + **validates** + writes the model at `<artifacts_dir>/<profile>/`: storage connection, **proposed subject areas**, per-table columns + types (catalog or value-inferred), PK→`grain`, other unique keys→`unique_keys`, FK→`relationships` with **inferred cardinality** (`many_to_one`/`one_to_many`/`one_to_one`, one-to-one when the join is on a whole key on both sides; a join only the data shows is unique keeps its label and lands `unreviewed` with a "looks one-to-one, confirm" note), `column_groups` on deep tables (≥30 cols), `sensitive` flags on PII (by whole-word column name, email-valued columns, and personal keys inside JSON values, named in a column caveat), cross-area edges, and a report. Relationships from **unenforced-FK** dialects (Redshift/Databricks/Trino) and everything from probe mode are confirmed-by-overlap or `unreviewed`. The report prints the **capability mode per step** (catalog vs probe) — surface that to the user so they know what was read vs inferred.

The validator gates the write — **if it fails, the model is not persisted.** Surface the errors verbatim and stop (this should be rare; the engine emits valid models).

**Post one progress line, with numbers from the engine's output only.** No card here: the build asks nothing. Every number comes from the report it printed (`tables:`, `relationships:`, `subject_areas (<A>)`, `capability modes:`, `deep tables:`, `sensitive columns:`); never estimate, round up or fill a number the output didn't give. For how many joins came from declared foreign keys, count the relationship items with `origin: "fk"` in `bash "${CLAUDE_PLUGIN_ROOT}/scripts/sm" review-items "$ROOT"`; if you didn't run it, leave that clause out.

`✓ Read <N> tables into <A> subject area(s) (<catalog|probe> mode); <R> joins, <F> from declared foreign keys; <S> columns flagged personal; <D> wide tables.`

Add **one finding** on the next line when the report has one worth a person's attention, quoted from it: a `suspected personal data (review):` line, a `large table` line, or a note that dropped a table. One, not a list; none when there is none. Batched builds post `batch 3/5 (+12 tables)` between calls, then this line once at the end.

**Empty tables — say so before going on.** Read `rows` per table from `bash "${CLAUDE_PLUGIN_ROOT}/scripts/sm" model-tree "$ROOT"` (measured during the build; `null` when it wasn't). When some tables have 0 rows, add to the progress line *"<k> of <N> tables are empty"* and name up to 5. **When every measured table is empty**, the build can't find joins or keys from the data, and every example will return nothing, so stop and say it plainly: *"These <N> tables have no rows: the database holds the structure only."* Then AskUserQuestion, header `Empty`: **Connect a different database (Recommended)** (Phase 0a, a new name) / **Continue with the structure only** (descriptions, metrics and sign-off still work; Phase 6's examples will show 0 rows, so say that again there) / **Stop here**. A live run built a whole model on an empty demo database before anyone noticed.

**Backstop reconciliation — if `<N>` is fewer than the catalog held, say what's NOT in the model and why** (the 1.3 guard should have covered this up front; repeat it here so it can't slip). E.g. *"Modeled 52 of 70 tables — left out `public` (17, separate Salesforce dataset) and `pg_auto_copy` (1, system). Say the word if you want any of them."* A user who picked "all" should never have to diff the catalog themselves to notice a schema is missing.

### 1.8 — Apply a model spec (ONLY when one was attached)

Introspection proposed areas by name and joins only where the catalog or `*_id` names showed them. The spec replaces both, in one validated step that writes nothing unless the whole result validates and restores the previous model if anything fails:

1. **Dry run** and show the result:
   ```bash
   bash "${CLAUDE_PLUGIN_ROOT}/scripts/sm" apply-spec "<artifacts_dir>/<profile>" \
     --file "<artifacts_dir>/local/model-spec/<profile>/spec.json" --dry-run
   ```
   `errors` lists every problem at once (a table the build didn't find, an area a table names that the spec doesn't declare, a join column that doesn't exist) — show them all and stop; the person fixes the workbook and you re-run 1.6s.

   **A table "not in the introspected model" that IS in the database** means its columns couldn't be read during the build (introspect's notes say `dropped … no readable columns`; a dropped connection is the usual cause). A second introspect into the same profile can't add it — once a model exists, its queries are scoped to the model's own tables — so move the partial model aside and rebuild once: `mv "<artifacts_dir>/<profile>" "<artifacts_dir>/<profile>.partial-<timestamp>"` (never delete it), then re-run 1.7 with the same `--tables-file`. If the same tables are missing again, stop and name them: the connecting role probably can't read them.
2. **If the spec approves anything** (`joins_approved` > 0, or a metric marked approved), ask ONE question: *"The spec marks <n> joins and <m> metrics approved. Sign them off as you (<email>)?"* — `Yes` → pass `--signer <email> --role owner`; `No` → re-run without `--signer` after the person clears the Approved column. Never sign off in someone's name without asking.
3. **Apply**: the same command without `--dry-run`. Each join lands where the runtime serves it — in the area that owns both its tables, else as a cross-area join between the two owners — so the counts are `joins` = area joins + `cross_area_joins`.
4. Surface the counts from the JSON, verbatim: *"Applied the spec: <A> subject areas (<per-area owns / lists>), <J> joins (<approved> approved, <with condition> with a condition, <cross> between areas), <M> metrics, <S> sensitive columns, <R> rules; dropped <D> introspected tables the spec doesn't name; replaced <I> joins introspection had guessed."*

Surface any `notes` too — in particular a metric flagged as a plain aggregate: ask once whether to drop it and put its name in the column description instead (re-run 1.6s after they edit the workbook), or keep it.

**After a spec, the model's structure is the person's, not yours.** Through the rest of the run: don't propose, add or re-infer joins; don't re-split subject areas (Phase 3 shows the spec's areas, it doesn't propose new ones); don't add metrics the spec didn't name (2c — the spec's metrics are already in); carry its rules as written (they are in `datasource.md` already). Enrichment still describes tables and the columns the spec's Columns sheet left out — the spec's own descriptions are `human` and a generated one is refused over them. **Run 2b (entities) as usual:** entities are the model's vocabulary (the business nouns questions are routed by), not structure, and a spec doesn't declare them. **Regroup wide tables as usual too** (the step at the end of 2a), even when the spec described every column: a spec doesn't declare column groups, so they're still the engine's name-prefix buckets.

---

## Phase 2: Enrich (the LLM layer — validated into the model)

The engine gives structure; you add meaning. Load the model with `cli bundle <root> --area <area>` (or read the YAMLs). After each enrichment pass, **re-validate** (`cli validate <root>`) and never persist a model that fails. `<root>` = `<artifacts_dir>/<profile>/`.

**One progress line per step, no card.** After each step below, post one line with the numbers that step's command printed, and nothing invented: never a count you worked out by reading YAML, never a percentage you estimated. Leave out a step that didn't run.

| Step | Line | Numbers from |
|---|---|---|
| 2a.0 | `✓ Read the database's own dictionary: <n> columns described, <j> joins added` | `enrich-metadata`'s `columns_enriched` and `relationships_added` (left out when it printed `enriched: false` with no `fetched`) |
| 2a | `✓ Described <C>% of columns` | `sm coverage`'s `coverage_pct` |
| 2b | `✓ Added <E> entities` | `sm add --kind entity`'s `applied` |
| 2c | `✓ Proposed <M> metrics (<f> flagged, left out)` | `sm suggest-metrics`'s proposals, then `--write`'s `applied` |
| 2d | `✓ Set units on <U> money columns` | `sm set-units`'s plan as applied |

Add **one finding** to a line when the step surfaced one, in the person's terms and quoted from what you read, e.g. *"`status` stores 1–6; decoded as pending, paid, shipped, …"* or *"`amount` is in cents."* One per step at most.

> **HARD RULE — decisions to you, plumbing to the engine. NEVER write a generator script** (`build_ops.py`, `build_desc.py`, `enrich_*.py`, a one-off that loops to build curate ops). It's the recurring failure mode and it's banned. When you have many edits, emit them as data and let a tested command apply them: `sm describe-file` (TSV of descriptions; a customer's CSV/JSON dictionary with `--source metadata`), `sm enrich-metadata` (in-DB metadata), `sm set-units`, `sm suggest-metrics`, or `sm curate --ops-file <json>`. You decide *what* (currency, which metrics, a column's meaning); the engine does the *applying*.

### 2a.0 — Deterministic metadata FIRST (run before any hand-enrichment)

**Many databases describe themselves.** Metadata-driven platforms ship their own data dictionary + value-label tables (ServiceNow `sys_dictionary` + `sys_choice`, SAP `DD03L`, Salesforce metadata…), and plenty of ordinary schemas have code→label lookup/dimension tables. When that metadata is present it is **authoritative** — strictly better than LLM guessing or fetching JS-walled vendor docs. Always try it first:

```bash
AGAMI_SQL_MAX_ROWS=200000 bash "${CLAUDE_PLUGIN_ROOT}/scripts/sm" enrich-metadata "$ROOT" \
  --profile <profile> --db-type <db_type>
```

It auto-detects a preset (e.g. `servicenow`) or takes `--preset`, reads the dictionary/lookup tables, and applies — in validated curate batches — **column descriptions** (stamped `description_source: metadata`, authoritative), **`choice_field` labels**, and **reference/FK relationships** (the `caller_id → sys_user` edges that `<x>_id` name-matching can't find). On ServiceNow this alone closes most of the column-coverage, enum-decode, and join-graph gaps deterministically. It prints what it enriched. **Then** hand-enrich only what it didn't cover (2a–2c below). `--skip-references` if you want descriptions/choices only.

**The raised cap is not optional here — [why](#raised-cap).** A real `sys_dictionary` is tens of thousands of rows, so without the prefix this command doesn't degrade, it **dies**: the read is refused and the runner re-raises the refusal as `RuntimeError: {"refusal": … "rule": "resource_limit" …}`. Nothing is half-applied when that happens (the fetch is above the first `curate` write), so re-running with the prefix is clean. Two things when you run it: **never pipe it through `tail`** — that masks the exit code and a hard failure reads as success; and if the command still refuses even at 200,000, read the `fetched` counts, raise the prefix to clear the largest source table, and say so.

**Then tell the user, once, using the numbers it printed** (`fetched` is a per-role row count, on both the success and the nothing-applicable paths):

> *Read `<dictionary_rows>` dictionary rows and `<choice_rows>` choice rows. Catalog and dictionary reads run with a temporarily raised row cap (200,000) — a data dictionary is far larger than the 1,000-row default. **Your query-time cap is unchanged:** a normal question that would return more than 1,000 rows is still refused rather than quietly truncated.*

The second sentence is the load-bearing one. Without it a user reasonably concludes their safety bound was weakened.

**<a id="customer-dictionary-file"></a>A customer dictionary file ([1.5](#15--existing-data-model--semantic-layer-mandatory--always-ask)).** When the user gave a column dictionary as a CSV or JSON, apply it in one command, before you write any descriptions of your own:
```bash
bash "${CLAUDE_PLUGIN_ROOT}/scripts/sm" describe-file "$ROOT" --source metadata --file "$DICTIONARY_FILE"
```
The format comes from the extension (`--format csv|json|tsv` overrides it). CSV needs a `table`, `column`, `description` header (`area` / `schema` optional); JSON is a list of objects with those keys, or an object mapping `"table.column"` to its description. Matched columns are stamped `description_source: metadata`: the text is taken as written from the customer's dictionary and is not checked against the data (see #564), so don't present it as verified. A different case (`ORDERS.Total`) lands on the model's spelling and is listed under `case_corrected`; a `SCHEMA.TABLE` in the table column whose schema is in the model is listed under `schema_prefix_stripped`. A description a person wrote is never replaced: those come back under `kept_human`. Rows that matched nothing come back under `unmatched`, each with a `reason` (`no_table`, `no_column`, `ambiguous_table`, `ambiguous_column`, `malformed`, `empty_description`); a CSV with broken quoting is refused as a whole with an `error` (exit 2). **Tell the user how many columns were described and list every unmatched row with its reason** (an `ambiguous_table` row needs an `area` or `schema` column to say which one). Never drop them, and never fix them by guessing: a dictionary entry for a table that isn't in the model is the user's call. Then describe only what the dictionary didn't cover (2a below), with `source: ai`. Your own descriptions never replace the dictionary's.

### 2a — Descriptions (describe coded columns; leave only self-evident ones empty)

For each table fetch up to 5 sample rows for evidence (`SELECT * FROM <t> LIMIT 5`; Snowflake `SAMPLE` for >10M rows). **Samples are never written to disk** — context only, then discarded. Also capture MIN/MAX of each time column → record under the table's `performance_hints` so Phase 5 anchors "last 30 days" to the data's real MAX, not `NOW()`.

Build a per-schema prompt with `$DATA_MODEL_DOC_TEXT` first (dominant prior), then `datasource.md`, then tables/columns/sample rows. **Always** emit a 1-line table `description`. For **columns**, classify each into one of three — don't default everything to empty:

| Kind | Examples | What to write |
|---|---|---|
| **Self-evident** | `id`, `created_at`, `email`, `name`, `gender`, `city` | `""` — the name + type already says it; a description would just restate it. |
| **Informative** | `revenue_usd`, `status`, `margin_pct`, `utilization_pct` | one line **iff** samples/doc support a fact the name doesn't (enum values, unit, derivation, a caveat). Else `""`. |
| **Coded / opaque-but-systematic** | `EL_REVENUE_30D`, `WEST_ORDERS_12M`, `<TIER_A_6M>`, `XX_<metric>` families | **always describe** — unreadable without the legend (see below). |

**Coded-schema detection + legend expansion — the case that used to get wrongly left empty.** Feature stores, wide denormalized marts, and coded analytic extracts encode meaning in column *names* via a small recurring token vocabulary: a category prefix (e.g. `EL/HM/AP…`), a window suffix (`_30D`, `_6M`, `_12M`), a bucket (`_0_30`, `_TIER_A`), a threshold (`_LT_1K`, `_LT_10K`), a metric stem (`_REVENUE`, `_QTY`, `_RECENCY`). When the same tokens recur across many columns:

1. **Decode the token legend once** — grounded in column samples + `$DATA_MODEL_DOC_TEXT` + `datasource.md`. For any token whose meaning isn't evident, **ask the user in one batched question** rather than guessing (this is the same decode 2b needs; do it once and share it).
2. **Expand the legend into one description per coded column — deterministically, NOT as N separate LLM guesses.** Write a small in-skill decoder (a token→phrase map + a per-table parse of `(prefix, metric, window, threshold)`) and compose each description from it, then emit them through the same `sm curate` batch. This is exact, internally consistent, and costs zero per-column LLM tokens. e.g. `EL_REVENUE_30D` → "Total revenue from the Electronics category over the last 30 days"; `WEST_ORDERS_12M` → "Number of orders in the West region over the last 12 months"; `EL_ORDERS_LT_1K_90D` → "Number of Electronics orders under 1,000 placed in the last 90 days." (Use the user's actual token vocabulary + domain, not these placeholders.)
3. The same decoded legend also lands in the **structured glossary** `key_terminology` (term → definition) — abbreviations and codes a reader needs (an acronym → its expansion; a status/type code → its meaning). **Decode once, write both** — the per-column descriptions (what shows in the explorer + feeds the SQL generator's column context) AND the glossary. Write it with the packaged command (a JSON object `{term: definition, …}`); it merges over any existing terms, validates, and commits:
   ```bash
   bash "${CLAUDE_PLUGIN_ROOT}/scripts/sm" set-terminology "$ROOT" --file "<artifacts_dir>/local/tmp/agami-terminology.json"
   ```
   The glossary is rendered into `datasource.md`'s `## Key terminology` by `org-draft` (which **also** auto-seeds enum legends from `choice_field` columns) — so it survives a regeneration and is never a bare placeholder. **Do NOT skip this on a code-dense schema** (abbreviations, master-coded values): an empty Key terminology on a DB full of codes is a real miss.

Don't skip a coded column because "the legend covers it" — the legend lives in a *different file*; the per-column description is what the explorer shows and what NL→SQL reads. A wide coded table (hundreds of columns) should finish at ~100% column coverage, not ~3%.

**Value-enum decode — FILL `choice_field` for coded-VALUE columns (the highest-leverage structured win).** Distinct from the coded-*name* legend above: this is a column whose stored VALUES are codes — a `severity` of 1/2/3, a `state` of 1/2/3/6/7, a `priority` of 1–5, a short status/type code. **Introspection now seeds a `choice_field` skeleton** — the column's complete set of values with blank labels (e.g. `{"1":"","2":"","3":""}`, not a single placeholder), so the keys already exist and you only fill the labels. The keys come from the catalog when it declares them (a Postgres enum, a MySQL `ENUM`, a `CHECK (col IN (...))`), on a table of any size; otherwise, on a small table only, from a `SELECT DISTINCT` over the whole column, kept only when it has at most 25 values. A column with no skeleton has more values than that, is on a large table, or is a key, measure or year — don't invent a list for it from a sample. Keys are the values exactly as stored: never trim or re-case them (`'Open '` and `'Open'` are different values; a caveat on the column names such pairs). **Your job is to fill the labels** — from samples + `$DATA_MODEL_DOC_TEXT`, or a lookup/choice table when the DB has one (e.g. ServiceNow `sys_choice`: `SELECT value, label FROM sys_choice WHERE name='<table>' AND element='<column>'` — read it once and apply, don't hand-guess). Write it as a **structured `choice_field` edit op**, not just prose:
```json
{"op":"edit","kind":"table","area":"<area>","name":"<table>","column":"severity","field":"choice_field","value":{"1":"High","2":"Medium","3":"Low"}}
```
Why structured beats prose: the SQL generator translates "high severity" → `severity = 1` and **labels result rows** from the map deterministically — a decode buried in the description can't be applied programmatically. **Fill the skeleton on every coded-value column** in the same `sm curate` batch as the descriptions; leave a label blank only when no sample/doc/lookup reveals it. (The filled map also auto-seeds the glossary via `org-draft`.) Self-labeling value sets (a `region` of North/South/East/West) can keep `choice_field` as the values themselves or be cleared — they need no translation.

**Write descriptions with `sm curate` edit ops — never hand-edit the table YAML, and never write a throwaway generator script to build the ops.** Build one ops array and run it once; it validates the whole model + commits + reverts on failure. **The op does NOT need `area`** — curate resolves which area owns a table/column by name, so you do **not** maintain a `table → area` map (that map is exactly what used to push the LLM into writing an `enrich_gen.py`; it's gone). Shapes:
- table: `{op:edit, kind:table, name:"<table>", field:"description", value:"<text>", source:"ai"}`
- column: same + `column:"<col>"`

Pass `area` **only** to disambiguate a bare name that exists in two schemas (curate skips an ambiguous op with a clear "pass an explicit area" reason rather than editing the wrong one). **Always include `"source":"ai"` on every generated description** — this stamps `description_source: ai_unvalidated` so the description earns trust through use (agami-query surfaces it in the answer receipt for confirmation the first time a query actually uses that column, instead of forcing an upfront review of hundreds of descriptions). **Skip any column that already has a description** (so a partial hand-edit or a reintrospect merge is never clobbered):
```bash
bash "${CLAUDE_PLUGIN_ROOT}/scripts/sm" curate "$ROOT" --ops-file "<artifacts_dir>/local/tmp/agami-descriptions.json"
```

| Column | Bad (reject) | Good (keep) | Empty (preferred) |
|---|---|---|---|
| `id`, `created_at`, `email`, `name` | "Primary key" / "When created" / "Email" | (always empty — self-evident) | `""` |
| `customer_id` | "The customer ID" | "FK to customers.id; 1:N with orders" | `""` if nothing to add |
| `status` | "A status code" | "lifecycle: pending → shipped → cancelled" (only if enum known) | `""` |
| `EL_AOV_90D` (coded) | "AOV value" | "Average order value for the Electronics category over the last 90 days" (from the decoded legend) | — **don't leave empty** |
| `v_1`, `tmp_col`, `x` | "A value" | (leave empty — opaque, no decodable structure) | `""` |

**What NOT to invent:** meanings for truly opaque single columns (`v_1`, `tmp_col`, `xyz`) that have no decodable token structure and no sample signal. Business semantics not present in the samples/doc. Name translations on self-evident columns (`amt`→"amount"). Write the decodable legend; don't fabricate the rest.

**For an opaque column you genuinely can't read, say so — don't leave it silently blank.** There are two kinds of empty description, and they must be distinguished:
- **Self-evident** (`id`, `created_at`, `email`, a clear `name`) → leave the description empty AND leave `description_source` unset (`null`). The name already says it; nothing to flag.
- **Opaque / unknown** (`xyz`, `v_1`, `tmp_col`, a code whose meaning no sample or doc reveals) → leave the description empty BUT set **`description_source: "ai_unknown"`** via a curate edit op (`{op:edit, kind:table, area, name:<table>, column:<col>, field:"description_source", value:"ai_unknown"}`). This records "agami looked and couldn't tell" — the human knows what `xyz` is, and the explorer + answer receipts surface these so they can fill it in. **Don't guess a meaning to avoid the flag; the flag is the honest answer.** (Do NOT mark a self-evident column `ai_unknown` — that's noise.)

For large schemas (>100 tables) batch 50 at a time; narrate `[batch 2/4] …`. Validate after each schema; on failure, surface errors and continue with the rest, then report which need attention.

**Regroup wide tables into SEMANTIC column groups (do this right after describing their columns).** A deep table (≥30 cols) gets `column_groups` from the engine, but those are a deterministic **name-prefix** split — buckets like `is`, `last`, `latest`, `created`, `bp` — not concepts. They exist only so the explorer can collapse a wide table; they're a fallback, not the goal. Having just described every column, **you** are positioned to cluster them by *meaning* into a handful of named groups that read like mini subject-areas — e.g. `identity`, `location`, `lifecycle_timestamps`, `telemetry`, `swap_details`, `alerts`, `flags`. Keep it to ~5–10 groups; **every column must land in exactly one** (the validator rejects orphans on a deep table). **Describe every group too:** the engine's descriptions ("Columns named exp* (grouped by prefix)") don't fit regrouped columns, and an agent reading the schema picks which group to fetch by its description. Say what the group holds and which questions need it, in one line, e.g. *"travel: destination, trip dates and foreign-activity fields; use for questions about where or when someone travelled."* Apply both as two curate edit ops per wide table:
```json
[{"op":"edit","kind":"table","name":"<wide_table>","field":"column_groups",
  "value":{"identity":["..."],"location":["..."],"lifecycle_timestamps":["..."],"telemetry":["..."]}},
 {"op":"edit","kind":"table","name":"<wide_table>","field":"column_group_descriptions",
  "value":{"identity":"...","location":"...","lifecycle_timestamps":"...","telemetry":"..."}}]
```
Send these in the same `sm curate` batch as the descriptions (or a follow-up batch). Only worth doing on genuinely wide tables — narrow tables have no `column_groups` and need none. **A wide table whose group descriptions are all the engine's own wording hasn't been regrouped yet**: "Columns named … (grouped by prefix)", or one of its fixed lines for `identity`, `references`, `codes`, `flags`, `audit`, `measures`, `dates` and `misc` ("Primary keys and unique identifiers for this table.", "Numeric columns that can be aggregated (summed or averaged).", and so on). Check for that before Phase 7 and do the rest.

**Column-pass completeness gate (MANDATORY — do not skip the column pass).** After enriching, run:
```bash
bash "${CLAUDE_PLUGIN_ROOT}/scripts/sm" coverage "$ROOT"
```
`ok: false` with `unenriched_tables` lists tables that got a **table** description but **no column descriptions at all** — i.e. you enriched the table layer and skipped the columns. That is not done: go back and run the column pass on those tables (describe each meaningful column from sampled values; mark genuinely-opaque ones `ai_unknown`; self-evident `id`/timestamps may stay blank). A wide coded table must finish near 100% described, not 0%. This is also enforced downstream — `seed-examples` **refuses** (`refused: "columns_unenriched"`) while any table is unenriched, so you cannot reach the trust dashboard on naked columns. Don't proceed to 2b until `coverage` returns `ok: true`.

### 2b — Entities (the semantic vocabulary)

Propose `entities[]` per subject area — the names users actually say. For each, fill `name`, `plural`, `other_names` (synonyms), `maps_to` (table+column, one `primary: true`), and — for opaque-identifier columns — a `value_pattern` regex (e.g. a VIN `^[A-Z0-9]{17}$`, a `BP`-prefixed serial) so the runtime can recognize literals. Ground these in column names + samples + the domain doc; don't invent entities the schema doesn't support. Because these are LLM-proposed, write them **`confidence: inferred, review_state: unreviewed`** so they surface in the Phase 4 pre-seed review (seeds reference entity vocabulary).

**Don't stop at id columns — wide / denormalized tables hide dimensions a `maps_to`-on-an-id scan misses.** When a schema is denormalized to one grain (e.g. one row per customer, every table keyed on the same id), an id scan finds exactly one entity and quits — yet real business dimensions are still there, encoded two other ways. Look for both, **discovered from the actual columns + samples, never hardcoded**:
- **Coded column-name prefixes / suffixes.** Many columns sharing a recurring token (`XX_<metric>` repeated for several `XX`, or `<metric>_<period>`) means that token is a dimension (a product line, a region, a time bucket). **Decode it from evidence** — column descriptions, sample values, the domain doc; if a code's meaning isn't evident, **ask in one batched question rather than guessing**. A prefix dimension has no id column, so it isn't a `maps_to` entity — record the decoded legend in `datasource.md` `## Key terminology` and fold the expansion into each affected column's description, so NL→SQL can map a phrase to the right prefixed columns.
- **Value-level entities.** A set of sibling string columns whose *values* are real-world instances (institution/lender names, branch names, merchant names, statuses) is an entity even with no id column — create it (`maps_to` the most representative such column; capture distinct sampled values as `other_names`/a `value_pattern` cue) so a user naming a literal value resolves to those columns.

If, after this, a single-grain schema genuinely has one entity, that's the right answer — say so. The point is to *check* the two hidden shapes before concluding "one entity," not to manufacture entities.

**Write them with the packaged command, not by hand** — build a JSON array and run it once (it validates each item, writes `subject_areas/<area>/entities/<slug>.yaml`, validates the whole model, reverts on failure, commits). Never author a throwaway script to loop. The canonical entity YAML shape is [`shared/metric-entity-shape.md`](../../shared/metric-entity-shape.md) — **never read another profile's artifacts to copy it** (see the boundary note under 2c):
```bash
bash "${CLAUDE_PLUGIN_ROOT}/scripts/sm" add "$ROOT" --kind entity --area <area> --file "<artifacts_dir>/local/tmp/agami-entities.json"
```

### 2c — Metrics

Metrics come from two sources, handled very differently. **Always prefer declared metrics** — schema-only inference is a shallow guess (it finds start→end durations, and flag rates when asked; it misses the domain KPIs a business actually tracks — refund rate, repeat-purchase rate, cohort retention, fulfillment SLA — because those aren't visible in column names).

**(A) Declared metrics — extract in FULL, no cap.** If the user attached a semantic-layer repo or a metrics file in 1.5 (`$DATA_MODEL_DOC_TEXT`), those are the org's *real* definitions — pull **every** one, don't sample to 4:
- **LookML** `measure {}` → metric: `type` + `sql` → `bindings`, `description` → `description` + `calculation`, `label` → `description` when there is no description (below), `label`+`view_label` → `other_names`.
- **dbt** `metrics:` / `semantic_models[].measures` (MetricFlow) → name, `agg`+`expr` → `bindings`, `description` → `description` + `calculation`, `label` → `description` when there is no description (below), `label` → `other_names`.
- **Cube** `measures` → `sql`+`type` → `bindings`.
- **Metrics file** (CSV/YAML/markdown KPI dictionary the user uploaded) → one metric per row/entry: name, definition → `description` + `calculation` (below), formula → `bindings`.

**`description` and `calculation` are two fields with two jobs — write both, never one copied into the other.** `description` is a **one-line label** (≈60 characters): *is this the metric I want?* It is all the agent sees of a metric before it opens the metric's table — `metric_index` lists it on every schema call and the metric search matches it — so a metric **without** one is left out of the index entirely (the validator warns `metric_undescribed`). `calculation` is the **full definition**: what is counted, the filters, the traps, and when to prefer it over a sibling. The agent writes the SQL from it once the table is open. So from a declared source, fill both every time, falling back in this order:
- `description` ← the source description's first sentence; else its `label`, if the label says more than the metric's name; else one line you write from the definition.
- `calculation` ← the source description in full; else a sentence you write from its `type`/`agg` and `sql`/`expr` (what it aggregates, over which rows, with which filter). Never leave it as the label: it is required, and a label is not a definition. A `description` that only restates the name (`"Incident count"` for `incident_count`) counts as none.

Translate the declared SQL/agg to the profile's dialect for `bindings`, set `source_tables`, write **`confidence: inferred, review_state: unreviewed`** (declared = strong signal, still wants a human sign-off on the `/agami-model` Review tab). If there are many (> ~8), **don't** funnel them through a 4-item picker — write them all and tell the user once: *"Added N metrics from your `<LookML/dbt/file>` — review or trim them in /agami-model."* (Offer a single "add all N / let me pick a subset" confirm if you want, but never silently drop declared metrics to fit a cap.)

**(B) Inferred metrics — infer a sensible set for BULK review, don't ask for 4.** Run **`sm suggest-metrics "$ROOT" --profile "$PROFILE_NAME"`**: it infers per-table measures — start→end durations, never a plain aggregate (see below), and no flag rate unless asked (below) — and **prints them without writing anything** (a dry run). `--profile` test-runs every binding against the database first (one bounded read per table — on a pay-per-scan warehouse such as BigQuery or Snowflake that read is billed, so say so before running it there; without `--profile` the output notes the bindings were not test-run). Each proposal shows its `binding`, `calculation`, `source_table`, any `error` (a binding that failed — never written) and `flags`: `degenerate` (constant, all zero or NULL), `low_confidence` (a table under 50 rows), `not_test_run` (a table defined by SQL whose read failed, so its bindings are unproven), `ambiguous_pairing` or `audit_column` (a duration whose start/end pairing needs a person's eye). **Show the user the proposals, flagged ones with their reason, then re-run with `--write`** — it writes the unflagged ones; add `--names <metric> …` to keep only some, or to keep a flagged one the user chose. Every written metric is `proposed`, because each picks a condition or pairs two columns. An unsigned metric is still used — the answer carries a warning that it is unreviewed (Rule 1) — so drop a proposal nobody would ask for rather than leave it for the queue; the user reviews the rest **on the Phase 4 cards**. Reserve **AskUserQuestion** only for a bespoke KPI the user describes in words that the column shapes don't imply.

**Flag rates — only the ones the business defines.** By default `suggest-metrics` proposes **no** share-of-true rate per boolean column (`<table>_<col>_rate`): a boolean column is a dimension and a filter, and the agent can compute its share from the column when a question needs it. The output carries one summary instead, `flag_rates: {available, columns, hint}` — the boolean columns that could each have one. **When the customer's definitions (a data dictionary, spec, KB, metrics file from 1.5) name a share-of-true rate as a measure**, request exactly that one and cite the document: `sm suggest-metrics "$ROOT" --profile "$PROFILE_NAME" --flag-rates <table.column>[,<table.column>…] --cite "<document>"`, show it, then re-run with `--write` (its description becomes `Defined in: <document>.`; when you humanize it below, keep that sentence at the end of the description). A name that matches no boolean column comes back in `errors` — fix the name, don't drop it silently. A table name in more than one schema or subject area is refused before anything is written, with the matching columns listed: name it `schema.table.column` (the column is after the last dot), or pass `--area`. **Otherwise don't request any:** mention the summary line to the user once (*"N boolean columns could each have a share-of-true rate — say if any is a KPI you track"*) and add only the ones they name. `--flag-rates all` restores a rate for every flag column; don't use it in onboarding. Rate metrics already in a model are left as they are; to clear out old ones, the explorer's Review tab (`/agami-model`) has **Reject all** for proposed metrics.

**Don't hand-add the plain ones it deliberately skips.** A `{table}_count` = `COUNT(*)` or a `{table}_total_{col}` = `SUM(col)` is **not** a metric: the column's `aggregation` class is enforced on every statement with or without a named metric, and everything such a metric could carry already reaches the agent on the table it opens — the column's `unit` (which the formatter carries through `SUM`/`AVG` by itself), the table's and column's `caveats`, and the table's `grain`. So the entry adds a name and nothing else; on a wide model that pattern was 137 of 175 metrics, each listed by its own name, burying the real ones. **A table with no metric of its own is the expected outcome**, not a gap to fill: mark the columns' `aggregation` and `unit` correctly and put caveats on the column or table, and spend metric effort on the ones with real definitions (ratios, multi-table measures, anything with a filter or a business rule in it).

**Run order matters — describe columns FIRST, then suggest metrics.** `suggest-metrics` **skips columns agami couldn't read** (`description_source: ai_unknown`) — it won't propose a metric on a column it can't explain (the prose would just restate opaque SQL like *"Fraction of alm_asset where active_to is true"*). Its output reports `skipped_opaque: N`. So: settle 2b/2c column descriptions first, run suggest-metrics, and **after the user describes any still-opaque columns** (the explorer's "couldn't read" pile, or a later pass), **re-run `sm suggest-metrics "$ROOT" --profile "$PROFILE_NAME"`, show the new proposals, then `--write`** — it's incremental (skips names already written), so the newly-described columns pick up their metrics with no duplicates. This is the re-pass: *describe → suggest → describe more → suggest again.*

**Then HUMANIZE the inferred prose (B-metrics only).** `suggest-metrics` writes a `calculation` that is a *mechanical restatement of the SQL* (`"Fraction of alm_asset where active_to is true"`) and **no `description`** (a cited flag rate has only its `Defined in: <document>.` line) — what a metric is *for* is judgement it cannot derive. Once the columns are described, write **both**, grounded in the column/table descriptions + the glossary (NOT product memory):
- `description` — the one-line label: `"Share of assets still in their active lifecycle window"`.
- `calculation` — the full definition, carrying anything the agent must know to write it: `"Fraction of assets whose active_to flag is true — the asset is inside its contracted lifecycle window. Retired assets keep their rows with the flag false, so the rate falls as the estate ages."`

Apply as **one `sm curate` batch** of `{op:edit, kind:metric, area, name, field:"description", value:"<label>"}` and `{…, field:"calculation", value:"<definition>"}` ops (decisions to you, plumbing to the engine — never a generator script). Leave declared (A) metrics alone — their prose already came from the source. **Drop a proposal rather than describe it** when you can't say what it is for: a flag rate on a column nobody would ask about is noise in the review queue, not a metric.

**In the SAME batch, fill `other_names` (aliases) where the org has well-known ones** — a metric with empty `other_names` only matches its literal name, so a user asking for "MTTR" / "AHT" / "P1 count" misses a metric named `incident_avg_duration_days`. Add the aliases the org actually uses: from the metrics file / glossary if provided, or the standard abbreviation for that metric *type* (a mean-time-to-resolve duration → `MTTR`; average handle time → `AHT`). Only add genuine aliases — don't pad with restatements of the name. `{op:edit, kind:metric, area, name, field:"other_names", value:["MTTR", …]}`.

For every metric (A or B) fill `name`, a one-line `description` (the index label — without it the metric is left out of `metric_index`), prose `calculation` (the full definition — **required**), per-dialect `bindings` (the SQL), `source_tables`, `other_names`. The canonical YAML shape is [`shared/metric-entity-shape.md`](../../shared/metric-entity-shape.md) (synthetic example). **Write them with the packaged command** — build a JSON array and run it once; never hand-write each YAML and never author a throwaway loop script. It validates each item, writes `subject_areas/<area>/metrics/<slug>.yaml`, validates the whole model, and reverts the batch on failure:
```bash
bash "${CLAUDE_PLUGIN_ROOT}/scripts/sm" add "$ROOT" --kind metric --area <area> --file "<artifacts_dir>/local/tmp/agami-metrics.json"
```
> **Never read another profile to learn the shape.** Do not glob or read other profiles' artifacts (e.g. `find <artifacts_dir> -path '*/metrics/*.yaml'`, or reading `<artifacts_dir>/<other-profile>/…`) to "copy the binding shape." That crosses the profile boundary — in a hosted/multi-tenant deployment it's a **tenant-data leak** (onboarding one customer must never read another's model), and even locally it risks lifting another profile's filters / calculation text into this one. You don't need to: the packaged `sm add` command validates every item against the schema. Get the shape from `shared/metric-entity-shape.md` and the profile's **own** schema — never a sibling profile.
Don't propose metrics depending on choice-field literals you didn't detect, or cross-area metrics unless a cross-area edge wires the join. A metric whose `source_tables` sit in two subject areas goes in the datasource's cross-area list — `--area _cross`, which writes `cross_subject_area_metrics.yaml`:
```bash
bash "${CLAUDE_PLUGIN_ROOT}/scripts/sm" add "$ROOT" --kind metric --area _cross --file "<artifacts_dir>/local/tmp/agami-cross-metrics.json"
```
`--area` must be `_cross` or a subject area `datasource.yaml` lists; any other name is refused with the list of areas (a file written under an unlisted area would never be loaded).

**A missing value is not a zero.** Write bindings so NULL propagates: a row whose input is missing gets no result, not an invented one. Don't wrap a column in `COALESCE(col, 0)` / `IFNULL` / `NVL` / `ISNULL` to make a formula return a number on more rows — when the column's description says NULL means "not recorded" or "not assessed", the zero is made up, it pulls averages and rates down, and every session copies the binding. Fill with a constant only when the column's metadata says a missing value means zero ("NULL means none"), or when the definition you were given writes the fill into its formula — then keep it, and say in the metric's `calculation` where it comes from ("the KB formula counts a missing percentage as 0"). A total over no rows (`COALESCE(SUM(x), 0)`) is fine. Tell two cases apart: **a recorded value that is missing** (a staff count nobody entered) stays empty; **no related rows at all** (a hub with no supply records serves no disasters) is often a real zero — decide it per metric from what the formula means, and say which you chose. The validator warns on a constant fill when neither the column nor the metric's calculation gives that reason (`metric_null_filled`). To average the values that exist, write `AVG(col)` for one column (it leaves NULLs out); for several columns write the sum of the present values over the count of the present values, `SUM(COALESCE(a, 0) + COALESCE(b, 0)) * 1.0 / NULLIF(COUNT(a) + COUNT(b), 0)` (the `* 1.0` stops integer division on PostgreSQL, SQLite and SQL Server), with no `VALUES` list or per-row subquery — the validator accepts that fill, since the zero is never counted. Never put an aggregate inside another (`AVG(SUM(x))`, or a `SUM` in a percentile's `WITHIN GROUP`): `sm add` refuses it; use a second-order metric (`shared/metric-entity-shape.md`). Every model's derived context already tells the query path to keep rows whose value cannot be calculated, shown empty and last, and to leave them out of averages and rates — so a metric doesn't need its own fill to stay usable.

**"Today" means `CURRENT_DATE`, never the data's last date.** A metric defined against today or now (days since start, age, days until due, overdue) binds `CURRENT_DATE` — or the engine's equivalent (`NOW()`, `GETDATE()`, `SYSDATE`) — and its `calculation` says the result depends on the run date. Don't measure to a fixed date or to `MAX(<date column>)` to make the figure reproducible: when the data isn't loaded up to today (a stale extract, a test or demo set) that silently changes the answer and which rows qualify, and it overrides the definition. Don't write how far the data lags today into a caveat either: that changes with every load, and the answer's receipt is where freshness shows. The validator warns when a metric's text says today but its binding doesn't use the current date (`metric_today_not_current_date`).

**A share of a total is a ratio of totals.** When a metric divides two amounts that add up (sales, watts, hours, counts), bind the group's value as `SUM(numerator) / NULLIF(SUM(denominator), 0)` over the rows where both are known — not `AVG(numerator / denominator)`, which weighs a small row the same as a large one and disagrees with the totals shown beside it. Do this even when the definition you were given writes the formula for one row ("(solar + wind) / total output, per reading"): a binding with no aggregate leaves the query to average the rows, which is the same mistake one step later, so bind the share as the ratio of totals and say in the `calculation` that a group's value is the ratio of its totals. Average the per-row ratios only when the metric really means that — an average per-student score, or a definition that scores each plant or fund and then averages them — and say so in its `calculation` ("the average of each row's ratio"). Mark the amount columns `aggregation: additive` while you are there: the validator warns on `AVG` of a ratio of two `additive` columns (`metric_avg_of_ratio`), and cannot see columns whose class was never set.

### 2d — Caveats, value_transforms, currency

From samples + the domain doc, add provider-portable cleaning where evidence supports it:
- **Caveats** (`caveats[]` on table/column/entity): data-quality notes, anti-patterns (e.g. "filter on the event date, not the load date"), dedup warnings.
- **State the lasting fact, never its current count.** A caveat, metric `calculation` or metric `description` says "NULL means the value was not recorded", not "427 of 670 accounts have a value", "covers only 40% of rows", "values range 49–142 here" or "twelve of forty sites report it". A count measured today is wrong after the next load, yet it reads as part of the definition and gets repeated in answers. If a figure helps the reviewer decide, put it in the question you ask them, not in the model. The validator warns on such figures (`measured_figure`); constants that define something, such as thresholds, weights, unit factors and scales, are fine, and so is how the formula behaves ("returns 0 when no row meets the threshold").
- **Bind a value as its label, unit and description say — never overrule them because the data looks implausible.** A field stored as `'0.73%'` means 0.73 percent (0.0073 as a ratio) even when every value is below 1; a column documented as "average annual return (decimal)" is that column even when its values look ten times too large. Don't decide the label is wrong and build the model on your own reading — no "the % is wrong, this is a 0–1 ratio" rule, no switching to a neighbouring column with a similar name, no rescaling. The data may well be wrong or oddly scaled, but that is the data owner's call: write what you saw as a caveat ("every value is below 1 although the field is labelled %; confirm the unit") and ask the user in plain words which reading is right. Change the binding only on their answer.
- **value_transform** on columns whose raw value needs cleaning (`regexp_replace(...)` for bracketed text, `TO_TIMESTAMP(...)` for epoch). Must parse as SQL in the datasource's dialect (validator checks), so a Postgres `CASE WHEN code ~ '^[0-9]+$' THEN code::int END` is fine on a Postgres model. **A field inside a JSON column:** there is no separate JSON-path field — put the transform on the JSON column and have it extract the key itself, e.g. Postgres `(profile->>'signup_date')::date`, SQLite `date(json_extract(profile, '$.signup_date'))`, DuckDB `CAST(json_extract_string(profile, '$.signup_date') AS DATE)`, and say in the column's description which key it reads.
- **default_filters** (`default_filters[]` on a table): soft-delete / tenancy filters that state what the org means by the table (use the `{alias}` placeholder, e.g. `{alias}.deleted_at IS NULL`). **Declarative only** — they are not AND-ed into a query for you; the query path reads them and writes in the ones a question needs. Declaring one is still worth it: the trust receipt reports, per table reference, which of them the statement that produced an answer applied and which it omitted, so an author sees whether their definition was honored.
- **Large-table filters (only when the introspection report lists `large table` lines):** each line names a table of about a million rows or more and its date columns; the build seeded `recommended_filters` with all of them (when there are six or fewer). On a warehouse many are load or audit stamps (the row's own created/updated/valid-from/valid-to) that no business question filters on. For each table **with** date columns, ask in plain words: *"`orders` has about 5 million rows. When someone asks about a period, which date do they mean?"* — a multi-select of **at most three** of its date columns (the likeliest business dates; leave out the load and audit stamps) plus `None of these`, because AskUserQuestion takes four options; the built-in `Other` lets them type any other column. Keep only what they pick; `None of these` clears the list (the right answer when the table is narrowed through a joined date or period table rather than a column of its own). A table with no date columns: skip it. Write each answer with the `recommended_filters` edit op below, which leaves the row count alone. Up to four tables per AskUserQuestion call; more → another call.
- **Currency (one ask per profile):** **find the money columns with `sm suggest-units "$ROOT"` — don't hand-roll a name regex** (a bare `count` pattern matches inside `discount` and silently drops `discount_amount`; the command's matcher splits names into whole words and is tested). It returns `{money_columns: [{area, table, column, type, suggested_unit}]}`: numeric columns named like money (amount/price/revenue/discount/… or a currency word such as `_usd`), minus rate/count/id/score and non-money unit words (minutes, hours, weight, points, units), skipping any that already have a `unit`. `suggested_unit` is what the name spells out (`revenue_usd_millions` → `USD_millions`, `cost_usd_per_day` → `USD/day`), or null. Ask once about the columns with a null suggestion: "What currency are these in?" (`USD`/`EUR`/`GBP`/`JPY`/`INR`/`Other`/`Mixed`). Then apply with **one tested command — never pipe `suggest-units` through a hand-rolled script** (that glue breaks, e.g. on empty stdin). Without `--yes` it only prints the plan:

```bash
bash "${CLAUDE_PLUGIN_ROOT}/scripts/sm" set-units "$ROOT" --currency INR        # prints {planned: [{table, column, unit}]}, writes nothing
```

**Show that plan to the user** (column → unit, one line each) and apply only what they agree to: all of it with `--yes`, or a subset with the same `--currency`/`--unit` plus `--columns <table.col …>` (which writes exactly those columns, each with the unit the plan showed for it: a name that states its currency keeps that unit, the rest get `--currency`/`--unit`). A column whose name states its currency gets that unit in the plan; the rest get `--currency`. The write is one validated curate batch. The runtime + chart renderer format the symbol + grouping **deterministically** from `unit` (`semantic_model/units.py`) — no prose caveat to re-interpret. `Mixed` → skip (leave `unit` unset), one-liner. Non-currency units (`cents`/`percent`/`days`) → `--unit <name>`.

  **Unit grammar:** `<ISO code>[_<scale>][/<period>]`. Scale (values stored in thousands/millions/…): `thousands`, `millions`, `billions`, `lakhs`, `crores` — `USD_millions` renders 12.5 as `$12.5M`. Period (a rate): `hour`, `day`, `week`, `month`, `quarter`, `year` — `USD/day` renders 120 as `$120.00/day`; both combine (`USD_millions/year`). Use a scale only when the column or the user says the values are stored that way; never rescale data to fit one. Anything else (`kg`, `days`) is shown as a plain label.
- **Date encodings (auto-sniffed — no question):** introspection already sets `date_format` (`epoch_s`/`ms`/`us`/`ns`, `yyyymmdd`, `iso8601`) + `timezone` on date-named columns whose sample values fit the shape, and `iso8601` on a text column with any name when at least 20 sampled values are present and every one is a real ISO date or timestamp (a single version string, impossible date or note rules it out), so epoch integers render as human dates (UTC) deterministically. You don't ask. **Do** glance at the result: if a time column was missed or mis-scaled (e.g. an epoch_ms tagged epoch_s), fix it with a `field:date_format`/`field:timezone` edit op in the same batch; mention any epoch columns found in the Phase 7 summary.

**Write all of these with `sm curate` edit ops — never hand-edit `tables/*.yaml` or script a loop over them.** One ops array, one call (validated + committed + reverted on failure):
```bash
bash "${CLAUDE_PLUGIN_ROOT}/scripts/sm" curate "$ROOT" --ops-file "<artifacts_dir>/local/tmp/agami-caveats.json"
```
```json
[{"op":"edit","kind":"table","area":"main","name":"orders","field":"caveats","value":["Order total can be negative on refunds."]},
 {"op":"edit","kind":"table","area":"main","name":"orders","field":"default_filters","value":["{alias}.deleted_at IS NULL"]},
 {"op":"edit","kind":"table","area":"main","name":"orders","field":"recommended_filters","value":["order_date"]},
 {"op":"edit","kind":"table","area":"main","name":"events","column":"ts","field":"value_transform","value":"TO_TIMESTAMP(ts)"}]
```

### 2e — Reintrospect merge

**Refreshing an existing model is the engine's job, not yours — never rebuild it and merge enrichment back by hand.** When the user says the database changed ("new columns were added", "reload the schema", "refresh `<table>`"), run introspection with `--append` on the existing profile, naming only the tables that changed when they say which (`--tables schema.table …`). When they don't say, first find out whether the database has **new tables**: `--tables` is an allowlist, so passing only the model's own tables can never turn one up. Run `sm discover` for the model's schemas, compare its table list with the tables already in the model, and if there are new ones ask (multi-select) which to include; then pass the model's tables plus the chosen ones:

1. **Preview first:** the same command with `--dry-run`. The report lists `new tables:` and `changed <table>: added …; dropped …; retyped …` and writes nothing. Show that list to the user in plain words and ask before writing. If it lists nothing, say the model already matches the database and stop.
2. **Then write:** the same command without `--dry-run`, then validate.

What the engine guarantees, so you don't re-do it: an existing table keeps every description, flag, caveat and sign-off; a new column arrives with no enrichment; a column the database dropped is kept and marked `stale` (never deleted — the person decides); a changed type is updated in place. Subject areas, their descriptions, entities, metrics and existing joins are untouched — except a confirmed join on a column whose type changed, which goes back to review (`joins to review` in the preview: tell the user they need re-approving in `/agami-model`); a join introspection approved itself whose label the database's primary and unique keys now settle differently, which is relabelled (`joins corrected from the database's constraints` in the preview: tell the user which joins changed; a join a person signed off or edited is never changed); and single-column joins introspection approved for one foreign key over several columns, which are replaced by one join on all its columns (`joins merged into one composite join` in the preview; tell the user). When a person signed off or edited one of those split joins, nothing changes for that key (`composite foreign key not applied` in the preview: tell the user to replace them in `/agami-model` with one join on every column of the key). A new table goes in the area named for its schema (or the only area), and you tell the user where it went. Afterwards, describe **only the new columns** (2b's skip-already-described rule does this), using the model spec's Columns sheet when one was attached. Never `rm`, move aside or rebuild the profile to refresh it.

### 2f — Seed the narrative if the user didn't write one

datasource.md is the human's narrative ONLY — it does **not** hold model facts. The factual summary (subject areas, conventions, the decoded glossary) is **derived from the structured model at read time** (`cli org-context` assembles it for the query path; the explorer shows it as a read-only field). The two homes stay separate by construction.

**Only on the skip path** (datasource.md missing/empty, the user gave no narrative), **seed it with a short factual narrative** so `# About this database` reads as something, not a blank. You've just enriched every table — so write a **1–2 sentence description of what this database is about**, synthesised from the table/area descriptions (e.g. *"Tracks an electric-vehicle battery-swap network — stations, vehicles, battery packs, the swap transactions between them, and the alerts they raise."*). Write it under `# About this database` with the editable comment, and `chmod 644` (a non-secret model file — keep it readable). Keep it factual (don't invent business specifics you can't see); it's a starting **draft** the user edits.

If you can't summarise (no descriptions yet), fall back to the deterministic starter:
```bash
bash "${CLAUDE_PLUGIN_ROOT}/scripts/sm" org-draft "$ROOT" > "$ROOT/datasource.md"   # one-line factual seed
chmod 644 "$ROOT/datasource.md"   # non-secret model file — must stay readable (deploy bundle / teammates)
```

If the user **did** write a narrative in 1.4, leave it untouched. The glossary from `set-terminology` (2b) needs no re-render — it's surfaced automatically by `org-context`. Mention in the Phase 7 summary that the glossary + summary are auto-derived and the narrative is theirs to edit.

**The one-line datasource description (from 1.4 C).** Act on `$DATASOURCE_DESCRIPTION` now, every onboard:
- a line the user wrote → it was already written right after 1.7; nothing to do.
- `keep` → nothing to do; the existing description stays.
- `generate` → synthesize **ONE line** from the subject-area and table descriptions you just wrote: what this datasource covers and the questions it answers (e.g. *"Invoices, payments, products and margins — use for revenue and profitability questions."*). Ground it in the model, never product memory. The line is built from database metadata, which can contain quotes, backticks or `$(…)`, so **never paste it inside a double-quoted argument** — bind it through a quoted heredoc and pass the variable:
  ```bash
  DATASOURCE_DESCRIPTION=$(cat <<'AGAMI_DESCRIPTION'
  <the line>
  AGAMI_DESCRIPTION
  )
  bash "${CLAUDE_PLUGIN_ROOT}/scripts/sm" set-description "$ROOT" --description "$DATASOURCE_DESCRIPTION"
  ```
- skipped → leave it; `datasource.yaml` keeps whatever description it already had (re-introspection preserves it).

**The company draft (from 1.4 A's "Draft it from the database").** Write two or three plain sentences on what the business does, inferred only from the subject areas and table descriptions you just wrote (never from general knowledge of a company name), and apply them as 1.4 A says (`sm set-org` plus `organization.md`), with the first line of `organization.md` reading *"Draft from the database; correct it in /agami-model."*. Phase 7 shows it for correction.

---

## Phase 3: Review the subject-area split

The engine **proposes** the split (one area for small DBs; for large single-schema ones, tables grouped where they join each other most densely — no target number of areas, none past the size limit, a hub table doesn't pull everything into one area — with tables that have no joins grouped by their whole first name word; each table owned once, cross-area joins as `cross_subject_area_relationships`). Area names keep the table's plural (`suppliers`, `status`). For a multi-area split, **surface it for the user to adjust** — boundaries are a curation decision, not a fact:

```
I split <N> tables into <A> subject areas:
  • <area1> — <t1, t2, …>
  • <area2> — <…>
<C> joins span areas (kept as cross-area relationships).
```

Show the split as above (at most 15 areas; say how many more), then one **AskUserQuestion** (header `Areas`): `Looks good (Recommended)` / `Adjust`, with "Other" taking the change in the person's words ("move `refunds` to `payments`"). Skip silently for a single-area small DB. A change that names an area or table the model doesn't have is said back and the same card asked again; never guess which one they meant.

If they adjust, move tables with the curate `move` op — never by editing the `subject_areas/` tree: `{"op":"move","kind":"table","area":"<from>","name":"<table>","to_area":"<to>"}` (add `"schema"` when two schemas share the table name; a `to_area` the model doesn't have yet is created). Its joins follow it — a join that now spans two areas becomes a cross-area join and the reverse — and metrics and entities move with it once every table they read is in the new area; the result's `notes` list what it moved and what stayed because it also reads another table. The whole batch is validated and undone on failure (sizing warns at 25 tables, errors at 30).

**Replace each area's auto-proposed description with a real one** — the engine seeds `"Auto-proposed subject area covering: <tables>"`, which is boilerplate, but the **subject-area description is load-bearing**: it's what the MCP shows in its first pass so the LLM can ROUTE a question to the right area (`get_datasource_schema` returns name + description per area). A blank/boilerplate description means the router is guessing. So write one business line per area — what domain it covers and when you'd query it — grounded in its tables/columns (NOT product memory). Apply through the validated path, one batch (no generator script):
```bash
# one {op:edit, kind:subject_area, area:<name>, name:<name>, field:description, value:"<line>"} per area
bash "${CLAUDE_PLUGIN_ROOT}/scripts/sm" curate "$ROOT" --ops-file "<artifacts_dir>/local/tmp/agami-area-desc.json"
```

---

## Phase 4: Curate before examples — exclude + sign off what seeds depend on

Seeds reference **columns, tables, metrics, and entities** — so settle those *before* generating examples (a seed that uses a column you'd later exclude breaks at query time, and a seed built on an unreviewed metric bakes in a guessed definition). **Relationships are NOT gated here** — they stay lazy: FK joins are already auto-approved by the engine (the DB declared them), and inferred joins self-approve as you query, and an unreviewed one raises the trust banner on the answer's report. So you're not asked to rubber-stamp database-declared foreign keys.

**4a — What needs a decision, asked on cards.** One call returns the counts (turn-boundary-safe — same answer on a fresh run or a resume):

```bash
bash "${CLAUDE_PLUGIN_ROOT}/scripts/sm" curate-gate "$ROOT"
```
→ `{pii_count, preseed_count, should_open_explorer}`. **PII count** = columns flagged `sensitive` still queryable (an excluded column, or any column under an excluded table, isn't counted — so once the person hides them it drops to 0). **preseed count** = metrics + named-filters + entities needing sign-off (relationships are NOT gated — FK joins are engine-approved, inferred joins self-approve as you query). `should_open_explorer` only means "something to decide": nothing opens on this path.

> **PII is advisory, not a seed blocker.** A non-zero `pii_count` is a *review nudge* (the person's call to hide or keep), **not** a gate on seed generation. Only a non-zero `preseed_count` (unreviewed metrics/entities) blocks `seed-examples`. Don't tell the person personal data is stopping their seeds; it isn't.

Also run `bash "${CLAUDE_PLUGIN_ROOT}/scripts/sm" sensitive "$ROOT"` every time: besides the flagged columns it prints `suspected` and `suspected_count`, columns that look personal but aren't flagged (`pii_count` doesn't count them).

**Guessed joins: one line, no card.** Read them with `bash "${CLAUDE_PLUGIN_ROOT}/scripts/sm" review-items "$ROOT" --scope rule2` and keep the items whose `kind` is `relationship` (that scope also returns entities, which the Sign-off step covers). When there are any, post: *"<N> joins were guessed from names and data; they're used as you query and flagged on the answer until confirmed. Review them any time in agami-model."* With 5 or fewer, a short table under it (`Join | Kind`, the item's `title` and `cardinality`); more, just the line. They are not put on a card.

**If `pii_count`, `preseed_count` and `suspected_count` are all 0 → say "Nothing to review before examples; continuing."** and go to Phase 5.

Otherwise run the two steps below that apply (conventions: [`shared/question-cards.md`](../../shared/question-cards.md)). When both apply, the review question and the personal-data question can go on the same card, two questions on one AskUserQuestion call.

**Sign-offs (when `preseed_count` > 0).** Read the queue from the engine:

```bash
bash "${CLAUDE_PLUGIN_ROOT}/scripts/sm" review-items "$ROOT" --scope preseed
```

Each item is `{kind, area, name, extra_lines:[{label,text}], bindings, review_state, …}`. A metric's meaning is its `extra_lines` entry labelled `Definition`; an entity's is its `maps_to` (and `source_signal` when set). An item with `changes_since_approval` was signed off before and has changed since: say so on its row. Show a markdown table, `Kind | Name | What it means | SQL`, one short line per item taken from those fields (never a meaning you made up), at most 15 rows, then *"Showing 15 of <P>; <P−15> more."* when there are more. The **SQL** column is what the item maps to, so the person checks the formula and not only the words: a metric's binding for this database's engine from `bindings`, with its `source_tables`; a join's condition; an entity's `maps_to` column. Show it in backticks, cut at about 80 characters with `…` (the whole of it is answered on request, question-cards rule 5a).

Then call AskUserQuestion, one question (header `Sign-off`), *"What should I do with the <P> definitions waiting for sign-off?"*:

- **Approve all**: sign off every item as listed. Mark it **(Recommended)** and put it first only when the list is short and confident: every item fits in the table above (15 or fewer), none has `changes_since_approval`, and every metric has a `Definition`. Otherwise put **Review one by one** first and mark that one (Recommended).
- **Review one by one**: up to 4 items per card (below).
- **Skip for now**: nothing is signed; they still work, and answers that use one carry a warning until it is signed off.

| Answer | Do |
|---|---|
| Approve all | The identity card first if needed (below), then one batch: `{"op":"approve","kind":"<kind>","area":"<area>","name":"<name>"}` per item (leave `area` out when the item's `area` is null, a cross-area metric), applied with `bash "${CLAUDE_PLUGIN_ROOT}/scripts/sm" curate "$ROOT" --ops-file "<artifacts_dir>/local/tmp/agami-signoff.json" --signer "<reviewer_email>" --role "<reviewer_role>"`. Say how many were signed off, from its `applied`. |
| Review one by one | The one-by-one cards below. |
| Skip for now | Nothing is written. Go to 4b. |

**One by one.** Up to 4 items per card, one question each. `header` is `Metric 3/9` or `Entity 3/9` (at most 12 characters; drop the word when the numbers are long). The question is the item's name, its one-line meaning and the SQL it maps to, *"revenue: sum of paid order totals, refunds excluded. SQL: `SUM(amount) FILTER (WHERE status = 'paid')` on orders"* (the SQL cut as in the table). Options:

- **Approve**: sign it off as written.
- **Reject**: drop it from the model (`{"op":"reject",…}`).
- **Approve all remaining**: sign off this item and every item not yet shown. Every card carries it, so a long queue finishes in one press. Answers given on the same card to the other questions still stand.

After each card, apply its answers in one `sm curate` batch (approvals with `--signer`/`--role`), then ask the next card. Every approval goes through that one command with the person's identity; never mark an item approved any other way.

**"Other" on an item is a change, in the person's words** ("revenue should exclude cancelled orders"). Turn it into `sm curate` edit ops on the field the change names (`calculation`, `bindings`, `description` or `other_names` on a metric; `maps_to`, `description` or `other_names` on an entity), e.g. `{"op":"edit","kind":"metric","area":"<area>","name":"<name>","field":"calculation","value":"<new definition>"}` plus the matching `bindings` edit when the SQL changes, and apply them with `bash "${CLAUDE_PLUGIN_ROOT}/scripts/sm" curate "$ROOT" --ops-file "<artifacts_dir>/local/tmp/agami-edit.json"`. Never hand-edit the YAML and never a generator script. Then ask about that item again, on the next card, showing its new meaning: an edit is not a sign-off.

**An "Other" that asks to stop is a stop, not a change.** When the words on an item card ask to stop, pause or finish later ("stop here", "I'll do the rest later", "pause") rather than change that item, apply the card's other answers, approve or change nothing more, and go to 4b's Continue / Pause card.

**Any other "Other" you can't act on is re-asked, never guessed.** A question ("show me the SQL", "why?") is answered first, then the same card asked again ([question-cards rule 5a](../../shared/question-cards.md#5a-a-question-typed-in-other-is-answered-then-the-same-card-is-asked-again)). An empty change, a column or table the model doesn't have, or words you can't turn into one field's new value: say in one line what didn't match, and ask the same card again. The same when `sm curate` returns `errors` or `skipped` for the edit: show them, and ask the same card again.

**Sign-off identity: asked once, on the first approval.** Before the first `approve` op of the run (from Approve all, Approve, or Approve all remaining), read `reviewer_email` and `reviewer_role` from `<artifacts_dir>/local/.config` (the keys `/agami-model` uses). When both are there, use them and don't ask. When either is missing, ask one card with two questions:

- header `Email`, *"Which email should sign-offs be recorded under? Type it in Other below."*: options **Don't sign anything yet** (nothing is approved; the items stay as they are and you go to 4b) and **Go back** (ask again the card that led here, with nothing approved). The email is typed in "Other", e.g. `you@example.com`; it is asked for in the question, never offered as an option, because an option like "Type it in Other" gets clicked with nothing typed ([question-cards rule 5](../../shared/question-cards.md#5-free-text-goes-in-other)).
- header `Role`, *"What is your role?"*: options **Data Lead**, **Engineer**, **Analyst**; "Other" takes any other role (≤ 40 characters).

The identity is typed by the person: **never infer it** from git config, the environment, the credentials, or the Claude login, and don't pre-fill a domain. An email that doesn't match `\S+@\S+\.\S+` is said back and the same card asked again. Merge the two keys into `<artifacts_dir>/local/.config` (keep every other key), then `chmod 600 <artifacts_dir>/local/.config`, so neither this run nor `/agami-model` asks again.

**Personal-data columns (when `pii_count` or `suspected_count` > 0).** From the `sm sensitive` output above, `{count, columns:[{area, table, column}], suspected, suspected_count}`: `columns` are flagged personal, `suspected` look personal but weren't flagged (a phone-like or birth-date-like sample, a name like `ip_address`). Show both in one table, `Table | Columns`, marking each suspected column "(suspected)", e.g. `customers | email, phone, ip_address (suspected)`; at most 15 rows and *"and N more"*. Then one multiSelect question (header `Personal`), **"Hide these from queries"**: *"These columns look like personal data. Which should queries never read?"*

Flagged and suspected columns are picked the same way:

- 3 columns or fewer in all: one option per column, `customers.email`, or `customers.ip_address (suspected)`.
- More: grouped by table, one option per table with its count, `customers (3: email, phone, ip_address (suspected))`; picking a table hides every listed column in it, suspected ones included. With more than 3 tables, the options are **All <n> listed columns** and the 2 tables with the most listed columns.
- Always last: **Keep them all queryable**.
- "Other" takes `table.column` names to hide, flagged, suspected or any other column in the model.

Hide the picks in one batch, `{"op":"exclude","kind":"table","area":"<area>","name":"<table>","column":"<column>"}` per column, with `bash "${CLAUDE_PLUGIN_ROOT}/scripts/sm" curate "$ROOT" --ops-file "<artifacts_dir>/local/tmp/agami-hide.json"`, then say how many columns are hidden and how many personal-data columns stay queryable (`sm sensitive`'s new `count`). **Keep them all queryable** writes nothing. A name in "Other" that isn't a column in the model, or **Keep them all queryable** picked together with columns to hide, is said back and the same card asked again; never guess which column was meant.

**Show the full view (optional, only when asked).** The model explorer, with every metric, entity, join and column: render it the way `/agami-model preseed` does (`render_model_explorer.py … --initial-tab review --read-only`) and serve it from localhost, giving the person the URL:

```bash
bash "${CLAUDE_PLUGIN_ROOT}/scripts/py" serve_page.py "<explorer.html>" --minutes 30
```

Run it with `run_in_background` and read its first line, `{"url": …}`, for the URL to give the person.

The page is read-only; decisions stay on the cards. A "Generate feedback for Claude" block from an older, editable page is still accepted: apply it as `/agami-model` does, then continue at 4b.

**4b — after the cards:** **recount the sign-offs** with `bash "${CLAUDE_PLUGIN_ROOT}/scripts/sm" review-items "$ROOT" --scope preseed` (`review-items`, not `review-queue`, which takes no scope). If **0** → Phase 5 (the seed command runs clean). If **> 0**:

- the person picked **Skip for now** or **Don't sign anything yet** → that is their decision to continue: say in one line that <n> definitions are unsigned and answers using them carry a warning, and go to Phase 5.
- they stopped part-way (an "Other" on an item card asked to stop, pause or finish later) → AskUserQuestion: `Continue (Recommended)` (seeds run against the current state; answers warn) / `Pause — I'll finish review first` (stop here; resume via `/agami-connect`).

This is the **only** place "continue to examples with items still unreviewed" is offered — and the **only** place you pass `seed-examples --after-review` (otherwise the preseed-review refusal is the engine telling you Phase 4 hasn't happened). **Personal data left queryable does NOT block seeds** — keeping a sensitive column queryable is the person's call; only unreviewed *sign-offs* gate the seeds.

On `reintrospect` with nothing flagged sensitive and nothing unreviewed, skip silently.

---

## Phase 5: Seed prompt examples

**Surface a progress warning first** (second-longest phase): "Generating NL→SQL seeds (a spread across your tables, metrics, entities, and joins — including cross-schema ones) and EXPLAIN-validating each against the live DB. Expect 1–3 min (longer on cloud / a multi-schema DB). I'll narrate per-example progress."

**5a — generate** (your job — the LLM step) candidate examples grounded in the model. Aim for **coverage and a good spread**, not a fixed count:

- **Shapes:** a count, a top-N, a time-bucketed trend, a breakdown, a recency filter. Anchor time filters to each table's `data_range` MAX (not `NOW()`) so seeds don't return 0 rows on a stale dataset.
- **Spread:** across the whole set, touch each major table/family, exercise **every approved metric at least once**, use the key **entities** by their real names/synonyms, and cover **representative joins** — intra-area *and* cross-schema.
- **Get every join from the model through `bash "${CLAUDE_PLUGIN_ROOT}/scripts/sm" context "$ROOT" --tables <t1> <t2> …`** — it returns the joins touching those tables exactly as a question will be answered (area and cross-area alike), each with its role label and full condition (a `current_flag = 'Y'` included), so a seed writes the join the model declares. Never read the model's YAML to find joins.
- **Cross-schema seeds — the highest-value few-shots, include them deliberately.** The cross-schema / cross-area joins are in that same output (and in `cross_subject_area_relationships`). For the most meaningful ones, write a question a user would actually ask that **spans both schemas** — a fact/metric in one schema joined to the entity it references in another. Use the relationship's declared join (`on:` / from→to columns) and **schema-qualified** table names (it's one database — ordinary cross-schema SQL, fully EXPLAIN-able). Include **2–4** of these and make **1–2 genuinely complex** (3+ tables across two schemas, with an aggregation + a filter) — cross-schema SQL is exactly what NL→SQL gets wrong unaided, so a worked example pays off most here. (Cross-*profile* / federation seeds are out of scope — those need DuckDB ATTACH; this is cross-*schema* within one DB.)
- **Count scales with the model:** a single-schema DB is fine at ~10–12; a multi-schema one wants ~2–3 per subject area **plus** the 2–4 cross-schema seeds — don't undersample a 5-schema DB at a flat 12.

**The person's own questions come first.** When 1.5 saved `<artifacts_dir>/local/tmp/agami-asked-questions.txt`, write one candidate per question, its `question` as the person wrote it and SQL from the model, before any you make up; they go through the same validation and the same Keep / Drop cards as the rest, and Phase 6's table marks them *(yours)*. A question the model can't answer (a table that was left out, say) is not forced: list it in one line after the table with the reason.

Tag each with its `tables` (list **all** tables it touches — both schemas for a cross-area one), `columns`, and `metrics` (**every** model metric it computes, by `name`, so it is found under each; a name the model doesn't declare rejects the seed). **Store a cross-area seed under its primary/driving table's area** (tagged with both schemas' tables, so the ranker can surface it from either side). Write the candidates as a JSON array to `<artifacts_dir>/local/tmp/agami-seeds.json` — each: **required** `question`, `sql`; optional `tables`, `columns`, `metrics`.

**5b+5c — validate + write in ONE packaged call.** **Do NOT write a script to loop EXPLAIN over the seeds and don't hand-write the YAML.** `seed-examples` validates every candidate against the live DB (wraps each as a zero-row query — dialect-agnostic, scans nothing), writes the passing ones (appends, dedups by `question`, commits), and returns the rejects with their DB error. Post one progress line from its output (`totals`, or `added` and `rejected` for one area): *"✓ Wrote <added> examples; <rejected> failed the dry run."*
```bash
bash "${CLAUDE_PLUGIN_ROOT}/scripts/sm" seed-examples "$ROOT" --profile <profile> --file "<artifacts_dir>/local/tmp/agami-seeds.json"
```
**Write every area's seeds in this one call:** give each seed an `area` (the subject area it is stored under) and leave out `--area`, or repeat `--area` to limit the call to those areas. A seed with no `area`, or one outside the call's areas, stops the call before anything is written. With exactly one `--area` the seeds need no `area` and the output is the single-area shape `{added, written, committed, rejected:[{question, error}]}`; with several or none it is that shape with each `rejected` entry labelled `area`, plus `areas` (one such result per area, labelled `area`) and `totals` (`areas`, `added`, `rejected`). For each `rejected`, optionally regenerate once and re-run with just those; don't block the flow on a few drops. Corrections later append via `/agami-save-correction` (same `add-example` path).

> **The command enforces Phases 2 and 4 for you.** It runs two gates before writing anything:
> - `{refused: "columns_unenriched", unenriched_tables}` → a table got no column descriptions at all (you skipped the Phase-2 column pass). Go back, run the column pass on those tables, re-run. **NOT bypassable** — naked columns degrade every answer.
> - `{refused: "preseed_review_pending", pending_count}` → metrics/entities the seeds depend on are still unreviewed (you skipped the review). **Go back to Phase 4a** and ask its Sign-off card. Bypass only via `--after-review`, and **only** on the Phase-4b path (the person has answered the Sign-off card and chose to continue with some items unreviewed). Never pass `--after-review` to force past a *fresh* refusal.

---

## Phase 6: Validate every seed example (the trust onboarding)

**Run every seed with ONE packaged call — `sm seed-validate` — never a hand-rolled "run all the seeds" script.** It executes each written seed against the live DB **through `execute_sql`** (the same path agami-query uses), so the **fan-trap / chasm-trap pre-flight and the scope/PII gates always apply** — a raw-connection driver could skip that safety and let a fan-out scan the whole table. It emits the examples-validation items (`{n, question, sql, row_headers, row_preview, row_count, state}`); a seed the pre-flight refuses or that errors comes back with its `error`, not a faked result:

```bash
bash "${CLAUDE_PLUGIN_ROOT}/scripts/sm" seed-validate "$ROOT" --profile <profile> > "<artifacts_dir>/local/tmp/agami-examples-items.json"
```

Without `--area` it runs every area's seeds (each through `execute_sql` scoped to its own area, with the same per-statement limits) and prints one object: `areas` (`{area, examples, errored}` each), `items` (the items above, each labelled `area`) and `totals` (`areas`, `examples`, `errored`). Repeat `--area` to run only those areas. One `--area` prints the bare items array, as before. An area the model doesn't have is refused with `error: unknown_area` and the `valid_areas`.

(Heads-up before you run it: on a large warehouse this executes every seed for real — some may be full-scan aggregates that take a few minutes and cost warehouse compute. It's a one-time onboarding step; surface a one-liner so the wait is expected, and run it in the background if it's slow.)

**Show what ran, then one card** (conventions: [`shared/question-cards.md`](../../shared/question-cards.md)). No page opens on this path, and no reply block is waited for. Post one progress line from `totals` (or the bare array's length and its items with an `error`): *"✓ Ran <examples> examples: <examples − errored> returned rows, <errored> failed."* Then a markdown table from the items, failing ones first, at most 15 rows, then *"Showing 15 of <n>; <n−15> more."* when there are more:

| # | Question | Rows | Result |
|---|---|---|---|
| 3 | Revenue by month this year | 12 | 2024-01 $41,200 · 2024-02 $39,850 · … |
| 7 | Top 5 customers by orders | — | failed: `column "cust_id" does not exist` |

`#` is the item's `n`, `Rows` its `row_count`, `Result` the first row or two of `row_preview` under `row_headers` (already unit-formatted), or `failed:` and its `error`. A result that looks wrong though it ran (a ratio far outside its range, a count of 0, a trend sorted by name) goes in one sentence under the table: that is 6a's case.

Then call AskUserQuestion, one question (header `Examples`), *"What should I do with these examples?"*:

- **Keep all that ran (Recommended)**: sign off every example that returned rows. Drop the (Recommended) and put **Review all one by one** first when a result that ran looks wrong (6a).
- **Review the failing ones**: one by one, only the ones with an `error`. Leave it out when none failed.
- **Drop the failing ones**: mark every failed example rejected. Leave it out when none failed.
- **Review all one by one**: every example, 4 per card.

| Answer | Do |
|---|---|
| Keep all that ran | The identity card from Phase 4a if `reviewer_email`/`reviewer_role` aren't saved yet; here **Don't sign anything yet** leaves every example written but unsigned and goes on to Phase 7 (not 4b). Then, per area, write the examples that ran back with their own `question` and `sql` (same SQL, so it is a sign-off, not a change; their tags are kept): `bash "${CLAUDE_PLUGIN_ROOT}/scripts/sm" add-example "$ROOT" --area <area> --file "<artifacts_dir>/local/tmp/agami-keep-examples.json" --signer "<reviewer_email>" --role "<reviewer_role>"`. The sign-off is recorded in `curation_log.jsonl`, not on the example. Failed ones are left as they are; say how many. |
| Review the failing ones | The one-by-one cards over the failed items. |
| Drop the failing ones | `bash "${CLAUDE_PLUGIN_ROOT}/scripts/sm" remove-example "$ROOT" --question "<area>/<question>" …` (one `--question` per failed item, `--signer`/`--role` when known). They are marked rejected and kept for audit, not deleted: say so. |
| Review all one by one | The one-by-one cards over every item. |

**One by one.** Ask up to 4 examples per card, one question each, header `Example 3/9` (at most 12 characters; past 9/99 use `Ex 12/40`). The question is the example's question and what it returned, *"Revenue by month this year: 12 rows, 2024-01 $41,200"* (or *"failed: <error>"*). Options: **Keep** (the `add-example` sign-off above, for that one), **Drop** (`remove-example` for that one), and **Keep all remaining** (keep this one and every remaining one that ran). "Other" describes a fix in the person's words ("should only count paid orders"): rewrite that example's SQL, write it with `sm add-example` (same question, so it replaces), re-run its area with `sm seed-validate "$ROOT" --profile <profile> --area <area>`, and ask about it again showing the new result. A fix that touches a column or metric beyond this one example is 6a's: fix it everywhere, as 6a says. A question ("show me the SQL", "show me the rows") is answered first, then the same card asked again ([question-cards rule 5a](../../shared/question-cards.md#5a-a-question-typed-in-other-is-answered-then-the-same-card-is-asked-again)). **An "Other" you can't act on is re-asked, never guessed**: an empty fix, or one naming a column the model doesn't have, is said back and you ask the same card again.

When every example has an answer, go to Phase 7.

**Show the full view (optional, only when asked).** The examples-validation page, with every result and its SQL. Render it from the items and serve it from localhost, giving the person the URL:

```bash
bash "${CLAUDE_PLUGIN_ROOT}/scripts/py" render_examples_validation.py \
  --title "Seed examples · <profile>" --profile <profile> \
  --items-file "<artifacts_dir>/local/tmp/agami-examples-items.json" \
  --read-only --out "<artifacts_dir>/local/examples-validation/<profile>/<ts>.html"
bash "${CLAUDE_PLUGIN_ROOT}/scripts/py" serve_page.py "<artifacts_dir>/local/examples-validation/<profile>/<ts>.html" --minutes 30
```

Run `serve_page.py` with `run_in_background` and read its first line, `{"url": …}`, for the URL to give the person.

It takes the combined object or a single area's array; repeat `--items-file` to put several files on one page, labelling a single area's array with its area as `--items-file <area>=<path>` (an unlabelled one is refused there, since two areas can share a question; so is the same file twice). Each card shows its area.

The page is read-only; decisions stay on the cards. A block pasted from an older, editable page is still accepted: `validate N` / `reject N` / `edit N` / `add example` / `note N` / `note all`. Apply each through the **packaged commands**, never by hand-rewriting `examples.yaml`. **`N` is the page's global example number (1..N across every area, assigned in render order).** `render_examples_validation.py` normalizes the items file to this same numbering, so resolve `N` → its example by reading that file back:
- **`validate N`** → the example is a trusted anchor; if the block carries a `by <email>` clause, record the sign-off via `sm add-example` (re-write the same example with `--signer`/`--role`). It reports `applied` (one line per example re-written).
- **`reject N`** → **`sm remove-example "$ROOT" --area <area> --question "<that example's exact question>"`** (`--signer`/`--role` to record who). To reject several across areas in one call, leave out `--area` and name each as `--question "<area>/<question>"` (looked up in that area only); a bare question is matched in whichever area has it, and one that more than one area has is refused (`refused: ambiguous_question`, with the areas) and nothing is rejected. That call adds `areas` and `totals` to the output. It marks the example `status: rejected`: kept in `examples.yaml` for audit (not deleted), dropped from the runtime ranker. Tell the person it's rejected and kept, not deleted.
- **`edit N`** that fixes *that example's* SQL → re-write it with `sm add-example` (dedups by question, so it replaces). Re-run the new SQL to capture its number.

When the user gives a **cross-cutting display/formatting rule** (currency, units, number formatting — applies to many examples, not one), classify it like any correction (see [`agami-save-correction`](../agami-save-correction/SKILL.md) → "DISPLAY / FORMATTING preference"): a currency/unit fact attaches to the **column** (a `caveat`/`value_transform` in the shared model — org-wide by construction); a cross-cutting presentation convention → `datasource.md`; a personal tic → `USER_MEMORY.md`. Don't reflexively file to USER_MEMORY, and don't reflexively ask — only ask if personal-vs-org is genuinely unclear. **Display rounding is a convention, not SQL** — a "show fewer decimals" rule → `datasource.md`, never `ROUND()` baked into the example's SQL (that corrupts the exact-number verification anchor; the system formats numbers in full via `units.py`).

**`note all >>>…<<<` — the model-wide note. Apply it ONCE; the user never repeats themselves.** The full view page has a "note for the whole model" box (separate from per-example `note N`). Its content arrives as a single **`note all >>>…<<<`** block. Treat it as one correction that applies across the board: classify it like any other (see [`agami-save-correction`](../agami-save-correction/SKILL.md)) and write it to the **right place, once** — a column `unit`/`caveat`/`value_transform` (a data fact: "amounts are in INR", "TOTAL can be negative"), a cross-cutting presentation convention → `datasource.md`, a personal display tic → `USER_MEMORY.md`, or a filter/business rule the user wants applied → the relevant column/example. Then **re-render so every affected example reflects it**, and name where it landed in one line — *"Got it — set `<col>`'s unit to INR model-wide; it'll show on every example now."* If the user is still typing the same thing into per-example `note N` boxes, that's a smell: lift it to a single model-wide change and tell them they don't need to repeat it. (Formatting is just the common case — the rule is generic: any cross-cutting fact is stated once, written once, applied everywhere.) Note the result preview is already unit-formatted — `sm seed-validate` runs numbers through the same `units.py` as the live query path — so a money column showing a **bare** number means a missing column `unit`, not a per-example note.

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

---

### 6b — Check the reports the person shared (only when 1.5 saved some)

When `<artifacts_dir>/local/reconcile/inbox/` has files (or a screenshot path) from 1.5, say in one line what was shared, then AskUserQuestion, one question, header `Reports`: *"Check your <n> reports against the new model now?"* Options: **Check them now (Recommended)** / **Later** (say they can run `/agami-reconcile` any time; the files stay in the inbox). **Check them now** hands the files to [agami-reconcile](../agami-reconcile/SKILL.md), which grades any SQL part by part, compares the numbers, and asks about each mismatch on cards; come back to Phase 7 when it is done.

## Phase 7: Post-introspect summary (MANDATORY — NEVER SKIP)

**Sequencing gate (read first):** Phases 7–8 run **only after the user has finished validating** — i.e. after every Phase 6 example has an answer on a card (or, in the full view, they sent `done`), and after the Phase 4 cards were answered. **Never print this summary or the Phase 8 closing while a review or examples card is still unanswered, or while a full view page is out and its block hasn't come back** — wait for the person, don't summarize over them.

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

When 2f drafted the company description, show it too, in one line: *"Company (draft): <description>. Correct it in /agami-model."* After the block, state the datasource's one-line description from `datasource.yaml` — *"Agents will pick this datasource by: "<description>" — edit it any time with `sm set-description`."* If you generated it in 2f, say it is a draft. If it is empty (they skipped 1.4 C), say so in one line: an organization with several datasources routes questions by this line.

Then **AskUserQuestion**: `Review what needs sign-off` (→ `/agami-model review` — approve or reject the pending metrics/entities on cards; when that review ends, come back here to Phase 8, the starter questions, rather than agami-model's overview card, so onboarding still ends on a first question) / `See the model` (→ `/agami-model` — the overview in the chat, then hide tables or columns) / `Skip — I'll review later` (default). If a sibling skill isn't built yet, omit that option — don't error.

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
4. **Write the confirmed ones** to `<artifacts_dir>/local/cross_profile_relationships.yaml` in the format [agami-query expects](../agami-query/SKILL.md) — **merge**, never clobber an existing file (load it, append new entries, dedup by the `from_profile/from_dataset/to_profile/to_dataset` tuple), `chmod 600`:
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
