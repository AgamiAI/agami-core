# Changelog

All notable changes to **agami** are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

The `version` in `.claude-plugin/marketplace.json` and `plugins/agami/.claude-plugin/plugin.json`
is the source of truth a host installs against — bumping it is what invalidates a
user's plugin cache (see [CONTRIBUTING.md](CONTRIBUTING.md)). Each released section
below corresponds to one such version.

## [Unreleased]

## [0.9.10] — 2026-10-07

### Added

- **A model spec workbook builds the model exactly as its owners describe it.** A warehouse whose
  joins are not declared — a star schema of views, keys named for their role (`ship_to_customer_key`
  joins the customer table), dimensions shared by several facts — introspected as disconnected tables
  grouped by name prefix, and a person had no way to say otherwise short of editing YAML. Now they fill
  in [`shared/model-spec-template.xlsx`](plugins/agami/shared/model-spec-template.xlsx): subject areas,
  which area owns each table and which others list it, the joins (a plain column pair, or a full
  condition such as a `current_flag = 'Y'` filter, each with a role), metrics, sensitive columns and
  rules. agami-connect recognises the workbook, introspects exactly its tables, and applies it with
  the new `sm apply-spec` as one validated step: every problem is reported at once on a dry run,
  nothing is written unless the whole model validates, and a failure restores the previous model.
  Each join is placed where questions find it (in the area owning both tables, else between the two
  owning areas), and what the spec marks approved is signed off as the person running it, after
  asking. An optional Columns sheet carries the person's own column descriptions (a data dictionary
  pastes straight in), and a Metrics row that is a plain aggregate of one column is flagged on the
  dry run: its name belongs in that column's description, the rule `suggest-metrics` already follows.
  An optional Grain column on the Tables sheet states the column(s) that make a row unique — what the
  engine can't probe on a large table and the fan-out check relies on. The workbook is checked before
  anything is built: a row missing a required value is reported by its row number (an empty Tables
  sheet would otherwise have meant "introspect everything"), and join types, metric names and source
  tables, the fiscal month, and table names two schemas share (write `schema.table`) are validated;
  two such tables can't share one area, a join gives either a column pair or a condition (not both),
  and two metrics in one area can't share a name once punctuation is ignored. Re-applying keeps the
  metrics the spec doesn't redefine, matched by area and name, and the glossary.
  agami-connect introspects the spec's tables in one call, then still proposes entities. `model_spec_workbook.py` counts what it read, so a run reports the workbook's numbers
  instead of estimating them. Format: [`shared/model-spec-format.md`](plugins/agami/shared/model-spec-format.md).
- **The model explorer's PII tab clears or marks a whole list at once.** A model with dozens of flagged
  columns had to be cleared one click at a time. Each list now has "Clear all N shown" / "Mark all N
  shown", which acts on whatever the search has narrowed it to (search `email`, clear those). Like
  every explorer change it only queues edits until the footer submits; clearing asks first, with the count.

### Security

- **agami-connect's environment check no longer prints the database password.** It echoed the whole
  credentials profile into its output, so a password, token or a connection URL's inline password
  landed in the agent's transcript on every run. Secret fields now show only that they are set, and
  a URL keeps its host and user with the password masked. Nothing read the values; the skill only
  checks which fields are present. If you ran agami-connect before this release, consider rotating
  the database password it used.

### Fixed

- **A join declared with a fixed-value condition matches a statement that wrote it.** A versioned
  dimension joined to its current row (`on: f.key = d.key AND d.current_flag = 'Y'`) could never be
  matched: the receipt treated any declared `on:` with a literal conjunct as unreadable, so every
  current-version join came back `undetermined` however exactly it was written. The fixed-value
  conjunct now counts toward the match, and a statement must write it too — one that leaves the filter
  out (or uses another value) is still not credited with the declared join, which is what the old
  refusal guarded, and a filter written on another alias of the same table doesn't count. Such a join
  stays `undetermined` rather than `undeclared`, since the filter may sit in a WHERE; the join-probe
  grader reads it the same way instead of calling it a wrong key.
