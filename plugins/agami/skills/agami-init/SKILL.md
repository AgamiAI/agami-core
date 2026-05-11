---
name: agami-init
description: "First-run setup for agami. Creates the .agami directory in the user's home (chmod 700), asks one short question to determine the database type, writes a credentials.example template the user fills in, and detects which database tool is available (psql / mysql / snowsql / sqlite3 native CLI, DuckDB binary, or the Python driver). Re-run with `verify` to check state, `switch-profile NAME` to change active profile, or `reconfigure-analytics` to re-prompt the telemetry opt-in."
when_to_use: "Run when the user installs the plugin for the first time, asks 'how do I set up agami', wants to add or switch a database connection, or asks to change their telemetry preference. Auto-invoked by agami-connect when ~/.agami/credentials is missing — that's the standard onboarding path."
argument-hint: "[verify | switch-profile NAME | reconfigure-analytics]"
---

# agami init

**Before suggesting any slash command in chat, read [`shared/invocation-conventions.md`](../../shared/invocation-conventions.md).** All four agami slash commands (`/agami-init`, `/agami-connect`, `/agami-query-database`, `/agami-save-correction`) work — the `agami-` prefix avoids collision with Claude Code's built-in `/init` and other plugins. Never write the un-prefixed forms or colon-namespaced forms — those don't exist. For everything except `/agami-init`, prefer natural language over slash commands.

You are walking the user through the one-time setup for `agami`. The goal: by the end of this skill, the user has a working `~/.agami/credentials` file, knows which database tool their machine will use to run queries, and has had `~/.agami/` set up with the right permissions. **You do NOT collect the password inline in chat** — instead you write a per-DB-type template the user fills in (file content has no chat exposure), then `agami-connect` picks up from there.

This skill is idempotent — running it again with no args walks the full first-run flow (overwriting `~/.agami/credentials.example` cleanly); running with `verify` surfaces any drift without modifying anything.

## Conversation style

- **Combine acknowledge + next question** — don't waste turns on "Got it!"
- **Use AskUserQuestion for every choice** — never bullet-list options inline. **Use `(Recommended)` only when there's a genuine recommendation** (e.g. "skip telemetry" is a privacy default we'd suggest). Don't mark it for fact-of-environment questions like "what database do you have?" — the user picks what they have, and labeling one option Recommended is misleading.
- **Keep prompts short** — 2-4 lines per question max.
- **Plain English over jargon** — for telemetry / privacy, sound like a human.

---

## Phase −1: Plan-mode check (before anything else)

Run the detection + ask logic from [`shared/plan-mode-check.md`](../../shared/plan-mode-check.md). If plan mode is active and the user picks `Stay in plan mode` (or this skill is invoked under an active plan-mode context with no prompt available), **refuse to proceed** and end the turn. **DO NOT write a plan file. DO NOT call `ExitPlanMode`.** Users in plan mode want to switch and proceed — not read a description of what would have happened.

Refusal text (verbatim):

> I can't run setup in plan mode. Press Shift+Tab to switch to Default or Auto-accept, then send any message to continue.

If plan mode is not active, skip this phase silently and go to Phase 0.

---

## Phase 0: Decide what to do based on `$ARGUMENTS`

- **No arguments**: Run the full first-run flow (Phases 1–5). The DB-type picker in Phase 2a is the entry point; the user fills in the credentials.example template after this skill writes it, then re-invokes `/agami-connect` (or just asks a data question — which auto-invokes connect).
- **`verify`**: Run Phase 1 (state check) only. Print what's working and what's missing. Exit.
- **`switch-profile <name>`**: Skip to Phase 2 with the profile name pre-set. Help the user add a new `[<name>]` section to `~/.agami/credentials`.
- **`reconfigure-analytics`**: Skip to Phase 4. Re-prompt the opt-in.

---

## Phase 1: State check

Run these checks via Bash, in parallel where possible:

