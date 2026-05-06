# Database error classifier

Friendly error routing for database-layer failures. Owned by `agami-data:query-database` and `agami-data-admin:refresh-log-dashboards` / `refresh-admin-dashboards`; consumed wherever a SQL execution touches a live database.

The contract: every raw exception from the four execution tiers (MCP / native CLI / Python driver / DuckDB) gets classified before it reaches the user. A solo OSS user staring at `psycopg2.errors.UndefinedColumn: column "x" does not exist` has no support channel to ask what to do; the classifier turns it into "Run `/agami-data-admin:update-semantic-model` to sync schema."

This file is the single source of truth for the classification rules. `query-database` and the refresh skills both implement the same classifier; the refresh skills additionally use it to populate the `_repair` block on dashboard widget payloads so the dashboard SPA can render a "needs repair" card via `chart.js` (see [build-dashboard SKILL.md → Widget repair card](../skills/build-dashboard/SKILL.md)).

## Classification rules

When SQL execution raises an exception, match the exception class and message against this table **in order**. First match wins. If no class matches, fall through to `kind: other` and surface the raw error.

| Class | Detection (any of) | Remediation |
|---|---|---|
| `auth` | psycopg2 `OperationalError` with "password authentication failed", "FATAL: password", "no pg_hba.conf entry"; mysql `Access denied`; snowflake `Incorrect username or password`; SF/HTTP 401; `KeyError` on a missing env var that resolves to a known credential field (`PASSWORD`, `PWD`, `USER`, `TOKEN`, `KEY`) | `"Edit <org_path>/local/.env (or <org_path>/.env) — your <db> credentials may have rotated, the password env var may be missing, or the user may not have permission to log in. Re-run after fixing."` |
| `dsn` | "could not translate host name", "Name or service not known", "getaddrinfo ENOTFOUND", "Unknown MySQL server host"; mysql `Can't connect`; "no such file or directory" against a sqlite/duckdb path | `"Check <org_path>/local/.env: the <DB_HOST or path env var> doesn't resolve. Common causes: typo in hostname, VPN not connected, server moved. For local sqlite/duckdb, confirm the file path exists."` |
| `network` | "Connection refused", "timed out", "Connection reset by peer", `socket.timeout`, requests `ConnectTimeout` / `ReadTimeout`, snowflake `OperationalError` with "Could not connect", "SSL: WRONG_VERSION_NUMBER" | `"Network error reaching <db_host>. Common causes: VPN not connected, firewall blocking the port, server is down, SSL/TLS misconfiguration. Test with: nc -zv <host> <port> (or your usual reachability check)."` |
| `driver_missing` | `ModuleNotFoundError` / `ImportError` for `psycopg2`, `pymysql`, `pyodbc`, `snowflake.connector`, `simple_salesforce`, `google.cloud.bigquery`, `redshift_connector`, `duckdb`; CLI tier shell error "command not found: <psql\|mysql\|bq\|snowsql\|sqlite3>" | `"Driver missing: pip install <driver_pkg>. Or install the CLI: brew install <cli_pkg>. The doctor (/agami-data:doctor) can tell you which datasources need which drivers."` |
| `permission` | "permission denied for table", "permission denied for relation", "permission denied for schema", "INSUFFICIENT_PRIVILEGES" (Snowflake), MySQL `1142` `SELECT command denied to user`, BigQuery `Access Denied`, SF `INSUFFICIENT_ACCESS_OR_READONLY` | `"Your DB user can connect but cannot read <object>. Grant SELECT on <schema>.<table> (or the higher-level GRANT USAGE on the schema), then re-run."` |
| `column_not_found` | psycopg2 `UndefinedColumn`; mysql `1054` `Unknown column`; snowflake `Invalid identifier`; BigQuery `Name <x> not found`; sqlite `no such column`; SF `INVALID_FIELD` | If the missing column **is** in the local semantic model YAML: `"Your semantic model references <col> but it's no longer in the live database. Schema may have drifted — run /agami-data-admin:update-semantic-model to sync."` Otherwise: `"Generated SQL referenced a column that doesn't exist. The model may have an outdated entry, or the SQL generator hallucinated. Re-run the query — it'll auto-retry with corrected SQL."` |
| `table_not_found` | psycopg2 `UndefinedTable`; mysql `1146` `Table doesn't exist`; snowflake `Object <x> does not exist`; BigQuery `Table <x> not found`; sqlite `no such table` | If the missing table **is** in the local semantic model: `"Your semantic model references <table> but it's no longer in the live database. Schema may have drifted — run /agami-data-admin:update-semantic-model to sync."` Otherwise: `"Generated SQL referenced a table that doesn't exist in this datasource. Re-run the query."` |
| `syntax` | psycopg2 `SyntaxError` (DB-side); mysql `1064` `You have an error in your SQL syntax`; snowflake `compilation error`; sqlite `near "X": syntax error` | `"SQL syntax error from the generator. Re-run the query — auto-retry usually fixes generator slips."` (Most syntax errors clear after one retry per the existing auto-retry-up-to-2 behaviour in query-database Phase 3.) |
| `timeout` | psycopg2 `QueryCanceled`; mysql `2013 Lost connection during query`; snowflake `query was canceled`; explicit `statement_timeout` errors | `"Query timed out. Try adding a tighter filter, a LIMIT, or a date range — large unfiltered scans on production tables hit the DB's statement_timeout. The performance-hints risk-assessor (query-database Phase 3) is meant to catch this before execution; if it didn't, the table may need a recommended_filters entry in its semantic-model YAML."` |
| `other` | anything else | `"<original error message>. If this keeps happening, /agami-data:doctor can sanity-check your environment, and the connection-reference.md docs cover deeper troubleshooting per datasource type."` |