- **A total behind a join no declaration settles is no longer called clean.** The fan-out check
  only weighs joins it can match to a declaration, so a total behind an `undetermined` or `undeclared`
  join was reported `not_multiplied` — a positive claim made without the check. It is now
  `undetermined`, and its reason names the joins, in the receipt and in `sm prepare`'s pre-flight
  alike. Only joins that can feed the number count: its own branch or a CTE, not another UNION arm or a
  `WHERE … IN` subquery (a join past the receipt's cap, which is never read, downgrades every number).
- **Rewriting `datasource.yaml` keeps the glossary and the cross-area entities and metrics stored in
  it.** `write_tree` wrote only the fields it built, so a re-introspect or a spec re-apply dropped
  terminology added with `sm set-terminology`. They are carried over like the description.
- **Signing off an example no longer strips it.** `sm add-example` replaced an example whose question
  already existed, so re-saving it with a signer, as agami-connect does to record a validation, dropped
  its `confirmed` status and scope tags. A re-save with the same SQL now keeps the fields it doesn't
  restate; different SQL is still a correction and replaces the example.

- **Approving a join approves that join, not the first one between its two tables.** Two tables are
  often joined several ways (one per date or person role), but the explorer keyed those joins by the
  table pair — every date role's join shared one key, so approving one approved, and overwrote, them all —
  and `curate` signed off the first match. Each join now has its own key in the explorer, its approval
  and edits carry the join's columns or condition (and both tables' schemas, so `sales.orders` and
  `archive.orders` stay apart), and a `curate` op that still matches more than one
  join is refused with a reason instead of guessed. `approve-queue` names each join the same way, and
  a cross-area join is found wherever it is stored (`add_relationships` writes them to their own file,
  which approvals never looked in).
- **The validator warns about a join that won't be served.** An area's joins are served only among
  the tables it defines, so a join to a table the area merely lists validated cleanly and then vanished
  when a question was answered. It now warns, naming the table and the fix (a cross-area join). An
  endpoint with a schema is compared by schema too, so owning `sales.orders` doesn't cover `archive.orders`.
- **Metric and entity files are named the same way by every writer.** `write_tree` used the display
  name and `sm add` / `curate` a slug, so rewriting a model after `sm add` duplicated items and broke
  lookup by name.
- **A missing or unreadable input file is a JSON error naming the resolved path**, for `curate
  --ops-file`, `add`, `add-example`, `set-terminology`, `seed-examples`, `describe-file` and `apply-spec` — not a
  traceback a skill read as "nothing to apply".
- **Warehouse key columns aren't offered a currency.** `invoice_key` and `payment_key`
  matched the money words in their names; `key`, `sk`, `fk` and `nbr` now rule a column out.
- **The plugin run from a checkout uses that checkout's agami-core.** `sm` compares versions only in a
  released plugin folder, so from a checkout (`claude --plugin-dir`) a released agami-core installed
  earlier satisfied it and the checkout's own code never ran. It now reinstalls from the checkout when
  the importable package isn't it.
- **`execute_sql --batch` reports where each result landed.** A relative `out` resolves from the current
  directory (now documented), and the manifest records the absolute path.
- **agami-connect's documented commands work as written**: the examples-validation page needs `--title`
  and `--profile`, and the pre-seed recount is `review-items --scope preseed`.
- **agami-serve uses the Python agami-connect already proved, and won't write a Desktop entry that
  can't connect.** It searched the usual install locations and could pick an interpreter without the
  database driver: Claude Desktop then started the server and every query failed. `AGAMI_PYTHON` and
  then the interpreter recorded in `local/.config` now go first, as in `sm`, and after installing, the
  chosen one must import both the driver and agami-core or nothing is written.
- **A year, period or identifier in a result isn't printed as a quantity.** The result formatter
  grouped every unit-less number, so a fiscal year came out as `2,024` and an id as `1,234,567`. A
  column named like a label (`year`, `fiscal_year`, `quarter`, `month`, `period`, `*_id`, `*_key`,
  `*_code`, `*_number`, `zip`) now shows the value as written; a column with a unit is unaffected.