```bash
# 1. Does ~/.agami/ exist with the right permissions?
ls -ld ~/.agami 2>/dev/null

# 2. Does ~/.agami/credentials exist? What are its permissions?
ls -l ~/.agami/credentials 2>/dev/null
stat -f '%A' ~/.agami/credentials 2>/dev/null || stat -c '%a' ~/.agami/credentials 2>/dev/null

# 3. Is AGAMI_DATABASE_URL set?
[ -n "$AGAMI_DATABASE_URL" ] && echo "AGAMI_DATABASE_URL is set"

# 4. Which database tools are available?
which psql mysql sqlite3 duckdb 2>/dev/null
python3 -c 'import psycopg2; print("psycopg2 OK")' 2>/dev/null
python3 -c 'import pymysql; print("pymysql OK")' 2>/dev/null

# 5. Has the user already opted into telemetry?
[ -f ~/.agami/.config ] && cat ~/.agami/.config 2>/dev/null
```

Report findings to the user in 3–5 lines. State only what's relevant — don't print every check.

If `verify` mode: print a one-line status per item (✓/✗) and exit.

---

## Phase 2: Create `~/.agami/` and write credentials template

If `~/.agami/` does not exist:

```bash
mkdir -p ~/.agami
chmod 700 ~/.agami
```

### 2a — Ask the database type

Skip this whole sub-phase if `~/.agami/credentials` already exists OR if `AGAMI_DATABASE_URL` is set. Otherwise, ask the user what kind of database they're connecting to **before** asking for a profile name. The answer determines which placeholder fields go into the template — it's confusing to write a postgres skeleton if the user has a MySQL database.

Use **AskUserQuestion** with this exact shape:

> What kind of database are you connecting to?

**No `(Recommended)` marker on this question** — the user picks based on what they actually have, not on a preference. **Cap at 4 hard options + Other** so AskUserQuestion fits on one screen without the awkward "split into DB type 1 / DB type 2" UX.

| label | description |
|---|---|
| `PostgreSQL` | Postgres + everything Postgres-compatible: Supabase, Neon, RDS, Aurora, Cloud SQL, Timescale, and **Amazon Redshift** (Postgres wire protocol — same psql tool, port 5439, SSL required by default). |
| `MySQL` | MySQL, MariaDB, RDS MySQL, PlanetScale. |
| `Snowflake` | Snowflake. Uses `snowsql` CLI or `snowflake-connector-python`. Account identifier instead of host. |
| `SQLite` | A local `.db` / `.sqlite` file. |
| `Other (Other field)` | Anything else, or paste a DSN string. Accepts `postgresql://`, `redshift://`, `snowflake://`, `mysql://`, `sqlite:///abs/path` and the `+driver` SQLAlchemy variants. |

Bind the chosen type to `$DB_TYPE` (one of `postgres` | `mysql` | `snowflake` | `sqlite` | `dsn` | `other`).

**Routing the chosen option:**

- `PostgreSQL` → `$DB_TYPE = postgres`, but if the user later enters port `5439` or a hostname matching `*.redshift.*.amazonaws.com`, transparently re-bind to `redshift` (different sslmode default, different port). The `psql` tool works either way.
- `MySQL`, `Snowflake`, `SQLite` → straight pass-through.
- `Other` → parse the user's free-form input:
  - If it parses as a DSN (starts with `postgresql://`, `redshift://`, `snowflake://`, `mysql://`, `sqlite://`, etc.) → treat as `dsn`, write `url = ...` in credentials, derive `db_type` from the scheme.
  - If it's a plain word like `bigquery`, `clickhouse`, `databricks`, `oracle`, `mssql` → tell the user that database isn't supported in v1.1 and point at the v1.2 roadmap. Don't write credentials for an unsupported type.
  - If it's a free-form description ("I have an internal tool", "MongoDB") → similar — surface "I don't have first-class support for that yet; only Postgres/MySQL/Snowflake/Redshift/SQLite for v1.1." Don't write.

### 2b — Pick a profile name

Now ask what to call this connection:

