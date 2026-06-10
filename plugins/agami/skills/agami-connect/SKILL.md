---
name: agami-connect
description: "End-to-end database connection for agami: sets up credentials on first run (DB-type picker → writes ~/.agami/credentials.example for the user to fill in), then introspects the live DB directly into the agami semantic model (subject areas, tables, columns, relationships with join cardinality, deep-table column groups, sensitive-column flags) under <artifacts_dir>/<profile>/. The structural model is built deterministically by scripts/semantic_model (catalog mode, or a probe-mode fallback when the catalog is locked down); the skill then layers LLM enrichment (descriptions, entities, metrics) and seeds EXPLAIN-validated NL→SQL examples. Every model write is gated by the semantic-model validator — no breaking model is ever persisted."
when_to_use: "Run when the user installs the plugin for the first time, asks 'how do I set up agami' / 'connect to my database' / 'introspect my database' / 'introspect the schema' / 'reload schema' / 'add a new database', or after the user changes their schema and wants the model refreshed. Also auto-invoked by agami-query-database the first time it runs (when the semantic model is missing). This skill handles credential setup, introspection, enrichment, and seed-example validation — one entry point for everything before the user can query."
argument-hint: "[reintrospect | profile NAME]"
---

# agami connect

**Before suggesting any slash command in chat, read [`shared/invocation-conventions.md`](../../shared/invocation-conventions.md).** Agami slash commands: `/agami-connect`, `/agami-query-database`, `/agami-review`, `/agami-model`, `/agami-save-correction`, `/agami-reconcile`. Never write the un-prefixed forms (`/init`, `/connect`, etc.) or colon forms (`/agami:connect`) — those don't exist. For chat replies, prefer natural language ("say 'reload the schema'", "say 'introspect my database'") — the `when_to_use` matcher routes correctly without an explicit slash command.

You are setting up the agami **semantic model** for the user's database. Goal: by the end there is a validated semantic model at `<artifacts_dir>/<profile>/` (`org.yaml` + `subject_areas/<area>/…` + `datasources/<connection>/storage.yaml`), a seeded examples library at `<artifacts_dir>/<profile>/prompt_examples/<area>/examples.yaml`, an `ORGANIZATION.md` the user can edit, and the user has seen one demo query execute end-to-end.

**The structural model is built by a deterministic engine, not hand-authored.** `bash "$AGAMI_PLUGIN_ROOT/scripts/sm" introspect` introspects the live DB across all supported dialects — **PostgreSQL (incl. Supabase / Redshift), MySQL/MariaDB, Snowflake, BigQuery, SQL Server, Oracle, Databricks, Trino/Presto, DuckDB, SQLite** — into the model: storage connection, proposed subject areas, tables, columns + types, primary-key grain, foreign-key relationships **with join cardinality**, `column_groups` on wide tables, and `sensitive` flags on PII. When the catalog (`information_schema` / PRAGMA / data-dictionary) is reachable it runs in **catalog mode**; when a locked-down role denies the catalog it falls back **per-capability to probe mode** (describe via a zero-row header, infer types from a value sample, grain from uniqueness probes, FKs from name+overlap) and everything inferred lands `unreviewed` for sign-off. Your job is the layer the engine can't do: **enrichment** (prose descriptions, entities, metrics, caveats) and **curation** (subject-area boundaries, trust review).

For the model format: [`scripts/semantic_model/__init__.py`](../../scripts/semantic_model/__init__.py) (layout) and the Pydantic models in `scripts/semantic_model/models.py`.
For credentials: [`shared/credentials-format.md`](../../shared/credentials-format.md).
For connection method + local execution: [`shared/connection-reference.md`](../../shared/connection-reference.md).
For DB error classification: [`shared/db_error_classifier.md`](../../shared/db_error_classifier.md).

## Conversation style

