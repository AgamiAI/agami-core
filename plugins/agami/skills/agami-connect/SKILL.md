---
name: agami-connect
description: "End-to-end database connection for agami: sets up credentials on first run (DB-type picker → writes ~/.agami/credentials.example for the user to fill in), then introspects the live DB directly into the agami semantic model (subject areas, tables, columns, relationships with join cardinality, deep-table column groups, sensitive-column flags) under <artifacts_dir>/<profile>/. The structural model is built deterministically by scripts/semantic_model (catalog mode, or a probe-mode fallback when the catalog is locked down); the skill then layers LLM enrichment (descriptions, entities, metrics) and seeds EXPLAIN-validated NL→SQL examples. Every model write is gated by the semantic-model validator — no breaking model is ever persisted."
when_to_use: "Run when the user installs the plugin for the first time, asks 'how do I set up agami' / 'connect to my database' / 'introspect my database' / 'introspect the schema' / 'reload schema' / 'add a new database', or after the user changes their schema and wants the model refreshed. Also auto-invoked by agami-query the first time it runs (when the semantic model is missing). This skill handles credential setup, introspection, enrichment, and seed-example validation — one entry point for everything before the user can query."
argument-hint: "[reintrospect | profile NAME]"
---

# agami connect

**Before suggesting any slash command in chat, read [`shared/invocation-conventions.md`](../../shared/invocation-conventions.md).** Agami slash commands: `/agami-connect`, `/agami-query`, `/agami-model`, `/agami-save-correction`, `/agami-reconcile`. (`/agami-model` is also the trust-review surface — its Review tab absorbed the former `/agami-review`.) Never write the un-prefixed forms (`/init`, `/connect`, etc.) or colon forms (`/agami:connect`) — those don't exist. For chat replies, prefer natural language ("say 'reload the schema'", "say 'introspect my database'") — the `when_to_use` matcher routes correctly without an explicit slash command.

You are setting up the agami **semantic model** for the user's database. Goal: by the end there is a validated semantic model at `<artifacts_dir>/<profile>/` (`org.yaml` + `subject_areas/<area>/…` + `datasources/<connection>/storage.yaml`), a seeded examples library at `<artifacts_dir>/<profile>/prompt_examples/<area>/examples.yaml`, an `ORGANIZATION.md` the user can edit, and the user has seen one demo query execute end-to-end.

**The structural model is built by a deterministic engine, not hand-authored.** `bash "$AGAMI_PLUGIN_ROOT/scripts/sm" introspect` introspects the live DB across all supported dialects — **PostgreSQL (incl. Supabase / Redshift), MySQL/MariaDB, Snowflake, BigQuery, SQL Server, Oracle, Databricks, Trino/Presto, DuckDB, SQLite** — into the model: storage connection, proposed subject areas, tables, columns + types, primary-key grain, foreign-key relationships **with join cardinality**, `column_groups` on wide tables, and `sensitive` flags on PII. When the catalog (`information_schema` / PRAGMA / data-dictionary) is reachable it runs in **catalog mode**; when a locked-down role denies the catalog it falls back **per-capability to probe mode** (describe via a zero-row header, infer types from a value sample, grain from uniqueness probes, FKs from name+overlap) and everything inferred lands `unreviewed` for sign-off. Your job is the layer the engine can't do: **enrichment** (prose descriptions, entities, metrics, caveats) and **curation** (subject-area boundaries, trust review).

For the model format: [`scripts/semantic_model/__init__.py`](../../scripts/semantic_model/__init__.py) (layout) and the Pydantic models in `scripts/semantic_model/models.py`.
For credentials: [`shared/credentials-format.md`](../../shared/credentials-format.md).
For connection method + local execution: [`shared/connection-reference.md`](../../shared/connection-reference.md).
For DB error classification: [`shared/db_error_classifier.md`](../../shared/db_error_classifier.md).

## Conversation style

- **Combine acknowledge + next question** — don't waste turns on "Got it!"
- **Use AskUserQuestion for every Yes/No/Skip** — never inline-bullet options. Use `(Recommended)` only when there's a genuine recommendation. For fact-of-environment questions ("which database type?", "which schemas?"), don't mark any option Recommended — the user picks what they have.
- **Every multiSelect needs an explicit "none / continue" option** — `AskUserQuestion` cannot be submitted with zero boxes checked, so any multiSelect where "pick nothing" is a valid answer MUST offer a selectable "Nothing — continue" (or "Keep everything") option. Never phrase the prompt as "leave all unchecked" — that's unsubmittable and traps the user.
- **Keep the user oriented** — print one-line progress markers between phases (`✓ Introspected 12 tables`, `✓ Validator passed`, `✓ Generated 10 examples`).

## Progress tracking — set up a todo list at the very start

This is a multi-phase skill that often takes 5–15 minutes end-to-end. **The very first action on every invocation is to call `TodoWrite`** with the skill's major phases, so the user can watch progress. Validated as a strong UX signal — it makes the wait feel intentional rather than opaque.

Seed (one task per major phase, in order):

```
1. Preflight: credentials check + tool detection
2. Introspect database → semantic model (engine: tables, columns, grain, FK cardinality)
3. Enrich: descriptions, entities, metrics (LLM, validated into the model)
4. Curate before examples: exclude columns/tables + sign off metrics & entities
5. Generate seed NL→SQL examples (validated against the live DB)
6. Validate every seed example (user reviews via dashboard)
7. Post-introspect trust summary
8. Follow-up suggestions
```

Use `content` for the imperative form and `activeForm` for the present-continuous form. **Mark each todo `in_progress` when its phase starts and `completed` immediately when it ends.** Exactly one `in_progress` at a time.

**Skip the seeding if the todo list already contains these items** (the skill is resuming after Phase 0 wrote the credentials template and waited). When `$ARGUMENTS == reintrospect`, the same todos apply.

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

1. **Connect ONLY to the host/port/database/user/password in `~/.agami/credentials`.** That file is the sole credential source — there is no env-var bypass. Never connect to anything else. Never probe `localhost` unless the credentials say so. Never substitute defaults for missing fields.
2. **Never ask the user for connection values (host / port / user / password / token / DSN) in chat.** Not even temporarily. The single authorized credential path is **Phase 0a**, which writes a `credentials.example` template the user fills in and saves. Phase 0a never reads secrets inline — it writes a template, surfaces a hand-off, and ends the turn.
3. **Never scan or guess.** No `pgrep`, `ps`, `lsof`, `find /`, `ls /Applications`, no port-listener scans, no testing connections to common hostnames. The only acceptable Bash probes here are `which <tool>` and `python3 -c 'import <module>'`.
4. **If credentials are missing for the active profile, run Phase 0a.** After the user fills in the template they re-invoke (or just ask a data question — `agami-query` auto-invokes us).
5. **NEVER put a credential on a Bash command line** — no `export PGPASSWORD=…`, no `psql -W <pw>`, no heredoc that interpolates a secret. Hosts render Bash calls in chat; anything on the line leaks. Runtime queries use the auth files from `scripts/setup_pgauth.py` (psql/mysql) or `scripts/execute_sql.py` (every driver, reads `~/.agami/credentials` itself). See [`shared/connection-reference.md → HARD RULES`](../../shared/connection-reference.md).

If you reach for a command that doesn't fit, stop and re-read this section.

### Preflight steps