> What should I call this database connection? You'll use this name to switch between databases later (e.g. `AGAMI_PROFILE=production`).

Options (mark exactly one Recommended, place it first):

| label | description |
|---|---|
| `main (Recommended)` | Generic catch-all name. Good if you only have one database. |
| `production` | If this is your prod / live database. |
| `staging` | If this is a staging / dev / pre-prod database. |
| (Other auto-provided) | The user types any short lowercase name — e.g. `supabase`, `analytics`, the company name. |

After the user picks (or types), validate the chosen name:

- Lowercase letters, digits, dashes, underscores only.
- 1–32 characters.
- Strip any leading/trailing whitespace; lowercase the input.
- If invalid, surface a tight error ("name must be lowercase letters/digits/dashes/underscores, 1–32 chars") and re-ask.

Bind this validated name to `$PROFILE_NAME`.

### 2c — Write `~/.agami/credentials.example` (per-DB-type template)

Write `~/.agami/credentials.example` using the **Write tool**. Pick the body **based on `$DB_TYPE` from 2a** and substitute `[$PROFILE_NAME]` for the section header. The shared header (top comment block) is the same for every type; the active section differs.

**Shared header (always written first):**

```ini
# ~/.agami/credentials
# Fill in your values and run: chmod 600 ~/.agami/credentials
# Format reference: plugins/agami/shared/credentials-format.md
# Switch profiles with AGAMI_PROFILE=<name>.
```

**If `$DB_TYPE = postgres`**, append. **Lead with the URL form** — that's what Supabase / Neon / RDS / Heroku / Railway hand you. The per-field form is the alternative for self-hosted Postgres where the user knows the parts:

```ini
# Postgres profile.
# Fastest path: paste your connection URL (Supabase, Neon, RDS all give you one).
[$PROFILE_NAME]
type = postgres
url  = postgresql://user:password@host:5432/database
# (Accepts postgresql://, postgres://, postgresql+asyncpg://, postgresql+psycopg2://)
# (Query params like ?sslmode=require are honored automatically.)

# --- OR, instead of `url`, fill in the fields below (typical for self-hosted) ---
# host     = your-host.example.com
# port     = 5432
# database = your-database-name
# user     = your-username
# password = your-password
# sslmode  = require        # uncomment for cloud DBs
```

**If `$DB_TYPE = redshift`**, append:

```ini
# Redshift profile.
# Provisioned cluster: your-cluster.<region>.redshift.amazonaws.com
# Redshift Serverless: <wg>.<acct>.<region>.redshift-serverless.amazonaws.com
# Default port 5439. SSL required.
[$PROFILE_NAME]
type = redshift
url  = redshift://user:password@your-cluster.us-west-2.redshift.amazonaws.com:5439/db

# --- OR, fill in fields ---
# host     = your-cluster.example.region.redshift.amazonaws.com
# port     = 5439
# database = your-database
# user     = your-username
# password = your-password
# sslmode  = require
```

**If `$DB_TYPE = snowflake`**, append:

```ini
# Snowflake profile.
# Account formats: xy12345  (legacy)
#                  xy12345.us-east-1.aws  (locator + region + cloud)
#                  myorg-myaccount  (newer org-account form)
# Do NOT add .snowflakecomputing.com — the connector appends it.
# Required: account, user, password (or authenticator).
# Optional: warehouse, database, schema, role.
[$PROFILE_NAME]
type      = snowflake
account   = your-account-locator
user      = your-username
password  = your-password
warehouse = COMPUTE_WH
database  = ANALYTICS
schema    = PUBLIC
role      = ANALYST_ROLE

# For SSO (Okta / Azure AD / etc.), remove the password line above and use:
# authenticator = externalbrowser

# OR — DSN form (path is /database/schema; query params carry warehouse/role):
# url = snowflake://user:pass@xy12345.us-east-1.aws/ANALYTICS/PUBLIC?warehouse=COMPUTE_WH&role=ANALYST_ROLE
```

