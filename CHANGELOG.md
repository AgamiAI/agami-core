# Changelog

All notable changes to **agami** are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

The `version` in `.claude-plugin/marketplace.json` and `plugins/agami/.claude-plugin/plugin.json`
is the source of truth a host installs against — bumping it is what invalidates a
user's plugin cache (see [CONTRIBUTING.md](CONTRIBUTING.md)). Each released section
below corresponds to one such version.

## [Unreleased]

### Added

- **MCP App seam: a page beside a tool, an app-only tool, and a tool-result hook.** Three optional
  seams for a consumer that wants an MCP App rendered beside a tool's answer; agami-core uses none of
  them, and with none in use every byte a client of either protocol era receives is unchanged. An
  `extra_tools` entry may carry `"page": {"uri": "ui://…", "html": str}`, which puts
  `_meta.ui.resourceUri` on the tool and serves the page over `resources/list` and `resources/read`
  with the MCP App MIME type, and `"app_only": True`, which adds `_meta.ui.visibility: ["app"]`. A
  page declares only `uri` and `html`: `csp`, `domain`, `permissions` or any other key is refused at
  composition, naming the tool, so a page cannot ask a host for network access. A page is visible
  when any tool linking it is, under `Adapters.tool_visibility`, and a hidden page answers the same
  `Unknown resource` error as an undeclared one, byte for byte. The `resources` capability, and
  `capabilities.extensions["io.modelcontextprotocol/ui"]` on 2026-07-28, appear only when a page is
  declared. **App-only is a marking, not a control: any client can still call the tool, so its
  handler must check its own scope.** `Adapters.tool_result_hook(name, arguments, result_text)`
  returns an object added to the result's `_meta` beside the unchanged text (not `structuredContent`,
  which a client may give the model in place of the text); it runs off the event loop in the
  request's context, only on a result a handler returned, keys under the reserved
  `io.modelcontextprotocol/` prefix are dropped, and a hook that raises or returns anything but a JSON
  object is logged once and dropped without touching the call's outcome or its audit row. `None` (the OSS default) is byte-identical to prior behaviour.

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