- **Combine acknowledge + next question** — don't waste turns on "Got it!"
- **Use AskUserQuestion for every Yes/No/Skip** — never inline-bullet options. Use `(Recommended)` only when there's a genuine recommendation. For fact-of-environment questions ("which database type?", "which schemas?"), don't mark any option Recommended — the user picks what they have.
- **Keep the user oriented** — print one-line progress markers between phases (`✓ Introspected 12 tables`, `✓ Validator passed`, `✓ Generated 10 examples`).

## Progress tracking — set up a todo list at the very start

This is a multi-phase skill that often takes 5–15 minutes end-to-end. **The very first action on every invocation is to call `TodoWrite`** with the skill's major phases, so the user can watch progress. Validated as a strong UX signal — it makes the wait feel intentional rather than opaque.

Seed (one task per major phase, in order):

```
1. Preflight: credentials check + tool detection
2. Introspect database → semantic model (engine: tables, columns, grain, FK cardinality)
3. Enrich: descriptions, entities, metrics (LLM, validated into the model)
4. Review subject-area split + trust queue (relationships/metrics sign-off)
5. Generate seed NL→SQL examples (EXPLAIN-validated)
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
4. **If credentials are missing for the active profile, run Phase 0a.** After the user fills in the template they re-invoke (or just ask a data question — `agami-query-database` auto-invokes us).
5. **NEVER put a credential on a Bash command line** — no `export PGPASSWORD=…`, no `psql -W <pw>`, no heredoc that interpolates a secret. Hosts render Bash calls in chat; anything on the line leaks. Runtime queries use the auth files from `scripts/setup_pgauth.py` (psql/mysql) or `scripts/execute_sql.py` (every driver, reads `~/.agami/credentials` itself). See [`shared/connection-reference.md → HARD RULES`](../../shared/connection-reference.md).

If you reach for a command that doesn't fit, stop and re-read this section.

### Preflight steps

1. **Resolve `<profile>`**: `AGAMI_PROFILE` → `active_profile` in `~/.agami/.config` → `"main"` (older installs may have `"default"`). The model's `organization` equals `<profile>`.
2. **Credentials check (binding).** Read `~/.agami/credentials`; look for `[<profile>]`.
   - File present with the section → apply the chmod check (refuse if world-readable), continue.
   - File missing **but `~/.agami/credentials.example` exists** → the user filled in the template; **run 0a.10 to promote it** (don't re-run 0a.4 — that would overwrite their edits).
   - Neither present → **run Phase 0a and stop.** Surface: *"No credentials yet for profile `<profile>` — running setup."*
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

### 0a.3 — Pick a profile name
> What should I call this connection? You'll use this name to switch databases later (e.g. `AGAMI_PROFILE=production`).

Options (`main` Recommended, first): `main` / `production` / `staging`. Validate: lowercase letters/digits/dashes/underscores, 1–32 chars. Bind `$PROFILE_NAME`.

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
The user filled in `~/.agami/credentials.example` and came back (or asked a data question / said "introspect my database"). **Promote it for them** — no manual save, no `chmod` step, no helper script. One command: the `mv` consumes the template (we don't keep `.example` around); the `grep` guard refuses to promote a still-unedited template:

```bash
if [ ! -f ~/.agami/credentials ] && [ -f ~/.agami/credentials.example ]; then
  if grep -qE 'your-(username|password|host|server|workspace|coordinator|database|token)|dapiXXX|/absolute/path/to|user:pass@host' ~/.agami/credentials.example; then
    echo "PLACEHOLDERS_REMAIN"
  else
    mv ~/.agami/credentials.example ~/.agami/credentials && chmod 600 ~/.agami/credentials && echo "SECURED"
  fi