- **A value list is built only from rows that represent the column.** The sample is the first rows a
  table returns, so `current_flag: N` could be recorded for a column that is mostly `Y`, and the SQL
  writer would filter on it. A list now needs at least two distinct values, and is built only when the
  sample is the whole table or the catalog says the table is small; on a large table or a view with no
  estimate, no list is better than a wrong one.
- **A generated description no longer replaces one a person wrote or a data dictionary supplied.**
  Enrichment's `source: ai` edits overwrote any column description, including `human` and `metadata`
  ones; such an edit is now skipped with its reason. A person can still change either.
- **Redshift late-binding views introspect with their columns.** A view created `WITH NO SCHEMA
  BINDING` is listed in `information_schema.tables` but its columns are not in
  `information_schema.columns`, so a schema of such views came out as tables with no columns (and,
  without an allowlist, nothing at all). Redshift now reads columns from `svv_columns`, which lists
  them for tables, views and late-binding views alike. A column read that fails (a dropped connection)
  is retried once instead of the table being dropped as having no columns.
- **`add_relationships` accepts a join written as an `on:` condition, and a failure part-way restores
  every file.** Labelling the result read `from_column`, which an `on:` join doesn't have, so the call
  crashed — after earlier areas' files were already written, leaving the model half-updated. The label
  now names the tables, and any error during the batch restores every file it touched.

## [0.9.9] — 2026-10-07

### Changed

- **Below the overview, a schema reply's domain context is cut to the tables in scope (#419).** The
  `## Domain context` section rode every `get_datasource_schema` reply whole, and a question makes
  several schema calls: in a run of real-world questions it was about a third of everything the
  agent received. The overview (no `area`, no `dataset_names`) still carries all of it. An `area` or
  `dataset_names` reply now keeps:
  - the narrative (`datasource.md`) in full, because its rules change answers, and a conversation
    can reach SQL without ever making the overview call;
  - the datasource glossary terms that name one of the tables in scope, and those tables'
    coded-value legends (a hosted deployment's company glossary stays whole: its terms are
    business vocabulary, not table names);
  - a count of the terms left out, plus a line saying the overview has the full glossary.

  It drops the subject-area listing. What is cut depends only on the call's own arguments, never on
  a guess about what the client already holds. On a 76-table model, a one-table reply's context
  drops from about 28,000 characters to 12,000–16,000. Re-running 20 real-world questions, a
  table-scoped schema reply averaged 40,120 characters instead of 50,851, and the questions
  received 20% fewer characters in all. Every question still answered, and the ones whose answer
  depends on the date-window rules wrote the same filters.

