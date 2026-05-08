---
name: init
description: "First-run setup for agami. Creates the .agami directory in the user's home (chmod 700), writes a credentials.example template, detects which database tool is available (psql / mysql / snowsql / sqlite3 native CLI, DuckDB binary, or the Python driver), and walks the user through one-time opt-in prompts for anonymous usage stats and email updates. Re-run any time to verify state, switch profiles, or change opt-in choices."
when_to_use: "Run when the user installs the plugin for the first time, asks 'how do I set up agami', wants to add or switch a database connection, or asks to change their telemetry / email preferences. Auto-invoked by the connect and query-database skills if the .agami directory or credentials file is missing."
argument-hint: "[verify | reconfigure-analytics | switch-profile NAME]"
---

# agami init

**Before suggesting any slash command in chat, read [`shared/invocation-conventions.md`](../../shared/invocation-conventions.md).** The only working slash command for agami is `/init` (bare). Never tell the user to type `/agami:init`, `/agami:connect`, `/connect`, `/save-correction`, or any other slash form — those don't exist in users' installations. Use natural-language phrasing for everything except `/init`.

You are walking the user through the one-time setup for `agami`. The goal: by the end of this skill, the user has a working `~/.agami/credentials` file, knows which database tool their machine will use to run queries, and has made conscious choices about telemetry and email opt-ins.

This skill is idempotent — running it again with no args verifies state and surfaces any drift (missing creds, wrong file permissions, no tool available, etc.).

## Conversation style

- **Combine acknowledge + next question** — don't waste turns on "Got it!"
- **Use AskUserQuestion for every choice** — never bullet-list options inline. Mark exactly one option `(Recommended)` first.
- **Keep prompts short** — 2-4 lines per question max.
- **Plain English over jargon** — for telemetry / privacy, sound like a human.

---

## Phase −1: Plan-mode check (before anything else)

agami's setup needs to make edits — write `~/.agami/credentials.example`, run `mkdir`, materialize `.pgpass`, etc. None of that works in Claude Code's **Plan mode**, which restricts the assistant to read-only tools.

Detect plan mode via two signals:

1. **System-reminder context.** When plan mode is active, the host injects a `<system-reminder>` saying so into the conversation. If the latest such reminder is in scope and indicates plan mode is active, treat that as confirmed.
2. **Optional probe.** If the system context is ambiguous, attempt one no-op Bash: `echo agami-plan-probe`. If it succeeds, edits will succeed. If it fails because of plan mode, the failure is the signal.

If plan mode is active, **stop the skill and ask the user to switch** via AskUserQuestion. Do not proceed to Phase 0 yet.

> agami's setup needs to write files in `~/.agami/` and run a few commands. **Plan mode is active**, which blocks edits. Switch modes?

Options (mark exactly one Recommended, place it first):

| label | description |
|---|---|
| `Default mode (Recommended)` | Switch to default mode — agami will ask for permission per command (you can approve once and the host caches the allow). |
| `Auto-accept edits` | Switch to auto-accept-edits mode — agami runs without per-command prompts. Use if you trust the skill. |
| `Stay in plan mode` | Don't run setup. I'll show you the plan only, no actual changes. |

After the user picks, surface a one-liner reminder of the keystroke (`Shift+Tab` cycles modes in Claude Code) so they know how to flip. Then:

- **`Default mode` or `Auto-accept edits`** → wait for the user to actually press Shift+Tab. Don't try to flip the mode programmatically — the skill can't. Ask them to confirm "I've switched, continue" before proceeding.
- **`Stay in plan mode`** → continue, but emit only a written plan (no file writes, no Bash). Tell the user: "I'll describe what I would do; re-invoke me out of plan mode when you're ready to actually run it."