fi
```
- `PLACEHOLDERS_REMAIN` → tell the user which fields still hold template values and **stop** (never introspect against a template).
- `SECURED` → `~/.agami/credentials` now exists (chmod 600, `.example` consumed). Preflight step 2 passes.

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

If `<artifacts_dir>/<profile>/org.yaml` exists and `$ARGUMENTS != reintrospect`: the profile is already onboarded. Offer (AskUserQuestion): re-introspect (refresh structure, preserve enrichment) / open the model explorer / cancel. The engine **auto-backs-up any legacy OSI** (`index.yaml` + per-schema `_schema.yaml`) it finds at the profile root into `.osi_backup/` before writing — so a first run over an old OSI profile is safe and reversible; surface a one-liner when that happens.

### 1.2 — Scope: schemas, and the no-catalog case

Run `cli areas`/probe is not needed yet — schema discovery happens inside the engine. But **decide scope first**:

- **Catalog reachable (common):** after the engine lists schemas, it introspects all of them. If the DB has many schemas (Snowflake with 50+), narrow first — ask the user which schemas matter (multi-select), then pass them as the engine's table allowlist scope. Pre-check `public` (Postgres) / `PUBLIC` (Snowflake) / the credentials' `database` (MySQL).
- **Catalog denied (locked-down role):** if a quick probe shows the catalog isn't readable, the engine **cannot enumerate tables** — ask the user for the table list:
  > Your role can read the data but not the catalog, so I can't list tables automatically. Paste the tables to model (e.g. `sales.orders, sales.customers`) — I'll describe each from the data itself.

  Pass these to the engine via `--tables schema.table …`. Everything the engine then infers (types, grain, FKs) lands `unreviewed` for sign-off.

### 1.3 — Schema picker (multi-select)

For non-SQLite/DuckDB with multiple schemas, **AskUserQuestion** multi-select: "Which schemas should I introspect?" One option per schema + `All schemas` + `Just <default> for now`. Record `selected_schemas`; the engine scopes to these.

### 1.4 — Organization context (MANDATORY — ALWAYS ASK)

This runs on **every** invocation. The user's yes/skip is theirs; the skill never decides for them. "don't ask clarifying questions" does NOT cancel this — it's required state-gathering, not a clarifying question. **Only conditional skip:** `ORGANIZATION.md` exists and has been edited beyond the template.

**AskUserQuestion:**
> Want to give me a one-paragraph description of what this database is about? It improves NL→SQL accuracy a lot. Examples: what the company/product is, what "MRR" or "active user" means in your terms.

`Yes — I'll type it now (Other field)` → write to `<artifacts_dir>/<profile>/ORGANIZATION.md` under `# About this database` + the commented default template. `Skip — I'll edit ORGANIZATION.md later (Recommended)` → write the template untouched. `chmod 600`. See [`shared/organization-context-format.md`](../../shared/organization-context-format.md).

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

### 2a — Descriptions (evidence-grounded; empty is the default)

For each table fetch up to 5 sample rows for evidence (`SELECT * FROM <t> LIMIT 5`; Snowflake `SAMPLE` for >10M rows). **Samples are never written to disk** — context only, then discarded. Also capture MIN/MAX of each time column → record under the table's `performance_hints` so Phase 5 anchors "last 30 days" to the data's real MAX, not `NOW()`.

Build a per-schema prompt with `$DATA_MODEL_DOC_TEXT` first (dominant prior), then `ORGANIZATION.md`, then tables/columns/sample rows. Emit a **1-line** table `description`, and a column `description` **only if it says something the column name + type doesn't.** Empty is preferred — there's no review cost to an empty description.

| Column | Bad (reject) | Good (keep) | Empty (preferred) |
|---|---|---|---|
| `id`, `created_at`, `email` | "Primary key" / "When created" / "Email" | (always empty — structural) | `""` |
| `customer_id` | "The customer ID" | "FK to customers.id; 1:N with orders" | `""` if nothing to add |
| `status` | "A status code" | "lifecycle: pending → shipped → cancelled" (only if enum known) | `""` |
| `revenue_usd` | "Revenue in USD" | "Net revenue, USD at invoice date, excludes refunds" (only if samples/doc support it) | `""` |
| `v_1`, `tmp_col`, `x` | "A value" | (leave empty — opaque) | `""` |

**What NOT to invent:** opaque-name meanings; business semantics not in the samples; name translations (`amt`→"amount"). Write only what tells the user something they couldn't learn from the name + type.

For large schemas (>100 tables) batch 50 at a time; narrate `[batch 2/4] …`. Validate after each schema; on failure, surface errors and continue with the rest, then report which need attention.

### 2b — Entities (the semantic vocabulary)

Propose `entities[]` per subject area — the names users actually say. For each, fill `name`, `plural`, `other_names` (synonyms), `maps_to` (table+column, one `primary: true`), and — for opaque-identifier columns — a `value_pattern` regex (e.g. a VIN `^[A-Z0-9]{17}$`, a `BP`-prefixed serial) so the runtime can recognize literals. Ground these in column names + samples + the domain doc; don't invent entities the schema doesn't support.

**Write them with the packaged command, not by hand** — build a JSON array and run it once (it validates each item, writes `subject_areas/<area>/entities/<slug>.yaml`, validates the whole model, reverts on failure, commits). Never author a throwaway script to loop:
```bash
bash "$AGAMI_PLUGIN_ROOT/scripts/sm" add "$ROOT" --kind entity --area <area> --file /tmp/agami-entities.json
```

### 2c — Metrics

Metrics come from two sources, handled very differently. **Always prefer declared metrics** — schema-only inference is a shallow guess (it finds `AVG(score)`, a row count, an `AVG(FOIR)`; it misses the domain KPIs a lender actually tracks — DPD/delinquency buckets, disbursement, approval rate, PAR — because those aren't visible in column names).

**(A) Declared metrics — extract in FULL, no cap.** If the user attached a semantic-layer repo or a metrics file in 1.5 (`$DATA_MODEL_DOC_TEXT`), those are the org's *real* definitions — pull **every** one, don't sample to 4:
- **LookML** `measure {}` → metric: `type` + `sql` → `bindings`, `label`/`description` → `calculation`, `label`+`view_label` → `other_names`.
- **dbt** `metrics:` / `semantic_models[].measures` (MetricFlow) → name, `agg`+`expr` → `bindings`, `description`/`label` → `calculation` + `other_names`.
- **Cube** `measures` → `sql`+`type` → `bindings`.
- **Metrics file** (CSV/YAML/markdown KPI dictionary the user uploaded) → one metric per row/entry: name, definition → `calculation`, formula → `bindings`.

Translate the declared SQL/agg to the profile's dialect for `bindings`, set `source_tables`, write **`confidence: inferred, review_state: unreviewed`** (declared = strong signal, still wants a human sign-off in `/agami-review`). If there are many (> ~8), **don't** funnel them through a 4-item picker — write them all and tell the user once: *"Added N metrics from your `<LookML/dbt/file>` — review or trim them in /agami-review."* (Offer a single "add all N / let me pick a subset" confirm if you want, but never silently drop declared metrics to fit a cap.)

**(B) Inferred metrics — only when there's no declared source (or to supplement a thin one).** These genuinely drift, so **suggest, don't auto-add**, capped at ~4 (AskUserQuestion fits ~4 + Other) from: aggregate-shaped numeric fields (SUM/AVG), fact tables (`count_<table>`), time fields, `ORGANIZATION.md` KPI mentions. **AskUserQuestion** multi-select: "I'd suggest these reusable metrics — pick which make sense." `Other (Other field)` for "describe a metric I want"; submitting none = skip. Write `confidence: proposed`.

For every metric (A or B) fill `name`, prose `calculation` (intent — **required**), per-dialect `bindings` (the SQL), `source_tables`, `other_names`. **Write them with the packaged command** — build a JSON array and run it once; never hand-write each YAML and never author a throwaway loop script. It validates each item, writes `subject_areas/<area>/metrics/<slug>.yaml`, validates the whole model, and reverts the batch on failure:
```bash
bash "$AGAMI_PLUGIN_ROOT/scripts/sm" add "$ROOT" --kind metric --area <area> --file /tmp/agami-metrics.json
```
Don't propose metrics depending on choice-field literals you didn't detect, or cross-area metrics unless a cross-area edge wires the join (then put them under the cross-cutting area).

### 2d — Caveats, value_transforms, currency

From samples + the domain doc, add provider-portable cleaning where evidence supports it:
- **Caveats** (`caveats[]` on table/column/entity): data-quality notes, anti-patterns ("use `tiu_date` not `tiu_time` for date filters"), dedup warnings.
- **value_transform** on columns whose raw value needs cleaning (`regexp_replace(...)` for bracketed text, `TO_TIMESTAMP(...)` for epoch). Must parse as SQL (validator checks).
- **Currency (one ask per profile):** if numeric fields look like money (`amount`/`price`/`revenue`/…, no `_usd` suffix giving the answer), ask once: "What currency are these in?" (`USD`/`EUR`/`GBP`/`JPY`/`INR`/`Other`/`Mixed`). Record it as a caveat on those columns (e.g. "Amounts in INR.") so charts/totals format correctly. `Mixed` → leave unannotated, one-liner.

### 2e — Reintrospect merge

On `reintrospect`, the engine rewrites the structural skeleton. **Preserve hand-edits**: descriptions, entities, metrics, caveats, value_transforms, and trust sign-offs (`confidence`/`review_state`/`signed_off_*`) carry over for tables/columns that still exist. Only structure the DB unambiguously reports (table list, columns, types, PK, FK) is refreshed. Mark entries `stale` only when their underlying column/table changed.

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

## Phase 4: Trust review — sign-off before examples

Relationships, metrics, and entities carry a trust block (`confidence` ∈ confirmed/inferred/proposed, `review_state`, `signed_off_*`). Mirror the hybrid review order:

- **Rule 1 (sign-off required NOW):** metrics + named filters — they drive what the seed examples *mean*. If Phase 5 fires against unreviewed metrics, the seeds exercise a guessed definition.
- **Rule 2 (lazy, after-the-fact):** inferred/proposed relationships + field descriptions — they surface as receipt warnings on the answers that use them and self-approve as the user queries.

**4a — count Rule 1:** run `sm review-items "$ROOT" --scope rule1` — its length *is* the Rule-1 sign-off count (metrics + named-filters needing review, plus any `stale`). If **0**, surface the one-liner ("No Rule 1 candidates — proceeding straight to seed generation; low-confidence joins surface as warnings later") and continue to Phase 5 — don't silently skip (users expect a review step).

**4b/4c — gate:** if Rule 1 count > 0, tell the user upfront, then invoke `/agami-review rule1` (the `rule1` argument scopes the dashboard to exactly those sign-off items — no env var). **End the turn** and wait for their approval batch.

**4d — return gate:** when they're back, recount. If 0 → Phase 5. If > 0 (partial) → AskUserQuestion: `Continue (Recommended)` (seeds run against current state; receipts warn) / `Pause — I'll finish review first` (end; resume via `/agami-connect`).

