# agami-connect · Phase 0a: connect a database

Part of [agami-connect](../SKILL.md). `<plugin_root>` in this file is the plugin's root directory: the absolute path agami-connect's SKILL.md shows in its own commands. Write that path into a command, quoted; never paste `<plugin_root>` or an unset variable into Bash. `<root>` is the database's model folder, `<artifacts_dir>/<profile>`, written out in full the same way. Each command below is its own Bash call, with no variable, pipe, `;`, `&&` or redirect on the line ([invocation-conventions.md](../../../shared/invocation-conventions.md)).

## Phase 0a: First-time credential bootstrap

**Runs when preflight step 1 returns `next: bootstrap` (no credentials for the profile) and the person picked "Connect a database", or when they picked "Connect another database".** If `<artifacts_dir>/local/credentials` already has the `[<profile>]` section for the database being connected, **skip Phase 0a entirely.**

**Two paths, chosen by the operating system** (`uname -s`: `Darwin` is macOS; `MINGW*` / `MSYS*` / `CYGWIN*`, or `$OS` = `Windows_NT`, is Windows):

- **macOS and Windows: the password page.** 0a.1 → 0a.2 → **0a.4c** (connection fields card) → 0a.3 (the name, offering the database's own name) → 0a.5 → 0a.5b → 0a.6 → 0a.7 → 0a.8 → **0a.4k** (the password page) → Phase 1. No file to edit and nothing to paste. The page needs the interpreter, agami-core (with `keyring`) and the artifacts folder in place, which is why 0a.5–0a.8 run before it.
- **Linux and anything else, and Databricks on every system** (its secret is a `token`, and the page stores a `password`): the file. 0a.1 → 0a.2 → 0a.3 → **0a.4** (`credentials.example`) → 0a.5 → 0a.5b → 0a.6 → 0a.7 → 0a.8 → **0a.9** (a card waits for the filled-in file) → 0a.10 → Phase 1.

### 0a.1 — Set up `<artifacts_dir>/local/`
Two calls:
```bash
mkdir -p "<artifacts_dir>/local"
```
```bash
chmod 700 "<artifacts_dir>/local"
```
If the user chose a non-default `<artifacts_dir>`, **persist the pointer** so future sessions find it, and gitignore `local/`: write the folder's absolute path as the only line of `<home>/.config/agami/path` with the Write tool (your home folder written out; the Write tool makes the folder), then give `<artifacts_dir>/.gitignore` a `local/` line (Read it first; add the line with the Edit tool, or write the file with that one line when it doesn't exist).

### 0a.2 — Ask the database type

**AskUserQuestion**, one question, header `DB type`, no `(Recommended)` — fact-of-environment. The sample data is on the start card (1a), not here.

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
bash "<plugin_root>/scripts/py" connect_helper.py --spec "<artifacts_dir>/local/tmp/agami-connect-spec.json"
```

Stdout is JSON lines and nothing else. **Wait for the helper's first line with the Monitor tool**: the Bash result names the task and its output file, so use the Monitor tool to watch the helper's output until its first line, or the task's exit (an until-loop on that output file that stops on the first JSON line, or when the task has exited). Never run a foreground `sleep`: the Desktop Code tab blocks it. As a fallback where Monitor isn't available, re-read the output file at spaced intervals, each read its own tool call, never a tight loop. Stop waiting when one of these happens:

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

The helper exits when the page is submitted or the 5 minutes pass, and that exit also brings you back if the card is still open: go to step 4 and don't ask the card again. Never wait silently: if the person writes before the helper is done, answer, and remind them the link is still open. If they want to stop, stop the background task with TaskStop and its task id (the helper then takes back whatever it stored), and show the database-type card again. Where TaskStop isn't available, the helper stops by itself when its 5 minutes are up: say so, and wait for its exit before starting it again.

**4. Read the final line** of the output when the helper exits, and act on it. The exit is what you wait for after the link is posted: the Monitor tool watching the task until it exits (or, as the fallback, spaced re-reads of the output file), never a foreground `sleep`; the background task's exit notice also brings you back. Exit code is 0 for every outcome; `status` (and a refusal's `reason`) says what happened, and `detail` is the one line to show the person. Each outcome gets one sentence plus the next card:

| final line | say | next |
|---|---|---|
| `connected` | *"Connected to `<name>`. The password is in your keychain."* (or the Windows store) | Run `setup_pgauth.py --all` (as in 0a.10) and continue to Phase 1. |
| `connect_failed` | `detail` (why it failed). Nothing was kept: the helper removed the section and the stored password. | Card `Next step`: `Try again` (the same fields, a new link; the person may have mistyped the password) / `Change the connection details` (0a.4c again) / `Use a file instead (manual)` (0a.4). |
| `expired` | *"The link expired before the page was submitted; nothing was saved."* | Card `Next step`: `New link` (step 2 again, same spec) / `Use a file instead (manual)`. |
| `missing_driver` | `detail`, which names `package`. | Card `Driver`: `Install <package>` (with the real package names) / `Not now`. On the press, install into the interpreter `scripts/py` uses: `bash "<plugin_root>/scripts/py" -m pip install --user <package>` (unquoted: `package` can hold two names, `duckdb pytz`), then step 2 again. If pip fails, show its last line and offer `Try again` / `Not now`. `Not now`: stop with *"Connect again when you're ready to install it."* |
| `refused`, `no_store` | `detail` (agami's keyring library isn't set up for the OS store yet). | Card `Secure store`: `Set up secure storage` / `Use a file instead (manual)`. The first runs `bash "<plugin_root>/scripts/sm" install` (it adds `keyring` on macOS and Windows), then step 2 again. |
| `refused`, `stale_install` | `detail` (the agami-core this Python has is too old for the store). | Card `Update`: `Update agami` (runs `bash "<plugin_root>/scripts/sm" install`, then step 2 again) / `Use a file instead (manual)`. |
| `refused`, `name_taken` or `saved_password_exists` | `detail` (the name is in use here, or the OS store already holds a password under it). | The name card (0a.3) again, then a new spec and step 2. |
| `refused`, `bad_name` | `detail`. | The name card (0a.3) again. |
| `refused`, `bad_field` | `detail` (a field the credentials file can't hold). | The connection fields card (0a.4c) again. |
| `refused`, `bad_spec` | Nothing to the person: the spec file you wrote is unreadable. | Rewrite it (step 1) and run step 2 again, once. A second `bad_spec`: say connecting failed and offer `Use a file instead (manual)`. |
| `refused`, `unreadable_credentials` | `detail` (the credentials file under `<artifacts_dir>/local/` can't be parsed). | Card `Next step`: `I've fixed it` (step 2 again) / `Stop`. Never open or print the file: it can hold passwords. |
| `refused`, `unsupported_os` | `detail`. | The file path: 0a.4, then 0a.9. |
| `refused`, `not_replaceable` | `detail` (`--replace` was run for a profile that doesn't exist or doesn't keep its password in the OS store). | Connect it as a new profile: the name card (0a.3), then step 2 without `--replace`. |
| `refused`, `store_error` or `write_failed` | `detail` (the store or the credentials file refused the write; nothing was kept). | Card `Next step`: `Try again` (step 2) / `Use a file instead (manual)`. |

