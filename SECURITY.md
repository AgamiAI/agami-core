# Security Policy

## Reporting a Vulnerability

Please report security vulnerabilities **privately** rather than opening a public issue.

- Email: [contact@agami.ai](mailto:contact@agami.ai)
- Or use GitHub Security Advisories: <https://github.com/AgamiAI/agami-core/security/advisories/new>

We will acknowledge receipt within **3 business days** and aim to provide a fix or mitigation timeline within **14 days** for confirmed vulnerabilities. Please give us a reasonable window to respond before any public disclosure.

When reporting, include where possible:

- A description of the issue and its impact
- Steps to reproduce (proof-of-concept welcome)
- Affected versions / commit SHAs
- Any suggested remediation

## Supported Versions

We support the **latest released version**; older releases will not receive security backports.

## Scope

In scope:

- The agami-core Claude Code plugin (`plugins/agami/`) and the SQL execution pipelines it ships.
- The plugin marketplace manifests (`.claude-plugin/marketplace.json`, `plugins/agami/.claude-plugin/plugin.json`).

Out of scope:

- Third-party databases, drivers, or services agami-core connects to.
- Vulnerabilities in Claude Code itself (report those to Anthropic).
- Issues that require physical access to a user's machine or already-compromised credentials.

## SQL execution model

`execute_sql` is read-only by construction. A single gate (`sql_guard`) runs at the
shared executor, so every path that can run SQL — the stdio server, the hosted HTTP
server, the skills, and cron — is protected identically. It rejects, before any
database connection is opened:

- anything that isn't a single `SELECT` / `WITH...SELECT` (DML, DDL, `SELECT ... INTO`);
- multi-statement SQL, including bypasses hidden in string literals, comments, or
  double-quoted identifiers;
- data-modifying CTEs (`WITH ... DELETE/INSERT/UPDATE ... RETURNING`);
- transaction-control, session-state, and prepared statements, and row-level locks;
- dangerous server-side functions, on every engine it dispatches to — file I/O
  (`pg_read_file`, `LOAD_FILE`, `OPENROWSET`), OS/command execution (`copy_program`,
  `reflect`), remote SQL (`dblink*`, `OPENQUERY`, `EXTERNAL_QUERY`, `UTL_HTTP`),
  notification (`pg_notify`), and resource-exhaustion (`pg_sleep`, `SLEEP`, `BENCHMARK`,
  advisory locks, `GET_LOCK`, the Snowflake `SYSTEM$…` namespace).

This is defense in depth at the application layer; you should still connect with a
read-only database role. A guard bypass — SQL that mutates data or reaches a blocked
function yet passes the gate — is in scope for a report.

### The semantic-model pass is separately switchable

A second layer runs after the gate above: the **semantic-model pass**, which confines a
query to the tables and columns your model declares, bans `SELECT *`, and refuses a model
whose declared engine disagrees with its credentials. On a server it is controlled by
`AGAMI_GOVERNANCE_ENFORCED`, and **it is off by default**.

With it off, a query may read anything the connecting role is granted, including columns
you deliberately excluded from the model, and may enumerate your schema through catalog
*relations* (`information_schema.tables`, `pg_catalog.pg_class`, `sqlite_master`). Catalog
*functions* are denied by the gate above in both postures; catalog relations are not, and
the read-only grant recipe we publish does not revoke them. Every answer and every audit
row states that the checks did not run, so a result is never presented as governed when it
was not.

Everything in the list above (read-only, confinement to safe functions, and the resource
bounds) is unaffected by this setting, as is your database role. **A statement that the
read-only or dangerous-function gate would refuse must never become executable because this
setting is off; if you find one, that is a guard bypass and in scope for a report.**

### What a refusal may name

A refusal names back the identifiers the caller's own statement used, sanitized and capped.
It never lists what the model declares, with one recorded exception (#386): a column-scope
refusal lists the declared columns of the tables that statement reads (capped per table and in
tables), with the closest listed name for each refused column. Without the list, an agent
repaired a refused column by guessing again.

A table's listed columns are exactly the ones `get_datasource_schema` shows the same caller for
it: a subject area that exposes only some of a table's column groups (`expose_column_groups`)
hides the rest here too. Every declared column is queryable by design: a column whose values must
not be readable is left out of the model, and is then absent from the list too.

Nothing else widens: no other table, and no declared name on any other refusal (the table-scope
refusal's declared set would be the whole datasource). `tests/test_ace035_no_enumeration.py`
enforces both halves.

The residual, for integrators: if you narrow the tool surface per caller (`Adapters.tool_visibility`)
so that a caller can run `execute_sql` but not call `get_datasource_schema`, that caller can learn
the visible column names of any declared table it names in a statement, up to five tables and the
first 40 columns of each per refusal.

Thank you for helping keep agami-core and its users safe.