On `reintrospect` with no new Rule 1 candidates, skip silently.

---

## Phase 5: Seed prompt examples

**Surface a progress warning first** (second-longest phase): "Generating 10–12 NL→SQL seeds and EXPLAIN-validating each against the live DB. Expect 1–3 min (longer on cloud). I'll narrate per-example progress."

**5a — generate** (your job — the LLM step) 10–12 candidate examples grounded in the model: a count, a top-N, a time-bucketed trend, a breakdown, a recency filter, plus domain-specific ones from entities/metrics. Anchor time filters to each table's `data_range` MAX (not `NOW()`) so seeds don't return 0 rows on a stale dataset. Tag each with its `tables`, `columns`, and `metric`. Write the candidates as a JSON array to `/tmp/agami-seeds.json` — each: **required** `question`, `sql`; optional `tables`, `columns`, `metric`.

**5b+5c — validate + write in ONE packaged call.** **Do NOT write a script to loop EXPLAIN over the seeds and don't hand-write the YAML.** `seed-examples` validates every candidate against the live DB (wraps each as a zero-row query — dialect-agnostic, scans nothing), writes the passing ones (appends, dedups by `question`, commits), and returns the rejects with their DB error:
```bash
bash "$AGAMI_PLUGIN_ROOT/scripts/sm" seed-examples "$ROOT" --area <area> --profile <profile> --file /tmp/agami-seeds.json
```
Output `{added, written, committed, rejected:[{question, error}]}`. For each `rejected`, optionally regenerate once and re-run with just those; don't block the flow on a few drops. Corrections later append via `/agami-save-correction` (same `add-example` path).

