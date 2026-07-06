# Read-only database user — copy-paste grants

agami only ever runs **read-only SELECT** queries (query generation refuses `INSERT` / `UPDATE` / `DELETE` / DDL, and the local MCP server double-checks every statement). So the safest thing to connect it to is a **read-only user** that can read your data and nothing else. It's optional — read-write credentials work too — but recommended, especially against a production database.

Create the user with one of the blocks below, then put **its** credentials in your profile: the `user` / `password` (or `url = …`) in `<artifacts_dir>/local/credentials` for the single-player flow, or the `DATASOURCE_URL` in `agami.env` for a self-host deploy.

Replace the `<…>` placeholders — `<password>`, `<db>`, `<schema>`, `<warehouse>`, `<catalog>`, `<project>`, `<dataset>`, and the `agami_ro` user/role name — with your values (each block uses only some of them). For **multiple schemas**, repeat the `USAGE` + `SELECT` grants once per schema.

## PostgreSQL / Redshift

```sql
CREATE USER agami_ro WITH PASSWORD '<password>';
GRANT CONNECT ON DATABASE <db> TO agami_ro;
GRANT USAGE ON SCHEMA <schema> TO agami_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA <schema> TO agami_ro;
-- keep future tables readable too (run once per schema):
ALTER DEFAULT PRIVILEGES IN SCHEMA <schema> GRANT SELECT ON TABLES TO agami_ro;
```

(Redshift uses the same statements. `<schema>` defaults to `public` if you didn't set one.)

## MySQL / MariaDB

```sql
CREATE USER 'agami_ro'@'%' IDENTIFIED BY '<password>';
GRANT SELECT ON <db>.* TO 'agami_ro'@'%';
FLUSH PRIVILEGES;
```

(Tighten `'%'` to a specific host if agami connects from a fixed IP.)

## Snowflake

```sql
CREATE ROLE IF NOT EXISTS agami_ro;
GRANT USAGE ON WAREHOUSE <warehouse> TO ROLE agami_ro;
GRANT USAGE ON DATABASE  <db>            TO ROLE agami_ro;
GRANT USAGE ON SCHEMA    <db>.<schema>   TO ROLE agami_ro;
GRANT SELECT ON ALL    TABLES IN SCHEMA <db>.<schema> TO ROLE agami_ro;
GRANT SELECT ON FUTURE TABLES IN SCHEMA <db>.<schema> TO ROLE agami_ro;
GRANT SELECT ON ALL    VIEWS  IN SCHEMA <db>.<schema> TO ROLE agami_ro;
GRANT SELECT ON FUTURE VIEWS  IN SCHEMA <db>.<schema> TO ROLE agami_ro;
-- a user to carry the role (or grant agami_ro to an existing user):
CREATE USER IF NOT EXISTS agami_ro_user PASSWORD = '<password>' DEFAULT_ROLE = agami_ro;
GRANT ROLE agami_ro TO USER agami_ro_user;
```

Put `role = agami_ro` in your Snowflake profile so the read-only role is the one used.

## SQL Server / Azure SQL Managed Instance

A server login plus a database user mapped to it (`db_datareader` is the built-in read-only role):

```sql
CREATE LOGIN agami_ro WITH PASSWORD = '<password>';
-- then, connected to the target database:
CREATE USER agami_ro FOR LOGIN agami_ro;
ALTER ROLE db_datareader ADD MEMBER agami_ro;   -- SELECT on every table/view
```

(On a Managed Instance, create the login in `master` first.)

## Azure SQL Database

Azure SQL Database doesn't support server logins — create a **contained user** with its own password, connected to the target database:

```sql
CREATE USER agami_ro WITH PASSWORD = '<password>';
ALTER ROLE db_datareader ADD MEMBER agami_ro;   -- SELECT on every table/view
```

## Oracle

Oracle has no single "grant select on all tables", so grant per table (or use the broad `SELECT ANY TABLE` if that's acceptable):

```sql
CREATE USER agami_ro IDENTIFIED BY "<password>";
GRANT CREATE SESSION TO agami_ro;
GRANT SELECT ON <schema>.<table> TO agami_ro;   -- repeat per table
-- broad alternative (reads every schema — use only if that's fine):
-- GRANT SELECT ANY TABLE TO agami_ro;
```

## Databricks (Unity Catalog)

agami connects with a token, so the "user" is the token's owner — use a **service principal** that only has read:

```sql
GRANT USE CATALOG ON CATALOG <catalog>            TO `agami_ro`;
GRANT USE SCHEMA  ON SCHEMA  <catalog>.<schema>   TO `agami_ro`;
GRANT SELECT      ON SCHEMA  <catalog>.<schema>   TO `agami_ro`;
```

## Trino / Presto

Trino itself doesn't store data — it defers reads to the underlying connector. Make the **backing user read-only**: create a read-only user on each source database (the Postgres / MySQL / etc. blocks above) and point the Trino catalog at it, and/or restrict the Trino user with `access-control` rules. There's no single Trino grant that covers every catalog.

## BigQuery

Not SQL — it's IAM. Give the service account (or user) in your `credentials` **viewer + job-runner** roles:

```bash
# run queries at all:
gcloud projects add-iam-policy-binding <project> \
  --member="serviceAccount:agami-ro@<project>.iam.gserviceaccount.com" \
  --role="roles/bigquery.jobUser"
# read data (dataset-level is finer than project-level bigquery.dataViewer):
bq add-iam-policy-binding --member="serviceAccount:agami-ro@<project>.iam.gserviceaccount.com" \
  --role="roles/bigquery.dataViewer" <project>:<dataset>
```

## SQLite / DuckDB

File-based — there's no user or role. Safety comes from agami's **read-only SQL guard** (it refuses anything that isn't a `SELECT`) plus **filesystem permissions**. DuckDB files are additionally opened in read-only mode; SQLite is not, so for a hard guarantee point agami at a **read-only copy** of the file, or mark the file read-only for the account agami runs as.