**If `$DB_TYPE = mysql`**, append:

```ini
# MySQL profile.
# Fastest path: paste your connection URL (PlanetScale, Aiven, RDS all give you one).
[$PROFILE_NAME]
type = mysql
url  = mysql://user:password@host:3306/database
# (mysql+pymysql:// also works; the +driver suffix is stripped.)

# --- OR, fill in fields (typical for self-hosted / localhost) ---
# host     = your-host.example.com
# port     = 3306
# database = your-database-name
# user     = your-username
# password = your-password
```

**If `$DB_TYPE = sqlite`**, append:

```ini
[$PROFILE_NAME]
type = sqlite
path = /absolute/path/to/your/database.db
```

**If `$DB_TYPE = dsn`**, append:

```ini
[$PROFILE_NAME]
url = paste-your-connection-string-here
# Examples:
#   postgresql://user:pass@host:5432/db
#   postgresql+asyncpg://postgres.<ref>:<pw>@aws-1-<region>.pooler.supabase.com:5432/postgres
#   mysql://user:pass@host:3306/db
#   sqlite:///absolute/path.db
# +driver suffixes (asyncpg, psycopg2, pymysql) are stripped.
# Query params like ?sslmode=require are honored.
```

**Always** finish the file with a tight "additional profiles" hint — one commented block, not three:

```ini

# Add more profiles by appending another [section]. Switch with AGAMI_PROFILE=<name>.
# Example:
# [staging]
# type = postgres
# url  = postgresql://readonly:pass@staging-db.example.com:5432/mydb
```

Then say something like:

> I've written a template at `~/.agami/credentials.example`. Open it, fill in your connection details, save it as `~/.agami/credentials`, and run `chmod 600 ~/.agami/credentials`. Then come back and ask me a question about your data.

If the user asks for help editing it, walk them through the fields per [`shared/credentials-format.md`](../../shared/credentials-format.md).

### 2d — Materialize provider-native auth files (after the user saves credentials)

Once `~/.agami/credentials` exists with real values (the user has copied the template, filled it in, and `chmod 600`-ed it), invoke the auth-file generator. This writes provider-native auth files (`~/.agami/.pgpass` for postgres profiles, `~/.agami/.mysql.cnf` for mysql profiles) that psql/mysql read silently. **The whole point** is that subsequent skill invocations can run psql/mysql WITHOUT the password appearing in any visible Bash command line:

```bash
python3 "$AGAMI_PLUGIN_ROOT/scripts/setup_pgauth.py" --all
```

(Or `--profile <name>` for one specific profile.)

The generator is idempotent and safe to re-run. Auth files are chmod 600. **Without these files, the psql/mysql/snowsql invocations would have to put the password on the command line — that's forbidden per [`shared/connection-reference.md → HARD RULES`](../../shared/connection-reference.md). Always run setup_pgauth.py before the first native-CLI query.**

If the user hasn't yet saved real credentials (just the template is there), skip this step — the generator will fail on placeholder values, and we'll re-run after the user fills in their connection details.

### Seed `<artifacts_dir>/USER_MEMORY.md` if missing

After `artifacts_dir` is resolved (above), check for `<artifacts_dir>/USER_MEMORY.md`. If it does not exist, create the directory if needed (`mkdir -p "$artifacts_dir" && chmod 755 "$artifacts_dir"`) and write the default seed (per [`shared/user-memory-format.md`](../../shared/user-memory-format.md) → "Default seed") via the Write tool, `chmod 644` (sharable; not secret).

USER_MEMORY.md holds free-form **cross-database** user preferences (default filters, display rules) that every other agami skill loads on each invocation. Don't overwrite an existing file — the user may have edited it (or pulled a team-shared copy from git).

**Migration:** if `~/.agami/USER_MEMORY.md` exists from a v1.1 install, move it: `mv "$HOME/.agami/USER_MEMORY.md" "$artifacts_dir/USER_MEMORY.md"`. Surface a one-line note: "Moved your USER_MEMORY.md to `<artifacts_dir>/USER_MEMORY.md`. It's sharable — `git init` and commit if you want."