---

## Phase 6: Validate every seed example (the trust onboarding)

Run every seed, build the items JSON, and render the examples-validation dashboard (per-profile subdir). The user reviews matches (green) / mismatches (red) with drill-down. Support the chat back-channel grammar (approve/reject/edit/done) and re-render after each batch. This is the strongest "do these numbers match?" trust moment — surface it.

---

## Phase 7: Post-introspect summary (MANDATORY — NEVER SKIP)

Runs on **every** invocation that produces or refreshes a model — even if Phase 4 found nothing and all entries auto-approved. (Past failure: the skill jumped Phase 6 → 8, leaving the user with unreviewed entries and no path to clear them.) Lead with the **must-do** count, break out optional polish separately.

Scan the model; count by `confidence`/`review_state`/type:

```
agami-connect just ran. Here's what we found:

  ✓  <N> tables, <M> columns across <A> subject areas   (structure)
  ✓  <K> relationships with join cardinality              (<E> confirmed from declared FKs)
  ⚠  <R1> inferred/probed relationships                   (review — confirm the join)
  ⚠  <R2> proposed metrics                                (sign-off — Rule 1)
  ✓  <S> sensitive columns flagged (never extracted)

  <R2 + stale> items need your sign-off to start querying.
  <R1> low-confidence joins can wait — they surface as warnings on the answers
  that use them and self-approve as you query.
```
(Omit any zero line. The closing two lines are mandatory — they tell the user "you can ship now; the tail is optional.")

