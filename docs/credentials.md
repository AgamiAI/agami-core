# Setup credentials

`agami` reads database connection details from `<artifacts_dir>/local/credentials`.
Same pattern as `~/.aws/credentials`, `~/.dbt/profiles.yml`, `~/.pgpass`.

`/agami-connect` creates a template at `<artifacts_dir>/local/credentials.example`
on first run (its Phase 0a). Edit it and save as
`<artifacts_dir>/local/credentials`:

```ini
[default]
type     = postgres
host     = localhost
port     = 5432
database = mydb
user     = myuser
password = mypassword
```

Then **make it readable only by you**:

```bash
chmod 600 <artifacts_dir>/local/credentials
```

`agami` refuses to read the file unless it's `chmod 600` — the same protection
`ssh` uses for private keys.

## Use a read-only user

`agami` only ever runs read-only SELECT queries, so the `user` above only needs
read access. Connecting a **read-only database user** is the safest choice,
especially against a production database. Copy-paste `CREATE USER` / `GRANT SELECT`
SQL for every dialect (Postgres, MySQL, Snowflake, SQL Server, Oracle, Databricks,
Trino, BigQuery) is in
[`plugins/agami/shared/readonly-grants.md`](../plugins/agami/shared/readonly-grants.md)
— or just ask agami for "the read-only grant" for your database.

## Multiple databases

Add more `[<profile>]` sections. Switch with `AGAMI_PROFILE=staging`:

```ini
[default]
type = postgres
host = prod-db.example.com
...

[staging]
type = postgres
host = staging-db.example.com
...
```

## Per-dialect examples

### MySQL

```ini
[default]
type     = mysql
host     = 127.0.0.1
port     = 3306
database = analytics
user     = analyst
password = secret
```

### Snowflake

```ini
[finance]
type      = snowflake
account   = xy12345.us-east-1.aws
user      = analyst@example.com
password  = secret
warehouse = COMPUTE_WH
role      = ANALYST_ROLE
database  = ANALYTICS
schema    = PUBLIC
# Or use SSO:
# authenticator = externalbrowser
```

### BigQuery

```ini
[gcp]
type                = bigquery
project             = my-gcp-project
dataset             = analytics                  # optional default dataset
service_account     = /abs/path/to/key.json      # omit to use Application Default Credentials
location            = US                          # optional, defaults to US
```

### Redshift

```ini
[warehouse]
type     = redshift
host     = my-cluster.abc123.us-west-2.redshift.amazonaws.com
port     = 5439
database = analytics
user     = readonly
password = secret
sslmode  = require           # default; verify-full / verify-ca / disable all accepted
```

### SQLite

```ini
[local]
type = sqlite
path = /Users/me/data/local.db
```

### Full format reference

[`plugins/agami/shared/credentials-format.md`](../plugins/agami/shared/credentials-format.md)
— every field, every database, every edge case.

## No Python required (usually)

The skill picks the first available connection method, in this order:

| Method | What you need | Install if missing |
|---|---|---|
| **Native CLI** | `psql` (Postgres / Redshift) / `mysql` (MySQL) / `snowsql` (Snowflake) / `bq` (BigQuery) / `sqlite3` (SQLite) on `PATH` | `brew install postgresql` / `brew install mysql` / [snowsql download](https://docs.snowflake.com/en/user-guide/snowsql-install-config) / [`gcloud` SDK](https://cloud.google.com/sdk/docs/install) |
| **DuckDB** universal binary | `duckdb` on `PATH` (covers Postgres / MySQL / SQLite, not Snowflake / BigQuery) | `brew install duckdb` (or [duckdb.org](https://duckdb.org/)) |
| **Python driver** (fallback) | Python + `psycopg2-binary` / `pymysql` / `snowflake-connector-python` / `google-cloud-bigquery` | `pip install psycopg2-binary pymysql snowflake-connector-python google-cloud-bigquery` |

`/agami-connect` Phase 0a tells you exactly what to install for your OS if nothing
is detected.
