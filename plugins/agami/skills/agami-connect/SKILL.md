---
name: agami-connect
description: "Introspects the user's database and emits a strict Open Semantic Interchange (OSI) v0.1.1 semantic model at the per-profile YAML file inside the .agami home directory. Generates seed NL-to-SQL few-shot examples (each EXPLAIN-validated against the live DB) at the per-profile examples file, then runs one demo query so the user immediately sees the skill working. Every model write is gated by the OSI + Agami validator — no breaking model is ever persisted."
when_to_use: "Auto-invoked by agami-query-database the first time it runs (when the semantic model YAML is missing). Invoke explicitly when the user says 'connect to my database', 'introspect the schema', 'reload schema', 'add a new database', or after the user changes their schema and wants the model refreshed. Requires agami-init to have run first (credentials must exist)."
argument-hint: "[reintrospect | profile NAME]"
---

# agami connect

**Before suggesting any slash command in chat, read [`shared/invocation-conventions.md`](../../shared/invocation-conventions.md).** All four agami slash commands (`/agami-init`, `/agami-connect`, `/agami-query-database`, `/agami-save-correction`) work. Never write the un-prefixed forms (`/init`, `/connect`, etc.) or colon forms (`/agami:connect`) — those don't exist. For chat replies, prefer natural language ("say 'reload the schema'", "say 'introspect my database'") — the agami-connect skill's `when_to_use` matcher routes correctly without an explicit slash command.

You are setting up the agami semantic model for the user's database. Goal: by the end, there is a **per-schema OSI v0.1.1 model** at `<artifacts_dir>/<profile>/` (`index.yaml` + one `<schema>.yaml` per database schema), a seeded examples library at `<artifacts_dir>/<profile>/examples.yaml`, an `ORGANIZATION.md` template the user can edit, and the user has seen one demo query execute end-to-end.

This skill orchestrates four phases:

1. **Introspect** — pull tables / columns / PK / FK from `information_schema` via the chosen database tool (psql / mysql / snowsql / sqlite3 / DuckDB / `execute_sql.py`).
2. **Build the OSI model** — assemble the YAML strictly to the OSI v0.1.1 spec, with Agami metadata (column types, choice fields, performance hints) packed under `custom_extensions[].vendor_name: COMMON` per [`shared/agami-osi-extensions.md`](../../shared/agami-osi-extensions.md).
3. **Validate, then write** — run the validator at `plugins/agami/scripts/validate_semantic_model.py`. If it fails, **DO NOT WRITE THE FILE.** Surface the errors and stop.
4. **Seed examples + run demo query** — generate few-shot pairs, EXPLAIN-validate each, then pick one to run as a demo and ask the user Yes / No / Skip.

For the OSI format spec: [`shared/schema-reference.md`](../../shared/schema-reference.md).
For the bundled JSON schema: [`shared/osi-schema.json`](../../shared/osi-schema.json).
For Agami's documented `custom_extensions`: [`shared/agami-osi-extensions.md`](../../shared/agami-osi-extensions.md).
For introspection SQL: [`shared/introspect-queries.md`](../../shared/introspect-queries.md).
For FK validation: [`shared/fk-validation.md`](../../shared/fk-validation.md).
For SQL dialect rules: [`shared/dialect-rules.md`](../../shared/dialect-rules.md).
For SQL safety: [`shared/sql-generation-rules.md`](../../shared/sql-generation-rules.md).
For DB error classification: [`shared/db_error_classifier.md`](../../shared/db_error_classifier.md).

## Conversation style

- **Combine acknowledge + next question** — don't waste turns on "Got it!"
- **Use AskUserQuestion for every Yes/No/Skip** — never inline-bullet options. **Use `(Recommended)` only when there's a genuine recommendation.** For fact-of-environment questions ("which database type?", "which schemas should I introspect?"), don't mark any option Recommended — the user picks what they have.
- **Keep the user oriented** — print one-line progress markers between phases (`✓ Introspected 12 tables`, `✓ Validator passed`, `✓ Generated 10 examples`).

## Progress tracking — set up a todo list at the very start

This is a multi-phase skill that often takes 5–15 minutes end-to-end. **The very first action on every invocation is to call `TodoWrite` with the skill's major phases as todos**, so the user can see what's coming and watch progress in real time. This was validated as a strong UX signal — users reported it makes the wait feel intentional rather than opaque.

The exact todo list to seed (one task per major phase, in this order):

```
1. Preflight: credentials check + tool detection
2. Introspect database schema (list tables, columns, PK, FK)
3. Build OSI semantic model (with trust-layer confidence per entry)
4. Validate + write model; snapshot under .snapshots/<hash>/; git init
5. Generate seed NL→SQL examples (EXPLAIN-validated)
6. Validate every seed example (user reviews via dashboard)
7. Post-introspect trust summary + dashboard offer
8. Follow-up suggestions
```

Use `content` for the imperative form and `activeForm` for the present-continuous form, e.g. `content: "Introspect database schema"` / `activeForm: "Introspecting database schema"`.

**Mark each todo `in_progress` when its phase starts and `completed` immediately when the phase ends.** Exactly one `in_progress` at a time. Never batch completions.

**Skip the seeding if the todo list already contains these items** (e.g., the skill is resuming a mid-run state because the user re-invoked after Phase 0 bailed to agami-init for credentials). Detect by inspecting the current todo list: if it already has todos matching this skill's phases (by content), don't re-create — just continue marking progress on the existing list.

When `$ARGUMENTS == reintrospect`, the same todos apply — re-introspection runs through the same phases.

---

## Phase −1: Plan-mode check

Run the detection + ask logic from [`shared/plan-mode-check.md`](../../shared/plan-mode-check.md). agami-connect needs Bash (introspection queries) and Write (per-schema yaml files) — both are blocked in plan mode.

**If plan mode is active and the user picks `Stay in plan mode` (or this skill is invoked under an active plan-mode context with no prompt available):** refuse with the one-liner below and **end the turn**. **DO NOT write a plan file describing what would happen. DO NOT call `ExitPlanMode`.** Generating "a brief plan of what introspect would do" is noise the user did not ask for — they invoked this skill to do its job, not to read a description of its job. The user switches modes via Shift+Tab and re-invokes.