If the user is NOT in plan mode (signal #1 absent and the probe succeeds), skip this phase silently and go to Phase 0.

---

## Phase 0: Decide what to do based on `$ARGUMENTS`

- **No arguments**: Run the full first-run flow (Phases 1–5).
- **`verify`**: Run Phase 1 (state check) only. Print what's working and what's missing. Exit.
- **`reconfigure-analytics`**: Skip to Phase 4. Re-prompt the opt-in.
- **`switch-profile <name>`**: Skip to Phase 2 with the profile name pre-set. Help the user add a new `[<name>]` section to `~/.agami/credentials`.

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

Options (mark exactly one Recommended, place it first):

| label | description |
|---|---|
| `PostgreSQL (Recommended)` | Postgres, Supabase, Neon, RDS Postgres, Aurora Postgres, Cloud SQL, Timescale. |
| `Redshift` | Amazon Redshift (provisioned cluster or Serverless). Speaks Postgres wire protocol; psql works. Default port 5439, SSL required. |
| `Snowflake` | Snowflake. Uses `snowsql` CLI or `snowflake-connector-python`. Account identifier instead of host. |
| `MySQL` | MySQL, MariaDB, RDS MySQL, PlanetScale. |
| `SQLite` | A local `.db` / `.sqlite` file. |
| `Paste a connection URL` | If you already have a DSN string (e.g. from Supabase / Neon / Railway dashboards). Accepts `postgresql://`, `redshift://`, `snowflake://`, `mysql://`, `sqlite:///abs/path` and the `+driver` SQLAlchemy variants. |

Bind the chosen type to `$DB_TYPE` (one of `postgres` | `redshift` | `snowflake` | `mysql` | `sqlite` | `dsn`). The "Paste a connection URL" path generates a `url = ...` placeholder; the user pastes their full DSN into the file directly.

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

**If `$DB_TYPE = postgres`**, append:

```ini
# Postgres profile — fill in below. localhost is fine for a local DB.
[$PROFILE_NAME]
type     = postgres
host     = your-host.example.com
port     = 5432
database = your-database-name
user     = your-username
password = your-password
# Uncomment the next line for cloud DBs (Supabase / Neon / RDS):
# sslmode = require

# OR — comment out the per-field block above and paste a DSN URL instead:
# Accepts postgresql://, postgres://, postgresql+asyncpg://, postgresql+psycopg2://
# Query params like ?sslmode=require are honored automatically.
# url = postgresql://user:pass@host:5432/db
```

**If `$DB_TYPE = redshift`**, append (same shape as postgres but with Redshift defaults):

```ini
# Redshift profile.
# Provisioned cluster host:    your-cluster.<region>.redshift.amazonaws.com
# Redshift Serverless host:    <wg>.<acct>.<region>.redshift-serverless.amazonaws.com
# Default port is 5439. SSL is required.
[$PROFILE_NAME]
type     = redshift
host     = your-cluster.example.region.redshift.amazonaws.com
port     = 5439
database = your-database
user     = your-username
password = your-password
sslmode  = require

# OR — use a DSN URL (port 5439 + sslmode=require auto-applied):
# url = redshift://user:pass@your-cluster.us-west-2.redshift.amazonaws.com:5439/db
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
# MySQL profile. localhost is fine for a local DB.
[$PROFILE_NAME]
type     = mysql
host     = your-host.example.com
port     = 3306
database = your-database-name
user     = your-username
password = your-password

# OR — use a DSN URL:
# url = mysql://user:pass@host:3306/db
# url = mysql+pymysql://user:pass@host:3306/db
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

**Always** finish the file with a small "additional profiles" section so the user can see how to add more later (commented examples; one block, doesn't need to be type-specific):

```ini

# --- Additional profile examples (uncomment, edit, and add) ---

# [staging]
# type     = postgres
# host     = staging-db.example.com
# port     = 5432
# database = mydb
# user     = readonly
# password = ...

# [analytics]
# type     = mysql
# host     = 127.0.0.1
# port     = 3306
# database = analytics
# user     = analyst
# password = ...

# [local]
# type = sqlite
# path = /Users/me/data/local.db
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

### Seed `~/.agami/USER_MEMORY.md` if missing

If `~/.agami/USER_MEMORY.md` does not exist, write the default seed (per [`shared/user-memory-format.md`](../../shared/user-memory-format.md) → "Default seed") via the Write tool, `chmod 600`. This file holds free-form **cross-database** user preferences (default filters, display preferences) that every other agami skill loads on each invocation. Don't overwrite an existing file — the user may have edited it.

### `ORGANIZATION.md` is per-profile, seeded by `connect`

`USER_MEMORY.md` is global (one file, applies across every database the user connects to). The **per-database** equivalent is `~/.agami/<profile>/ORGANIZATION.md` — domain context, terminology, what the data represents. See [`shared/organization-context-format.md`](../../shared/organization-context-format.md).

`init` does NOT create `ORGANIZATION.md` here, because the profile directory `~/.agami/<profile>/` doesn't exist yet — `connect` builds it during introspection. `connect`'s Phase 1.5 prompts the user once for a one-paragraph description and writes the file alongside the per-schema yamls. Don't reach for that file from `init`.

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

Tool detection is path-only. A connection probe (`SELECT 1`) requires credentials. Init does NOT have credentials yet — they're written by the user after Phase 5 closes. The connection probe happens later, in `connect/SKILL.md` Phase 0, against the host in the credentials file. Never against `localhost` as a default.

### Persist the chosen method + tool paths in `~/.agami/.config`

Write the `.config` schema documented in Phase 3 above (`tier`, `host`, `tool_paths`, `tool_imports`, `detected_at`). **No telemetry fields here yet** — telemetry consent is asked later (after the user has seen `connect` work end-to-end), not at install time.

For ISO8601 timestamp: `date -u +"%Y-%m-%dT%H:%M:%SZ"`. Detect `host` from the environment — Claude Code CLI sets `CLAUDE_CODE_HOST=cli` (or similar; fall back to `unknown` if you can't tell).

After writing, `chmod 600 ~/.agami/.config`.

`connect/SKILL.md` will later add `analytics_consent`, `install_id`, and `consent_ts` to this same file once the user has opted in (or out).

---

## Phase 4: Deferred opt-ins (no prompts here)

`init` does not ask for telemetry or email opt-in. Both are asked later, when the user has felt the value of the skill:

- **Telemetry consent** — asked by `connect/SKILL.md` after the demo query succeeds. Asking at `init` time is too early; the user hasn't seen anything work yet.
- **Email updates** — asked by `query-database/SKILL.md` after the user's first real successful query.

Don't do anything in this phase. Just mention in the closing message below that the user will be asked once about each (separately) after the first interaction.

---

## Phase 5: Hand-off

When all phases done, end with a short status + next step:

> ✓ `~/.agami/` ready (chmod 700)
> ✓ Credentials template written to `~/.agami/credentials.example`
> ✓ Tool detected: psql (native CLI for Postgres)
>
> Next: edit `~/.agami/credentials` with your DB connection, then ask me a question like "how many orders did we ship last month?". I'll introspect your schema on the first query, then ask if you'd like to share anonymous usage stats.

If the user already has credentials and just ran `init` to verify, skip the "edit credentials" line and prompt them to ask a question directly.

---

## Error handling

- All credential reads route through the chmod check in Phase 2. Refuse on world-readable.
- All SQL runs route through [`shared/db_error_classifier.md`](../../shared/db_error_classifier.md). Surface one-line remediations, not raw stacktraces.