`Use a file instead (manual)` always means 0a.4 → 0a.9 → 0a.10, the same as Linux. A line that is not one of these, or no final line at all: show the last line of output and offer `Try again` / `Use a file instead (manual)`. Delete the spec file once the helper has exited.

On `connected` (the connection test passed), mark the step done and refresh the task list ([task-list.md](../../../shared/task-list.md)); `<root>` is `<artifacts_dir>/<profile>`:
```bash
bash "<plugin_root>/scripts/sm" steps "<root>" --mark connect=done
```

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

Bodies — `postgres`, `redshift`, `snowflake`, `mysql`, `bigquery`, `sqlite` are unchanged from [`shared/credentials-format.md`](../../../shared/credentials-format.md) (URL-form first for Postgres/MySQL/Redshift; account fields for Snowflake; `project`+`service_account_path` for BigQuery; `path` for SQLite). The new dialects:

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

**`<python3>` = `data.interpreter.python3`, written out in full, from the Phase-0 `connect_resolve.py` call** — the ONE Python agami uses for the model *and* DB connections (introspection always runs `python -m execute_sql` under it on *every* tier, so the DB driver must live in it). It's already the **scored** pick — the candidate that has `pydantic`+`sqlglot`+`pyyaml` AND the `$DB_TYPE` driver — so there's no guessing and the user sets nothing. **Re-run `bash "<plugin_root>/scripts/py" connect_resolve.py --db-type <db_type>` now** that the DB type is known, so the interpreter is scored against the right driver and native tools are refreshed. It records as `tool_paths.python3` in 0a.7. (`AGAMI_PYTHON` is honored as a first-priority override but is never required.)

