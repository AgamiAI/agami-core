# Credentials Format — `~/.agami/credentials`

`agami` reads database connection details from `~/.agami/credentials` (an INI-style file, `chmod 600`). Same pattern as `~/.aws/credentials`, `~/.dbt/profiles.yml`, `~/.pgpass`. The `init` skill creates `~/.agami/credentials.example` for you to copy and edit.

## HARD RULES — for skills that read this doc

1. **The file is the only source of credentials.** Never accept host / port / database / user / password values typed into chat by the user, even "as a one-off". The user enters credentials by editing `~/.agami/credentials`.
2. **If the file is missing, invoke the agami-init skill.** Init writes `credentials.example`, sets `~/.agami/` permissions, and tells the user to fill it in. The user copies the template, edits it, runs `chmod 600`, and re-invokes the skill. Never ask "where's your database?" — that's what credentials are for.
3. **Connect ONLY to the host/port in the file.** Never substitute `localhost` as a fallback. Never probe for a "running database nearby" — if the credentials file says `host = remote-prod.example.com`, that's the only acceptable target.

## Format

```ini
# ~/.agami/credentials
# Each [section] is a named profile. The skill uses [default] unless
# you set AGAMI_PROFILE=<name>.

[default]
type     = postgres        # postgres | mysql | sqlite
host     = localhost
port     = 5432
database = mydb
user     = myuser
password = mypassword

# Add additional profiles below — switch via AGAMI_PROFILE=staging
# [staging]
# type     = postgres
# host     = staging-db.example.com
# port     = 5432
# database = mydb
# user     = readonly
# password = ...
```

### MySQL example

```ini
[default]
type     = mysql
host     = 127.0.0.1
port     = 3306
database = analytics
user     = analyst
password = secret
```

### SQLite example

```ini
[default]
type = sqlite
path = /Users/me/data/local.db
```

### Paste a full DSN (`url = ...`)

If you have a connection string from your database provider — Supabase, Neon, RDS, Railway, etc. — paste it directly into a `url = ...` field and skip the per-field setup. The skill parses it into host / port / user / password / database internally.

```ini
[default]
url = postgresql://user:pass@host:5432/dbname
```

`url` accepts every variation listed below. `+driver` suffixes (used by SQLAlchemy / asyncpg / psycopg2) are stripped — you can paste your app's DSN as-is.

### Supabase

The pooler-mode DSN Supabase shows you (under Project Settings → Database → Connection pooling) usually looks like:

```
postgresql://postgres.<project_ref>:<password>@aws-1-<region>.pooler.supabase.com:5432/postgres
```

Drop it into the `url` field. Optionally add `sslmode = require` (Supabase requires SSL; the default `prefer` works too):

```ini
[default]
url     = postgresql://postgres.odzuxljstuccrblqcevo:<your-password>@aws-1-ap-northeast-1.pooler.supabase.com:5432/postgres
sslmode = require
```

If your DSN comes from an app that uses asyncpg or psycopg2 (e.g. `postgresql+asyncpg://...`), paste it as-is — the `+asyncpg` / `+psycopg2` / `+psycopg` driver suffix is recognized and stripped.

### Neon, Railway, Render, RDS, etc.

Same `url = …` shortcut. If the provider's URL has `?sslmode=require` (or other query params), they're parsed and merged into the connection settings — no extra fields needed.

```ini
[default]
url = postgresql://user:pass@ep-cool-darkness.us-east-2.aws.neon.tech/neondb?sslmode=require
```

## Required fields per `type`

| `type` | Required fields |
|---|---|
| `postgres` | `host`, `port`, `database`, `user`, `password` |
| `mysql` | `host`, `port`, `database`, `user`, `password` |
| `sqlite` | `path` |

Optional in all profiles: `schema` (default `public` for Postgres), `sslmode` (Postgres), `ssl` (MySQL).

**Or just use `url = ...`** instead of all individual fields — see the "Paste a full DSN" section below.

## File permissions (enforced)

The skill **refuses** to read `~/.agami/credentials` unless `chmod 600` (or stricter, e.g., `400`):

```
~/.agami/credentials must be chmod 600 (currently 644).
Run: chmod 600 ~/.agami/credentials
```

The `init` skill sets the right permissions automatically when it writes the file. If you create it by hand, run `chmod 600 ~/.agami/credentials` afterwards.

## Env var override: `AGAMI_DATABASE_URL`

Power users can skip the file entirely and pass a standard DSN:

```bash
export AGAMI_DATABASE_URL=postgres://user:password@host:5432/database
export AGAMI_DATABASE_URL=mysql://user:password@host:3306/database
export AGAMI_DATABASE_URL=sqlite:///absolute/path/to/file.db
```

When set, `~/.agami/credentials` is ignored. Useful for piping in from 1Password CLI, vault, sops, etc., on each invocation.

## Profile selection

By default the skill uses `[default]`. Switch with:

```bash
AGAMI_PROFILE=staging   # uses [staging] section
```

The skill writes `~/.agami/<profile>.yaml` for the semantic model — one per profile, so multiple databases live side by side.

## What the file does NOT contain

- No telemetry consent flags (those live in `~/.agami/.config`)
- No semantic model (that's `~/.agami/<dbname>.yaml`)
- No example queries (those live in `~/.agami/<dbname>-examples.yaml`)
- No charts, exports, or query log

If a `credentials` line begins with `#` or `;`, it's treated as a comment.