Refusal text (verbatim — don't elaborate):

> I can't introspect in plan mode — switch to **Auto** or **Edit Automatically** mode (Shift+Tab to cycle) and re-invoke me. The schema picker, description generation, and demo query all need write access to `<artifacts_dir>/<profile>/`.

If plan mode is not active, skip this phase silently and go to Phase 0.

---

## Phase 0: Preflight

### HARD RULES — read before doing anything

These are non-negotiable. They override every other instruction in this file when they conflict.

1. **Connect ONLY to the host/port/database/user/password in `~/.agami/credentials`** (or, if set, in `AGAMI_DATABASE_URL`). Never connect to anything else. Never probe `localhost` "to see if there's a database running there" unless the credentials file explicitly says `host = localhost`. Never substitute defaults for missing credential fields.
2. **Never ask the user for host / port / database / user / password values in chat.** Not even "as a temporary thing while we set up". Credentials live in `~/.agami/credentials` only — that's the contract. The single authorized credential-collection path is `agami-init`, which writes a `credentials.example` template the user fills in and saves locally. This skill (`agami-connect`) only *reads* credentials, never *collects* them.
3. **Never scan or guess.** No `pgrep`, no `ps`, no `find /` for databases, no `ls /Applications/Postgres.app`, no `ls /Library/PostgreSQL`, no listing port-listeners, no testing connections to common hostnames. The only acceptable Bash probes in this phase are `which <tool>` (to find a CLI binary on `PATH`) and `python3 -c 'import <module>'` (to test a driver). Nothing else.
4. **If credentials are missing for the active profile, hand off to `agami-init`** and stop this skill. agami-init runs the DB-type picker, writes the per-DB-type credentials template, and detects the runtime tool. The user fills in `~/.agami/credentials`, runs `chmod 600`, and re-invokes this skill (or asks a data question — `agami-query-database` auto-invokes us). Do NOT prompt for the connection URL in chat from here.
5. **NEVER put the password (or any credential field) in a Bash command line.** That includes `export PGPASSWORD='<value>'`, `export MYSQL_PWD='<value>'`, `psql -W <password>`, `mysql -p<password>`, or any heredoc form that interpolates the password into stdin. Hosts render Bash tool calls in chat — anything in the command leaks. Use the auth files generated by `scripts/setup_pgauth.py` for runtime queries: `PGPASSFILE=$HOME/.agami/.pgpass psql -h ... -U ... -d ... -c "$SQL" --csv` (psql) or `mysql --defaults-file=$HOME/.agami/.mysql.cnf --defaults-group-suffix=_<profile> ...` (mysql). For the Python driver path use `python3 scripts/execute_sql.py`. See [`shared/connection-reference.md → HARD RULES`](../../shared/connection-reference.md).

If you find yourself reaching for any command that doesn't fit the rules above, stop and re-read this section.

### Preflight steps

1. **Resolve `<profile>`** in this order: `AGAMI_PROFILE` env var → `active_profile` field in `~/.agami/.config` → literal string `"main"` (current default; older installs may still have `"default"` and that continues to work). The OSI `semantic_model[].name` MUST equal the resolved `<profile>`.
2. **Credentials check (binding).** Read `~/.agami/credentials` if present and look for the `[<profile>]` section. If the file is missing, OR the section for the active profile is missing, OR `AGAMI_DATABASE_URL` is unset → **invoke `agami-init` and stop this skill.** Do not continue. Do not probe anything. Surface a one-liner before the handoff: *"No credentials yet for profile `<profile>` — running setup."*. If credentials exist, apply the chmod check (refuse if world-readable).
3. **Resolve the connection fields** from the credentials file's `[<profile>]` section (or parse from `AGAMI_DATABASE_URL`):
   - **postgres / redshift / mysql:** either `url = ...` (DSN form, recommended for cloud DBs) or per-field `host`, `port`, `database`, `user`, `password` (+ optional `sslmode`).
   - **snowflake:** either DSN `url = snowflake://...` or per-field `account`, `user`, `password` (or `authenticator`), plus optional `warehouse`, `database`, `schema`, `role`. **No `host`/`port` for Snowflake** — the connector uses the account identifier directly.
   - **bigquery:** either DSN `url = bigquery://<project>[/<dataset>]?service_account=/path/to/key.json&location=US` or per-field `project` (required), `service_account_path` (recommended; falls back to Application Default Credentials if omitted), and optional `dataset`, `location`. **No `host`/`port`** — BigQuery is HTTPS-only via the Google Cloud REST API.
   - **sqlite:** `path` (absolute).

   Never substitute a value that's missing — surface a clear "your credentials file is missing field X for profile Y; please add it" message and stop.
4. **Tool detection.** Look up the cached connection method and tool paths from `~/.agami/.config`. If absent, run tool detection per [`agami-init/SKILL.md → Phase 3 (tool detection)`](../agami-init/SKILL.md#phase-3-tool-detection).
5. **Resolve `<artifacts_dir>`** per [`shared/file-layout.md → Configuring artifacts_dir`](../../shared/file-layout.md#configuring-artifacts_dir): `AGAMI_ARTIFACTS_DIR` env var → `~/.agami/.config.artifacts_dir` → default `$HOME/agami-artifacts`. All semantic-model files (`index.yaml`, `<schema>.yaml`, `examples.yaml`, `ORGANIZATION.md`) for this skill go inside `<artifacts_dir>/<profile>/`. The directory is created lazily — if it doesn't exist yet, `mkdir -p "$artifacts_dir" && chmod 755 "$artifacts_dir"`.
6. **Update-check (best-effort, non-blocking).** Run the version probe from [`shared/version-check.md`](../../shared/version-check.md). If a newer plugin version exists on `main`, surface a one-line note ("agami X.Y.Z is available — run `/plugin marketplace update litebi && /reload-plugins`"). Never block on a network failure or stale local file — the probe is informational only.
7. If `$ARGUMENTS` is `reintrospect`: skip Phase 1's "already-have-a-model?" check and re-introspect from scratch. **Hand-edits the user made (descriptions, ai_context, choice_fields, metrics, trust-layer sign-offs) MUST be preserved** — re-introspection only updates what the DB unambiguously tells us (table list, columns, types, PK, FK).

---

## Phase 1: Introspect

### Phase 1.0 — set expectations before kicking off

Introspecting can take a while, especially against cloud DBs. Tell the user **before** the first probe so they don't think the skill has hung. The estimate depends on the database type:

| db_type | Typical setup time | Why |
|---|---|---|
| `sqlite` | < 5 seconds | Local file, instant metadata. |
| `postgres` (local) | 5–15 seconds | Local network, fast `information_schema` queries. |
| `postgres` (cloud — Supabase / Neon / RDS) | 15–60 seconds | Network round-trip per query, plus FK validation join checks. |
| `mysql` | 10–30 seconds | Similar to postgres. |
| `redshift` | 1–5 minutes | Cloud + Redshift's metadata can be slow to return; FK validation joins amplify the cost. |
| **`snowflake`** | **5–15 minutes (longer with many tables / large data)** | Cold warehouse spin-up (often the dominant cost), per-table SHOW commands, EXPLAIN-validation against the live warehouse, FK-validation join checks, choice-field cardinality scans, and per-table sample-row queries for description generation. Real-world tested against a 100-table credit-bureau Snowflake account: ~12 minutes. Accounts with hundreds of tables or huge warehouses can push past 30 minutes. **Always set the user's expectation honestly** — narrate per-table progress so they can see it's working, not stuck. |

Surface a one-liner with **per-step duration estimates** so the user can tell the skill apart from a hang at any moment, not just the first. **Don't lowball — err on the high side.** A user told "5 min" who waits 4 minutes thinks "almost there"; one told "1 min" who waits 4 minutes thinks "stuck or broken." Honest estimates buy patience.

> Setting up your `<profile>` connection — for `<db_type>` this typically runs:
> - Listing schemas (~5s)
> - Discovering tables (~<10–60>s depending on schema size)
> - Generating descriptions (~30–60s per ~50 tables — scales linearly)
> - Choice-field detection (~5–10s per ~50 tables)
> - FK validation joins (~<5–60>s depending on table sizes)
> - Seeding examples (~20s)
> - Demo query (~5s)
>
> **Total: <high-end estimate>.** I'll narrate progress so you can see it's working.

For Snowflake specifically, write the total as a range like "5–15 minutes (longer if your warehouse needs to spin up cold)" — never lowball it as "60–180 seconds" the way earlier versions of this skill did. Real users on real warehouses regularly take 10+ minutes, and the under-promise-over-deliver framing keeps them patient.

**For reintrospect:** prepend "Re-introspecting (this takes about as long as initial setup)." so the user knows the estimate still applies.

### Phase 1.1 — existing-model check + legacy-layout migration

The current layout (v1.3) is `<artifacts_dir>/<profile>/index.yaml` + `<artifacts_dir>/<profile>/<schema>/_schema.yaml` + per-table yamls. Three earlier layouts exist and need migration:

- **v1.0** — single file at `~/.agami/<profile>.yaml` (plus `<profile>-examples.yaml`).
- **v1.1** — per-schema directory at `~/.agami/<profile>/<schema>.yaml` (under secrets dir, not yet split to artifacts).
- **v1.2** — per-schema directory at `<artifacts_dir>/<profile>/<schema>.yaml` (under artifacts, but each schema is one big file with all its tables).

Detection (check in order — first match wins):

```bash
artifacts_profile_dir="$artifacts_dir/$profile"
v11_profile_dir="$HOME/.agami/$profile"
v10_legacy_file="$HOME/.agami/$profile.yaml"
v10_legacy_examples="$HOME/.agami/$profile-examples.yaml"
v11_user_memory="$HOME/.agami/USER_MEMORY.md"

is_v13_artifacts() {
  # v1.3: artifacts dir + index.yaml + at least one <schema>/_schema.yaml
  [ -d "$artifacts_profile_dir" ] && [ -f "$artifacts_profile_dir/index.yaml" ] && \
    find "$artifacts_profile_dir" -mindepth 2 -name "_schema.yaml" -print -quit | grep -q .
}

is_v12_artifacts() {
  # v1.2: artifacts dir + index.yaml + at least one top-level <schema>.yaml (no _schema.yaml subdirs)
  [ -d "$artifacts_profile_dir" ] && [ -f "$artifacts_profile_dir/index.yaml" ] && \
    ls "$artifacts_profile_dir"/*.yaml 2>/dev/null | grep -v -E '/(index|examples)\.yaml$' | grep -q .
}

if is_v13_artifacts; then
  layout=existing-v13
elif is_v12_artifacts; then
  layout=v1.2-needs-table-split
elif [ -d "$v11_profile_dir" ] && [ -f "$v11_profile_dir/index.yaml" ]; then
  layout=v1.1-under-agami-home
elif [ -f "$v10_legacy_file" ]; then
  layout=v1.0-single-file
else
  layout=fresh
fi
```

**Branch on `layout`:**

- **`existing-v13`** and `$ARGUMENTS` is not `reintrospect`:
  - "I already have a model for `<profile>` at `<artifacts_dir>/<profile>/`. What would you like to do?"
  - AskUserQuestion options (no `(Recommended)` — user picks based on intent):
    | label | description |
    |---|---|
    | `Re-introspect from DB` | Drop everything not hand-edited and pull fresh from the database. Preserves descriptions, ai_context, choice_fields, metrics, and human-approved trust-layer sign-offs. |
    | `Verify and continue` | Validate the existing model (no DB queries), then continue to seed-examples / dashboard offer. |
    | `Skip to seeding examples` | Skip introspection and validation. Regenerate examples.yaml and run the validation dashboard. |
    | `Set up a different database (new profile)` | Add a separate profile for another DB (e.g., staging, analytics). Leaves `<profile>` untouched and starts the full first-run flow for the new profile name. |
  - **If the user picks `Set up a different database`:** ask for the new profile name in a follow-up inline message: *"What should I call the new profile? (lowercase letters / digits / dashes / underscores, 1–32 chars; this is the name you'll use in `AGAMI_PROFILE=<name>` to switch between DBs.)"* Default-suggest names based on common patterns (`staging`, `analytics`, `production`) if the user seems unsure. Then set the in-process `<profile>` variable to the new name, invoke `agami-init` to walk the DB-type picker + write `credentials.example` for a new `[<new-name>]` section, and after the user fills it in re-enter this skill at Phase 0 with the new profile active. **Do NOT modify the existing `[<old-profile>]` credentials section** — only append.

- **`v1.2-needs-table-split`** — model exists in artifacts dir but uses the single-file-per-schema layout. Migrate to per-table layout in place.
  - Tell the user: "Splitting `<schema>.yaml` files into per-table yamls (`<schema>/<table>.yaml`). Smaller diffs in git, faster relevance retrieval at scale. No DB queries — purely a file rewrite."
  - For each `<schema>.yaml` at the top level:
    - Read the file, parse YAML.
    - `mkdir -p "$artifacts_profile_dir/<schema>"`.
    - For each `dataset` in `semantic_model[0].datasets[]`: write `<schema>/<table>.yaml` as a standalone OSI doc with one dataset (per Phase 2 shape above). Keep all the field definitions, custom_extensions, etc. as-is. Add `agami.table` to the model-level extension.
    - Write `<schema>/_schema.yaml` with the table TOC + the original schema yaml's `relationships[]` and `metrics[]`.
    - `rm "$artifacts_profile_dir/<schema>.yaml"`.
  - Update `index.yaml.schemas[].file` from `<schema>.yaml` to `<schema>/_schema.yaml` for each migrated schema.
  - Run validator in `--directory` mode to confirm. If it fails, restore from `<schema>.yaml.bak` (which the migration creates first) and surface the errors. Don't leave a half-migrated state.

- **`v1.1-under-agami-home`** — move the per-schema dir to artifacts AND split into per-table.
  - Tell the user: "Moving your model from `~/.agami/<profile>/` to `<artifacts_dir>/<profile>/` (sharable location), and splitting each schema into per-table files."
  - `mkdir -p "$artifacts_dir" && chmod 755 "$artifacts_dir"`
  - `mv "$v11_profile_dir" "$artifacts_profile_dir"`
  - `chmod 644` on `*.yaml` and `*.md` inside.
  - Then run the v1.2-needs-table-split path on the moved directory.
  - Also migrate `$v11_user_memory` if `<artifacts_dir>/USER_MEMORY.md` doesn't exist: `mv "$v11_user_memory" "$artifacts_dir/USER_MEMORY.md" && chmod 644 "$artifacts_dir/USER_MEMORY.md"`.

- **`v1.0-single-file`** — ancient single-file install.
  - Tell the user: "Upgrading your model to the new per-table layout (faster, sharable, supports cross-schema relationships). Backing up your old model and re-introspecting now (~30–90s)."
  - `mkdir -p "$artifacts_profile_dir" && chmod 755 "$artifacts_profile_dir"`
  - `mv "$v10_legacy_file" "$artifacts_profile_dir/_legacy.yaml.bak"`
  - Migrate `$v10_legacy_examples` and `$v11_user_memory` if present.
  - Force `$ARGUMENTS=reintrospect` so we re-introspect from the DB into the new layout.

- **`fresh`** — first install.
  - `mkdir -p "$artifacts_profile_dir" && chmod 755 "$artifacts_profile_dir"`
  - Continue to introspection.

In every migration case, surface a one-liner pointing at the new location: "Your semantic model lives at `<artifacts_dir>/<profile>/` now. Credentials and per-user state stay in `~/.agami/`. See [`shared/file-layout.md`](../../shared/file-layout.md) for the split."

Otherwise, continue to schema selection.

### Phase 1.2 — list schemas

Run the dialect-specific schema query from [`shared/introspect-queries.md`](../../shared/introspect-queries.md). For SQLite, skip this entirely and use the single implicit `main` schema.

Capture: list of schema names the connected user has access to.

Surface: `Found <K> schemas: <name1>, <name2>, …`

### Phase 1.3 — schema picker (multi-select)

For non-SQLite databases, ask the user which schemas to introspect. Skipping this step means the skill defaults to *every* schema, which is rarely what the user wants on a Snowflake account with 50+ schemas.

**AskUserQuestion** with multi-select:

> Which schemas should I introspect? (Pick one or more — I'll only build the model for what you select.)

Options:
- One option per discovered schema. Pre-check `public` (Postgres), the credentials' `database` (MySQL), or `PUBLIC` (Snowflake) by default.
- A top option `All schemas (Recommended for small DBs)` — useful when the user has only 2–3 schemas.
- A `Just <default> for now (skip the rest)` shortcut — useful when the user knows they only need one schema right now.

Record the selection: `selected_schemas := [...]`. The next phases (1a list tables, 1b per-table, 1c FK validation, 1d descriptions) constrain to these schemas only. Reintrospect later only touches the same schemas unless the user explicitly says "also introspect the `<x>` schema."

The selection is recorded in `index.yaml.schemas[]` (see Phase 2). Schemas not selected do NOT appear in `index.yaml`.

Run introspection. For every step, use the SQL from [`shared/introspect-queries.md`](../../shared/introspect-queries.md), executed via the chosen tool (psql / mysql / snowsql / sqlite3 / DuckDB / `execute_sql.py`):

### Phase 1.4 — collect a one-paragraph organization context

After the schema picker (1.3) but before the heavy per-table work, prompt the user once for domain context. Domain context boosts NL→SQL accuracy a lot — a 30-second ask that often pays for itself.

If `<artifacts_dir>/<profile>/ORGANIZATION.md` exists AND has been edited beyond the default template (any line longer than the template's parenthetical guidance), skip this phase.

Otherwise, **AskUserQuestion**:

> Want to give me a one-paragraph description of what this database is about? It improves NL→SQL accuracy a lot — without it I have to guess the domain from table/column names alone.
>
> Examples of useful context: what the company / product is, what "MRR" or "active user" means in your terms, what kinds of users / customers you have.

Options:
- `Yes — I'll type it now (Other field)` — capture the user's free-form paragraph and write it to `<artifacts_dir>/<profile>/ORGANIZATION.md` under a `# About this database` heading. Add the rest of the default template (terminology / who's in this data / what we don't track) as commented prompts the user can fill in later.
- `Skip — I'll edit ORGANIZATION.md later (Recommended)` — write only the default template (untouched) so the user knows where the file lives.

In both cases, write to `chmod 600`. Format and content rules: see [`shared/organization-context-format.md`](../../shared/organization-context-format.md).

### Phase 1.5 — optional: data-model document upload

Many users have an existing artifact describing their schema — an ERD, a data dictionary, a "what each table means" Confluence page. Feeding it to the description generator is a big lift on accuracy with zero extra introspect work.

Ask **once**, low-friction:

**AskUserQuestion**:

> Got a data-model document I can read for additional context? Things like an ERD diagram, data dictionary, schema doc, or anything else that explains what your tables are for.
>
> Drag-and-drop a file here, or paste a path. **PDF, PNG / JPG, plain text, markdown, or CSV** all work. For Excel or Word docs, save them as PDF first (File → Save As PDF) — it's the fastest way and works in every editor.

Options:
- `Yes — I'll attach it now (Other field)` — user pastes a path or drags a file into chat.
- `Skip — no doc to share` — proceed to introspection. (No `(Recommended)` marker — sharing a doc genuinely improves accuracy when you have one; it's a fact-of-environment question, not a default we'd push.)

If the user provides a path:

1. Use the `Read` tool against the path. Claude's `Read` handles PDFs (with `pages` for large files), images (multimodal — can see diagrams), markdown, plain text, and CSV natively. No format-specific parsing logic needed in the skill.
2. If the file is `.xlsx` / `.docx` / another binary office format, surface a one-liner: "I can't read `.xlsx` / `.docx` directly. Save it as PDF (File → Save As PDF) and re-attach, or paste the relevant content as text and I'll use it as-is." Don't block — proceed without the doc.
3. If the file is huge (> 50 pages PDF or > 100KB markdown), trim to a summary: read the first 20 pages, surface "Loaded the first 20 pages of <name>; let me know if there's a specific section I should focus on." For tabular data (CSV with > 200 rows), keep only the first 50 rows + the header.
4. Stash the loaded content in a working-memory variable: `$DATA_MODEL_DOC_TEXT` (or for images, the multimodal block).
5. **Phase 1d's description-generation prompt** receives this content under a labeled heading: `## User-provided data-model document` (placed BEFORE the schema's tables/columns/sample rows so the LLM treats it as a domain prior, similar to ORGANIZATION.md).

The doc is **never written to disk** — it lives only in the description-generation prompt's context, then is discarded. Same privacy posture as the per-table sample rows from Phase 1d.i.

If the user uploads but the file is unreadable (corrupted PDF, unreachable path, etc.), surface "Couldn't read `<path>` — proceeding without it. You can always re-introspect later if you want to retry." Don't block.

### 1a — list tables (within `selected_schemas` only)

Filter to the `selected_schemas` from Phase 1.3. (System schemas are already filtered by Phase 1.2's discovery query, so they're not in `selected_schemas`.)

For Postgres / Redshift:

```sql
SELECT table_schema, table_name
FROM information_schema.tables
WHERE table_type = 'BASE TABLE'
  AND table_schema IN (<selected_schemas>);
```

Adapt the `IN (...)` clause for MySQL / Snowflake / SQLite (SQLite always introspects the implicit `main` schema).

Surface: `Found <N> tables across <K> schema(s).`

### 1b — for each table, pull columns + PK + FK + row count + indexes

Use the per-dialect queries from [`shared/introspect-queries.md`](../../shared/introspect-queries.md).

For each column: capture `name`, `data_type` (raw DB type), nullability. Map to the simple OSI-extension type set (`string | integer | decimal | timestamp | date | boolean`) using the type mapping table at the bottom of `introspect-queries.md`. Keep the raw DB type as `agami.original_type`.

For each table:
- **Row count** from `pg_stat_user_tables` (Postgres) or `information_schema.tables.table_rows` (MySQL). Tables with > 100k rows get a `agami.performance_hints` extension; tables ≤ 100k don't need one.
- **Indexes** via the index-discovery query (Postgres `pg_indexes`, MySQL `information_schema.statistics`, Snowflake doesn't have traditional indexes — use clustering keys instead, SQLite `PRAGMA index_list`). Capture each index as a list of column names. Skip auto-generated PK indexes (the PK is already in `primary_key`). Persist as `agami.performance_hints.indexes: [[col1], [col2, col3], ...]`.
  - **Why we capture indexes**: the SQL generator in agami-query-database (Phase 2b) uses this to prefer indexed columns for `WHERE` filters and `JOIN` conditions on large tables. A query that filters on an indexed column runs in milliseconds; the same query on an un-indexed column scans the whole table. The LLM doesn't know which columns are indexed unless we tell it.
  - **For all tables, not just > 100k rows**: even on smaller tables, knowing which columns are indexed informs join planning. The `agami.performance_hints` extension is created for any table with indexes, even if `estimated_row_count` is small. (Earlier guidance was "only if > 100k" — overruling that here for indexes specifically.)

**Progress narration (Phase F):** print one line per table as it completes, so the user can see the skill working through their schema. Format:

```
[3/47] public.orders — 12 columns, 2 FKs (description: "Customer-facing orders…")
```

`<i>/<N>` is the table index across all selected schemas. For batched description generation (Phase 1d below), additionally print one line per batch:

```
[batch 2/3] generating descriptions for tables 51–100 in public…
```

Keep narration to ≤ 80 chars per line — long lines wrap in some hosts and look messy.

### 1c — FK validation (live join check)

Run the orphan-ratio query from [`shared/fk-validation.md`](../../shared/fk-validation.md) against every detected FK. Drop any with > 5% orphans. For each FK that survives, record the result as a `agami.fk_validation` extension on the resulting `relationships[]` entry.

If the database had **zero declared FKs**, run heuristic FK inference per `fk-validation.md` and ask:

> I detected N likely foreign-key relationships from column-name conventions:
> - `orders.customer_id` → `customers.id` (1 orphan in 2403 rows)
> - …
>
> Add these to the model?

AskUserQuestion: `Add all (Recommended)` / `Add only zero-orphan ones` / `Skip — let me edit by hand later`.

### 1c.5 — detect `agami.choice_field` (low-cardinality scan)

For each candidate column in each introspected dataset, run the low-cardinality detection query from [`shared/introspect-queries.md → Choice-field detection`](../../shared/introspect-queries.md#choice-field-detection-low-cardinality-scan). If the column has ≤ 20 distinct non-null values, capture them as a `choice_field` map under the field's `agami` extension.

**Candidate selection** (apply all):

- `agami.type` is `string` or `integer`
- Not in the dataset's `primary_key`
- Not in any FK's `from_columns` (foreign-key values aren't enums even when finite)
- Column name matches enum-y patterns (`status`, `state`, `type`, `kind`, `category`, `priority`, `tier`, `level`, `mode`, `flag`, `role`, `phase`, `stage`) **OR** the name is ≤ 32 chars and not in the free-text exclusion list (`name`, `description`, `notes`, `comment`, `body`, `content`, `email`, `address`, `url`, `path`, `slug`, `title`, `subject`, `message`)

**For tables with `estimated_row_count > 10_000_000`**, sample first per the introspect-queries doc — don't scan the full column.

**Output.** For each detected choice_field, write into the field's `custom_extensions` JSON:

```yaml
- name: status
  expression: { dialects: [{ dialect: ANSI_SQL, expression: status }] }
  custom_extensions:
    - vendor_name: COMMON
      data: '{"agami": {"type": "string", "choice_field": {"pending": "pending", "shipped": "shipped", "delivered": "delivered", "cancelled": "cancelled"}}}'
```

Display labels default to the stored value (`label = value`). Don't invent prettier labels — that's a `field_metadata` correction the user can apply later via agami-save-correction.

**Progress narration:** print one line per detected choice_field so the user sees what was found:

```
[choice_field] public.orders.status — 4 values: pending, shipped, delivered, cancelled
```

If a candidate column has > 20 distinct values, skip silently. Don't narrate misses.

### 1c.7 — propose `agami.performance_hints.recommended_filters` (the targeted-warning unlock)

Phase 2d in agami-query-database treats any query against an `estimated_row_count > 1_000_000` table as **HIGH risk** unless the WHERE clause matches a column listed in `recommended_filters`. Without that field populated, every query against every big table blocks with a "this is heavy, want to add a filter?" prompt — even queries that already have a perfectly good filter. That's blunt.

For each table with `estimated_row_count > 1_000_000`, propose a list of recommended filter columns. Heuristic candidates, in priority order:

1. **Primary key columns.** Equality on PK is always cheap.
2. **Indexed columns.** Already captured in Phase 1b's `agami.performance_hints.indexes` — every leading column of every index is a recommended filter (single-col equality OR range on a covered prefix). For composite indexes `(a, b, c)`, only `a` is index-eligible without `a` first.
3. **Time / date dimensions.** Any column with `agami.type` in `{date, timestamp}` AND a name suggesting recency (`created_at`, `updated_at`, `_at` suffix, `_date` suffix, `event_time`, `as_of`) — date-range filters are universally useful.
4. **Identity / FK columns.** Columns named like `*_id` (especially the customer/user/account/applicant kind) — common per-entity-narrowing filters.
5. **Choice-field columns from Phase 1c.5.** A `WHERE status='active'` is selective enough to count as a recommended filter for tables where `status` is a choice_field with ≤ 5 values.

Persist as `{column, kind, reason}` per [`shared/agami-osi-extensions.md`](../../shared/agami-osi-extensions.md):

```yaml
custom_extensions:
  - vendor_name: COMMON
    data: '{"agami": {"performance_hints": {"estimated_row_count": 134000000, "indexes": [["created_at"], ["payment_status"]], "recommended_filters": [{"column": "id", "kind": "equality", "reason": "primary key"}, {"column": "applicant_id", "kind": "equality", "reason": "FK to applicant — the natural per-entity narrowing"}, {"column": "report_date", "kind": "range", "reason": "indexed time dimension"}]}}}'
```

`kind` is machine-read by Phase 2d's risk classifier:
- `equality` — `WHERE col = ?` is the expected shape (PK, FK, choice_field).
- `range` — `WHERE col BETWEEN ? AND ?` or `WHERE col >= ?` is the expected shape (time/date columns).

`reason` is short free-form prose for humans hand-editing the schema yaml later — keep it under one line.

The Phase 2d risk classifier checks both `column` AND `kind` against the user's WHERE clause: an equality filter matches an `equality` entry, a range filter matches a `range`. A column queried by both `=` and `BETWEEN` is allowed — list it twice (one entry per kind).

**Why this matters for finbud-shaped schemas:** without recommended_filters, every query against a 134M-row credit-bureau table blocks the user with a HIGH-risk warning. With recommended_filters listing `applicant_id`, `pan`, and `report_date`, queries that filter on any of those are LOW risk and run silently. Queries that DON'T filter on any of those still warn — that's the targeted behavior.

**Cap at ~6 filters per table.** More than that and the list stops being a recommendation and becomes "everything's a filter" — the user should hand-edit if they need to add more.

Don't narrate per-table — just emit a single line per schema:

```
[recommended_filters] public — proposed filter columns on 8 of 47 tables (>1M rows). Validator will surface them in the schema yaml.
```

For tables with `estimated_row_count <= 1_000_000`, skip — no need to propose recommended_filters when the whole table fits in a few seconds of scan.

**Note on Snowflake clustering keys:** Snowflake has clustering keys (organize micro-partitions for cheaper range scans), which sound like indexes but behave differently — equality on a clustered column is no faster than on any other column once partitions are pruned. We deliberately do NOT capture clustering keys as a separate field; the validator's `agami.performance_hints` allowlist excludes `clustering_keys`. If clustering matters for a specific column, it shows up naturally as a `recommended_filters` entry with `kind: range`. Don't add a `clustering_keys` field — it would mislead the SQL generator into treating clustered columns like indexed ones.

### 1d — auto-generate descriptions (per-schema batched, evidence-grounded)

Generate a one-line `description` for **every table and every column** in the selected schemas. Without descriptions, NL→SQL quality drops sharply on large schemas — this pass is mandatory, not optional.

#### 1d.i — sample rows per table

For each table fetch up to 5 sample rows for evidence:

```sql
SELECT * FROM <schema>.<table> LIMIT 5;
```

Snowflake-only: for tables with `estimated_row_count > 10_000_000` use the `SAMPLE` clause to avoid scanning a huge prefix. See [`shared/introspect-queries.md`](../../shared/introspect-queries.md#sample-rows-for-phase-c).

The sample is **never written to disk and never sent in telemetry.** It lives in the description-generation prompt's context, then is discarded.

#### 1d.ii — per-schema batched generation

Process schemas one at a time. For each schema, build a prompt with:

- The user-provided data-model document from Phase 1.5 (`$DATA_MODEL_DOC_TEXT`, or multimodal image block if it's a diagram), if present — placed **first** so it acts as the dominant domain prior. Header: `## User-provided data-model document`.
- The user's `<artifacts_dir>/<profile>/ORGANIZATION.md` (if non-empty) — domain context for the database. Header: `## Organization context`.
- The schema's tables, columns (with types), FKs, choice-field hints
- The 5 sample rows per table

Ask the model to emit, for each table:
- A 1-line `description` summarizing what the table holds
- For each column, a 1-line `description` — but **skip if blindingly obvious from the column name** (e.g., `id`, `created_at`). Leave such columns' description empty.

#### 1d.iii — width bounding for large schemas

If a single schema has > 100 tables, batch within the schema: process **50 tables at a time**. Each batch sees the full column list + sample rows for *its* tables, but only summary names of the rest of the schema (for FK context).

Print one line per batch (Phase F narration):

```
[batch 2/4] Generated descriptions for tables 51–100 of public.
```

#### 1d.iv — validate-then-write per schema

After every schema completes, validate that schema's yaml as a standalone OSI doc:

```bash
python3 "$AGAMI_PLUGIN_ROOT/scripts/validate_semantic_model.py" "/tmp/agami-staging-<profile>/<schema>.yaml"
```

If a schema fails validation, the staging file stays at `/tmp/agami-staging-<profile>/<schema>.yaml`. Surface the errors and continue with the next schema — don't block the rest of the introspection. The user gets a single end-of-Phase-1 summary listing which schemas need attention. Phase 3 then runs `--directory` mode on the merged result.

#### 1d.v — what NOT to invent

- Don't invent column meanings for opaque names (`v_1`, `tmp_col`, `x`). Leave empty.
- Don't invent business semantics not supported by sample rows (e.g., don't claim a `status` column is "active vs cancelled" if the samples only show `pending`).
- Don't translate column names ("`amt`" → "amount") — keep descriptions about what the column *means*, not what it's *named*.
- The user can hand-edit any `<artifacts_dir>/<profile>/<schema>.yaml` and the changes will survive future re-introspections (Phase 2 hard rule #8 — preserve descriptions, ai_context, choice_fields, metrics).

### 1e — detect units (`agami.unit`) + currency ask

After descriptions but before metric suggestions, scan numeric fields for unit hints. Three categories of unit can be inferred or asked for:

#### 1e.i — auto-detect (no user prompt)

For each `decimal` or `integer` field, infer unit from:

- **Percent**: column name ends with `_pct`, `_percent`, `_rate`, `_ratio` AND sample values are between 0 and 100 (or 0 and 1). Set `agami.unit: "percent"`.
- **Duration**: column name ends with `_seconds`, `_sec`, `_secs`, `_ms`, `_milliseconds`, `_minutes`, `_min`, `_hours`, `_hrs`, `_days`. Set `agami.unit: "<seconds|ms|minutes|hours|days>"` matching the suffix.
- **Bytes**: column name ends with `_bytes`, `_kb`, `_mb`, `_gb`. Set `agami.unit: "<bytes|kb|mb|gb>"`.

These don't need user input — the unit is unambiguous from naming convention.

#### 1e.ii — currency ask (one prompt per profile)

For each numeric field whose name suggests a money amount — `amount`, `price`, `cost`, `revenue`, `total`, `subtotal`, `fee`, `tax`, `discount`, `paid`, `balance`, `salary`, `wage`, `payment`, `charge`, or anything ending in `_usd`, `_eur`, `_gbp`, etc. (the suffix is the answer; skip the prompt for those) — collect the field into a list of currency-candidates.

If the candidate list is non-empty AND there's no per-column suffix giving the answer, ask the user **once per profile** (not per column):

**AskUserQuestion**:

> I detected `<N>` numeric fields that look like money amounts: `<table.field>, <table.field>, ...`. **What currency are these in?**
>
> Pick one — I'll annotate every field with the right unit so charts and totals format correctly.

Options: `USD` / `EUR` / `GBP` / `JPY` / `INR` / `Other (Other field — e.g., AUD, CAD, CHF)` / `Mixed — different fields use different currencies, I'll edit by hand`

If the user picks a single currency, set `agami.unit: "<CURRENCY_CODE>"` (lowercase ISO 4217: `usd`, `eur`, `gbp`, etc.) on every detected money column. If "Mixed", skip — no auto-annotation, surface a one-liner ("OK, leaving currency fields unannotated. Edit by hand or save as a correction later.")

#### 1e.iii — record and continue

The unit annotation is part of `agami.unit` per the existing extension allowlist; no schema changes needed. Phase 4 (chart rendering) and Phase 3c (cell formatting) in agami-query-database already use `agami.unit` for currency / percent / duration formatting.

### 1f — suggest metrics (user-confirmed only — never auto-write)

agami-query-database treats `metrics[]` as canonical aggregations the user wants reused across queries (e.g., `total_revenue` is `SUM(orders.amount)` filtered to non-cancelled). Metrics drift fast across domains, so we don't auto-detect — but we do **suggest** plausible ones during introspection so the user can pick.

#### 1f.i — generate candidates

Per schema, propose **at most 4 candidate metrics** (cap is hard — AskUserQuestion's multi-select fits about 4 options + Other on one screen; more than that triggers the "Metrics / More metrics" tab split that confuses users into thinking they need to click another tab to confirm). Pick the highest-confidence 4 from these signals:

- Numeric fields named like aggregates (`amount`, `revenue`, `cost`, `quantity`, `count`) — propose a SUM metric and (if the field has multiple decimals) an AVG metric.
- Tables that look fact-shaped (have FKs to dimension tables, time field, numeric measures) — propose `count_<table>` (`COUNT(*) FROM <table>`) as a baseline metric.
- Time fields — for tables with timestamps, propose a `daily_<count|sum>_<x>` metric grouped by day, especially if the table looks high-traffic (`> 100k` rows).
- ORGANIZATION.md hints — if it defines vocabulary like "MRR" or "active user", propose a metric matching that definition.
- The user-uploaded data-model document (Phase 1.5) — if it lists KPIs by name, propose those.

For each candidate, include:
- A snake_case `name` (e.g. `total_revenue`, `count_orders`, `avg_order_value`)
- The aggregation expression in ANSI_SQL referencing `<dataset>.<field>`
- A 1-line description
- 2-3 synonyms

#### 1f.ii — confirm with the user, batch-style

**AskUserQuestion** with the candidate list as multi-select:

> I'd suggest adding these metrics to your model — they're reusable aggregations the skill will use whenever you ask about them by name (or synonym). Pick which ones make sense for your domain.

Options: one option per candidate (pre-checked when the candidate is grounded in ORGANIZATION.md or the data-model doc; un-checked otherwise). Plus `Other (Other field)` for "describe a metric I want — e.g. 'MRR = SUM(price) where plan=subscription'". The user types a free-form metric description and the skill drafts the SQL + adds it. **No "None / skip" option** — leaving every candidate unchecked and submitting is the implicit skip.

**Hard cap: 4 candidate options + Other (5 total).** Above that, the AskUserQuestion modal splits across tabs ("Metrics" / "More metrics") with a confusing "Already answered above" entry. If you've identified more than 4 high-confidence candidates, surface only the top 4 and tell the user inline: "I had 3 more metric ideas (`<name>`, `<name>`, `<name>`). Say 'add the X metric' anytime and I'll wire it via save-correction."

For each metric the user picks: write into the schema yaml's `metrics[]`. **Always include a draft `agami.definition_prose`** — without it the metric can never be approved later (Rule 1 of the trust layer requires non-empty prose for approval). Source the draft in this order:

1. Use the **column comment** of the metric's source column if present (DBA-authored — strongest signal).
2. Else use the **ORGANIZATION.md** definition if the metric's name matches a defined business term.
3. Else generate a one-sentence LLM draft from the metric name + expression + a sample of values (e.g., `"Sum of orders.amount_usd across all rows — total revenue in USD."`).

The user will edit this prose later via `agami-review` if it's wrong; the point is to ship something non-empty so the validator + the approval flow have a starting state.

Validate before write (the per-schema yaml must still pass OSI). If a metric fails validation (e.g., references a non-existent column), drop it silently and surface a one-liner: "Skipped `<name>` — couldn't validate against your model."

If the user picks "None", write nothing. They can add metrics later via agami-save-correction (`new_metric` correction kind).

#### 1f.iii — what NOT to suggest

- Don't suggest metrics that depend on choice_field literals you didn't detect (e.g., don't propose `MRR = SUM(price) WHERE plan='subscription'` if you never saw `plan='subscription'` in the choice_field detection).
- Don't suggest more than 4 candidates per schema — the AskUserQuestion splits across tabs above that. Pick the highest-confidence ones; mention the rest in chat prose for the user to add later via save-correction.
- Don't propose metrics that span multiple schemas in the multi-schema case unless `cross_schema_relationships` already wires the join. Cross-schema metrics belong in `index.yaml` (future) — for now, scope each metric to a single schema.

---

## Phase 2: Build the per-table OSI model

Output is a directory tree: `<artifacts_dir>/<profile>/` contains `index.yaml`, plus one subdirectory per schema. Each schema subdirectory contains `_schema.yaml` (slim TOC + within-schema relationships) and one `<table>.yaml` per table (full OSI doc for that table). The split lets the two-pass retrieval (Pass 1) read only `_schema.yaml` files for relevance picking, then lazy-load only the picked tables' yamls in Pass 2 — much smaller prompt for 1000+-table schemas.

### Per-table yaml shape (one file per table)

Each `<artifacts_dir>/<profile>/<schema>/<table>.yaml` is a **standalone OSI v0.1.1 document** with exactly one dataset:

```yaml
version: "0.1.1"

semantic_model:
  - name: <profile>
    custom_extensions:
      - vendor_name: COMMON
        data: '{"agami": {"profile": "<profile>", "db_type": "<db_type>", "schema": "<schema_name>", "table": "<table_name>"}}'

    datasets:
      - name: <table_name>                                # source table name verbatim
        source: <database>.<schema>.<table>               # ALWAYS three-part
        primary_key: [<col>, ...]
        unique_keys:
          - [<col>]
        description: <plain English or empty string>
        ai_context:
          synonyms: [...]
        fields:
          - name: <column_name>
            expression:
              dialects:
                - dialect: ANSI_SQL
                  expression: <column_name>
            dimension:
              is_time: <true if timestamp/date else false>
            description: <empty string is OK>
            custom_extensions:
              - vendor_name: COMMON
                data: '{"agami": {"type": "<simple_type>", "original_type": "<DB native type>"}}'
        custom_extensions:
          - vendor_name: COMMON
            data: '{"agami": {"performance_hints": {"estimated_row_count": <int>, "indexes": [["col1"], ["col1","col2"]]}}}'

    # No `relationships:` here — they live in <schema>/_schema.yaml.
    # No `metrics:` here either — single-table metrics live in the same dataset's
    # `custom_extensions` if measure-like; multi-table go in _schema.yaml; multi-
    # schema go in index.yaml.
```

`agami.schema` and `agami.table` at the model level **must match** the file's location — the validator rejects mismatches. Each table file MUST have exactly one dataset (one file per table).

### `<schema>/_schema.yaml` shape (agami-bespoke, slim TOC)

```yaml
version: "0.1.1"
schema: <schema_name>
description: <one-line schema role>
tables:
  - name: <table_name>
    file: <table_name>.yaml
    description: <one-line — what the table holds (used by Pass 1 retrieval)>
    primary_key: [<col>, ...]
    estimated_row_count: <int>           # optional, helps Pass 2 risk assessment
relationships:                             # within-schema only
  - name: <from>_to_<to>
    from: <table_name>                   # bare names — both must be in this schema's tables[]
    to: <table_name>
    from_columns: [<col>, ...]
    to_columns: [<col>, ...]
    custom_extensions:
      - vendor_name: COMMON
        data: '{"agami": {"fk_validation": {...}}}'
metrics:                                   # multi-table within this schema (optional)
  - name: <metric_name>
    expression: { dialects: [{ dialect: ANSI_SQL, expression: SUM(orders.amount) }] }
    description: <one-line>
```

`_schema.yaml` is **NOT OSI** — it's agami-bespoke. The validator has its own allowlist for it.

### `index.yaml` shape (top-level TOC)

```yaml
version: "0.1.1"
profile: <profile>
db_type: <db_type>
schemas:
  - name: <schema_name>
    file: <schema_name>/_schema.yaml      # path to the schema's TOC, NOT a single yaml
    table_count: <int>
    description: <one-line schema summary or empty>
cross_schema_relationships:                # relationships that span schemas
  - name: <from_schema>_<from_table>_to_<to_schema>_<to_table>
    from: <from_schema>.<from_dataset>    # qualified
    to: <to_schema>.<to_dataset>          # qualified
    from_columns: [<col>, ...]
    to_columns: [<col>, ...]
    description: <optional>
introspect_meta:
  introspected_at: <ISO>
  tier: <cli|duckdb|python>
  source_db_version: <version string>
```

The `file` field for each schema is `<schema>/_schema.yaml` (path with subdirectory) in v1.3, replacing the v1.2 `<schema>.yaml` (top-level single file). The validator detects layout from this path and dispatches accordingly.

**Where each kind of metadata lives:**

| Metadata | Lives in | Why |
|---|---|---|
| One table's columns / indexes / FKs / row count | `<schema>/<table>.yaml` (datasets[0]) | Per-table — load only when the table is picked |
| Within-schema relationships | `<schema>/_schema.yaml` | Span tables in one schema |
| Cross-schema relationships | `index.yaml.cross_schema_relationships[]` | Span schemas |
| Single-table metrics (e.g. `SUM(orders.amount)`) | `<schema>/<table>.yaml` (model-level metrics or measures via custom_extensions) | Aggregates one table |
| Multi-table-within-schema metrics | `<schema>/_schema.yaml.metrics[]` | Need the schema's relationships to compute |
| Multi-schema metrics | `index.yaml.metrics[]` (future) | Need cross-schema relationships |
| Per-database (profile-wide) settings | `index.yaml.introspect_meta` | One per profile |
| User preferences (any DB) | `<artifacts_dir>/USER_MEMORY.md` | Cross-database |
| Domain context (this DB) | `<artifacts_dir>/<profile>/ORGANIZATION.md` | Per-database, free-form |

### Hard rules when building

1. **Every field must have an `expression.dialects[]` with at least one entry.** Even for plain column references — write `expression: { dialects: [{ dialect: ANSI_SQL, expression: <column_name> }] }`. No exceptions.
2. **`agami.type` is mandatory** on every field. If the DB native type is exotic and you can't map it, default to `string` and put the original in `agami.original_type`.
3. **Relationships are top-level** under the model. Never nest them inside datasets. Each one needs a unique `name`. Within-schema: `<from>_to_<to>` (suffix with `_<col>` if multiple FK pairs share endpoints). Cross-schema: include the schema names: `<from_schema>_<from>_to_<to_schema>_<to>`.
4. **`from_columns` and `to_columns` MUST have the same length.** Composite keys are arrays.
5. **`source` must be three-part dotted notation.** `database.schema.table` — never bare table name. For sqlite use `file_basename.main.<table>`.
6. **Don't invent `custom_extensions` keys.** Only emit the keys documented in [`shared/agami-osi-extensions.md`](../../shared/agami-osi-extensions.md). Adding a new key requires updating that doc + the validator's allowlist + a test.
7. **Dataset name uniqueness within a schema.** The validator merges all `<schema>/<table>.yaml` files in a schema and rejects duplicates. (Across schemas, duplicate table names ARE allowed in v1.3 because the qualified `<schema>.<table>` is the addressable name.)
8. **Each `<table>.yaml` MUST have exactly one dataset.** The validator rejects multi-dataset table files. If you need a synthetic view that combines tables, that's a metric (model-level) or a separate yaml the user hand-creates — not something Phase 2 generates.
9. **`agami.schema` and `agami.table` at the model level must match the file's location.** Validator-enforced. The mapping `<schema>/<table>.yaml` ↔ `agami.schema = "<schema>"` and `agami.table = "<table>"` is the only valid combination.
10. **Reintrospect preserves hand-edits.** When `$ARGUMENTS == reintrospect` and existing `<artifacts_dir>/<profile>/<schema>/<table>.yaml` files exist:
    - Read each existing table yaml.
    - For each existing field: keep its `description`, `ai_context`, and any `agami.choice_field` / `agami.unit` extensions. Refresh only `agami.type` / `agami.original_type` from the DB.
    - For each existing dataset: keep its `description`, `ai_context`. Refresh `agami.performance_hints` from the DB.
    - For each `_schema.yaml` relationship: keep it as-is if both endpoints still exist. Drop if the underlying FK is gone.
    - Keep all existing `metrics[]` entries (per-table or per-schema) — user-authored, never lose them.
    - For `index.yaml.cross_schema_relationships`: same preservation rules.
    - **Trust-layer fields preserved.** For every entry whose `agami.review_state` is `approved` or `rejected` in the existing yaml, keep the full trust block (`confidence`, `signal_breakdown`, `review_state`, `origin`, `signed_off_*`) verbatim — human review is never lost on reintrospect. New entries (those that didn't exist before) get fresh trust blocks per Phase 2c. Entries whose underlying schema element changed (column type, FK target, etc.) get their `agami.review_state` flipped to `stale` (drift signal), preserving prior `signed_off_*` for audit.
11. **Trust-layer fields on every entry.** Every dataset, field, and relationship MUST carry the universal trust-layer keys (`confidence`, `signal_breakdown`, `review_state`, `origin`, `signed_off_by`, `signed_off_at`, `signed_off_role`) inside its `custom_extensions[].vendor_name=COMMON` agami payload. The validator rejects any entry without them. See Phase 2c below for how to compute the values.

---

## Phase 2c: Confidence and auto-approve (the trust spine)

Before promoting any yaml from staging, every dataset / field / relationship gets a **trust block** populated. The trust spine has two pieces:

1. **Compute a confidence number** in `[0, 1]` from the signals the introspect step already collected.
2. **Apply auto-approve rules** — entries with strong-enough provenance flip `review_state` straight to `approved` with `signed_off_by: agami_introspect_v1`. Everything else stays `unreviewed`.

This is what makes the trust marketing claim defensible: every join is either FK-derived (auto-approved) or human-approved later via the review dashboard. Nothing slips through silently.

### 2c.1 — compute confidence per entity

Use [`plugins/agami/scripts/compute_confidence.py`](../../scripts/compute_confidence.py) — one pure function per entity type:

- `confidence_for_join(...)` — for each relationship in `_schema.yaml` and every cross-schema relationship in `index.yaml`
- `confidence_for_field_description(...)` — for every field with a non-empty `description` (hand-edits, DBA column comments, or LLM-generated descriptions)
- `confidence_for_metric(...)` — for any future-proposed metrics (Phase 2 currently doesn't auto-propose metrics; metrics that exist were hand-authored and stay `human_authored` / `approved` if they were already approved)
- `confidence_for_named_filter(...)` — same — named filters aren't auto-proposed in v1

The signal inputs come from data the introspect step already collects:

| Signal | Where it comes from |
|---|---|
| `fk_declared` | `information_schema.table_constraints` rows where `constraint_type = 'FOREIGN KEY'` |
| `pk_overlap` | both endpoints listed as PK in `information_schema.table_constraints` |
| `unique_index_match` | the target column has a unique index (`pg_indexes` for postgres, `SHOW INDEXES` for mysql) |
| `column_type_match` | `data_type` matches between source and target columns |
| `column_name_similarity` | Jaccard similarity over the columns' name tokens |
| `plural_pattern_match` | `<table>.<col>` matches `<plural-of-target-table>.<col>` (heuristic) |
| `dba_column_comment` | `pg_description` / `INFORMATION_SCHEMA.COLUMNS.COMMENT` is non-empty |
| `business_term_match` | column name appears in the introspect dictionary (`status`, `email`, `country`, `name`, etc.) |
| `enum_like_distribution` | sampled distinct values are ≤ 50 short strings (already used today for `choice_field`) |

If a signal isn't observable for a given DB type (e.g., SQLite has no FK metadata when foreign_keys pragma is off; MySQL has no column comments unless the DBA wrote them), pass `False` for that signal — `compute_confidence.py` clamps appropriately.

Invocation pattern (pseudocode — Claude composes the actual YAML):

```python
# For a relationship between orders.customer_id → customers.id:
score, signal_breakdown = confidence_for_join(
    fk_declared=True,
    pk_overlap=False,
    unique_index_match=True,
    column_type_match=True,
    column_name_similarity=0.92,
    plural_pattern_match=True,
)
# score ≈ 0.95 (typical FK-declared join with corroborating signals)
```

### 2c.2 — auto-approve rules (the queue-shrinkers)

These produce `review_state: approved` upfront, with `signed_off_by: agami_introspect_v1`, `signed_off_role: system`, `signed_off_at: <UTC ISO8601 of introspect run>`. Do NOT use auto-approve for `metrics` or `named_filters` — those are Rule 1 and require human sign-off. Joins / field descriptions / dataset metadata can auto-approve when:

- **FK declared** in DB metadata → relationship auto-approved (`origin: fk`)
- **DBA-authored column comment** present → field description auto-approved (`origin: column_comment`)
- **`agami.type` derived from SQL column type** → field auto-approved (`origin: introspect_heuristic`, but the type itself is mechanical so it's safe)
- **Single-column unique index + plural-of-table-name pattern + column type match** → join auto-approved (`origin: introspect_heuristic`, supplementary auto-approve case)

Anything else that doesn't auto-approve gets `review_state: unreviewed`, `signed_off_by: null`, `signed_off_at: null`, `signed_off_role: null`.

### 2c.3 — origin enum (which path produced this entry)

Pick exactly one:

- `fk` — derived from FK metadata in the source DB
- `introspect_heuristic` — derived from name-similarity / unique-index-match / etc.
- `column_comment` — derived from a DBA-authored column comment
- `llm_suggested` — proposed by an LLM during introspect (e.g., generated description) with no stronger signal
- `human_authored` — written by a human (e.g., preserved from a prior reintrospect, or hand-edited)

### 2c.4 — example: a relationship with the trust block filled in

```yaml
- name: orders_to_customers
  from: orders
  to: customers
  from_columns: [customer_id]
  to_columns: [id]
  custom_extensions:
    - vendor_name: COMMON
      data: '{"agami": {"fk_validation": {"validated_at": "2026-05-10T14:23:11Z", "orphan_count": 0, "total_rows": 4213, "orphan_ratio": 0.0}, "confidence": 1.0, "signal_breakdown": {"fk_declared": true, "pk_overlap": true, "unique_index_match": true, "column_type_match": true, "column_name_similarity": 0.95, "plural_pattern_match": true, "llm_inferred": false}, "review_state": "approved", "origin": "fk", "signed_off_by": "agami_introspect_v1", "signed_off_at": "2026-05-10T14:23:11Z", "signed_off_role": "system"}}'
```

### 2c.5 — example: a field description below threshold (review queue)

```yaml
- name: status
  expression:
    dialects:
      - dialect: ANSI_SQL
        expression: status
  description: "Customer lifecycle status (active / churned / trial / inactive)"
  custom_extensions:
    - vendor_name: COMMON
      data: '{"agami": {"type": "string", "choice_field": {"active": "Active", "churned": "Churned", "trial": "Trial", "inactive": "Inactive"}, "confidence": 0.55, "signal_breakdown": {"dba_column_comment": false, "business_term_match": true, "enum_like_distribution": true, "llm_inferred": true}, "review_state": "unreviewed", "origin": "llm_suggested", "signed_off_by": null, "signed_off_at": null, "signed_off_role": null}}'
```

---

## Phase 3: Validate, then write

This phase is the keystone. **No file is ever written to `<artifacts_dir>/<profile>/` without the directory-mode validator passing.**

### 3a — stage the directory tree

Stage the full directory tree at `/tmp/agami-staging-<profile>/` (a fresh directory), then run the validator. Never touch `<artifacts_dir>/<profile>/` until validation passes.

```bash
staging="/tmp/agami-staging-$profile"
rm -rf "$staging" && mkdir -p "$staging"
# Write index.yaml at the top, then for each schema:
#   mkdir "$staging/<schema>"
#   write "$staging/<schema>/_schema.yaml"
#   write "$staging/<schema>/<table>.yaml" for each table
python3 "$AGAMI_PLUGIN_ROOT/scripts/validate_semantic_model.py" --directory "$staging"
```

### 3b — handle the result

- **Exit 0** (PASSED): atomically promote the staging directory to `<artifacts_dir>/<profile>/`.
  ```bash
  rm -rf "$artifacts_dir/$profile.tmp_old" 2>/dev/null
  if [ -d "$artifacts_dir/$profile" ]; then
    # Preserve ORGANIZATION.md and examples.yaml from the existing dir on reintrospect.
    cp -p "$artifacts_dir/$profile/ORGANIZATION.md" "$staging/" 2>/dev/null || true
    cp -p "$artifacts_dir/$profile/examples.yaml"   "$staging/" 2>/dev/null || true
    mv "$artifacts_dir/$profile" "$artifacts_dir/$profile.tmp_old"
  fi
  mv "$staging" "$artifacts_dir/$profile"
  chmod 755 "$artifacts_dir/$profile"
  find "$artifacts_dir/$profile" -type d -exec chmod 755 {} +
  find "$artifacts_dir/$profile" -type f \( -name '*.yaml' -o -name '*.md' \) -exec chmod 644 {} +
  rm -rf "$artifacts_dir/$profile.tmp_old"
  ```
  Surface: `✓ Validator passed. Wrote <artifacts_dir>/<profile>/ (<K> schemas, <N> tables, <M> fields, <R> relationships).`

- **Exit 1** (FAILED): surface the validator's error list verbatim. **Do NOT promote the staging directory.** Tell the user "I built a model but it failed OSI validation. Here's what's wrong: …" and offer to attempt a fix or stop. Re-validate after every edit until clean. The staging dir remains at `/tmp/agami-staging-<profile>/` for inspection.

- **Exit 2** (TOOLING ERROR — missing dependencies, missing schema): surface the error and ask the user to install `pyyaml` and `jsonschema`.

### 3c — never bypass

If the validator can't be run for any reason (missing Python, missing dependencies, missing schema file), **DO NOT PROMOTE THE STAGING DIRECTORY**. Tell the user the validator is unavailable and offer to install the dependencies. The files in `<artifacts_dir>/<profile>/` are the source of truth for every future query — a broken model breaks every query that follows.

### 3d — snapshot for reproducibility

After promotion succeeds, freeze the model under `.snapshots/<model_version>/`. The `model_version` is a 12-char content hash; the **directory name itself is the canonical version pin** — we don't stamp it into `index.yaml` (the OSI extension allowlist doesn't include it, and there's no need: the snapshot dir name is the source of truth, the receipt reads it at query time).

```bash
# Compute model_version: SHA-256 of every yaml file's content, sorted by relative path.
# This is a content hash — identical introspects produce identical hashes (idempotent
# snapshots), and any change to any yaml produces a new hash.
model_version=$(
  cd "$artifacts_dir/$profile"
  find . -type f \( -name '*.yaml' -o -name '*.md' \) \
    ! -path './.snapshots/*' ! -path './.git/*' \
    | LC_ALL=C sort \
    | xargs sha256sum \
    | sha256sum | cut -d' ' -f1 | head -c 12
)

# Snapshot the directory under .snapshots/<model_version>/ (immutable copy).
mkdir -p "$artifacts_dir/$profile/.snapshots/$model_version"
rsync -a --exclude '.snapshots' --exclude '.git' \
  "$artifacts_dir/$profile/" "$artifacts_dir/$profile/.snapshots/$model_version/"
chmod -R a-w "$artifacts_dir/$profile/.snapshots/$model_version"
```

Surface: `✓ Snapshot saved at .snapshots/<model_version>/ — query receipts pin this version.`

**Do NOT add `model_version` as a field inside `index.yaml.introspect_meta`** — that's not in the OSI agami-extension allowlist (see [`shared/agami-osi-extensions.md`](../../shared/agami-osi-extensions.md) → `agami.introspect_meta`), and adding it would fail validation. The receipt builder in agami-query-database reads the version directly from the `.snapshots/` directory listing — see Phase 4e.iii.5 of agami-query-database for the lookup pattern.

### 3e — code-as-artifact: git init + commit

The model is files. Diffable, PR-able, git-blame-able is the trust property — *the dashboard is a view; YAML is canonical*. On first introspect, init a git repo at `<artifacts_dir>/<profile>/` so every change a curator makes via the dashboard creates a traceable diff.

```bash
profile_dir="$artifacts_dir/$profile"
if [ ! -d "$profile_dir/.git" ]; then
  ( cd "$profile_dir" && git init -q -b main ) || true
  cat > "$profile_dir/.gitignore" <<'EOF'
# Internal — don't commit ephemeral state
.snapshots/
EOF
fi

# Stage and commit (best-effort — never block the introspect on git failure).
( cd "$profile_dir" && git add -A && git -c user.name="agami" \
  -c user.email="agami_introspect_v1@local" \
  commit -q -m "introspect: $profile @ $model_version" --allow-empty ) || true
```

`.snapshots/` is gitignored — snapshots are local-only audit history, not source-controllable content. The user can `cd <artifacts_dir>/<profile> && git log` to see every model change over time.

If the user already committed the directory to a wider repo (e.g., they `git init`'d their whole `~/agami-artifacts/` for cross-profile tracking), skip the per-profile init and just commit there. Detect via `git rev-parse --show-toplevel` from `$profile_dir`.

Surface: `✓ Committed to <profile_dir>/.git as <short-sha>.` (Or: `✓ Committed to existing repo at <toplevel>.`)

---

## Phase 4: Seed prompt examples

Generate **10–15** NL→SQL examples covering this distribution. The bias is intentionally toward multi-table joins — that's where NL→SQL gets hard, and seed examples covering 3- and 4-table joins lift answer quality on real questions far more than another COUNT(*) example.

| # | Pattern | Min tables | Example shape |
|---|---------|---|---------------|
| 1 | Count rows | 1 | "How many orders are there?" |
| 2 | Filter + count | 1 | "How many orders are still pending?" |
| 3 | GROUP BY | 1 | "Orders by status" |
| 4 | Date range | 1 | "Orders placed last month" |
| 5 | Top N (single table) | 1 | "Top 5 statuses by order count" |
| 6 | JOIN (2 tables) | 2 | "Total spend per customer" |
| 7 | JOIN (3 tables) | 3 | "Top 10 products by revenue" |
| 8 | **JOIN (3 tables) + filter** | **3** | **"Top 10 customers by spend on shipped orders this quarter"** |
| 9 | **JOIN (4 tables)** | **4** | **"Revenue per category per region last 90 days"** (orders → order_items → products → categories) |
| 10 | **JOIN (4 tables) + GROUP BY two dimensions** | **4** | **"Order count by customer-segment and product-category last quarter"** |
| 11 | Boolean filter | 1 | "Active customers only" |
| 12 | Aggregate | 1 | "Average order size" |
| 13 | Combined (filter + JOIN + GROUP BY + ORDER BY) | 2-3 | "Top 5 active customers by spend last 30 days" |

**Hard rule: at least 3 examples must touch ≥ 3 tables, and at least 1 must touch ≥ 4 tables** — when the user's schema supports it (i.e., the relationships graph is deep enough). If the schema only has 2 tables connected by FKs, skip patterns 7-10 and document why in the staging log: "schema only has 2 connected tables; skipped multi-join examples".

Skip patterns that don't fit the user's schema (e.g., no time field → no "last month" example; no boolean column → no #11). The 3- and 4-table patterns require enough relationships in the graph to traverse — use the relationships from Phase 1c when picking which tables to join.

### 4a — generate

First, **load `<artifacts_dir>/USER_MEMORY.md`** (strip HTML comments) and **`<artifacts_dir>/<profile>/ORGANIZATION.md`** (same — strip HTML comments). USER_MEMORY holds cross-database preferences; ORGANIZATION.md holds domain context for *this* database. Both improve seed-example quality.

For each example:
- Build `(question, sql)` using the model from Phase 3.
- Reference fields by their **OSI dataset.field name** (which equals the DB column name in the simple introspect case).
- Use SQL safety rules from [`shared/sql-generation-rules.md`](../../shared/sql-generation-rules.md) and dialect-specific syntax from [`shared/dialect-rules.md`](../../shared/dialect-rules.md).
- **Apply USER_MEMORY policies.** If USER_MEMORY says "exclude test users where email matches @example.com", every seed example that touches `customers` includes that filter. If it says "default time window: last 30 days", date-relevant examples honor that default.
- **Apply ORGANIZATION.md domain vocabulary.** If ORGANIZATION.md defines "active user = signed in in the last 30 days", any "active user" example uses that definition.

### 4b — EXPLAIN-validate each

Before adding to the YAML, run `EXPLAIN <sql>` (or `EXPLAIN QUERY PLAN <sql>` for SQLite) via the chosen tool. If EXPLAIN fails:
1. Read the error through [`shared/db_error_classifier.md`](../../shared/db_error_classifier.md).
2. Make ONE auto-fix attempt (typically a column-name typo or missing alias).
3. If still failing, move that example to `~/.agami/.rejected/` (with the error) and continue. Don't block.

### 4c — write `<artifacts_dir>/<profile>/examples.yaml`

This file is **NOT OSI** — it's an agami-bespoke few-shot library. Format:

```yaml
# <artifacts_dir>/<profile>/examples.yaml
# NL → SQL few-shot examples loaded by the agami-query-database skill.
# Corrections appended by /agami-save-correction.

examples:
  - question: How many orders are there?
    sql: SELECT COUNT(*) AS order_count FROM orders
    source: seed
    created_at: 2026-05-06T12:00:00Z

  - question: Orders by status
    sql: |-
      SELECT status, COUNT(*) AS count
      FROM orders
      GROUP BY status
      ORDER BY count DESC
    source: seed
    created_at: 2026-05-06T12:00:00Z
```

`source` is `seed` here, `correction` for entries added by `/agami-save-correction`. The query-database skill loads at most 50 most-recent.

Surface: `✓ Generated <N> examples (<R> rejected, see ~/.agami/.rejected/). Saved to <artifacts_dir>/<profile>/examples.yaml.`

---

## Phase 5: Validate every seed example (the trust onboarding)

Earlier versions of this skill picked ONE seed example as a demo. Replaced — three independent sets of early-adopter feedback (Sourav + Intuit + Asana) said the same thing: "let me validate the queries you've inferred, not just see one of them work." Validating all 10–15 seeds in a single guided pass is what turns LLM-generated guesses into golden truths the query skill can trust.

**Output of this phase:** an HTML dashboard at `~/.agami/examples-validation/<ts>.html` listing every seed example with its question, SQL, and a 5-row result preview, plus a chat back-channel for the user to validate / reject / edit each one.

### 5a — Run every seed example

Read `<artifacts_dir>/<profile>/examples.yaml`. For each example:

1. Execute the SQL via the same tool used by agami-query-database (psql / mysql / snowsql / sqlite3 / DuckDB / `execute_sql.py`). **Add a `LIMIT 5` (or dialect-specific equivalent) wrapped via a CTE for the row preview** — but ALSO capture the full `row_count`. Pseudo:
   ```sql
   -- For the count:
   SELECT COUNT(*) FROM (<original_sql>) sub;
   -- For the preview:
   SELECT * FROM (<original_sql>) sub LIMIT 5;
   ```
   (For SQLite/Postgres/Redshift/MySQL this works. For Snowflake, use the same pattern with `LIMIT 5`.)
2. Capture: `row_count`, `row_headers` (column names from the result), `row_preview` (up to 5 rows as arrays of stringified values).
3. If the SQL fails: capture the one-line error from the db_error_classifier; set `error: <message>`. Do NOT block — broken examples become `state: error` cards in the dashboard, the user can fix them via `edit N`.

Run all examples sequentially (parallel execution risks overloading small DBs and is hard to attribute errors). Surface a one-line progress note: `Running 12 seed examples…`. Total time scales with example count × per-query latency — typically 30–60s for 12 examples on a small DB.

### 5b — Build the items JSON for the dashboard

For each example, build:

```json
{
  "n": <1-indexed display number>,
  "question": "<NL question>",
  "sql": "<SQL string, multi-line OK>",
  "state": "<state from examples.yaml: unreviewed | validated | rejected>",
  "row_count": <int>,
  "row_headers": ["col1", "col2", ...],
  "row_preview": [["v1", "v2", ...], ...],
  "validated_by": "<email or null>",
  "validated_at": "<ISO or null>",
  "error": "<error message or null>"
}
```

Write the array to `/tmp/agami-examples-items-<ts>.json`. The exact schema is documented in [`shared/examples-validation-template.html`](../../shared/examples-validation-template.html) → `ITEMS_JSON`.

### 5c — Render the dashboard

```bash
ts=$(date +%Y%m%d-%H%M%S)
mkdir -p ~/.agami/examples-validation
python3 "$AGAMI_PLUGIN_ROOT/scripts/render_examples_validation.py" \
  --title "Seed examples · $profile" \
  --profile "$profile" \
  --items-file "/tmp/agami-examples-items-$ts.json" \
  --out "$HOME/.agami/examples-validation/$ts.html"
rm -f "/tmp/agami-examples-items-$ts.json"
```

Surface in chat (single block):

```
Validating <N> seed examples — dashboard rendered:
  ~/.agami/examples-validation/<ts>.html

The dashboard has 3 tabs (For Validation · Validated · Rejected) and
click-to-act buttons on each card. Click Validate / Reject / Edit on
the items you want, then hit "Generate feedback for Claude" at the
bottom and paste the result back here.

You can also type commands directly:
  validate N           (or `validate 1, 3, 5`)
  validate all         (bulk — skips errored examples)
  reject N
  edit N               (I'll prompt for the new SQL in chat)
  done
```

Auto-open with the same multi-command fallback chain as agami-query-database Phase 4e.vi (`open` → `xdg-open` → `start` → fall through with the path printed). End the turn here.

### 5d — Chat back-channel grammar

The user replies with one or more commands. Commands can come from the dashboard's "Generate feedback for Claude" button (newline-separated block) or be typed directly. Same grammar either way.

- **`validate N`** (or `validate N, M, …` / `validate N, M by you@example.com`) — for each item, set `state: validated`, `validated_at: <UTC ISO>`, and `validated_by` sourced in this order (stop at the first hit):
  1. **The chat command** itself if it includes `by <email>` — use it.
  2. **`~/.agami/.config`'s `reviewer_email`** if present.
  3. **Otherwise, ask the user once**: *"What's your email? I'll save it to `~/.agami/.config.reviewer_email` so future validations and Rule 1 approvals don't re-ask."* Validate the email shape, persist to `.config` (merge — don't overwrite `tier`, `host`, etc.). See the same persistence block in [`agami-review/SKILL.md → Phase 3a`](../agami-review/SKILL.md#3a--validate-the-command). Never infer from `git config`, environment, or credentials — that path produces silent inconsistency.
- **`validate all`** — bulk-set every non-errored example to `validated`. Skip errored ones; surface the count: *"Validated 9 examples; 3 had errors and stay unreviewed (use `edit N` to fix)."*
- **`reject N`** — set `state: rejected`. Don't delete the example from the YAML (preserves audit trail).
- **`edit N`** — interactive form: surface the example's current SQL in chat, accept the user's edit conversationally, write back, re-execute, update the dashboard.
- **`edit N sql>>>` ... `<<<`** — inline form (dashboard generates this when the user clicks Edit + Save edit in the textarea on the page). Multi-line block:
  ```
  edit 8 sql>>>
  SELECT customer_id, SUM(amount) AS total
  FROM orders
  WHERE status = 'shipped'
  GROUP BY customer_id
  <<<
  ```
  Parser: see a line matching `edit N sql>>>` (case-sensitive, exact closing token `<<<` on its own line); read every line after it until `<<<`; that's the new SQL. Write it back to the example's `sql` field, re-EXPLAIN-validate via the chosen tool, re-execute, refresh the dashboard's row preview. **Apply the same SQL-safety checks as Phase 4b** (refuse DDL/DML, refuse system tables) — broken edits leave the example as `state: error` and the dashboard surfaces the error in the next render.
- **`note N >>>` ... `<<<`** — separate from edit. For comments / formatting hints / context that isn't a SQL rewrite. Multi-line block:
  ```
  note 4 >>>
  Format counts with commas — applies to every result, not just this example.
  <<<
  ```
  Parser: same pattern as edit (token `note N >>>`, closing `<<<` on its own line, body in between).

  **Where to write the note** — classify by the note's content:
    - **Cross-cutting preference** (mentions formatting, defaults, "always", "every", "from now on") → append to `<artifacts_dir>/USER_MEMORY.md` under a `## Preferences` section. This is the cross-database file applied to every query.
    - **Per-example commentary** (specific to this example only — e.g., "this counts active users only, excluding trials") → append to the example's entry in `examples.yaml` as a new `notes:` array. Append rather than overwrite if a `notes` array already exists.
    - **Per-database / domain context** (mentions a metric definition, a business term, a table-meaning clarification specific to this DB) → append to `<artifacts_dir>/<profile>/ORGANIZATION.md` under a `## Notes from review` section.

  If the note's classification is ambiguous, default to `USER_MEMORY.md` (the broadest scope) and surface a one-liner: *"Saved to USER_MEMORY.md (applies across every database). Reply 'move that to ORGANIZATION.md' if it's specific to this DB."*

  Notes do NOT mutate the example's `state` — they're additive context. The user can `validate N` and `note N` in the same batch.
- **`add example: <question>` followed by a `sql>>>` ... `<<<` block** — a new user-authored example to append to `examples.yaml`. The dashboard generates this when the user clicks "+ Add example" in the sticky footer. Multi-line block:
  ```
  add example: How many invoices are still unpaid?
  sql>>>
  SELECT COUNT(*) AS unpaid_count
  FROM invoices
  WHERE status != 'paid'
  <<<
  ```
  Parser: see a line starting with `add example:` — capture the rest of the line as the **question**. The next non-empty line MUST be `sql>>>`; everything after it until `<<<` on its own line is the **SQL**.

  Validation: EXPLAIN-validate the SQL against the live DB before writing (same safety checks as Phase 4b — refuse DDL/DML, refuse system tables). If EXPLAIN fails, surface the one-line error and **don't write the example**. The user can resubmit a fixed version in the next batch.

  On success: append a new entry to `<artifacts_dir>/<profile>/examples.yaml`:
  ```yaml
  - question: <question text verbatim>
    sql: |-
      <SQL with original indentation>
    source: manual          # NEW source value — distinguishes from seed/correction
    created_at: <UTC ISO>
    created_by: <reviewer_email from ~/.agami/.config, or "<unknown>">
    state: unreviewed        # The user can validate it on the next render
  ```
  Multiple `add example:` blocks in one batch are fine — process each independently. The next dashboard re-render shows the new examples with their assigned `n` (continues the existing numbering — appended at the end).
- **`done`** — close the session. Surface `✓ Validation complete: <V> validated, <R> rejected, <U> unreviewed, <A> added.` and continue to Phase 5.5.

For each successful edit, the user is also offered: *"Promote this to a golden test in `tests.yaml`? (yes / no / skip)"*. If yes → append a new test entry to `<artifacts_dir>/<profile>/tests.yaml` with the same question + an `equals` assertion against the actual returned value(s). This is the bridge to the Quality-Loop launch's `agami test`.

### 5e — Re-render after each batch of edits

After applying a batch of validate / reject / edit commands, **always re-render the dashboard to a NEW timestamped file** at `~/.agami/examples-validation/<new-ts>.html`. Don't overwrite the previous file — the user may have the old tab open and you need them to notice the fresh state. Numbering stays stable (don't renumber after rejects — the chat history references specific Ns).

**Auto-open the new file on every re-render** (same multi-command fallback chain as Phase 5c — `open` → `xdg-open` → `start` → `cmd /c start` → echo the path). The user gets a new browser tab with the fresh state; the previous tab is now stale and can be closed.

**Surface the new file path in the chat ack** so the user can't miss the refresh:
```
✓ Applied: validated 3 (#1, #3, #5), rejected 1 (#7). Re-rendered.

Open: ~/.agami/examples-validation/<new-ts>.html
(Previous tab is stale and can be closed.)
```

Then end the turn. Wait for the user.

If the queue is fully reviewed (no `unreviewed` items remain) OR the user types `done`, surface:
```
✓ Validation complete: <V> validated, <R> rejected, <U> unreviewed (errors).
```
…and continue to Phase 5.5 (the trust-layer summary).

### 5f — examples.yaml schema additions

Each example entry now supports trust-layer-style state:

```yaml
examples:
  - question: How many orders are there?
    sql: SELECT COUNT(*) AS order_count FROM orders
    source: seed
    created_at: 2026-05-06T12:00:00Z
    state: validated                          # NEW: unreviewed | validated | rejected
    validated_by: ashwin@agami.ai             # NEW (when state=validated)
    validated_at: 2026-05-10T14:30:00Z        # NEW (when state=validated)
```

Existing examples without these fields are treated as `state: unreviewed`. Backward-compatible — agami-query-database loads everything regardless of state, with `validated` examples weighted slightly higher in the few-shot mix (future enhancement; for v1, equal weighting).

---

## Phase 5.5: Post-introspect summary (the trust-layer landing)

This is the first surface a curator sees. It tells them what auto-approved cleanly and what needs their attention — bounded, scannable, never a 350-item wall.

Scan the freshly-written model under `<artifacts_dir>/<profile>/` and count entries by `agami.review_state` and entity type. Produce the summary block exactly:

```
agami-connect just ran. Here's what we found:

  ✓  <N>  datasets, <M> fields                            (auto-approved)
  ✓  <K>  FK relationships                                 (auto-approved)
  ✓  <J>  field descriptions from DBA column comments      (auto-approved)
  ⚠  <R1> inferred relationships from column-name matches  (review)
  ⚠  <R2> field descriptions below confidence 0.7          (review)
  ⚠  <R3> metric proposals (sign-off required — Rule 1)
  ⚠  <R4> named-filter proposals (sign-off required — Rule 1)

  <R1+R2+R3+R4> items need your attention at threshold 0.7.
```

Counting rules (read the YAMLs after promotion, not the staging dir):

- `auto-approved datasets/fields` — `agami.review_state == "approved"` AND `agami.signed_off_by == "agami_introspect_v1"`. Count datasets and fields separately and combine in the first row.
- `FK relationships` — `agami.review_state == "approved"` AND `agami.origin == "fk"` (across `_schema.yaml` and `index.yaml.cross_schema_relationships`).
- `field descriptions from DBA column comments` — fields with `agami.review_state == "approved"` AND `agami.origin == "column_comment"`.
- `inferred relationships ... (review)` — relationships with `agami.review_state == "unreviewed"` AND `agami.origin == "introspect_heuristic"`.
- `field descriptions below confidence 0.7 (review)` — fields with `agami.review_state == "unreviewed"` AND `agami.confidence < 0.7`.
- `metric proposals` and `named-filter proposals` — count `metrics[]` entries in any yaml + `named_filters[]` entries in `index.yaml`'s model-level extension where `agami.review_state == "unreviewed"`.

If a category's count is `0`, omit that line entirely — don't show "0 metric proposals". A small DB might collapse the summary to two or three lines, which is fine.

Then offer the review dashboard via AskUserQuestion (single question, three options):

| Option | What happens |
|---|---|
| `Open the review dashboard` | Invoke the `agami-review` skill (built in §4 of the plan; ships with the dashboard launch). The user lands on the queue and walks the items. |
| `Skip — I'll review later` (default) | Acknowledge: "OK — `<R1+R2+R3+R4>` items remain unreviewed. Run `/agami-review` (or say 'open the review dashboard') anytime." Continue to Phase 6. |
| `Adjust the threshold` | Ask for a number (`0.0` – `1.0`). Re-render the summary using that threshold. Persist to `<artifacts_dir>/<profile>/agami.config.yaml` under `review.threshold`. |

If `agami-review` doesn't exist yet (the dashboard skill ships in a follow-up of the same launch), surface the summary block but skip the AskUserQuestion — instead surface a one-liner: *"Review dashboard not yet shipping in this build — the unreviewed items will surface in answer receipts as warnings until you can review them."*

---

## Phase 6: Post-setup follow-up suggestions

(Telemetry consent was previously asked here. It has been removed in the current 0.x line — there is no opt-in, no install event, no `~/.agami/.config.analytics_consent` field written, no `.telemetry-queue.jsonl` appended. The server-side telemetry endpoint and the privacy spec are preserved in the repo for future re-enable, but the runtime flow is silent. Don't surface anything about telemetry here.)

### 6a — gate on Rule 1 review status

**Before declaring `<profile> is set up`, check whether the trust-layer dashboard has any Rule 1 items unreviewed** (metrics with `review_state != approved`, named_filters with `review_state != approved`). Rule 1 items block at runtime — agami-query-database refuses to answer a question that depends on an unreviewed metric, per the strict-gate rule in §3.4 of `agami-osi-extensions.md`. Declaring "set up" while metrics are still unreviewed is misleading.

Count:
```python
rule1_unreviewed = (
  count(metrics where review_state != "approved") +
  count(named_filters where review_state != "approved")
)
```

If `rule1_unreviewed > 0`, **skip the "Now that you're set up" framing** and surface the **in-progress** variant in 6b. Otherwise, fall through to the **fully-set-up** variant in 6c.

### 6b — in-progress framing (Rule 1 items still unreviewed)

```
✓ <artifacts_dir>/<profile>/ — OSI v0.1.1 semantic model (<K> schemas, validated)
✓ <artifacts_dir>/<profile>/examples.yaml — <N> NL→SQL examples
✓ Snapshot pinned at .snapshots/<hash>/

⚠ Setup is partial — <rule1_unreviewed> Rule 1 items still need sign-off:
   - <M> metric proposal<s> (run /agami-review to walk them)
   - <K> named-filter proposal<s>

Until those are reviewed, agami-query-database will refuse questions that
depend on them (the trust-layer strict gate). Run `/agami-review` (or say
"open the review dashboard") to finish.

Here are five things you could already ask that don't depend on Rule 1 items:

1. <a count question — "How many orders are in the database?">
2. <a top-N from a single FK-approved table>
3. <a time-bucketed count>
4. <a status / category breakdown>
5. <a recency filter>

Pick deliberately — anything that touches `revenue`, `MRR`, `active_customer`,
or similar unreviewed metrics will refuse. Reply with a number, or run
`/agami-review` first.
```

Then end the turn.

### 6c — fully-set-up framing (no Rule 1 items pending)

```
✓ <artifacts_dir>/<profile>/ — OSI v0.1.1 semantic model (<K> schemas, validated)
✓ <artifacts_dir>/<profile>/examples.yaml — <N> NL→SQL examples
✓ Snapshot pinned at .snapshots/<hash>/
✓ All metrics + named filters signed off

Now that <profile> is set up, here are five things you could ask:

1. <a count question grounded in a real table — "How many orders shipped last month?">
2. <a top-N grouped question — "Top 10 customers by total spend">
3. <a time-series — "Revenue trend over the last 6 months">
4. <a comparison or breakdown — "Order count by status this quarter">
5. <a broader narrative — "How is the business doing this quarter?">

Reply with a number, or ask anything else.
```

Then end the turn. The user picking a number routes the chosen question into `query-database` for a real answer.

Pick suggestions that show off the schema's distinctive shape. If the model has tables like `orders` and `customers`, suggest things grounded in those. If it's a content/CRM schema, pick something domain-relevant. Keep each under 80 characters.

---

## Error handling

| Symptom | Action |
|---|---|
| Credentials chmod wrong | Refuse, offer to `chmod 600` |
| Cached connection tool no longer works | Re-detect, update `~/.agami/.config` |
| Introspection SQL fails | Route through `db_error_classifier.md`, surface the one-line remediation |
| **Validator fails** | **Refuse to promote `/tmp/agami-staging-<profile>/` to `<artifacts_dir>/<profile>/`. Show errors verbatim. Loop on edits + re-validate.** |
| EXPLAIN fails for a seed example | Auto-fix once → if still bad, move to `~/.agami/.rejected/`. Don't block the connect flow. |
| Reintrospect would lose hand-edits | Phase 2 hard rule #8 — preserve descriptions, ai_context, choice_fields, metrics. |
| Legacy single-file install detected | Auto-migrate: backup to `<artifacts_dir>/<profile>/_legacy.yaml.bak`, re-introspect into the new directory layout. |