Native CLIs (optional fast path for *queries* — introspection doesn't use them) are in `data.tools` (`psql`/`mysql`/`snowsql`/`sqlite3`/`duckdb`/`bq`, `null` if absent), detected with `which` only. **Forbidden** elsewhere: `pgrep`/`ps`/`lsof`/`find /`/`ls /Applications`/port scans.

**If `data.interpreter.has_driver` is `false`** the `$DB_TYPE` driver isn't in `<python3>` yet — confirm via AskUserQuestion, then `"<python3>" -m pip install --user <package>` from the table, where `<python3>` is `data.interpreter.python3` written out in full (probe was already done in `<python3>` by `connect_resolve.py`):

| `$DB_TYPE` | probe (`"<python3>" -c '…'`) | pip package |
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

If the driver is missing, **confirm via AskUserQuestion**, then `"<python3>" -m pip install --user <package>` (plain `pip install` fallback). Same "never install silently" convention as the model deps (0a.5b). Do this for `<python3>` so `sm introspect` connects on the first try.

### 0a.5b — Ensure the semantic-model dependencies

The model (introspection, validation, traversal, curation — everything the `sm` wrapper drives) needs **`pydantic` + `sqlglot` + `pyyaml`** in the interpreter agami uses. Check the resolved interpreter (`$AGAMI_PYTHON` → `.config` `tool_paths.python3` → `python3`):

```bash
"<python3>" -c 'import pydantic, sqlglot, yaml'
```

Exit 0 means they are there; an `ImportError` names the one that is missing.

If they're present, continue. If missing, **confirm via AskUserQuestion before installing** (same convention as the DB-driver install above — agami never installs silently):
> agami needs the **agami-core** package (which pulls `pydantic`, `sqlglot`, `pyyaml`) to build and read the semantic model. Install it now? (one-time, user-site — `pip install --user`)

On **Yes**: `bash "<plugin_root>/scripts/sm" install` — the `sm` launcher is the single place that installs the agami-core library into the resolved interpreter (editable from a dev checkout, else the published package via PyPI or pinned git — so it works in a marketplace install with no `packages/` dir), and everything (`python -m execute_sql` / `python -m semantic_model.cli` / `sm`) then resolves it. On **No**: stop with *"Can't build the model without it — re-run when you're ready to install."* — don't proceed to introspect.

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
  "active_profile": "<profile>",
  "artifacts_dir": "<resolved absolute path>",
  "tool_paths": { "psql": "...", "python3": "<python3>" },
  "detected_at": "<ISO8601 UTC from `date -u +%Y-%m-%dT%H:%M:%SZ`>"
}
```
`python3` MUST be the `<python3>` resolved in 0a.5 (the interpreter that has both the model deps and the DB driver) — `sm` and the introspection engine read it from here, so recording the wrong one reintroduces the interpreter mismatch. `chmod 600 <artifacts_dir>/local/.config`.

The database's folder is known from here, `<root>` = `<artifacts_dir>/<profile>`: mark the step started and refresh the task list ([task-list.md](../../../shared/task-list.md)):
```bash
bash "<plugin_root>/scripts/sm" steps "<root>" --mark connect=in_progress
```

### 0a.8 — Seed `<artifacts_dir>/USER_MEMORY.md` if missing
Create the parent (`mkdir -p`, then `chmod 755`, two calls) and write the default seed (per [`shared/user-memory-format.md`](../../../shared/user-memory-format.md)), `chmod 644`. Don't overwrite. Migrate a v1.1 `<artifacts_dir>/local/USER_MEMORY.md` if present.

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
grant, read [`shared/readonly-grants.md`](../../../shared/readonly-grants.md), pick the `$DB_TYPE`
block, and fill in whatever `<…>` placeholders it uses (e.g. `<db>` / `<schema>` / `<warehouse>` /
`<catalog>` / `<project>` / `<dataset>`) from the values they entered.

### 0a.10 — On re-entry: promote the filled-in template, then continue
The person pressed `I've filled it in` on the 0a.9 card (or came back later with a data question, and preflight returned `promote`). **Promote it deterministically** with the helper — do NOT hand-roll an `mv`/append, and do NOT assume "no file yet." The script handles all four cases (first profile → move; Nth profile → append; name clash → refuse; placeholders → refuse), so the second-profile and `[main]`/`[main]` cases can't silently corrupt the file:

```bash
bash "<plugin_root>/scripts/py" promote_credentials.py
```

Read the first token of stdout and act:
- `SECURED <profile>` → credentials created from the template (chmod 600, `.example` consumed). Continue.
- `APPENDED <profile>` → the new profile was appended to an existing credentials file (other profiles untouched, chmod 600). Continue.
- `COLLISION <profile>` → a profile by that name **already exists**; nothing was changed. Tell the user *"You already have a profile named `<profile>` — pick a different name for this database,"* go back to **0a.3** to rename, re-write the template in 0a.4 under the new name, and show the 0a.9 card again. Never overwrite or duplicate an existing profile.
- `PLACEHOLDERS_REMAIN <fields>` → the template still holds template values; tell the person which fields to fill and show the 0a.9 card again (never introspect against a template).
- `NOTHING` → no `credentials.example` to promote (already promoted, or never written) — fall back to the preflight decision.

(`<plugin_root>` is the plugin root, filled in with its absolute path when this skill loads; `promote_credentials.py` is stdlib-only and needs no special interpreter.)

**Run `setup_pgauth.py --all`** before the first native-CLI query (writes `.pgpass` / `.mysql.cnf` so passwords never hit the command line). Idempotent. Then continue to Phase 1.

Before Phase 1, with the connection saved (this path doesn't test it; Phase 1's first read does), mark the step done and refresh the task list ([task-list.md](../../../shared/task-list.md)):
```bash
bash "<plugin_root>/scripts/sm" steps "<root>" --mark connect=done
```

---