### `ORGANIZATION.md` is per-profile, seeded by `agami-connect`

`USER_MEMORY.md` is global (one file at the top of `<artifacts_dir>/`, applies across every database the user connects to). The **per-database** equivalent is `<artifacts_dir>/<profile>/ORGANIZATION.md` — domain context, terminology, what the data represents. See [`shared/organization-context-format.md`](../../shared/organization-context-format.md).

`agami-init` does NOT create `ORGANIZATION.md` here, because the profile directory `<artifacts_dir>/<profile>/` doesn't exist yet — `agami-connect` builds it during introspection. `agami-connect`'s Phase 1.4 prompts the user once for a one-paragraph description and writes the file alongside the per-schema yamls. Don't reach for that file from `agami-init`.

### Permissions enforcement

Whenever this skill (or any other agami skill) reads `~/.agami/credentials`, verify perms:

```bash
perms=$(stat -f '%A' ~/.agami/credentials 2>/dev/null || stat -c '%a' ~/.agami/credentials)
if [ "$perms" != "600" ] && [ "$perms" != "400" ]; then
  echo "~/.agami/credentials must be chmod 600 (currently $perms)" >&2
  echo "Run: chmod 600 ~/.agami/credentials" >&2
  exit 1
fi
```

Offer to fix it for them: "I can run `chmod 600 ~/.agami/credentials` now — OK?"

---

## Phase 3: Tool detection

### Allowed probes (exhaustive list)

Detect which database tool(s) are available **only** with these commands. Anything else is forbidden — no `pgrep`, `ps`, `find /`, `ls /Applications`, `ls /Library`, network port scanning, or any other discovery technique.

```bash
which psql 2>/dev/null
which mysql 2>/dev/null
which sqlite3 2>/dev/null
which duckdb 2>/dev/null
python3 -c 'import psycopg2' 2>/dev/null && echo "psycopg2 OK"
python3 -c 'import pymysql' 2>/dev/null && echo "pymysql OK"
```

If `which psql` returns empty, you may try the common Homebrew location once: `ls /opt/homebrew/Cellar/libpq/*/bin/psql /opt/homebrew/opt/libpq/bin/psql 2>/dev/null | head -1`. Same for `/opt/homebrew/opt/mysql-client/bin/mysql`. **Do not** scan beyond those exact globs.

### What probing is FORBIDDEN

- Probing or testing connectivity to `localhost`, `127.0.0.1`, or any other host. The user's database is the one in `~/.agami/credentials` — and that file is read by other skills, not init.
- Scanning the filesystem to find a database (`find / -name "postgres*"`, etc.).
- Running `pgrep`, `ps`, `lsof`, or any process / port discovery.
- Asking the user where their database is. They tell us by editing `~/.agami/credentials`.

### Choose a connection method and persist tool paths

Read the user's preferred profile from `~/.agami/credentials` (or `AGAMI_DATABASE_URL`) **only to determine `db_type`** — `postgres` / `mysql` / `sqlite`. Then pick the first available method per [`shared/connection-reference.md`](../../shared/connection-reference.md#how-agami-picks-a-connection-method):

1. **Native CLI** — `psql` (postgres / redshift), `mysql` (mysql), `snowsql` (snowflake), `sqlite3` (sqlite)
2. **DuckDB** — universal binary, scans postgres / mysql / sqlite
3. **Python driver** — `scripts/execute_sql.py` (psycopg2 / pymysql / snowflake-connector-python / stdlib sqlite3)

Persist the chosen method **and the absolute paths of every tool we found** in `~/.agami/.config`, so future skills don't re-probe. (The internal field is named `tier` for backward-compatibility with shipped installs; values are `cli` / `duckdb` / `python`.)