1. **Resolve `<profile>`**: `AGAMI_PROFILE` → `active_profile` in `~/.agami/.config` → `"main"` (older installs may have `"default"`). The model's `organization` equals `<profile>`.
2. **Credentials check (binding).** Read `~/.agami/credentials`; look for `[<profile>]`.
   - The `[<profile>]` section is present → apply the chmod check (refuse if world-readable), continue.
   - The `[<profile>]` section is **absent** (whether or not the file exists) **but `~/.agami/credentials.example` exists** → the user filled in the template; **run 0a.10 to promote it** (don't re-run 0a.4 — that would overwrite their edits). This is the *second-profile* path too: the file exists with other profiles but not this one — 0a.10's helper appends deterministically.
   - Neither the section nor the template present → **run Phase 0a and stop.** Surface: *"No credentials yet for profile `<profile>` — running setup."*
3. **Resolve connection fields** from the `[<profile>]` section. Field shapes per dialect are in [`shared/credentials-format.md`](../../shared/credentials-format.md). Never substitute a missing value — surface "missing field X for profile Y" and stop.
4. **Tool detection.** Read cached tool paths from `~/.agami/.config`; if absent, run detection per Phase 0a.
5. **Resolve `<artifacts_dir>`**: `AGAMI_ARTIFACTS_DIR` → `~/.agami/.config.artifacts_dir` → `$HOME/agami-artifacts`. The model lives in `<artifacts_dir>/<profile>/`. Create lazily (`mkdir -p … && chmod 755 …`).
6. **Update-check (best-effort).** Run the probe from [`shared/version-check.md`](../../shared/version-check.md); surface a one-liner if a newer version exists. Never block on network failure.
7. If `$ARGUMENTS` is `reintrospect`: re-introspect from scratch, but **preserve hand-edits** (descriptions, entities, metrics, caveats, trust sign-offs). The engine writes the structural skeleton; merge it over the existing enrichment rather than discarding it (see Phase 2's reintrospect note).

---

## Phase 0a: First-time credential bootstrap

**Runs only when preflight step 2 failed (credentials missing).** If `~/.agami/credentials` already has the `[<profile>]` section, **skip Phase 0a entirely.**

### 0a.1 — Set up `~/.agami/`
```bash
mkdir -p ~/.agami && chmod 700 ~/.agami
```

### 0a.2 — Ask the database type

**AskUserQuestion** (no `(Recommended)` — fact-of-environment). Cap at 4 visible + Other:

| label | description |
|---|---|
| `PostgreSQL` | Postgres + compatible: Supabase, Neon, RDS, Aurora, Cloud SQL, Timescale, and **Amazon Redshift** (port 5439, SSL by default). |
| `MySQL` | MySQL, MariaDB, RDS MySQL, PlanetScale. |
| `Snowflake` | Snowflake. Account identifier instead of host. |
| `BigQuery` | Google BigQuery. Auth via service-account JSON or ADC. |
| `Other (Other field)` | **SQL Server, Oracle, Databricks, Trino/Presto, DuckDB, SQLite**, or paste any DSN. |

Bind `$DB_TYPE` ∈ `postgres | mysql | snowflake | bigquery | sqlserver | oracle | databricks | trino | duckdb | sqlite | dsn`.

**Routing:**
- `PostgreSQL` → `postgres`; if the user later enters port `5439` or a `*.redshift.*.amazonaws.com` host, transparently re-bind to `redshift`. A `*.pooler.supabase.com` host stays `postgres` (Supabase is hosted Postgres).
- `MySQL`/`Snowflake`/`BigQuery` → pass-through.
- `Other` → parse the free-form input: a DSN scheme → derive `db_type`; `.db`/`.sqlite`/`.duckdb` suffix or absolute file path → SQLite or DuckDB; a named DB (`sqlserver`/`mssql`, `oracle`, `databricks`, `trino`/`presto`, `duckdb`) → that dialect. Only refuse with "not supported yet" for engines outside the supported set above (e.g. MongoDB, Cassandra, ClickHouse).

### 0a.3 — Name the database profile (the user's choice)

Ask the user to **name** this connection — don't pick for them. The name is how they'll switch databases later (`AGAMI_PROFILE=<name>`) and it names the model folder (`<artifacts_dir>/<name>/`), so a name that means something to them — their database, product, team, or environment — beats a generic default.

**AskUserQuestion**, with the **Other** free-text as the encouraged path (that's where they type their own name):
> What should I call this database? Pick a name you'll recognize when you connect more than one — e.g. your database or product name, or an environment.

Offer a few *examples* as options (`prod`, `staging`, `analytics`) but make clear in the prompt that typing their own in **Other** is the point — **don't present a `main` default that nudges them past the choice.** Bind `$PROFILE_NAME` to their answer (the Other text, or a picked example). Validate: lowercase letters/digits/dashes/underscores, 1–32 chars; **and not already a `[section]` in `~/.agami/credentials`** (a profile name is a unique key — reusing one would clash). If it fails either rule, show the reason and re-ask. (The 0a.10 promote helper enforces the uniqueness backstop too — it returns `COLLISION` rather than overwrite — but catch it here so the user isn't surprised later.)

### 0a.4 — Write `~/.agami/credentials.example`

Use the **Write tool**. Shared header first, then the `$DB_TYPE` body with `[$PROFILE_NAME]` as the section.

**Header:**
```ini
# ~/.agami/credentials.example
# Fill in your values below, then come back and say "introspect my database".
# agami moves this file to ~/.agami/credentials and chmod-600s it for you — no
# manual save or chmod needed. (Don't rename it yourself.)
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

**First resolve the ONE Python agami uses for everything** — the model *and* DB connections. Call it `$PY`. This matters: **introspection always runs `scripts/execute_sql.py` under this interpreter** (via `sm`/`sys.executable`), on *every* tier — even when `psql` is installed. So the DB driver must live in `$PY`. The `sm` wrapper and the engine both read this interpreter from `~/.agami/.config`, so resolving it once here removes all interpreter guessing — **no environment variables, the user sets nothing.**

**Discover it automatically — prefer an interpreter that already has the DB driver** (so a user whose driver lives in a venv / framework / Homebrew Python is used as-is, with zero install). Probe a bounded candidate list for the `$DB_TYPE` driver + the model deps; first full match wins:

```bash
DRIVER_MOD="<import module for $DB_TYPE — see the table below; sqlite/duckdb skip the driver>"
CANDIDATES="$(command -v python3) $(command -v python) ${VIRTUAL_ENV:+$VIRTUAL_ENV/bin/python} \
  $(ls /opt/homebrew/bin/python3.* /usr/local/bin/python3.* 2>/dev/null) \
  $(ls /Library/Frameworks/Python.framework/Versions/*/bin/python3 2>/dev/null) \
  $(ls "$HOME"/.pyenv/versions/*/bin/python3 2>/dev/null)"
PY=""
for c in $CANDIDATES; do
  [ -x "$c" ] || continue
  "$c" -c "import ${DRIVER_MOD:-sys}, pydantic, sqlglot, yaml" 2>/dev/null && { PY="$c"; break; }
done
# Nothing fully equipped yet → take the first working base interpreter; 0a.5b + the
# driver step below install what's missing INTO it.
[ -z "$PY" ] && PY="$(command -v python3 || command -v python)"
PY="$("$PY" -c 'import sys; print(sys.executable)')"   # canonical absolute path
```
Record `$PY` — it becomes `tool_paths.python3` in 0a.7. (`AGAMI_PYTHON`, if the user happens to have it set, is honored as a first-priority override — but it is **never required** and the skill never asks the user to set it.)

**Detect native CLIs** (optional fast path for *queries* — introspection doesn't use them) with `which` only:
```bash
for t in psql mysql snowsql sqlite3 duckdb bq; do which $t 2>/dev/null; done
```
If `which psql` is empty, try the Homebrew libpq glob once. **Forbidden:** `pgrep`/`ps`/`lsof`/`find /`/`ls /Applications`/port scans.

**Ensure the DB driver for `$DB_TYPE` is importable in `$PY`** (probe in `$PY`, NOT bare `python3` — they may differ):

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
| duckdb | `import duckdb` | `duckdb` |
| sqlite | stdlib — always present | — |

If the driver is missing, **confirm via AskUserQuestion**, then `"$PY" -m pip install --user <package>` (plain `pip install` fallback). Same "never install silently" convention as the model deps (0a.5b). Do this for `$PY` so `sm introspect` connects on the first try.

### 0a.5b — Ensure the semantic-model dependencies

The model (introspection, validation, traversal, curation — everything the `sm` wrapper drives) needs **`pydantic` + `sqlglot` + `pyyaml`** in the interpreter agami uses. Check the resolved interpreter (`$AGAMI_PYTHON` → `.config` `tool_paths.python3` → `python3`):

```bash
"$PY" -c 'import pydantic, sqlglot, yaml' 2>/dev/null && echo "model deps OK"
```

If they're present, continue. If missing, **confirm via AskUserQuestion before installing** (same convention as the DB-driver install above — agami never installs silently):
> agami needs `pydantic`, `sqlglot`, and `pyyaml` to build and read the semantic model. Install them now? (one-time, user-site — `pip install --user`)

On **Yes**: `"$PY" -m pip install --user -r "$AGAMI_PLUGIN_ROOT/scripts/semantic_model/requirements.txt"` (fall back to a plain `pip install` if `--user` is rejected). On **No**: stop with *"Can't build the model without those — re-run when you're ready to install."* — don't proceed to introspect.

(The `sm` wrapper also self-installs these on first use as a safety net, but doing it here makes it explicit, confirmed, and at a predictable moment rather than mid-introspection.)

### 0a.6 — Ask for `<artifacts_dir>`

Detect the OS once so the options are platform-native — `uname -s` (`Darwin` = macOS, `Linux` = Linux) or treat `$OS == Windows_NT` / a `MINGW*`/`MSYS*` uname as Windows. Then **AskUserQuestion** with the two defaults for that OS as named options (Recommended first). The auto-provided **Other** lets the user type any absolute path — so this both gives sensible options *and* allows a full custom path:

> Where should agami save your semantic model, examples, and preferences? This is the **parent** for ALL profiles — each lands in `<artifacts_dir>/<profile>/`. It's non-secret (no credentials) — point it inside a git repo to share the tuned model with your team. Credentials stay in `~/.agami/` regardless.

| OS | Option 1 — Recommended | Option 2 |
|---|---|---|
| macOS | `~/agami-artifacts` | `~/Documents/agami-artifacts` |
| Linux | `~/agami-artifacts` | `~/Documents/agami-artifacts` |
| Windows | `%USERPROFILE%\agami-artifacts` | `%USERPROFILE%\Documents\agami-artifacts` |

(For Other, suggest a team repo path as the example, e.g. `~/code/acme-data/agami`.) Expand `~` / `%USERPROFILE%` to an absolute path. Validate: absolute, not inside `~/.agami/`, parent creatable. Store the **resolved absolute path** in `.config.artifacts_dir`.

### 0a.7 — Write `~/.agami/.config`
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
`python3` MUST be the `$PY` resolved in 0a.5 (the interpreter that has both the model deps and the DB driver) — `sm` and the introspection engine read it from here, so recording the wrong one reintroduces the interpreter mismatch. `chmod 600 ~/.agami/.config`.

### 0a.8 — Seed `<artifacts_dir>/USER_MEMORY.md` if missing
Create the parent (`mkdir -p && chmod 755`) and write the default seed (per [`shared/user-memory-format.md`](../../shared/user-memory-format.md)), `chmod 644`. Don't overwrite. Migrate a v1.1 `~/.agami/USER_MEMORY.md` if present.

### 0a.9 — Hand-off + END THE TURN
```
✓ ~/.agami/ ready (chmod 700)
✓ Credentials template → ~/.agami/credentials.example
✓ Tool detected: <tool> (<tier>)
✓ Artifacts dir: <resolved path>

Next:
1. Open ~/.agami/credentials.example and fill in your real connection details
   (keep the filename as-is — don't rename it).
2. Come back and say "introspect my database" — I'll secure the file and run the
   full introspect → enrich → seed flow.

Heads-up: a cold cloud warehouse (Snowflake especially) makes introspect the slow
step — ~5–15 min for a sizable account. Postgres / MySQL are seconds.
```
**End the turn.** Do NOT continue to Phase 1.

### 0a.10 — On re-entry: promote the filled-in template, then continue
The user filled in `~/.agami/credentials.example` and came back (or asked a data question / said "introspect my database"). **Promote it deterministically** with the helper — do NOT hand-roll an `mv`/append, and do NOT assume "no file yet." The script handles all four cases (first profile → move; Nth profile → append; name clash → refuse; placeholders → refuse), so the second-profile and `[main]`/`[main]` cases can't silently corrupt the file:

```bash
python3 "$AGAMI_PLUGIN_ROOT/scripts/promote_credentials.py"
```

Read the first token of stdout and act:
- `SECURED <profile>` → credentials created from the template (chmod 600, `.example` consumed). Continue.
- `APPENDED <profile>` → the new profile was appended to an existing credentials file (other profiles untouched, chmod 600). Continue.
- `COLLISION <profile>` → a profile by that name **already exists**; nothing was changed. Tell the user *"You already have a profile named `<profile>` — pick a different name for this database,"* go back to **0a.3** to rename (then re-write the template in 0a.4 under the new name), and **stop**. Never overwrite or duplicate an existing profile.
- `PLACEHOLDERS_REMAIN <fields>` → the template still holds template values; tell the user which fields to fill and **stop** (never introspect against a template).
- `NOTHING` → no `credentials.example` to promote (already promoted, or never written) — fall back to the preflight decision.

(`$AGAMI_PLUGIN_ROOT` is the plugin root; `promote_credentials.py` is stdlib-only and needs no special interpreter.)

**Run `setup_pgauth.py --all`** before the first native-CLI query (writes `.pgpass` / `.mysql.cnf` so passwords never hit the command line). Idempotent. Then continue to Phase 1.

---

## Phase 1: Introspect → semantic model

### 1.0 — Set expectations before kicking off

Introspection can take a while against cloud DBs. Tell the user **before** the first probe. Honest estimates — **don't lowball** (a user told "5 min" who waits 4 thinks "almost there"; one told "1 min" thinks "stuck").

| db_type | Typical | Why |
|---|---|---|
| sqlite / duckdb | < 5s | local file |
| postgres / mysql (local) | 5–15s | fast catalog |
| postgres / mysql (cloud) | 15–60s | network RTT per query + FK overlap checks |
| redshift | 1–5 min | slow metadata + overlap joins |
| **snowflake** | **5–15 min** | cold-warehouse spin-up dominates; per-table queries, sample scans, EXPLAIN validation. A 100-table account measured ~12 min. |
| sqlserver / oracle / databricks / trino | 30s–5 min | network + per-table catalog |

Surface a one-liner with per-step estimates and **narrate per-table progress** so it never looks hung. For `reintrospect`, prepend "Re-introspecting (about as long as initial setup)."

### 1.1 — Existing-model check

If `<artifacts_dir>/<profile>/org.yaml` exists and `$ARGUMENTS != reintrospect`: the profile is already onboarded. Offer (AskUserQuestion, no `(Recommended)` — these are equal-weight choices), capped at 4:
- **Re-introspect `<profile>`** — refresh the structure from the live DB (new/changed tables, columns, FKs) while preserving descriptions, entities, metrics, caveats, and sign-offs (the `reintrospect` path).
- **Open model explorer** — browse + curate the existing model and review/sign off the trust layer (`/agami-model`).
- **Onboard another database** — set up a **different** database (a different connection) under a **new** profile, leaving `<profile>` untouched. On this choice, **start a fresh onboarding for a new profile**: jump to the profile-naming step (Phase 0a's naming question) → have the user name the new profile (must differ from `<profile>` and any existing `[section]` in `~/.agami/credentials`) and pick its DB type → write that profile's `credentials.example` → run the full flow for it. Never reuse or overwrite the current profile's credentials or model.
- **Cancel** — do nothing; the model is ready to query.

**Same DB, another *schema*? That's the Re-introspect path, not "Onboard another database."** If the user wants to add a schema that lives in the **same database** they already onboarded (e.g. they did `public`, now they want `billing` too), choose **Re-introspect** and **expand the schema selection** in Phase 1.3 to include both the old and the new schemas. The engine scans them together in one pass, so any relationship between the original and the new schema is detected as a first-class **cross-schema** join (Case 1) and surfaced for review. Picking "Onboard another database" instead would split the two schemas into separate models and demote any link between them to manual cross-profile glue (Phase 2b federation) — wrong for one DB. If you're unsure which the user means, ask: *"Is `billing` in the same database connection as `<profile>`, or a different server/database?"* — same connection → Re-introspect + expand schemas; different → new profile.

The engine **auto-backs-up any legacy OSI** (`index.yaml` + per-schema `_schema.yaml`) it finds at the profile root into `.osi_backup/` before writing — so a first run over an old OSI profile is safe and reversible; surface a one-liner when that happens.

### 1.2 — Scope: schemas, and the no-catalog case

Run `cli areas`/probe is not needed yet — schema discovery happens inside the engine. But **decide scope first**:

- **Catalog reachable (common):** after the engine lists schemas, it introspects all of them. If the DB has many schemas (Snowflake with 50+), narrow first — ask the user which schemas matter (multi-select), then pass them as the engine's table allowlist scope. Pre-check `public` (Postgres) / `PUBLIC` (Snowflake) / the credentials' `database` (MySQL).
  - **Supabase is handled automatically** — the engine detects it by signature and drops its system schemas (`auth`, `storage`, `vault`, `realtime`, `extensions`, …) so only the user's app schemas (e.g. `public`) are modeled (it surfaces a note saying which it skipped). **Don't hand-build a `--tables` allowlist for Supabase** to dodge the system tables — that's already done, and a manual allowlist is fragile (shell word-splitting, missed tables). Only allowlist when the user genuinely wants a subset of their *own* tables.
- **Catalog denied (locked-down role):** if a quick probe shows the catalog isn't readable, the engine **cannot enumerate tables** — ask the user for the table list:
  > Your role can read the data but not the catalog, so I can't list tables automatically. Paste the tables to model (e.g. `sales.orders, sales.customers`) — I'll describe each from the data itself.

  Pass these to the engine via `--tables schema.table …`. Everything the engine then infers (types, grain, FKs) lands `unreviewed` for sign-off.

### 1.3 — Schema picker (multi-select)

For non-SQLite/DuckDB with multiple schemas, **AskUserQuestion** multi-select: "Which schemas should I introspect?" One option per schema + `All schemas` + `Just <default> for now`. Record `selected_schemas`; the engine scopes to these.

**On re-introspect / an existing model — pre-check the schemas already modeled, and make "add" vs "replace" explicit.** Read the schemas already in `<profile>`'s model (the distinct `schema` of its existing tables) and **pre-check those** in the picker, labelled `<schema> (already in your model)`. Tell the user plainly:

> The engine re-scans exactly the schemas you check here and rebuilds the structure from them. To **add** a schema, leave the existing ones checked and tick the new one — relationships between them get found in the same pass. **Unchecking a schema removes its tables from the model** (its hand-edits are preserved only if you keep it checked).

This is the union-rescan that makes Case 2 work: adding `billing` to a model that already has `public` must scan `public` ∪ `billing` together — scanning only `billing` would build the new schema's internal joins but **miss every join back to `public`** (a cross-schema edge needs both endpoints in one introspection pass). Never silently re-scan just the delta.

### 1.4 — Organization context (MANDATORY — ALWAYS ASK)

This runs on **every** invocation. The user's yes/skip is theirs; the skill never decides for them. "don't ask clarifying questions" does NOT cancel this — it's required state-gathering, not a clarifying question. **Only conditional skip:** `ORGANIZATION.md` exists and has been edited beyond the template.

**AskUserQuestion:**
> Want to give me a one-paragraph description of what this database is about? It improves NL→SQL accuracy a lot. Examples: what the company/product is, what "MRR" or "active user" means in your terms.

`Yes — I'll type it now (Other field)` → write to `<artifacts_dir>/<profile>/ORGANIZATION.md` under `# About this database` + the commented default template. `Skip — I'll auto-fill it from my data (Recommended)` → **don't write the bare template** — leave ORGANIZATION.md absent for now; Phase 2f auto-drafts it from the enriched model so it's never blank. `chmod 600` whatever you write. See [`shared/organization-context-format.md`](../../shared/organization-context-format.md).

### 1.5 — Existing data model / semantic layer (MANDATORY — ALWAYS ASK)

Independent of 1.4 (paragraph ≠ doc). Same "required state-gathering" rule. Two very different sources qualify, so ask once and branch on the answer. **AskUserQuestion** (multi-select; the repo path is the high-value one — it encodes metrics + joins, not just structure):

> Got an existing data model or metrics list I can read? Three kinds help:
> • **A doc** — ERD, data dictionary, schema diagram (PDF, PNG/JPG, text, markdown, CSV).
> • **A metrics / KPI list** — a spreadsheet, CSV, or doc of your metrics and how each is defined (e.g. "Approval rate = approved ÷ applications"). I'll turn each into a reusable metric so answers match your numbers.
> • **A semantic-layer / transform repo** — LookML, dbt, Cube, MetricFlow. These define your metrics, dimensions, and joins explicitly, which is gold for NL→SQL accuracy. They're usually git-backed — just point me at the folder.

Options: `Doc / metrics file — I'll attach it` / `Semantic-layer repo — I'll give a path` / `Both` / `Skip — nothing to share`.

**If a doc:** `Read` the path (handles PDF/image/md/text/CSV natively; trim huge files to first 20 pages / 50 rows). `.xlsx`/`.docx` → ask for PDF, proceed without if not.

**If a semantic-layer repo:** ask for the directory (a local clone / monorepo path — no upload needed since it's git-backed). Glob the **definition** files and `Read` them up to a budget (~30 files / ~250 KB total; if larger, prefer metric/model definitions and tell the user what you sampled). **Skip compiled SQL and data files** — you want the declared metrics/joins, not the warehouse output:
> | Layer | Read these | Carries |
> |---|---|---|
> | **LookML** | `*.view.lkml`, `*.explore.lkml`, `*.model.lkml` | dimensions, **measures** (→ metrics), **joins** (→ relationships), `sql_table_name` |
> | **dbt** | `models/**/*.yml` (esp. `schema.yml`), `semantic_models/**`, `metrics/**`, `dbt_project.yml` | column descriptions, `relationships` tests (→ FKs), MetricFlow metrics/measures |
> | **Cube** | `model/**/*.{yml,js}` (or `schema/**`) | `measures`, `dimensions`, `joins` |

Stash everything gathered (doc text + repo definitions) as `$DATA_MODEL_DOC_TEXT` for enrichment — give entities/metrics/relationships found here **`confidence: inferred`** (a declared metric is a strong signal but still wants a human sign-off; FK-derived joins stay as the engine set them). **Never written to disk** — lives only in the enrichment prompt, then discarded. `Skip` → proceed.

### 1.6 — Run the introspection engine

This is the deterministic core — it replaces hand-authoring tables/columns/FK SQL/confidence formulas. From `plugins/agami/scripts/`:

```bash
bash "$AGAMI_PLUGIN_ROOT/scripts/sm" introspect \
  --profile <profile> --db-type <db_type> \
  --artifacts "<artifacts_dir>" \
  [--tables schema.table …]      # only for the no-catalog case (1.2)
```

It builds + **validates** + writes the model at `<artifacts_dir>/<profile>/`: storage connection, **proposed subject areas**, per-table columns + types (catalog or value-inferred), PK→`grain`, FK→`relationships` with **inferred cardinality** (`many_to_one`/`one_to_many`/`one_to_one`), `column_groups` on deep tables (≥30 cols), `sensitive` flags on PII, cross-area edges, and a report. Relationships from **unenforced-FK** dialects (Redshift/Databricks/Trino) and everything from probe mode are confirmed-by-overlap or `unreviewed`. The report prints the **capability mode per step** (catalog vs probe) — surface that to the user so they know what was read vs inferred.

The validator gates the write — **if it fails, the model is not persisted.** Surface the errors verbatim and stop (this should be rare; the engine emits valid models).

Surface: `✓ Introspected <N> tables across <A> subject area(s) (<catalog|probe> mode); <R> relationships, <D> deep tables, <S> sensitive columns flagged.`

---

## Phase 2: Enrich (the LLM layer — validated into the model)

The engine gives structure; you add meaning. Load the model with `cli bundle <root> --area <area>` (or read the YAMLs). After each enrichment pass, **re-validate** (`cli validate <root>`) and never persist a model that fails. `<root>` = `<artifacts_dir>/<profile>/`.

### 2a — Descriptions (describe coded columns; leave only self-evident ones empty)

For each table fetch up to 5 sample rows for evidence (`SELECT * FROM <t> LIMIT 5`; Snowflake `SAMPLE` for >10M rows). **Samples are never written to disk** — context only, then discarded. Also capture MIN/MAX of each time column → record under the table's `performance_hints` so Phase 5 anchors "last 30 days" to the data's real MAX, not `NOW()`.

Build a per-schema prompt with `$DATA_MODEL_DOC_TEXT` first (dominant prior), then `ORGANIZATION.md`, then tables/columns/sample rows. **Always** emit a 1-line table `description`. For **columns**, classify each into one of three — don't default everything to empty:

| Kind | Examples | What to write |
|---|---|---|
| **Self-evident** | `id`, `created_at`, `email`, `name`, `gender`, `city` | `""` — the name + type already says it; a description would just restate it. |
| **Informative** | `revenue_usd`, `status`, `margin_pct`, `utilization_pct` | one line **iff** samples/doc support a fact the name doesn't (enum values, unit, derivation, a caveat). Else `""`. |
| **Coded / opaque-but-systematic** | `EL_REVENUE_30D`, `WEST_ORDERS_12M`, `<TIER_A_6M>`, `XX_<metric>` families | **always describe** — unreadable without the legend (see below). |

**Coded-schema detection + legend expansion — the case that used to get wrongly left empty.** Feature stores, wide denormalized marts, and coded analytic extracts encode meaning in column *names* via a small recurring token vocabulary: a category prefix (e.g. `EL/HM/AP…`), a window suffix (`_30D`, `_6M`, `_12M`), a bucket (`_0_30`, `_TIER_A`), a threshold (`_LT_1K`, `_LT_10K`), a metric stem (`_REVENUE`, `_QTY`, `_RECENCY`). When the same tokens recur across many columns:

1. **Decode the token legend once** — grounded in column samples + `$DATA_MODEL_DOC_TEXT` + `ORGANIZATION.md`. For any token whose meaning isn't evident, **ask the user in one batched question** rather than guessing (this is the same decode 2b needs; do it once and share it).
2. **Expand the legend into one description per coded column — deterministically, NOT as N separate LLM guesses.** Write a small in-skill decoder (a token→phrase map + a per-table parse of `(prefix, metric, window, threshold)`) and compose each description from it, then emit them through the same `sm curate` batch. This is exact, internally consistent, and costs zero per-column LLM tokens. e.g. `EL_REVENUE_30D` → "Total revenue from the Electronics category over the last 30 days"; `WEST_ORDERS_12M` → "Number of orders in the West region over the last 12 months"; `EL_ORDERS_LT_1K_90D` → "Number of Electronics orders under 1,000 placed in the last 90 days." (Use the user's actual token vocabulary + domain, not these placeholders.)
3. The same decoded legend also lands in the **structured glossary** `key_terminology` (term → definition) — abbreviations and codes a reader needs (e.g. `TIU` → "Telematics Interface Unit", `SoC` → "State of Charge", `bp` → "battery pack", a master-type code → its meaning). **Decode once, write both** — the per-column descriptions (what shows in the explorer + feeds the SQL generator's column context) AND the glossary. Write it with the packaged command (a JSON object `{term: definition, …}`); it merges over any existing terms, validates, and commits:
   ```bash
   bash "$AGAMI_PLUGIN_ROOT/scripts/sm" set-terminology "$ROOT" --file /tmp/agami-terminology.json
   ```
   The glossary is rendered into `ORGANIZATION.md`'s `## Key terminology` by `org-draft` (which **also** auto-seeds enum legends from `choice_field` columns) — so it survives a regeneration and is never a bare placeholder. **Do NOT skip this on a code-dense schema** (abbreviations, master-coded values): an empty Key terminology on a DB full of codes is a real miss.

Don't skip a coded column because "the legend covers it" — the legend lives in a *different file*; the per-column description is what the explorer shows and what NL→SQL reads. A wide coded table (hundreds of columns) should finish at ~100% column coverage, not ~3%.

**Write descriptions with `sm curate` edit ops — never hand-edit the table YAML, and never write a throwaway generator script to build the ops.** Build one ops array and run it once; it validates the whole model + commits + reverts on failure. **The op does NOT need `area`** — curate resolves which area owns a table/column by name, so you do **not** maintain a `table → area` map (that map is exactly what used to push the LLM into writing an `enrich_gen.py`; it's gone). Shapes:
- table: `{op:edit, kind:table, name:"<table>", field:"description", value:"<text>", source:"ai"}`
- column: same + `column:"<col>"`

Pass `area` **only** to disambiguate a bare name that exists in two schemas (curate skips an ambiguous op with a clear "pass an explicit area" reason rather than editing the wrong one). **Always include `"source":"ai"` on every generated description** — this stamps `description_source: ai_unvalidated` so the description earns trust through use (agami-query surfaces it in the answer receipt for confirmation the first time a query actually uses that column, instead of forcing an upfront review of hundreds of descriptions; see [`docs/design/validated-through-use-descriptions.md`](../../../../docs/design/validated-through-use-descriptions.md)). **Skip any column that already has a description** (so a partial hand-edit or a reintrospect merge is never clobbered):
```bash
bash "$AGAMI_PLUGIN_ROOT/scripts/sm" curate "$ROOT" --ops-file /tmp/agami-descriptions.json
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

**Regroup wide tables into SEMANTIC column groups (do this right after describing their columns).** A deep table (≥30 cols) gets `column_groups` from the engine, but those are a deterministic **name-prefix** split — buckets like `is`, `last`, `latest`, `created`, `bp` — not concepts. They exist only so the explorer can collapse a wide table; they're a fallback, not the goal. Having just described every column, **you** are positioned to cluster them by *meaning* into a handful of named groups that read like mini subject-areas — e.g. `identity`, `location`, `lifecycle_timestamps`, `telemetry`, `swap_details`, `alerts`, `flags`. Keep it to ~5–10 groups; **every column must land in exactly one** (the validator rejects orphans on a deep table). Apply via one curate edit op per wide table:
```json
{"op":"edit","kind":"table","name":"<wide_table>","field":"column_groups",
 "value":{"identity":["..."],"location":["..."],"lifecycle_timestamps":["..."],"telemetry":["..."]}}
```
Send these in the same `sm curate` batch as the descriptions (or a follow-up batch). Only worth doing on genuinely wide tables — narrow tables have no `column_groups` and need none.

**Column-pass completeness gate (MANDATORY — do not skip the column pass).** After enriching, run:
```bash
bash "$AGAMI_PLUGIN_ROOT/scripts/sm" coverage "$ROOT"
```
`ok: false` with `unenriched_tables` lists tables that got a **table** description but **no column descriptions at all** — i.e. you enriched the table layer and skipped the columns. That is not done: go back and run the column pass on those tables (describe each meaningful column from sampled values; mark genuinely-opaque ones `ai_unknown`; self-evident `id`/timestamps may stay blank). A wide coded table must finish near 100% described, not 0%. This is also enforced downstream — `seed-examples` **refuses** (`refused: "columns_unenriched"`) while any table is unenriched, so you cannot reach the trust dashboard on naked columns. Don't proceed to 2b until `coverage` returns `ok: true`.

### 2b — Entities (the semantic vocabulary)

Propose `entities[]` per subject area — the names users actually say. For each, fill `name`, `plural`, `other_names` (synonyms), `maps_to` (table+column, one `primary: true`), and — for opaque-identifier columns — a `value_pattern` regex (e.g. a VIN `^[A-Z0-9]{17}$`, a `BP`-prefixed serial) so the runtime can recognize literals. Ground these in column names + samples + the domain doc; don't invent entities the schema doesn't support. Because these are LLM-proposed, write them **`confidence: inferred, review_state: unreviewed`** so they surface in the Phase 4 pre-seed review (seeds reference entity vocabulary).

**Don't stop at id columns — wide / denormalized tables hide dimensions a `maps_to`-on-an-id scan misses.** When a schema is denormalized to one grain (e.g. one row per customer, every table keyed on the same id), an id scan finds exactly one entity and quits — yet real business dimensions are still there, encoded two other ways. Look for both, **discovered from the actual columns + samples, never hardcoded**:
- **Coded column-name prefixes / suffixes.** Many columns sharing a recurring token (`XX_<metric>` repeated for several `XX`, or `<metric>_<period>`) means that token is a dimension (a product line, a region, a time bucket). **Decode it from evidence** — column descriptions, sample values, the domain doc; if a code's meaning isn't evident, **ask in one batched question rather than guessing**. A prefix dimension has no id column, so it isn't a `maps_to` entity — record the decoded legend in `ORGANIZATION.md` `## Key terminology` and fold the expansion into each affected column's description, so NL→SQL can map a phrase to the right prefixed columns.
- **Value-level entities.** A set of sibling string columns whose *values* are real-world instances (institution/lender names, branch names, merchant names, statuses) is an entity even with no id column — create it (`maps_to` the most representative such column; capture distinct sampled values as `other_names`/a `value_pattern` cue) so a user naming a literal value resolves to those columns.

If, after this, a single-grain schema genuinely has one entity, that's the right answer — say so. The point is to *check* the two hidden shapes before concluding "one entity," not to manufacture entities.

**Write them with the packaged command, not by hand** — build a JSON array and run it once (it validates each item, writes `subject_areas/<area>/entities/<slug>.yaml`, validates the whole model, reverts on failure, commits). Never author a throwaway script to loop. The canonical entity YAML shape is [`shared/metric-entity-shape.md`](../../shared/metric-entity-shape.md) — **never read another profile's artifacts to copy it** (see the boundary note under 2c):
```bash
bash "$AGAMI_PLUGIN_ROOT/scripts/sm" add "$ROOT" --kind entity --area <area> --file /tmp/agami-entities.json
```

### 2c — Metrics

Metrics come from two sources, handled very differently. **Always prefer declared metrics** — schema-only inference is a shallow guess (it finds `AVG(rating)`, a row count, an `AVG(order_value)`; it misses the domain KPIs a business actually tracks — refund rate, repeat-purchase rate, cohort retention, fulfillment SLA — because those aren't visible in column names).

**(A) Declared metrics — extract in FULL, no cap.** If the user attached a semantic-layer repo or a metrics file in 1.5 (`$DATA_MODEL_DOC_TEXT`), those are the org's *real* definitions — pull **every** one, don't sample to 4:
- **LookML** `measure {}` → metric: `type` + `sql` → `bindings`, `label`/`description` → `calculation`, `label`+`view_label` → `other_names`.
- **dbt** `metrics:` / `semantic_models[].measures` (MetricFlow) → name, `agg`+`expr` → `bindings`, `description`/`label` → `calculation` + `other_names`.
- **Cube** `measures` → `sql`+`type` → `bindings`.
- **Metrics file** (CSV/YAML/markdown KPI dictionary the user uploaded) → one metric per row/entry: name, definition → `calculation`, formula → `bindings`.

Translate the declared SQL/agg to the profile's dialect for `bindings`, set `source_tables`, write **`confidence: inferred, review_state: unreviewed`** (declared = strong signal, still wants a human sign-off on the `/agami-model` Review tab). If there are many (> ~8), **don't** funnel them through a 4-item picker — write them all and tell the user once: *"Added N metrics from your `<LookML/dbt/file>` — review or trim them in /agami-model."* (Offer a single "add all N / let me pick a subset" confirm if you want, but never silently drop declared metrics to fit a cap.)

**(B) Inferred metrics — only when there's no declared source (or to supplement a thin one).** These genuinely drift, so **suggest, don't auto-add**, capped at ~4 (AskUserQuestion fits ~4 + Other) from: aggregate-shaped numeric fields (SUM/AVG), fact tables (`count_<table>`), time fields, `ORGANIZATION.md` KPI mentions. **AskUserQuestion** multi-select: "I'd suggest these reusable metrics — pick which make sense." `Other (Other field)` for "describe a metric I want"; submitting none = skip. Write `confidence: proposed`.

For every metric (A or B) fill `name`, prose `calculation` (intent — **required**), per-dialect `bindings` (the SQL), `source_tables`, `other_names`. The canonical YAML shape is [`shared/metric-entity-shape.md`](../../shared/metric-entity-shape.md) (synthetic example). **Write them with the packaged command** — build a JSON array and run it once; never hand-write each YAML and never author a throwaway loop script. It validates each item, writes `subject_areas/<area>/metrics/<slug>.yaml`, validates the whole model, and reverts the batch on failure:
```bash
bash "$AGAMI_PLUGIN_ROOT/scripts/sm" add "$ROOT" --kind metric --area <area> --file /tmp/agami-metrics.json
```
> **Never read another profile to learn the shape.** Do not glob or read other profiles' artifacts (e.g. `find <artifacts_dir> -path '*/metrics/*.yaml'`, or reading `<artifacts_dir>/<other-profile>/…`) to "copy the binding shape." That crosses the profile boundary — in a hosted/multi-tenant deployment it's a **tenant-data leak** (onboarding one customer must never read another's model), and even locally it risks lifting another profile's filters / calculation text into this one. You don't need to: the packaged `sm add` command validates every item against the schema. Get the shape from `shared/metric-entity-shape.md` and the profile's **own** schema — never a sibling profile.
Don't propose metrics depending on choice-field literals you didn't detect, or cross-area metrics unless a cross-area edge wires the join (then put them under the cross-cutting area).

### 2d — Caveats, value_transforms, currency

From samples + the domain doc, add provider-portable cleaning where evidence supports it:
- **Caveats** (`caveats[]` on table/column/entity): data-quality notes, anti-patterns (e.g. "filter on the event date, not the load date"), dedup warnings.
- **value_transform** on columns whose raw value needs cleaning (`regexp_replace(...)` for bracketed text, `TO_TIMESTAMP(...)` for epoch). Must parse as SQL (validator checks).
- **default_filters** (`default_filters[]` on a table): soft-delete / tenancy filters AND-ed in at query time (use the `{alias}` placeholder, e.g. `{alias}.deleted_at IS NULL`).
- **Currency (one ask per profile):** **find the money columns with `sm suggest-units "$ROOT"` — don't hand-roll a name regex** (a bare `count` pattern matches inside `discount` and silently drops `discount_amount`; the command's matcher is word-boundary-correct and tested). It returns `{money_columns: [{area, table, column, type}]}` (numeric columns named like money — amount/price/revenue/discount/… — minus rate/count/id/score, and skipping any that already have a `unit`). Glance at the list; if a `_usd`/`_inr`-suffixed column makes the currency obvious, set it directly. Otherwise ask once: "What currency are these in?" (`USD`/`EUR`/`GBP`/`JPY`/`INR`/`Other`/`Mixed`). On the answer, **set the column `unit` to the ISO code** on every returned column — via `cli curate` edit ops (`{op:edit, kind:table, area, name:<table>, column:<col>, field:unit, value:"INR"}`), one batch. The runtime + chart renderer format the symbol + grouping **deterministically** from `unit` (`semantic_model/units.py`) — no prose caveat to re-interpret. `Mixed` → leave `unit` unset, one-liner. (Non-currency units — `cents`, `percent`, `days` — set `unit` the same way when a column's scale/unit is unambiguous.)
- **Date encodings (auto-sniffed — no question):** introspection already sets `date_format` (`epoch_s`/`ms`/`us`/`ns`, `yyyymmdd`, `iso8601`) + `timezone` on date-named columns whose sample values fit the shape, so epoch integers render as human dates (UTC) deterministically. You don't ask. **Do** glance at the result: if a time column was missed or mis-scaled (e.g. an epoch_ms tagged epoch_s), fix it with a `field:date_format`/`field:timezone` edit op in the same batch; mention any epoch columns found in the Phase 7 summary.

**Write all of these with `sm curate` edit ops — never hand-edit `tables/*.yaml` or script a loop over them.** One ops array, one call (validated + committed + reverted on failure):
```bash
bash "$AGAMI_PLUGIN_ROOT/scripts/sm" curate "$ROOT" --ops-file /tmp/agami-caveats.json
```
```json
[{"op":"edit","kind":"table","area":"main","name":"orders","field":"caveats","value":["Order total can be negative on refunds."]},
 {"op":"edit","kind":"table","area":"main","name":"orders","field":"default_filters","value":["{alias}.deleted_at IS NULL"]},
 {"op":"edit","kind":"table","area":"main","name":"events","column":"ts","field":"value_transform","value":"TO_TIMESTAMP(ts)"}]
```

### 2e — Reintrospect merge

On `reintrospect`, the engine rewrites the structural skeleton. **Preserve hand-edits**: descriptions, entities, metrics, caveats, value_transforms, and trust sign-offs (`confidence`/`review_state`/`signed_off_*`) carry over for tables/columns that still exist. Only structure the DB unambiguously reports (table list, columns, types, PK, FK) is refreshed. Mark entries `stale` only when their underlying column/table changed.

### 2f — Auto-draft ORGANIZATION.md if the user didn't write one

ORGANIZATION.md must never be blank. **Generate it ONLY on the skip path** — when it's **missing or empty** (the user gave no org context of their own: skipped 1.4, or only HTML comments remain). If the user **did** provide context (wrote a paragraph in 1.4 → prose beyond comments), leave it untouched — never overwrite their words with an auto-summary.

```bash
bash "$AGAMI_PLUGIN_ROOT/scripts/sm" org-draft "$ROOT" > "$ROOT/ORGANIZATION.md"
chmod 600 "$ROOT/ORGANIZATION.md"
```

`org-draft` writes a concise **summary**, NOT a re-listing of the model: org name + counts (N tables across M areas), the subject areas (bounded — names + table counts, never the table list), cross-cutting conventions (currency/units), and the domain glossary (`key_terminology` + enum legends). It does **not** enumerate every table, metric, or entity — those already live in the structured model and would be unusable at scale (a DB with thousands of tables would dump a thousand lines). It invents no business semantics. Mention in the Phase 7 summary that ORGANIZATION.md was auto-summarised and is theirs to edit.

---

## Phase 3: Review the subject-area split

The engine **proposes** the split (one area for small DBs; prefix-family clusters for large ones, each table owned once, cross-area joins as `cross_subject_area_relationships`). For a multi-area split, **surface it for the user to adjust** — boundaries are a curation decision, not a fact:

```
I split <N> tables into <A> subject areas:
  • <area1> — <t1, t2, …>
  • <area2> — <…>
<C> joins span areas (kept as cross-area relationships).
```

**AskUserQuestion:** `Looks good (Recommended)` / `Adjust — merge/rename/move tables (Other field)` / `Open the model explorer`. If they adjust, edit the `subject_areas/` tree accordingly and re-validate (sizing warns at 25 tables, errors at 30). For a single-area small DB, skip this phase silently.

---

## Phase 4: Curate before examples — exclude + sign off what seeds depend on

Seeds reference **columns, tables, metrics, and entities** — so settle those *before* generating examples (a seed that uses a column you'd later exclude breaks at query time, and a seed built on an unreviewed metric bakes in a guessed definition). **Relationships are NOT gated here** — they stay lazy: FK joins are already auto-approved by the engine (the DB declared them), and inferred joins self-approve as you query / surface as receipt warnings. So you're not asked to rubber-stamp database-declared foreign keys.

**4a — Exclude columns/tables you don't want queried.** Two parts:

**(i) Proactively surface what introspection flagged `sensitive`** — group the flagged columns by table/kind and offer the riskiest as a **multiSelect** quick-exclude, secrets first (a plaintext password / access-code column → *"strongly recommend dropping"*). **Cap at the 3 riskiest groups** — never try to enumerate every sensitive column, the modal only holds 4 options.

**(ii) ALWAYS include "Open the model explorer" as an option** — it's the real bulk-exclude surface (per-table/column Exclude toggles + Mark-PII), and there are almost always more exclusions than fit in a modal. If the user picks it **or answers Other** ("there are a bunch more I want to exclude"), **do NOT parse a free-text list of tables** — invoke `/agami-model`, **end the turn**, and wait for their exclude batch.

`AskUserQuestion` shape: each detected group is a checkbox (e.g. `users.access_code_plain + _hash — plaintext/hashed login secrets, recommend dropping`), plus **Open the model explorer (to exclude more)** and **Nothing to exclude — continue**. Apply the checked groups via the model-explorer curate path; route the explorer/Other choice to `/agami-model`. The loader then drops every exclusion so seeds never reference them.

> **MANDATORY: include the explicit "Nothing to exclude — continue" option, and NEVER tell the user to "leave all unchecked."** `AskUserQuestion` cannot be submitted with zero boxes checked — so "keep everything" must be a *selectable* option, not an implied empty state. A modal whose only escape is "leave it blank" traps a user who doesn't want any of the listed exclusions (they can't submit). Phrase the prompt as "check any to drop, or pick *Nothing to exclude — continue*" — do **not** write "leave all unchecked to keep everything."

**4b — Sign off metrics + entities.** These define what seeds *mean* and *say*. Count via `sm review-items "$ROOT" --scope preseed` — its length is the sign-off count (metrics + named-filters + entities needing review; relationships excluded). If **0**, surface the one-liner ("Nothing to sign off before examples — proceeding") and continue to Phase 5. If **> 0**, tell the user upfront, then invoke `/agami-model preseed` (the `preseed` argument opens the dashboard on its **Review** tab, where these metrics + entities sit under "Needs your eyes"). **End the turn** and wait for their approval batch.

> **4b is NOT a choice — it OPENS the explorer.** When the count is > 0, you **open `/agami-model preseed` and end the turn**. Do **NOT** present an `AskUserQuestion` offering "continue to examples now" vs "open explorer" here, and do **NOT** mark continuing as Recommended. The explorer-first review is the designed path; a high count (dozens of items across many schema-areas) is a reason to open it, not to skip it (the user bulk-approves the *Looks right* pile and eyeballs the cross-schema joins there). The "continue anyway" option exists **only** at the 4c return gate — i.e. **after** the user has already been in the explorer at least once. Never surface the 4c choice before 4b has opened the explorer.

**4c — return gate:** when they're back (they have now opened the explorer at least once), recount via `--scope preseed`. If 0 → Phase 5 (the seed command runs clean). If > 0 (partial — they reviewed some and stopped) → AskUserQuestion: `Continue (Recommended)` (seeds run against current state; receipts warn) / `Pause — I'll finish review first` (end; resume via `/agami-connect`). This is the **only** place "continue to examples with items still unreviewed" is offered — and the **only** place you pass `seed-examples --after-review` (to bypass the preseed-review refusal, which is otherwise the engine telling you Phase 4 hasn't happened).

On `reintrospect` with no new exclusions and nothing unreviewed, skip silently.

---

## Phase 5: Seed prompt examples

**Surface a progress warning first** (second-longest phase): "Generating NL→SQL seeds (a spread across your tables, metrics, entities, and joins — including cross-schema ones) and EXPLAIN-validating each against the live DB. Expect 1–3 min (longer on cloud / a multi-schema DB). I'll narrate per-example progress."

**5a — generate** (your job — the LLM step) candidate examples grounded in the model. Aim for **coverage and a good spread**, not a fixed count:

- **Shapes:** a count, a top-N, a time-bucketed trend, a breakdown, a recency filter. Anchor time filters to each table's `data_range` MAX (not `NOW()`) so seeds don't return 0 rows on a stale dataset.
- **Spread:** across the whole set, touch each major table/family, exercise **every approved metric at least once**, use the key **entities** by their real names/synonyms, and cover **representative joins** — intra-area *and* cross-schema.
- **Cross-schema seeds — the highest-value few-shots, include them deliberately.** Read the model's `cross_subject_area_relationships` (the cross-schema / cross-area joins). For the most meaningful ones, write a question a user would actually ask that **spans both schemas** — a fact/metric in one schema joined to the entity it references in another. Use the relationship's declared join (`on:` / from→to columns) and **schema-qualified** table names (it's one database — ordinary cross-schema SQL, fully EXPLAIN-able). Include **2–4** of these and make **1–2 genuinely complex** (3+ tables across two schemas, with an aggregation + a filter) — cross-schema SQL is exactly what NL→SQL gets wrong unaided, so a worked example pays off most here. (Cross-*profile* / federation seeds are out of scope — those need DuckDB ATTACH; this is cross-*schema* within one DB.)
- **Count scales with the model:** a single-schema DB is fine at ~10–12; a multi-schema one wants ~2–3 per subject area **plus** the 2–4 cross-schema seeds — don't undersample a 5-schema DB at a flat 12.

Tag each with its `tables` (list **all** tables it touches — both schemas for a cross-area one), `columns`, and `metric`. **Store a cross-area seed under its primary/driving table's area** (tagged with both schemas' tables, so the ranker can surface it from either side). Write the candidates as a JSON array to `/tmp/agami-seeds.json` — each: **required** `question`, `sql`; optional `tables`, `columns`, `metric`.

**5b+5c — validate + write in ONE packaged call.** **Do NOT write a script to loop EXPLAIN over the seeds and don't hand-write the YAML.** `seed-examples` validates every candidate against the live DB (wraps each as a zero-row query — dialect-agnostic, scans nothing), writes the passing ones (appends, dedups by `question`, commits), and returns the rejects with their DB error:
```bash
bash "$AGAMI_PLUGIN_ROOT/scripts/sm" seed-examples "$ROOT" --area <area> --profile <profile> --file /tmp/agami-seeds.json
```
Output `{added, written, committed, rejected:[{question, error}]}`. For each `rejected`, optionally regenerate once and re-run with just those; don't block the flow on a few drops. Corrections later append via `/agami-save-correction` (same `add-example` path).

> **The command enforces Phases 2 and 4 for you.** It runs two gates before writing anything:
> - `{refused: "columns_unenriched", unenriched_tables}` → a table got no column descriptions at all (you skipped the Phase-2 column pass). Go back, run the column pass on those tables, re-run. **NOT bypassable** — naked columns degrade every answer.
> - `{refused: "preseed_review_pending", pending_count}` → metrics/entities the seeds depend on are still unreviewed (you skipped the explorer-first review). **Go back to Phase 4b**, open `/agami-model preseed`, end the turn. Bypass only via `--after-review`, and **only** on the Phase-4c return path (the user has already been in the explorer and chose to continue with some items unreviewed). Never pass `--after-review` to force past a *fresh* refusal.

---

## Phase 6: Validate every seed example (the trust onboarding)

**Run every seed with ONE packaged call — `sm seed-validate` — never a hand-rolled "run all the seeds" script.** It executes each written seed against the live DB **through `execute_sql.py`** (the same path agami-query uses), so the **fan-trap / chasm-trap pre-flight + `default_filters` always apply** — a raw-connection driver could skip that safety and let a fan-out scan the whole table. It emits the examples-validation items (`{n, question, sql, row_headers, row_preview, row_count, state}`); a seed the pre-flight refuses or that errors comes back with its `error`, not a faked result:

```bash
bash "$AGAMI_PLUGIN_ROOT/scripts/sm" seed-validate "$ROOT" --area <area> --profile <profile> > /tmp/agami-examples-items.json
```

(Heads-up before you run it: on a large warehouse this executes every seed for real — some may be full-scan aggregates that take a few minutes and cost warehouse compute. It's a one-time onboarding step; surface a one-liner so the wait is expected, and run it in the background if it's slow.)

Then render the examples-validation dashboard (per-profile subdir) from those items:

```bash
python3 "$AGAMI_PLUGIN_ROOT/scripts/render_examples_validation.py" \
  --items-file /tmp/agami-examples-items.json \
  --out "$HOME/.agami/examples-validation/<profile>/<ts>.html"
```

The user reviews matches (green) / mismatches (red) with drill-down. This is the strongest "do these numbers match?" trust moment — surface it.

**Then END THE TURN and WAIT — do NOT continue to Phase 7/8 in the same message.** Like the Phase 4 gate, this is a hard stop: render the dashboard, give the one-line hand-off + the chat grammar (`approve` / `reject` / `edit` / `done`), and **stop**. The user is *in* the dashboard validating; printing the post-introspect summary or the "things you could ask" closing now (before they've validated) is exactly the bug we're avoiding. Re-render after each batch they send back, and only when they reply **`done`** (or have actioned every example) do you proceed to Phase 7.

Hand-off line (then end the turn):
```
Examples validated against your live DB → ~/.agami/examples-validation/<profile>/<ts>.html
Open it: green = numbers match, red = mismatch (drill in to see the SQL).
Reply: approve N · reject N · edit N · done (when you're through).
```

**Processing the batch:** the dashboard emits `validate N` / `reject N` / `edit N` / `add example` / `note N` / `note all` — apply each through the **packaged commands**, never by hand-rewriting `examples.yaml`:
- **`validate N`** → the example is a trusted anchor; if the user signed in (the `by <email>` clause), stamp it via `sm add-example` (re-write the same example with `--signer`/`--role`).
- **`reject N`** → **`sm remove-example "$ROOT" --area <area> --question "<that example's exact question>"`** (`--signer`/`--role` to record who). It flags the example `status: rejected` — kept in `examples.yaml` for audit, dropped from the runtime ranker. **Do NOT hand-delete or hand-rewrite the YAML** (there's a command now); map `N` → the example's question from the items you rendered.
- **`edit N`** that fixes *that example's* SQL → re-write it with `sm add-example` (dedups by question, so it replaces). Re-run the new SQL to capture its number.

When the user gives a **cross-cutting display/formatting rule** (currency, units, number formatting — applies to many examples, not one), classify it like any correction (see [`agami-save-correction`](../agami-save-correction/SKILL.md) → "DISPLAY / FORMATTING preference"): a currency/unit fact attaches to the **column** (a `caveat`/`value_transform` in the shared model — org-wide by construction); a cross-cutting presentation convention → `ORGANIZATION.md`; a personal tic → `USER_MEMORY.md`. Don't reflexively file to USER_MEMORY, and don't reflexively ask — only ask if personal-vs-org is genuinely unclear. **Display rounding is a convention, not SQL** — a "show fewer decimals" rule → `ORGANIZATION.md`, never `ROUND()` baked into the example's SQL (that corrupts the exact-number verification anchor; the system formats numbers in full via `units.py`).

**`note all >>>…<<<` — the model-wide note. Apply it ONCE; the user never repeats themselves.** The dashboard has a "note for the whole model" box (separate from per-example `note N`). Its content arrives as a single **`note all >>>…<<<`** block. Treat it as one correction that applies across the board: classify it like any other (see [`agami-save-correction`](../agami-save-correction/SKILL.md)) and write it to the **right place, once** — a column `unit`/`caveat`/`value_transform` (a data fact: "amounts are in INR", "TOTAL can be negative"), a cross-cutting presentation convention → `ORGANIZATION.md`, a personal display tic → `USER_MEMORY.md`, or a filter/business rule the user wants applied → the relevant column/example. Then **re-render so every affected example reflects it**, and name where it landed in one line — *"Got it — set `<col>`'s unit to INR model-wide; it'll show on every example now."* If the user is still typing the same thing into per-example `note N` boxes, that's a smell: lift it to a single model-wide change and tell them they don't need to repeat it. (Formatting is just the common case — the rule is generic: any cross-cutting fact is stated once, written once, applied everywhere.) Note the result preview is already unit-formatted — `sm seed-validate` runs numbers through the same `units.py` as the live query path — so a money column showing a **bare** number means a missing column `unit`, not a per-example note.

### 6a — A bad number is the highest-value catch: fix it COMPLETELY, and ask in PLAIN language

Validation often surfaces a result that's *obviously wrong* — a bounded ratio averaging to a huge negative number, a "count" of 0, an alphabetically-sorted "trend." This almost always means a **data-quality** problem: sentinel/junk values in a column (encoded nulls / "not computed" markers stored as extreme numbers like ±1e9), a mis-typed column (a date stored as a string), or similar. This is the most valuable thing onboarding can find — handle it deliberately:

1. **Diagnose the cause** with a quick probe — `MIN`/`MAX`, and a small histogram (`COUNT` per coarse bucket) so you can SEE where the junk sits (sentinels show up as a sharp spike at an absurd value, separated by a gap from the real data). Confirm what's actually wrong before proposing a fix.

2. **Exclude the SENTINELS — do NOT clip to a "textbook" range.** The bug is junk values, not real-but-extreme data, and the two are easy to confuse. Don't reflexively clamp to the range you *expect*: many columns legitimately pass their "obvious" bounds — a ratio can exceed 1 when its numerator really can exceed its denominator, an age can be 0, a balance can be negative — and clipping to the expected range silently drops real (often the most important) records and biases the result. Cut **only** the implausible sentinels (the absurd spike), and **when the line between "junk" and "legitimate-but-extreme" is unclear, ask the user for the real valid range — that's the user's domain knowledge, not something to guess.**

3. **Fix it at EVERY level the bad data reaches — not just the seed.** A contaminated column poisons *three* paths, and fixing one leaves the others broken:
   - the **seed example** itself → correct its SQL;
   - any **metric** whose `bindings` SQL touches that column → guard the binding (wrap the aggregate in a `CASE WHEN <col> BETWEEN <lo> AND <hi> THEN <col> END`, with bounds drawn from the distribution + the user's domain input — never a hard-coded "textbook" range), and update its `calculation` prose to match;
   - **ad-hoc questions** that aggregate the column without naming the metric → add a **caveat** on the column (state the sentinel values to exclude, plus any non-obvious valid range the user confirmed) so the SQL generator guards them too.
   Use a `caveat` for this (advisory steer), NOT a `default_filter` (that wrongly drops the whole row from every query) and NOT a `value_transform` (you can't cleanly sanitize a sentinel). Apply all of it in **one `sm curate` batch**; keep any signed-off metric `approved` and re-stamp `signed_off_at` (the user is re-vetting the corrected definition).

4. **Ask ONE plain-language question — about the NUMBER, never the model's vocabulary.** A first-time user does not know what "bindings" or "caveats" are and cannot be asked to choose between them. **Never surface those words in the question.** Frame it by the wrong number and the consequence, in the user's own domain terms, and make the **complete fix the recommended default**. The skill decides *what* to edit; the user only confirms *whether* to fix (and, when the valid range is ambiguous, confirms that range). The pattern below is the *shape* — fill it with the user's actual column, value, and domain, not these placeholders:
   > The average <column> came out as **<impossible value>**, which can't be right — some rows hold junk values (e.g. <absurd sentinel>) that look like an encoded "missing", not a real <column>. I'd leave those out. One thing to confirm: can <column> legitimately go past <expected bound> in your data? If so I'll keep those — they look like the records that matter most. Want me to fix it?
   > • **Fix it everywhere (recommended)** — agami leaves the junk values out whenever it works with <column> (this metric and any question that uses it).
   > • Just fix this one example — leave everything else as-is.
   > • Leave it for now — I'll note it; you can fix it later.

   On **"Fix it everywhere"** → apply the seed + metric-binding + column-caveat edits as one batch. Do **not** present a "patch the bindings vs add a caveat" choice — that's the skill's call, not the user's. (If the user is technical and asks for the detail, then show it.)

---

## Phase 7: Post-introspect summary (MANDATORY — NEVER SKIP)

**Sequencing gate (read first):** Phases 7–8 run **only after the user has finished validating** — i.e. after they replied `done` (or actioned everything) at the Phase 6 examples gate, and after the Phase 4 curate gate returned. **Never print this summary or the Phase 8 closing in the same turn that you rendered a dashboard, and never while the user is mid-review.** If a dashboard (Phase 4 review, Phase 6 validation) is still open and unanswered, you should have ended the turn there — wait for the user, don't summarize over them.

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

Then **AskUserQuestion**: `Open the review queue` (→ `/agami-model review` — sign off the pending metrics/entities) / `Browse the full model` (→ `/agami-model` — explore + exclude tables/columns) / `Skip — I'll review later` (default). If a sibling skill isn't built yet, omit that option — don't error.

---

## Phase 8: Follow-up suggestions

(No telemetry — agami has none; don't surface anything about it.)

**8a — gate on Rule 1 status:** count metrics/named-filters with `review_state != approved`. If > 0, use the **in-progress** framing (8b); else the **fully-set-up** framing (8c). Unsigned Rule 1 metrics don't *block* queries — agami still answers — but an answer that uses one carries a "not signed off yet" **warning** on its receipt until you approve it, so reviewing them is still worth doing.

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
• /agami-model — browse the whole model and refine it (exclude raw PII / staging tables, edit descriptions).
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
• /agami-model — one dashboard to review & sign off metrics/joins/entities (Review tab),
  exclude tables/columns you don't want queried, add metrics, and edit descriptions.
```
End the turn. Picking a number routes the question into query-database. Keep each suggestion under 80 chars and grounded in real tables.

---

## Phase 8.5: Cross-profile link offer (ONLY when ≥2 profiles now exist)

**Gate:** run this **only** when, after this onboarding, the user has **two or more** onboarded profiles (count distinct onboarded profiles — `<artifacts_dir>/*/org.yaml`, or `[section]`s in `~/.agami/credentials`). Skip entirely for the first/only profile, and skip on a plain `reintrospect` of a single-profile setup. This is the **cross-datasource** case — different databases, joined at query time via federation (Phase 2b.federation in agami-query), declared in `~/.agami/cross_profile_relationships.yaml`. Unlike cross-*schema* joins (same DB, auto-detected — Case 1), cross-*profile* links are **never** auto-detected at introspection: each profile is a separate connection, so there's nothing to confirm by overlap.

**Ask (opt-in, AskUserQuestion):**
> You now have <N> databases connected (`<profileA>`, `<profileB>`, …). Want me to look for likely links between them — so you can ask questions that span both (e.g. join `<profileA>`'s data to `<profileB>`'s)?

Options (no `(Recommended)` — it's the user's call): `Yes — look for links` / `Not now` (default).

**On "Not now":** one line — *"No problem. Say 'link my databases' anytime, or hand-edit `~/.agami/cross_profile_relationships.yaml`."* End.

**On "Yes":**
1. **Propose candidates by name+type** — compare the just-onboarded profile's columns against the other profile(s)' columns (read each model's tables/columns; no DB access needed). A candidate is a column pair with the **same or obviously-equivalent name** (`dept_id` ↔ `department_id`, `customer_id` ↔ `cust_id`) **and a compatible type**, where one side is a key/grain column. Rank by name closeness.
2. **Be honest about confidence.** Say plainly: *"These are name/type guesses — I can't sample-join across two separate databases to confirm them, so they're lower-confidence than the joins inside one DB. They'll prove out the first time a federated query uses them."* Never present them as confirmed.
3. **Confirm each, never auto-write.** Show the candidates (`<profileA>.<schema>.<table>.<col>  →  <profileB>.<schema>.<table>.<col>`) and let the user pick which to keep (multi-select) or skip all.
4. **Write the confirmed ones** to `~/.agami/cross_profile_relationships.yaml` in the format [agami-query expects](../agami-query/SKILL.md) — **merge**, never clobber an existing file (load it, append new entries, dedup by the `from_profile/from_dataset/to_profile/to_dataset` tuple), `chmod 600`:
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
5. **Confirm where it landed** — *"Saved <K> cross-database link(s) to `~/.agami/cross_profile_relationships.yaml`. A question that spans both DBs will now use them (federated via DuckDB). I'll flag low confidence on the first answer that does."* Don't block; this is additive.

---

## Error handling

| Symptom | Action |
|---|---|
| Credentials chmod wrong | Refuse, offer to `chmod 600` |
| Cached connection tool no longer works | Re-detect, update `~/.agami/.config` |
| Catalog denied (no `information_schema`/PRAGMA/dict access) | Engine falls back to probe mode; if even table enumeration is denied, ask for the table allowlist (Phase 1.2) |
| Introspection SQL fails | Route through `db_error_classifier.md`; surface the one-line remediation |
| **Validator fails** | **Model is NOT persisted. Show errors verbatim, fix, re-validate.** |
| EXPLAIN fails for a seed | Auto-fix once → else move to `~/.agami/.rejected/`. Don't block. |
| Reintrospect would lose hand-edits | Phase 2e — preserve descriptions, entities, metrics, caveats, sign-offs |
| Legacy OSI profile at the root | Engine backs it up to `.osi_backup/` before writing; surface a one-liner |
| Unsupported engine (MongoDB, Cassandra, …) | "Not supported yet — supported: Postgres/Redshift/Supabase, MySQL, Snowflake, BigQuery, SQL Server, Oracle, Databricks, Trino, DuckDB, SQLite." |
