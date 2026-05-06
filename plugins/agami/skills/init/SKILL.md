---
name: init
description: "First-run setup for agami. Creates the .agami directory in the user's home (chmod 700), writes a credentials.example template, detects which database execution tier is available (psql/mysql native CLI, DuckDB binary, or Python driver), and walks the user through one-time opt-in prompts for anonymous usage stats and email updates. Re-run any time to verify state, switch profiles, or change opt-in choices."
when_to_use: "Run when the user installs the plugin for the first time, asks 'how do I set up agami', wants to add or switch a database connection, or asks to change their telemetry / email preferences. Auto-invoked by the connect and query-database skills if the .agami directory or credentials file is missing."
argument-hint: "[verify | reconfigure-analytics | switch-profile NAME]"
---

# agami init

You are walking the user through the one-time setup for `agami`. The goal: by the end of this skill, the user has a working `~/.agami/credentials` file, knows which execution tier their machine supports, and has made conscious choices about telemetry and email opt-ins.

This skill is idempotent — running it again with no args verifies state and surfaces any drift (missing creds, wrong file permissions, no tier available, etc.).

## Conversation style

- **Combine acknowledge + next question** — don't waste turns on "Got it!"
- **Use AskUserQuestion for every choice** — never bullet-list options inline. Mark exactly one option `(Recommended)` first.
- **Keep prompts short** — 2-4 lines per question max.
- **Plain English over jargon** — for telemetry / privacy, sound like a human.

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

# 4. What tiers are available?
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

If `~/.agami/credentials` does not already exist (and `AGAMI_DATABASE_URL` is unset), write `~/.agami/credentials.example` using the **Write tool** with this exact content (substituting only the placeholder comments — keep section names, field names, and indentation as-is):

```ini
# ~/.agami/credentials
# Copy this file to ~/.agami/credentials, fill in your values,
# and run: chmod 600 ~/.agami/credentials
#
# This file holds the connection details for the databases
# agami can talk to. Each [section] is a named profile.
# Default profile is [default]. Switch with: AGAMI_PROFILE=<name>
#
# Format reference: plugins/agami/shared/credentials-format.md

[default]
type     = postgres        # postgres | mysql | sqlite
host     = localhost
port     = 5432
database = mydb
user     = myuser
password = mypassword

# --- Additional profile examples (uncomment and edit) ---

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

### Seed `~/.agami/USER_MEMORY.md` if missing

If `~/.agami/USER_MEMORY.md` does not exist, write the default seed (per [`shared/user-memory-format.md`](../../shared/user-memory-format.md) → "Default seed") via the Write tool, `chmod 600`. This file holds free-form user preferences (default filters, domain vocabulary, display preferences) that every other agami skill loads on each invocation. Don't overwrite an existing file — the user may have edited it.

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

## Phase 3: Tier detection

Detect which execution tier(s) are available on the machine. Run all checks in parallel:

```bash
which psql 2>/dev/null
which mysql 2>/dev/null
which sqlite3 2>/dev/null
which duckdb 2>/dev/null
python3 -c 'import psycopg2' 2>/dev/null && echo "psycopg2 OK"
python3 -c 'import pymysql' 2>/dev/null && echo "pymysql OK"
```

Read the credentials file (or `AGAMI_DATABASE_URL`) to determine the user's `type` (postgres/mysql/sqlite). Then choose a tier per [`shared/connection-reference.md`](../../shared/connection-reference.md#tier-selection-algorithm):

1. Tier 1 — native CLI (`psql` for postgres, `mysql` for mysql, `sqlite3` for sqlite)
2. Tier 2 — DuckDB universal binary
3. Tier 3 — Python driver

Persist the chosen tier in `~/.agami/.config` so other skills can re-use it without re-probing every invocation.

### When no tier is available

If neither tier 1, 2, nor 3 is available for the user's database type, surface the **exact** "all tiers failed" message from `shared/connection-reference.md`. Offer to install the simplest tier via Bash if the user accepts:

```bash
# macOS
brew install postgresql      # tier 1, postgres
brew install mysql           # tier 1, mysql
brew install duckdb          # tier 2, universal
```

Don't install silently. Always confirm via AskUserQuestion first.

### Test the chosen tier

Once a tier is chosen, run a `SELECT 1` probe via that tier. If it fails, route the error through [`shared/db_error_classifier.md`](../../shared/db_error_classifier.md) and surface the one-line remediation.

### Persist the tier in `~/.agami/.config`

Write a minimal `.config` with the chosen tier and detected host. **No telemetry fields here yet** — telemetry consent is asked later (after the user has seen `connect` work end-to-end), not at install time.

```bash
chmod 700 ~/.agami
cat > ~/.agami/.config <<'JSON'
{
  "schema_version": 1,
  "tier": "<cli|duckdb|python>",
  "host": "<claude-code-cli|claude-code-vscode|claude-code-cursor|claude-cowork>",
  "detected_at": "<ISO8601 UTC>"
}
JSON
chmod 600 ~/.agami/.config
```

For ISO8601 timestamp: `date -u +"%Y-%m-%dT%H:%M:%SZ"`. Detect `host` from the environment — Claude Code CLI sets `CLAUDE_CODE_HOST=cli` (or similar; fall back to `unknown` if you can't tell).

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
> ✓ Tier detected: psql (tier 1)
>
> Next: edit `~/.agami/credentials` with your DB connection, then ask me a question like "how many orders did we ship last month?". I'll introspect your schema on the first query, then ask if you'd like to share anonymous usage stats.

If the user already has credentials and just ran `init` to verify, skip the "edit credentials" line and prompt them to ask a question directly.

---

## Error handling

- All credential reads route through the chmod check in Phase 2. Refuse on world-readable.
- All SQL runs route through [`shared/db_error_classifier.md`](../../shared/db_error_classifier.md). Surface one-line remediations, not raw stacktraces.