```json
{
  "schema_version": 1,
  "tier": "cli",
  "host": "claude-code-cli",
  "active_profile": "main",
  "artifacts_dir": "/Users/me/agami-artifacts",
  "tool_paths": {
    "psql": "/opt/homebrew/Cellar/libpq/18.3/bin/psql",
    "mysql": null,
    "sqlite3": "/usr/bin/sqlite3",
    "duckdb": null,
    "python3": "/usr/bin/python3"
  },
  "tool_imports": {
    "psycopg2": false,
    "pymysql": false
  },
  "detected_at": "2026-05-07T17:30:00Z"
}
```

`artifacts_dir` is the absolute path of the **parent directory that holds ALL profiles** (not just the current one). Each profile gets its own subdirectory: `<artifacts_dir>/<profile_name>/`. Don't include the profile name in `artifacts_dir` — that's the most common mistake (e.g., setting `artifacts_dir: ~/Documents/finbud-agami` then later adding a `turning-pages` profile that lands at `~/Documents/finbud-agami/turning-pages/`, which reads weird because the parent directory is named after a different profile).

The agami home (`~/.agami/`) holds secrets and per-user state; the artifacts dir holds what teams may want to commit. See [`shared/file-layout.md`](../../shared/file-layout.md) for the full split.

Ask the user where to put it via **AskUserQuestion** before writing `.config`. Two options — **never label one of them "Other"**, since AskUserQuestion auto-adds an "Other" with a free-text input. Naming a labeled option "Other" produces a duplicate row and confused users.

> Where should agami save your semantic model, examples, and preferences?
>
> This is the **parent directory** for ALL your database profiles — each profile lands in a subdirectory (`<artifacts_dir>/finbud/`, `<artifacts_dir>/turning-pages/`, etc.). Pick a profile-neutral path. These are non-secret files; pointing at a folder inside a git repo lets your team commit them and share. Credentials stay in `~/.agami/` either way.
>
> Need a custom location (e.g. inside a team repo at `~/code/myteam/data/agami/` for git-sharing)? Pick **Other** and paste an absolute path.

| label | description |
|---|---|
| `~/agami-artifacts/ (Recommended)` | Default — a profile-neutral folder in your home. Each profile lives in its own subdir (`~/agami-artifacts/<profile>/`). You can `git init` there later, or copy contents into your team's repo whenever you want to share. |
| `~/Documents/agami/` | Alternative if you keep code and project files under `~/Documents/`. Each profile lives at `~/Documents/agami/<profile>/`. Same `git init` / sharing options apply. |

The auto-provided **Other** option captures any other absolute path the user wants to paste — including inside an existing team repo. Don't list it explicitly; AskUserQuestion's runtime adds it automatically.

Validate the chosen path:

- Must be absolute.
- Must NOT be inside `~/.agami/` (refuse with: "That's the secrets directory — pick a different location").
- **If the basename of the chosen path matches the active_profile** (e.g., user set `~/Documents/finbud-agami/` for the `finbud` profile), warn but accept: "Heads up: `<basename>` looks profile-specific. When you add another profile, it'll land at `<path>/<other_profile>/`, which may read confusingly. You can edit `~/.agami/.config.artifacts_dir` later if you'd rather keep this profile-neutral. Continue?"
- Parent of the chosen path must exist or be creatable.

Persist the resolved absolute path to `.config.artifacts_dir`. The directory is created lazily on first write by `agami-connect`.

Set `active_profile` to the name the user picked in Phase 2a (e.g. `main`, `supabase`, `production`). All other agami skills resolve the active profile in this order:

1. `AGAMI_PROFILE` environment variable (highest priority — explicit per-session override)
2. `active_profile` from this `.config` file (set by `init` once)
3. The literal string `"default"` (legacy fallback for users who set up before this field existed)

If the user later wants to add another database connection, they edit `~/.agami/credentials` to add a new section, then either set `AGAMI_PROFILE=<name>` for that session or re-run `init switch-profile <name>` to update `active_profile` permanently.

Future skills that need to run SQL look up the tool path from this file and use it directly. They do NOT re-run `which` unless the cached path no longer exists on disk.