Then **AskUserQuestion**: `Open the review dashboard` (→ `/agami-review`) / `Open the model explorer` (→ `/agami-model`, browse + exclude tables/columns) / `Skip — I'll review later` (default) / `Adjust the threshold`. If a sibling skill isn't built yet, omit that option — don't error.

---

## Phase 8: Follow-up suggestions

(No telemetry — agami has none; don't surface anything about it.)

**8a — gate on Rule 1 status:** count metrics/named-filters with `review_state != approved`. If > 0, use the **in-progress** framing (8b); else the **fully-set-up** framing (8c). Rule 1 items block at runtime (query-database refuses questions depending on an unreviewed metric), so "set up" is misleading while they're pending.

**8b — in-progress:**
```
✓ <artifacts_dir>/<profile>/ — semantic model (<A> subject areas, validated)
✓ prompt_examples/ — <N> NL→SQL examples
⚠ Setup is partial — <rule1_unreviewed> Rule 1 items still need sign-off:
   - <M> metric proposal(s) (run /agami-review)
Until those are reviewed, agami-query-database refuses questions that depend on
them. Run /agami-review (or "open the review dashboard"). Or /agami-model to
browse + exclude tables/columns.

Five things you could already ask that don't depend on Rule 1 items:
1.–5. <count / top-N / time-bucket / breakdown / recency — all on FK-approved tables>
Pick a number, or run /agami-review first.
```

**8c — fully set up:**
```
✓ <artifacts_dir>/<profile>/ — semantic model (<A> subject areas, validated)
✓ prompt_examples/ — <N> NL→SQL examples
✓ All metrics signed off

(Want to remove tables/columns agami shouldn't use? Run /agami-model.)

Now that <profile> is set up, here are five things you could ask:
1.–5. <count / top-N / trend / breakdown / narrative — grounded in the schema's distinctive tables>
Reply with a number, or ask anything else.
```
End the turn. Picking a number routes the question into query-database. Keep each suggestion under 80 chars and grounded in real tables.

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