- **Tool replies are compact JSON (#418).** Every tool reply was serialised with `indent=2`. Its
  reader is a model, which gets nothing from indentation and pays for every character of it. Measured
  on a 76-table model: an overview reply drops from 59,168 to 50,941 characters, a one-table
  `get_datasource_schema` reply from 73,558 to 62,148, and a 100-row `execute_sql` result from 18,010
  to 14,030 — about 10% of everything an agent receives across a run of real-world questions. No field
  changes, so nothing a client parses changes; only whitespace.

  The hosted HTTP server re-serialises every reply to stamp `caller_identity`, so it uses the same
  separators — otherwise it would have put the whitespace back on every hosted reply. Files written
  to disk (snapshot and batch manifests, the discovery inventory) and the `sm` CLI's own output stay
  pretty-printed: people read those. A test fails if `indent=` reappears in either module that builds
  replies.

- **`metric_index` lists only metrics with a description; the rest are counted (#406).** A metric
  with no `description`, or one that only restates its name, was listed with its own name as its
  description: characters on every `get_datasource_schema` call that told the agent nothing the key
  did not. It is now counted in a new `metrics_without_description` field instead, and still comes
  back in full when its table is opened. On a 76-table model with 22 of 175 metrics described, the
  index drops from 12,257 to 2,220 characters on every schema reply. A `query` now also matches an
  undescribed metric's `calculation`, through the capped word-overlap ranking only, so a question
  can still find it. The validator warns once per model (`metric_undescribed`) with the count and
  the first few names.

  **Clients:** "every metric appears in `metric_index`" no longer holds. A client that looked a
  metric up there by name should also check the full `metrics` block of a table-scoped reply.

- **`sm suggest-metrics` no longer proposes plain `COUNT(*)` / `SUM(col)` / `AVG(col)` metrics
  (#406).** #404 kept one when it carried a column `unit`, a caveat, or (for a row count) a grain
  that is not one primary key. All of those already reach the agent on the table it opens, and the
  formatter carries a column's unit through `SUM`/`AVG` by itself, so the metric added only a name.
  The generator now proposes flag rates and start-to-end durations, all left for review, and its
  output drops `auto_approved`. The agami-connect skill now writes a one-line `description` for
  every metric beside its full `calculation`, including metrics imported from LookML, dbt or a
  metrics file, whose source description used to go into `calculation` alone.

### Fixed

- **`sm suggest-metrics` no longer undoes a rejection (#406).** It checked proposals against the
  model as loaded for serving, which leaves rejected metrics out, and then wrote each proposal over
  any file of the same name. So a re-run reset a metric a person had rejected to `unreviewed`. It
  now checks against every metric file on disk, rejected ones included. It still proposes nothing
  on a rejected table or column.

## [0.9.8] — 2026-09-30

### Security

- **A `column_scope` refusal lists what the tables it read DO declare (#386).** It said a column
  was not declared and not what the agent could use, so the agent guessed again: refused,
  guessed, refused. `remediation` now leads with the declared columns of the tables the refused
  statement reads — the same columns `get_datasource_schema` shows for them, up to 40 per table
  and 5 tables — and the closest listed name for each refused column, as a typo hint. This is a
  recorded amendment of the rule that a refusal never lists the declared surface; see SECURITY.md,
  "What a refusal may name", for the boundary and the residual.

### Changed

- **The HTTP server runs on MCP SDK 2 and serves protocol 2026-07-28 beside 2025-06-18.** The server
  extra now requires `mcp>=2.2,<3`; the 1.x line gets security fixes only. Tool names, descriptions
  and input schemas are unchanged, and a client on either protocol era sees the same answers as
  before, except in four places an operator or client author should know about:
  - **A crashed tool's reason no longer reaches the client.** A handler that raises, or an audit
    write that fails, now answers `isError` with the fixed text `Error executing tool <name>`. The
    exception's own words could name a column the caller never sent. They go to the server log, as
    one ERROR record per failure with its traceback (two when the handler raises and the audit
    write then fails too), and to the new operator-only `tool_calls.error_detail` column, cut to
    the same bound as `query_executions.error_detail`. Migration 027 adds the column and runs on
    startup.
  - **A hidden or unknown tool now answers JSON-RPC error `-32602` `Unknown tool: <name>`** rather
    than an `isError` result. A hidden tool and a name nobody registered get byte-identical answers
    on both eras, including when the arguments are invalid, and the stdio server answers the same.
  - **`/mcp` refuses a request whose `Host` is not `PUBLIC_BASE_URL`'s host with 421, and one whose
    `Origin` is some other origin with 403.** This guards against DNS rebinding. A loopback base URL
    also accepts `localhost`, `127.0.0.1` and `[::1]` on any port. Server-to-server clients send no
    `Origin` and are unaffected, but a browser-based client served from another origin is now
    refused. A proxy in front of the server must pass the public `Host` through. Discovery and
    OAuth routes are not checked.
  - **On 2026-07-28, `tools/list` and `server/discover` carry cache hints** (`ttlMs: 60000`,
    `cacheScope: "private"`), so a client may reuse a tool list for a minute within one
    authorization. `server/discover` returns the same instructions as `initialize`, a consumer's
    `extra_instructions` included.
- **`get_datasource_schema` answers a wrong name with the right ones.** A datasource, area,
  table or metric name the model does not have now comes back with `did_you_mean` (the closest
  real names, or nothing when nothing is close), and a `hint` when a real name was sent in the
  wrong parameter: an area as a table, a column as a table, a table as an area. A table scope
  naming only unknown tables is refused rather than answered as an empty model; unknown
  `metric_names` are reported in `unknown_metric_names` instead of dropped. Values of the wrong
  shape are repaired where the intent is clear (`"orders"` for `["orders"]`, `["sales"]` for
  `"sales"`) and otherwise refused as `invalid_argument`; an unknown `mode` is refused rather
  than silently becoming `summary`. The tool description now says where each name comes from.
- **`get_prompt_examples` refuses an unknown `area`** with the same suggestions, instead of
  answering as if it were a real area with no examples. Omitting `area` still returns the top
  examples across every area.
- **The activity log records more of what each tool call did.** Each tool call now
  records the parameters it was sent, the names the model did not have and what was offered
  instead, how many names missed, and the size of the reply. A call that answered with one name
  missing is still a success, with a miss count above zero. Migration `027_tool_calls_facts.sql`
  adds the five columns; no tool response changes.

### Fixed

- **Introspecting Postgres as the read-only role now finds its keys (#175).** Postgres shows
  constraints in `information_schema` only to a table's owner or a role with a privilege other than
  SELECT, so the role `readonly-grants.md` creates saw no primary or foreign keys: every grain was
  guessed and every join inferred from column names, which can bind the wrong parent. The PostgreSQL
  dialect now reads `pg_constraint`, which any role can read. Re-run `/agami-connect` to pick the
  keys up; the model gains `confirmed` joins and catalog grains where it had guesses. Redshift keeps
  the `information_schema` queries, since it lacks the `LATERAL` and `unnest ... WITH ORDINALITY`
  the new ones use. When no declared foreign keys are visible at all, the introspection report now
  says the joins were inferred instead of staying silent. A declared foreign key to a table outside
  the model (pruned, or in a schema not introspected) is skipped, and the report counts it, rather
  than becoming a join to a table the model does not have.

- **A failed `execute_sql` on the HTTP server records its real kind.** The activity log
  said `failed` for every failure. It now says which: `syntax`, `timeout`, `auth`, and so on.

- **The read-only guard now speaks every dialect it serves (#395).** `execute_sql` advertises the
  MCP `readOnlyHint`, but the dangerous-function deny-list held Postgres primitives only, so a
  single `SELECT` could still carry a side effect on every other engine: `SELECT SLEEP(30)` and
  `SELECT GET_LOCK('x', 10)` on MySQL, `SELECT SYSTEM$ABORT_SESSION(…)` on Snowflake,
  `SELECT reflect('java.lang.Runtime', …)` on Databricks, `SELECT … FROM OPENROWSET(BULK
  '/etc/passwd', …)` on SQL Server, `SELECT UTL_HTTP.REQUEST('http://…')` on Oracle,
  `SELECT EXTERNAL_QUERY(…)` on BigQuery, `SELECT load_extension(…)` on SQLite / DuckDB. The
  deny-list is now organized by engine and covers all of them, and the whole Snowflake `SYSTEM$…`
  namespace is refused rather than a member list that would go stale.
- **Three Postgres holes in families the list already claimed.** `pg_notify` — the function
  spelling of the denied `NOTIFY` keyword, which `\bNOTIFY\b` cannot see because `_` is a word
  character. The advisory-lock family beyond the four names listed (`pg_try_advisory_lock` and the
  `_shared` variants — eleven functions, matched by one prefix now). And the siblings of the
  already-denied `pg_drop_replication_slot`: `pg_promote`, WAL-replay pause/resume, backup
  start/stop, slot create/copy, `pg_logical_emit_message`, and `pg_logical_slot_get_changes`, which
  is a destructive read — it advances the slot, so a downstream consumer loses those rows.
- The deny-list names SQL Server's `OPEN…` functions individually rather than matching `OPEN\w+`,
  so the ordinary `OPENJSON` still runs, and the false-positive corpus was extended with the names
  most able to collide (`sleep_minutes`, `AVG(sleep)`, `lock_count`, `reflection_score`).
- **One deliberate over-refusal, since these are the first deny-list entries that are ordinary
  English words.** `WORD(` is also the column-list form of a CTE or derived table, so
  `WITH benchmark (region, target) AS (…)` and `JOIN (…) AS benchmark (region)` are now refused,
  naming a function the statement does not contain. A lookaround narrow enough to admit them would
  let a real call through in some position, and this gate fails closed, so the behaviour is pinned
  as `REJECT_CTE_NAME_COLLISION` instead of worked around. If the trade is judged the wrong way
  round, the fix is to drop `sleep` and `benchmark` — both are pure time-wasters already bounded by
  the executor's per-statement timeout.
- `docs/mcp-server.md` carried a third copy of the Postgres-only function list, beside `SECURITY.md`
  and `plugins/agami/shared/sql-generation-rules.md`; all three now say "every engine it dispatches
  to". `load_extension` is labelled SQLite-only — DuckDB loads extensions with `INSTALL` / `LOAD`,
  statements the opening-keyword step already refuses — and Trino likewise adds no entry.

## [0.9.7] — 2026-09-26

### Changed

- **`get_datasource_schema` stops repeating the join graph at both tiers.** Two wire-shape
  changes to the same response, each measured on a wide model (22 subject areas, 76 tables).

  `mode="index"` sent `cross_area_relationships` as one entry per declared cross-area edge. The
  projection drops the join columns on purpose — mechanics belong on the `dataset_names` tier —
  but the column is the only thing telling apart several edges that reach the same pair of
  tables, so those serialized identically and every copy was sent: 283 entries for 181 distinct
  facts, 32,596 chars. It is now an adjacency map, `{table: [table, …]}`, at 5,112 chars. Names
  are bare, which is the only form `dataset_names` can honour: it strips every qualifier and
  resolves first-match, so a qualified node would look precise and select a different table.
  Where one name is declared under two schemas both edges land on one node — a merge the next
  call cannot avoid either, and #258 is its fix. The key is omitted entirely when there is
  nothing to route — an empty map is truthy where the old empty list was not.

  `dataset_names` sent every relationship *touching* a requested table in full. On a hub table —
  a user or group dimension half the warehouse references — that is the whole graph: a two-table
  request returned 136 edges, of which 4 joined the two tables asked for, and the block ran
  98,724 of the response's 129,858 chars. Every edge still ships, because the scope gate admits
  any table the model declares and a correlated `EXISTS` needs only the join key. What changes is
  the detail: an edge between two requested tables is unchanged, and every other edge carries its
  join — endpoints, columns or the `on:` expression, schemas, cardinality, join type — without
  the sign-off block, review state, confidence, subject-area labels or the generated description.
  The trust block costs nothing to omit: the receipt recomputes review state and sign-off for
  each join the statement actually wrote. `executable` survives whenever it is not `same_engine`,
  since a `split` edge written as a `JOIN` cannot run.

  Both tiers now also drop `for_questions_about` (deprecated, never written) and `executable` at
  its default, so one response no longer omits the common value on one edge and states it on the
  next.

- **The SQL dialect rules ride `list_datasources`, not every schema response (#405).** They
  describe the ENGINE, so they never varied with the scope being asked about — the same ~1,750
  chars were re-sent on all four `get_datasource_schema` tiers and on every call of a multi-call
  question. They now sit on the call that answers "which datasource, on what engine", which is
  made once: a listing entry gains `engine` (the `storage_type` the model declares) and
  `dialect_rules`. A schema response carries `dialect: {engine, rules_from: "list_datasources"}`
  instead — it still names the engine, and a client that arrived without listing datasources is
  told where to get the rules. On a wide model the `mode="index"` call goes from 50,178 to 48,470
  chars, and the saving repeats per call.

  All three fields are conditional, and absent rather than null when they do not apply: `engine`
  and `dialect` when the model declares no single engine (no connection, or two that disagree —
  "which dialect" has no answer then, and guessing is worse than saying nothing), `dialect_rules`
  when the engine has no known gaps. `contracts.DatasourceInfo` and `DatasourceSchemaResult`
  declare them accordingly.

  `engine` is emitted beside `database_type`, not instead of it: that field is derived from the
  DSN and reports the connection's scheme, which is `postgres` for a Redshift warehouse reached
  the usual way. The two can disagree, so the one the MODEL declares is the one the rules are
  chosen by, and it is named.

- **The metric generator stops proposing metrics that restate their own name (#404).** A plain
  aggregate over a column whose `aggregation` class already licenses it is not a metric:
  `_check_aggregation_semantics` enforces that class on every statement with or without a named
  metric, so `orders_total_amount = SUM(amount)` adds a name and nothing else. Proposed per table
  and per column, these were most of a wide model's catalogue — 137 of 175 served metrics had a
  bare `COUNT(*)` / `SUM(col)` / `AVG(col)` as their whole binding.

  `suggest_metrics` now proposes a plain aggregate only when the proposal carries something the
  class does not, and only something it actually writes onto the metric: a `unit` (the formatter
  needs it), a caveat (appended to the `calculation` prose, so the caveat that justified the
  metric arrives with it), or — for `COUNT(*)` — a grain that is not exactly one primary key, which
  means it may not be counting things. A table's `default_filters` deliberately does **not**
  license one: `execute_sql` does not apply default filters and a bare `COUNT(*)` binding does not
  embed them, so a filtered table's plain count says exactly as little as any other. Flag rates and
  duration pairs are unaffected — those encode a choice. Re-run against the same wide model, the
  generator's plain-aggregate proposals fall from 203 to 58. A curator can still add any plain
  metric by hand; this is only what the generator proposes unprompted.

  Two consequences worth stating. **A caveat-justified metric no longer auto-approves.** A trivial
  binding used to skip the review queue with a system sign-off, and that claimed only that
  `SUM(col)` is `SUM(col)`. Such a metric now survives precisely because someone attached a caveat,
  the caveat is its whole justification, and it rides as prose nothing verified — so it waits for a
  person, for the same reason a flag rate does. A metric carrying only a `unit` is still
  judgment-free and still auto-approves. And **`--max-per-table 0` now returns no metrics** rather
  than one: the cap was floored at 1 to protect a row count that was unconditional, and it is not
  unconditional any more.

## [0.9.6] — 2026-09-23

### Changed

- **Read-only tools now say so on the wire.** Every core tool (`list_datasources`,
  `get_datasource_schema`, `get_prompt_examples`, `execute_sql`) is listed with the MCP
  `readOnlyHint` annotation, `destructiveHint` off, on both transports (HTTP and the stdio harness
  `/agami-serve` wires into Claude Desktop). A client's confirmation policy keys off these hints:
  Gemini Enterprise treated each un-annotated tool as potentially destructive and asked the person
  before every call, once per distinct argument set, so each query cost a prompt. The hint is opt-in
  per registry entry (`read_only: True`, or `tools.register(..., read_only=True)`) and only by the
  literal `True`; `create_app` refuses a non-bool value. A consumer tool that writes keeps the
  client's default caution. After deploying, reload the connector's actions so the hints are
  re-imported.
- **`mcp` floor raised to 1.7**, the first release whose `mcp.types` carries `ToolAnnotations`. An
  older SDK would fail every `tools/list`.

- **The `SELECT *` ban is now stated up front, not only inside the refusal (#387).** A star is
  refused wherever it appears — the outer query, a subquery, a CTE body, `t.*` — and the served
  instructions never said so, so a client met the strictest rule on this surface for the first time
  as a refusal. This is the same gap #360 closed for column scope, in the same place, for the same
  reason. The gate is unchanged: every projected star still refuses, and `COUNT(*)` and other
  aggregates over a star are unaffected — there the star is inside the call, not the
  projection, and the instruction says so rather than reading as a wider ban than the gate.

- **And the refusal says what is true of a star** rather than what we happen not to know. The old
  sentence — "every column must be named so it can be checked" — read alongside the reasoning that
  a star's columns "live in the catalog" led a caller whose star sat over a CTE naming its own
  columns two lines up to conclude the refusal was mistaken about their query. It was not: a star
  returns columns the statement never names, whatever can be inferred about which ones they are.
  The remediation now names the CTE case explicitly, because that is where a caller is most likely
  to believe the rule cannot mean them.