### When no tool is available for `db_type`

If none of the native CLI, DuckDB, or the Python driver is available, surface the "no tool available" template from [`shared/connection-reference.md`](../../shared/connection-reference.md#when-no-tool-is-available) — it lists exactly which tools are missing and the install command for each. Offer to install the simplest one via Bash if the user accepts:

```bash
# macOS
brew install postgresql      # native CLI for postgres / redshift
brew install mysql           # native CLI for mysql
brew install duckdb          # DuckDB universal client
```

Don't install silently. Always confirm via AskUserQuestion first.

### Do NOT test the chosen tool here

Tool detection is path-only. A connection probe (`SELECT 1`) requires credentials. Init does NOT have credentials yet — they're written by the user after Phase 5 closes. The connection probe happens later, in `agami-connect/SKILL.md` Phase 0, against the host in the credentials file. Never against `localhost` as a default.

### Persist the chosen method + tool paths in `~/.agami/.config`

Write the `.config` schema documented in Phase 3 above (`tier`, `host`, `tool_paths`, `tool_imports`, `detected_at`). **No telemetry fields here yet** — telemetry consent is asked later (after the user has seen `connect` work end-to-end), not at install time.

For ISO8601 timestamp: `date -u +"%Y-%m-%dT%H:%M:%SZ"`. Detect `host` from the environment — Claude Code CLI sets `CLAUDE_CODE_HOST=cli` (or similar; fall back to `unknown` if you can't tell).

After writing, `chmod 600 ~/.agami/.config`.

`agami-connect/SKILL.md` will later add `analytics_consent`, `install_id`, and `consent_ts` to this same file once the user has opted in (or out).

---

## Phase 4: Deferred opt-ins (no prompts here)

`agami-init` does not ask for telemetry or any other opt-in. Both are asked later, when the user has felt the value of the skill:

- **Telemetry consent** — asked by `agami-connect/SKILL.md` after the demo query succeeds. Asking at install time is too early; the user hasn't seen anything work yet.
- **GitHub star** — asked by `agami-query-database/SKILL.md` after the user's first real successful query. No email collection, no list — just a one-click ask.

Don't do anything in this phase. **Don't mention the deferred asks in the closing message either** — previewing them at install time is noise the user can't act on, and telegraphing "I'll ask you for X later" reads as nagging. The asks fire when the user has actually felt the value; that's the right moment to surface them, not earlier.

---

## Phase 5: Hand-off

End with a short status + next step. **No telegraphed opt-ins** (don't preview the GitHub-star ask or the telemetry consent — those fire later, when the user has felt the value).

For **first-run** (default — Phases 2–4 just ran):

> ✓ `~/.agami/` ready (chmod 700)
> ✓ Credentials template written to `~/.agami/credentials.example`
> ✓ Tool detected: psql (native CLI for Postgres)
>
> Next: edit `~/.agami/credentials.example` with your real connection details, save it as `~/.agami/credentials`, run `chmod 600 ~/.agami/credentials`, then ask me a data question (or re-invoke `/agami-connect`). I'll pick up the introspect from there.

For **`verify`** mode:

> ✓ `~/.agami/` exists (chmod 700)
> ✓ Credentials present for profile `main` (chmod 600)
> ✓ Tool detected: psql (native CLI for Postgres)
> ✓ Active profile: main
>
> All set. Ask me a data question or run `/agami-connect reintrospect` to refresh the schema.

If something is missing in `verify`, surface what's missing and the one command that fixes it. Examples:
- *"No credentials yet. Run `/agami-init` to write a template, then fill it in."*
- *"Credentials exist but `psql` isn't on PATH — install with `brew install postgresql` or `apt install postgresql-client`, then re-run."*

---

## Error handling

- All credential reads route through the chmod check in Phase 2. Refuse on world-readable.
- All SQL runs route through [`shared/db_error_classifier.md`](../../shared/db_error_classifier.md). Surface one-line remediations, not raw stacktraces.