## Drift-aware column / table classification

The `column_not_found` and `table_not_found` cases need a follow-up step before remediation: **does the missing object exist in the local semantic model?**

For column errors:
1. **Extract the qualified name when available.** Most drivers emit a qualified form somewhere in the error: psycopg2's `UndefinedColumn` includes "of relation `<schema>.<table>`" in its `diag` block; Snowflake's `Invalid identifier '<schema>.<table>.<column>'` is fully qualified; BigQuery's `Name <project>.<dataset>.<table>.<column> not found` is fully qualified. Parse the most-qualified form available (preferring `<schema>.<table>.<column>` > `<table>.<column>` > `<column>`).
2. **Match strategy by qualification level:**
   - **Fully qualified** (`<schema>.<table>.<column>`): exact match against the loaded semantic model. If the schema-qualified table exists locally and contains the column, emit `drift_match: true` with `drift_candidates: [{schema, table, column}]`. If the table exists but lacks the column, that's the textbook drift signal — same `drift_match: true`. If the table doesn't exist locally, emit `drift_match: false`.
   - **Table-qualified** (`<table>.<column>`, no schema): find every loaded table whose unqualified name matches; check each for the column. If exactly one match, emit `drift_match: true` with that single candidate. If multiple match (rare — same table name across schemas), emit `drift_match: true` with `drift_candidates: [...]` listing all matches; the remediation message asks the user which schema's table drifted.
   - **Bare column name** (no driver-supplied qualification): scan all loaded tables for any whose `columns:` block contains a key matching the bare name. If exactly one, emit `drift_match: true` with that candidate. If multiple, emit `drift_match: true` with a `drift_candidates: [...]` list — the remediation text should then list the candidate tables and prompt the user, since `update-semantic-model` will sync all of them anyway but the user's mental model needs the disambiguation.
3. **Regex parse miss** (the message format is unrecognized — e.g. a Postgres / MySQL fork like CockroachDB or AlloyDB worded the error differently): emit `kind: other` with `classifier_unmatched: true` rather than silently routing to a generic `column_not_found` "auto-retry will fix" remediation. Auto-retry won't fix drift, and a Cockroach-flavoured wording for "column does not exist" should not silently degrade. The Training dashboard's failure-by-kind widget surfaces `classifier_unmatched: true` rows so unmatched message formats become a backlog of new patterns to add to the rules above.

For table errors: same flow — match against the table-YAML index built in query-database Step 1b. Qualified matching uses `<schema>.<table>` exact match; unqualified falls back to scanning all loaded tables. The `kind: other` + `classifier_unmatched: true` fallback applies the same way.

## Output shape

The classifier returns:

```python
{
  "kind": "auth | dsn | network | driver_missing | permission | column_not_found | table_not_found | syntax | timeout | other",
  "remediation": "<one-line user-facing remediation message>",
  "raw_message": "<original exception message, truncated to 500 chars>",
  "drift_match": True | False,    # set only on column_not_found / table_not_found
  "tier": "mcp | cli | python | duckdb",
  "datasource": "<datasource name>"
}
```

Callers use `kind` for control flow and `remediation` for the user-facing message. `raw_message` is preserved so `--verbose` mode and the query log capture the original; `drift_match` lets refresh skills decide whether to populate `_repair.kind` as `"schema_drift"` vs the generic `"column_not_found"`.

## Where this is consumed

- **`query-database`** — wrap each of the four execution tiers' SQL calls. On classified `auth` / `dsn` / `network` / `driver_missing` / `permission`, emit the remediation **before** moving to the next tier (auth failures cascade across all tiers; surfacing once is correct). On `column_not_found` / `table_not_found`, route through the drift-match step and emit the appropriate message. On `timeout` / `syntax`, the existing auto-retry-up-to-2 in Phase 3 fires; the classifier just labels the failure for the log.
- **`refresh-log-dashboards`** and **`refresh-admin-dashboards`** — these skills don't execute saved widget queries (their widgets are built from local JSONL aggregation and metadata); they don't need the classifier directly. *Saved-query widget refresh* belongs to **`build-dashboard`'s `refresh` intent**, which is where the classifier matters: when a saved query fails on refresh, build-dashboard populates the widget's `_repair` block via this classifier instead of letting the chart render empty.
- **`build-dashboard`** — `refresh` intent runs each saved query, classifies failures, writes `_repair` blocks. Sidebar repair-count is computed from the count of widgets with `_repair` present. Per-widget UI in `chart.js` reads `_repair` and renders the friendly "needs repair" card variant of the empty-state component.

## `_repair` block shape (writeable to insights.json)

```json
{
  "_repair": {
    "kind": "schema_drift | column_not_found | table_not_found | auth | dsn | network | permission | timeout | syntax | other",
    "message": "Your semantic model references customer_name but it's no longer in the live database.",
    "remediation": "Run /agami-data-admin:update-semantic-model to sync schema.",
    "last_failed_at": "2026-04-26T10:00:00Z",
    "raw_message": "column \"customer_name\" does not exist"
  }
}
```

Lifecycle:
- **Set** by `build-dashboard refresh` on a widget whose saved SQL fails.
- **Read** by `chart.js renderInsight` to dispatch to the "needs repair" variant of the empty-state component.
- **Cleared** the next time the widget refreshes successfully (or the user manually clears it via the dashboard SPA).

When `_repair` is set, the widget MUST still preserve its original `sql`, `user_query`, `data_source`, and `title` — those are what the user needs to manually fix or re-run the widget. The classifier never overwrites those fields.
