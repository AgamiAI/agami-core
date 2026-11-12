# Context export (`sm export`)

`sm export` writes a reviewed agami semantic model into the files that Snowflake, Databricks and
the Apache Ossie interchange format load: tables, columns, metrics, joins, example questions and
business terms, each in that target's own shape. It reads the model from disk and writes files
under `--out`. It opens no database connection and makes no network call; applying the files is
something you do yourself, in your own account.

Two limits come first, because everything else follows from them:

- **The export carries knowledge, not enforcement.** agami's pre-query checks (table, column,
  sensitive-column and join scope), the receipt on every answer, and the audit log run inside
  agami. Nothing in a Snowflake, Databricks or Ossie file can run them, so they do not travel and
  nothing in the files imitates them. A Genie space or a Cortex Agent built from these files answers
  without agami's checks. Every report says so in its `does_not_travel` list.
- **SQL is never translated.** A metric travels only with a binding a person wrote for the target's
  engine (`bindings.Snowflake` for Snowflake, `bindings.Databricks` for Databricks; Ossie carries
  both). A metric with no such binding is left out and named in the report. Every other piece of SQL
  (example and golden-answer SQL, default filters, `on:` join conditions, tables defined by a query)
  travels only when the model is written for the target's engine. A model built on SQLite or
  Postgres therefore exports its example questions without their SQL, and its default filters not at
  all.

## What the export carries, and what it leaves out

Only approved items travel by default. A golden item counts as approved when its
`expected.sql_confirmed` is true; a prompt example when its status is `confirmed` or its
`review_state` is `approved`. Rejected items
never travel. A column flagged `sensitive` stays home, and so does anything whose SQL or text names
it, matched by name in any table: a metric, a default filter, a relationship, an entity mapping, an
example, a golden answer, an ambiguity, a terminology entry, and a table defined by a query (its
query is SQL like any other, so `SELECT email AS contact FROM customers` stays home, and so does
whatever names that table). SQL counts as naming the column when it cannot be parsed, and when it
projects a `*` or `t.*` over a table holding a column left home, since the star returns the column
without naming it (`COUNT(*)` does not count; a star whose table cannot be told does).

Descriptive text is checked the same way. Every free-text field the export carries (descriptions,
caveats, a metric's calculation and business question, `choice_field` labels, synonyms, an
entity's format hint, column-group descriptions, performance hints, the datasource and subject-area
descriptions, the default time window, and any prose field a later model version adds) that
mentions a column or table left home travels blank; its item still travels, reported as `degraded`
· `depends_on_excluded`. The match is by whole word in any case, so a false match blanks more and
never less. That has a cost: a sensitive column with a common-word name, such as `status` or
`name`, blanks every description that uses the word. Rename the column in the model, or pass
`--include-sensitive` if the files stay where the column may go.

The report never prints a sensitive column's name. The column is named by an export id, and so is
any other item whose own name holds the column's name (a question, a term, a metric); a `detail`
that would quote it is withheld. One flag writes sensitive column names into a file:
`--genie-hide-sensitive`, which lists them in the Genie space to mark them hidden there (see the
flag table).

One subject area is one unit: one semantic view, one Genie space, one Ossie semantic model.
Relationships, entities and metrics that span subject areas, and golden items no single area can
hold, are left out as `spans_subject_areas`.

The signer of an item stays in agami. An item travels as approved, and in some places with the
time it was approved (a Snowflake verified query's `verified_at`, Ossie's extension block), never
with who approved it.

## The command

In Claude Code, ask for it ("export my model to Snowflake"): the `/agami-export` skill asks for the
formats, the location and the extras on cards, shows the exact command, and writes the files to
`<artifacts_dir>/local/exports/context/<profile>/`. This section is the command reference. Directly,
with the plugin installed (`<plugin_root>` is the plugin's own folder):

```bash
bash "<plugin_root>/scripts/sm" export ~/agami-artifacts/<profile> \
  --target snowflake --target databricks --target ossie \
  --location demo.sales_data --out ~/agami-artifacts/local/exports/context/<profile>
```

From a clone of this repository, against the bundled sample:

```bash
PYTHONPATH=packages/agami-core/src python -m semantic_model.cli export \
  plugins/agami/samples/store/model \
  --target snowflake --target databricks --target ossie \
  --location demo.sales_data --out /tmp/agami-export
```

(That needs `pydantic`, `pyyaml` and `sqlglot` importable; `uv run --with pydantic --with pyyaml
--with 'sqlglot<30.26' python -m …` supplies them.)

Every flag below is off, or empty, unless you pass it. The last column says what passing it puts at
risk; "none" means it only changes what the files hold.

| Flag | Default | What it writes or changes | Risk |
|---|---|---|---|
| `<root>` | required | The semantic model directory (the one holding `datasource.yaml`). It is only read. | none |
| `--target T` | required | `snowflake`, `databricks` or `ossie`. Repeat for several targets in one run. Naming a target twice, or one that does not exist, is refused, and so is a flag below that belongs to a target the run does not name (`--genie-hide-sensitive` or any `--uc-*` or `--pages-*` flag without `--target databricks`, a `--snowflake-*` flag without `--target snowflake`, `--ossie-version` without `--target ossie`): the refusal names the flag and the target it needs. | none |
| `--out DIR` | required | Where the files go. Each run replaces `DIR/<target>/` whole and writes `DIR/export-report.json`; nothing else under `DIR` is touched. A `DIR/<target>` that is a symbolic link is replaced, never followed. `DIR` must not be, hold, or lie inside the model (or its `golden_datasets` or `prompt_examples` directory, wherever a link points them), and must not be an existing file; such a run is refused, since the export never writes into the model. | Anything you keep in `DIR/<target>/` is deleted on the next run. |
| `--area A` | every area | Export only this subject area. Repeat for several. | none |
| `--location DATABASE.SCHEMA` | none | Where the tables live on the target: `database.schema` for Snowflake, `catalog.schema` for Databricks. Required for `snowflake` always (the view is created in a schema), and for any target whose engine differs from the model's. Without it, those runs are refused. When the model is written for the target's engine, a table keeps the schema the model names and takes only the database or catalog from `--location`. | none |
| `--include-unreviewed` | off | Also carry items not yet approved. The report lists each one under `included_unreviewed`. | Definitions nobody signed off reach the target's AI. They are never marked certified (Snowflake tags them `TO BE REVIEWED`), and their Pages stay drafts. |
| `--include-sensitive` | off | Also carry columns flagged `sensitive`, and whatever names them. | Sensitive column names, and the SQL and text that use them, go into every file. |
| `--ossie-version V` | `0.1.1` | `0.1.1` or `0.3` (writes `0.3.0.dev0`, a development version whose shape may still change). | none |
| `--snowflake-warehouse NAME` | none | The warehouse the Snowflake agent runs its SQL on, written as the tool's `execution_environment`. It must be a name an unquoted identifier reaches (letters, digits, `_` and `$`, not starting with a digit, all one case, not a keyword) and is written upper-case; any other name is refused. Without it no warehouse is written, and the agent does not run for a caller with no default warehouse. | none |
| `--snowflake-certification-tags` | off | Tags the semantic view and its tables, dimensions, facts and metrics with `SNOWFLAKE.TAGS.CERTIFICATION_STATUS`: `CERTIFIED` for an approved item, `TO BE REVIEWED` for one `--include-unreviewed` carried. A table is `CERTIFIED` only when it and every column it carries are approved. The view is `CERTIFIED` only when every item carried for it is approved and it carries no key term or ambiguity: agami holds those with no review state, so no review of them is recorded, and the view is `TO BE REVIEWED`. A time dimension has no `tags` field and goes untagged. | Changes the catalog: the tags mark objects certified for everyone who can see them. Applying the view then needs the `SNOWFLAKE.OOB_TAG_APPLY` application role, which only ACCOUNTADMIN holds by default. |
| `--snowflake-no-expected-answers` | off (answers are computed) | Writes the evaluation data without expected answers, except a golden item's `bounds`. | Turns off the one default that copies data: without this flag, running the evaluation file stores up to 20 rows of each golden query's result in a table and an evaluation dataset in your account. Only a query that reads columns the semantic view carries, and no `*`, is run that way. |
| `--snowflake-mcp-server` | off | Also writes `<area>.mcp_server.sql`, a Snowflake-managed MCP server with a Cortex Analyst tool on the view and the agent as a tool. | Opens an endpoint on the account that MCP clients can call with the grants you give. |
| `--genie-hide-sensitive` | off | Lists each sensitive column of a table the Genie space lists, under `column_configs` with `exclude: true`, which marks the column hidden in the space. The space lists the whole table, so without the flag the report counts those columns, unnamed. | Writes those columns' names into the space file, the only place an export names one. Hiding is not access control: it does not prevent a query that names the column; restricting who can read a column is done with a Unity Catalog column mask. |
| `--pages-publish` | off | Writes each approved Page as `PUBLISHED` and its domain as not a draft; unreviewed pages stay drafts. Key-term pages stay drafts too, and are never marked certified: agami holds key terms with no review state, so no review of one is recorded. The report lists each (`degraded` · `not_approved`). | Makes the pages visible to every user of the domain, and Genie One uses them in every conversation. Without it, a page is a draft that only its owner sees. |
| `--pages-metrics` | off | Also writes a Page per metric. Off by default, since the metric views already give Genie each metric's definition. | none |
| `--uc-comments` | off | With `--target databricks`, writes `unity_catalog/<area>.sql` (and `<area>_undo.sql`) with `COMMENT ON TABLE` and `COMMENT ON COLUMN` from descriptions, units and caveats. | Changes the catalog: each statement replaces the existing comment, including one a person or Databricks' AI-generated comments wrote. Undo sets a table comment to NULL and a column comment to an empty string; neither brings the old one back. |
| `--uc-constraints` | off | Adds informational primary and foreign keys to that file, with the `NOT NULL` a primary key column needs. Every constraint is named `agami__<table>__…`, so a constraint of that name that existed before is implausible. | Changes the catalog: `SET NOT NULL` checks every existing row and fails on a NULL, and adding a constraint may upgrade a Delta table's writer protocol. Undo's `DROP NOT NULL` also relaxes a column that was `NOT NULL` before. A table can have at most one primary key, so on a table that already has one the `ADD … PRIMARY KEY` fails and can be skipped (its foreign keys still apply when the existing key is on the same columns; skip that table's undo lines); a commented-out query in the section lists the primary keys the tables already have. Once applied, the section cannot run again until its undo has run. |
| `--uc-certify` | off | Adds `SET TAG … system.certification_status = certified` for each table approved with every column it carries, and for each of this export's metric views whose every measure, table, column and join is approved and that holds no level-of-detail measure (its file asks you to compare that measure with agami's answer first). | Changes the catalog: marks those objects certified for everyone who can see them. The section opens with a check per catalog and schema that stops it, naming the objects, when any object it tags already carries `system.certification_status`; to skip an object, delete its `SET TAG` line and its name in the check. The undo file's tag removals are commented out: enable the line of each object whose `SET TAG` succeeded. |

Every refusal (an `--out` that overlaps the model, two subject areas a target names alike, and
every flag refused above) happens before anything is written or removed: the command prints
`{"error": "export_refused", "detail": "…"}` and exits 2. A successful run prints the report and
exits 0. The same model, target and options always give the same bytes, so exporting into a
directory under version control gives a readable diff.

## Flags that change your catalog or make content visible

These are all off by default. Each is its own decision, made by the person who owns the account:
pass one only when you mean it.

- `--snowflake-certification-tags`: certification tags on the Snowflake view and its members.
- `--snowflake-mcp-server`: an MCP endpoint on the Snowflake account.
- `--uc-comments`, `--uc-constraints`, `--uc-certify`: comments, keys and certification tags in
  Unity Catalog. With none of them, no Unity Catalog file is written.
- `--pages-publish`: Pages every user of the domain sees, used by Genie One.
- `--genie-hide-sensitive`: sensitive column names written into the Genie space file.
- `--include-sensitive` and `--include-unreviewed`: sensitive or unreviewed content in every file.

One default writes data inside your account: the Snowflake evaluation file computes each expected
answer from your tables when you run it, and stores up to 20 result rows. It does so only for a
golden query whose every column is one the semantic view carries, with no `*` anywhere (bare, as
`t.*`, inside a call such as `OBJECT_CONSTRUCT(*)`, or in a subquery; `COUNT(*)` alone is allowed,
since it counts rows): leaving a column out of the model is how you keep its values out of agami,
and that holds here too. `--snowflake-no-expected-answers` turns it off.

SQL that names a table in a schema or catalog other than the model table's own (`archive.orders`,
`otherdb.otherschema.orders`), or spells it in a case the engine treats as another object (on
Snowflake, `"orders"` is not `ORDERS`), or names it dynamically (`IDENTIFIER('orders')`), is never
pointed at the model's table: a verified query, a Genie example or benchmark, or a computed answer
built from it is left out and reported instead.

SQL carried as text (verified queries, Genie example and benchmark SQL, the evaluation data's
expected tool output) carries no values, and the rule above on declared columns is not applied to
it: it already leaves out anything that names a column the export excludes. A Snowflake verified
query must use the view's own columns anyway, and a Genie space lists whole tables, so a column
the model never declared is part of a listed table whether or not example SQL names it.

## What each target writes

The file lists below are what the bundled sample produces (one subject area, `agami-example`) with
`--location demo.sales_data`, plus what a model with golden items adds. A file is named after the
object it creates, so `<area>` below is the subject area's name under the target's naming rule (the
sample's `agami-example` becomes `agami_example` on every target). Two subject areas
that one target would name alike (`a-b` and `a_b`, or `Sales` and `sales`) are refused; rename one,
or export each with `--area` into its own `--out`.

A table's default filter is descriptive in agami: it is not applied automatically, and the query
writer adds it only when a question calls for it. Each target carries it with the same words: "An
optional filter, declared in agami as a default filter: use it only when the question asks for this
restriction. It is not applied automatically, and metric definitions are not filtered by it." In
Snowflake that is the `labels: [filter]` dimension's description (and a line in the agent's
instructions), in a Genie space the filter snippet's instruction, and in Ossie the `usage` entry of
the extension block's `default_filters`. The words are guidance for the target's AI, not a rule: an
agent may still choose to apply the filter.

### Snowflake (`<out>/snowflake/`)

| File | When | What it is |
|---|---|---|
| `<area>.semantic_view.yaml` | always | The semantic view: logical tables with keys, dimensions, time dimensions and facts; metrics (their Snowflake binding); relationships; examples as verified queries when their SQL travels; ambiguities, the fiscal year and the area's default time window as Cortex Analyst instructions; entities folded into their tables' descriptions and synonyms; key terms in the view's description. A verified query carries `verified_at` from its sign-off time (never the signer), and the first five approved ones are flagged `use_as_onboarding_question`. A self-join becomes a second logical table on the same base table (a role-playing table), and a metric that reads tables no one table relates to becomes a view-level (derived) metric when each of its aggregates reads one table. |
| `<area>.sql` | always | `CALL SYSTEM$CREATE_SEMANTIC_VIEW_FROM_YAML('<database>.<schema>', $$…$$)`, carrying the YAML file byte for byte. Running it creates or replaces the view. With `--snowflake-certification-tags`, its header says what applying the tags needs, and that re-applying with CREATE OR ALTER semantics (`TRUE` as a fourth argument) does not add or change tags. |
| `<area>.agent.sql` | always | `CREATE OR REPLACE AGENT <database>.<schema>.<view>_agent COPY GRANTS FROM SPECIFICATION $$…$$`: a Cortex Agent with one Cortex Analyst tool bound to the view. COPY GRANTS keeps the grants on an agent it replaces. Its instructions carry the descriptions, the fiscal year, the optional-filter rule, the ambiguities, and an instruction to name the view as the source of the tool's results; the example questions are its sample questions (cut from the end at the 100,000-byte specification limit). No model or budget is set. The warehouse is set only by `--snowflake-warehouse`; without it the header shows the lines to add. |
| `<area>.evaluation.sql` | the area has approved golden items | A table of `input_query` / `ground_truth`, registered with `SYSTEM$CREATE_EVALUATION_DATASET` as a Cortex Agent evaluation dataset. Each row names the expected tool call and, when the golden SQL travels, that SQL as its expected output, rewritten onto the view's logical table and column names the way its verified queries are; where that rewrite is refused, the SQL stays as written and the report says so. Unless `--snowflake-no-expected-answers`, each row's expected answer (`ground_truth_output`) is computed inside your account when the file runs: the golden SQL runs as a subquery on the physical tables, and its row count and up to 20 of its rows go into a plain-language rubric. Each row is quoted as JSON with its keys in order, and each number is rounded to 6 decimal places and written in positional notation (no exponent, no trailing zeros); a number too large for `NUMBER(38, 6)` keeps its own text. Only SQL that reads columns the view carries, with no `*`, is run, and its result columns must have distinct names (one unnamed column alone is fine). A golden item's `bounds` become one more rubric sentence. |
| `<area>.evaluation_config.yaml` | with the evaluation data | The agent evaluation run configuration: answer correctness (when some row has an expected answer), tool selection, tool execution and logical consistency, pinned to metric version `v3`; plus a custom metric for the area's ambiguity rules and one for its optional filters, when it has them. Each custom metric names its judge, `model: claude-sonnet-4-6`, the default judge of `v3`, since Snowflake's documentation says to set `model` explicitly. An account that does not allow Anthropic models changes each `model:` line to `openai-gpt-5.4`, the other `v3` judge; the header says so. |
| `<area>.analyst_evaluation_config.yaml` | the view holds verified queries, with or without golden items | A Cortex Analyst evaluation of the view, named `<database>.<schema>.<view>`, which runs the SQL Cortex Analyst generates for its verified queries and compares results. `source_metadata.verified_queries` lists those queries by question, in the view's order; remove a line to leave a query out of the run. |
| `<area>.evaluation_run.sql` | with either evaluation configuration | The statements that create a stage, copy the configurations written into it, start each run, check its status and read its results. Written, never run. Every name in it is fully qualified; it opens with `USE SCHEMA` because an evaluation started with SQL runs in the session's database and schema (Snowflake's Cortex Agent evaluations page). No other exported file sets a schema. |
| `<area>.mcp_server.sql` | `--snowflake-mcp-server` | `CREATE OR REPLACE MCP SERVER <database>.<schema>.<view>_mcp`: a Cortex Analyst tool on the view and the agent as a tool, annotated read-only (a hint to the client; the grants are what control access). Its header lists the grants a client's role needs and the URL a client connects to. |

The bundled sample has no golden dataset and no example whose SQL becomes a verified query, so it
writes the first three files (and the MCP server file with its flag).

Snowflake's documented rules are checked at export (no relationship cycle, a relationship points at
a primary or unique key, a table-level metric aggregates and reads only tables it relates to,
synonyms are unique across the view), so what is written can be created as it stands; an item that
would break one is left out and reported.

### Databricks (`<out>/databricks/`)

| File | When | What it is |
|---|---|---|
| `metric_views/<area>__<table>.sql` | always | One metric view per fact table (a table some exported metric is measured from): `CREATE OR REPLACE VIEW <catalog>.<schema>.<area>__<table> WITH METRICS LANGUAGE YAML AS $$…$$` at version 1.1, with many-to-one joins, the joined tables' columns as fields, and each metric's Databricks binding as a measure with its synonyms. A metric in a plain ISO currency (`USD`) gets `format: {type: currency}`. Semi-additive metrics become `semiadditive` windows. A second-order metric (`OUTERAGG({base})` at an `inner_grain`) becomes a level-of-detail measure with a `partition` block when that states agami's meaning exactly; the file's header then says so. Default filters never go into the view's `filter`, which Databricks applies to every query. A definition holding a `$` says in its header to run the file in the SQL editor or on a SQL warehouse, not in a notebook cell: a notebook reads `$name` and `${name}` as widget references, and a dollar-quoted body has no escape for them (the Unity Catalog file, whose strings are ordinary literals, escapes `$` as `\$` instead). The sample writes five: `invoices`, `order_items`, `orders`, `refunds`, `subscriptions`. |
| `<area>.geniespace.json` | always | A Genie space (`serialized_space`, version 2): the tables and metric views, column descriptions (with units, caveats and code labels) and synonyms, entity matching on coded string columns, join specs, default filters and measures as SQL snippets, one text instruction (ambiguities, the datasource description, the fiscal year, the default time window, key terms), example questions with SQL when it travels, every example question as a sample question, and golden items as benchmarks. Example and benchmark SQL names each table `catalog.schema.table`, as the space lists it, so it does not depend on a default catalog or schema; SQL that names a table in another schema or catalog is left out, never re-pointed at the listed one. Genie's sorting, id and size limits hold at export, and each text field is a list of strings that ends each string but the last at a line end. |
| `resources/<area>.genie.yml` | always | A Databricks Asset Bundle resource (`resources.genie_spaces.<area>`) that deploys the space file. Its warehouse is the bundle variable `genie_warehouse_id`; no warehouse id is written. Its header says what the bundle needs (see "Applying the files"), and, when the space has benchmarks, the calls that start a benchmark run. |
| `pages/<area>__entity__<name>.json`, `pages/<area>__terminology__<name>.json` | per approved entity and key term | One Databricks Page request body: `domain`, `display_name`, `description`, `synonyms`, `body`, and `state: PUBLISHED` with `--pages-publish` (never on a key-term page, since agami records no review of a key term). The domain is a `{domain_id__…}` placeholder. No owner is set, so a page is owned by whoever submits it. Key terms belong to the whole datasource: they sit in one datasource domain, and every subject area's copy of a term page creates the same single page. |
| `pages/<area>__metric__<name>.json` | `--pages-metrics` | One Page per metric, in the same shape. |
| `pages/<area>__manifest.json` | when there are pages | How to submit them, as `steps` in order (governed tag policy, domain, page, asset link, certification), the `domains` to create, the `update` call for a re-export, the placeholders to `fill_in`, and per page: its file, domain, a stable `request_id` (so a re-submitted create returns the page already made), its state and `update_mask`, its related tables as asset links (with a `{metastore_id}` placeholder), and whether it is certified in agami: approved on a recorded review, which a key term never is. |
| `unity_catalog/<area>.sql`, `unity_catalog/<area>_undo.sql` | any of `--uc-comments`, `--uc-constraints`, `--uc-certify` | The Unity Catalog statements for the sections asked for, each section headed by what it overwrites and the privileges it needs, and a script that reverses them statement by statement. A table two subject areas carry gets its statements once, in the file of the first area by name, since a second `ADD CONSTRAINT` or `SET TAG` on it would fail and either undo file would then remove what the other created; a foreign key goes with its child table, and a file whose foreign key references a key another area's file declares says to run that file first. Run an undo file's lines only for the statements whose apply succeeded. The certification section opens with a check that stops it when an object it tags already carries `system.certification_status`, and the undo's tag removals are commented out, since `UNSET TAG` removes the tag whoever set it. Keys are declared without `RELY`: RELY lets the optimizer trust a key to rewrite queries, and agami reviewed these keys but never checked the data against them, so a key the data breaks would return wrong results. Add RELY yourself once you have checked. |

### Ossie (`<out>/ossie/`)

| File | What it is |
|---|---|
| `<area>.ossie.yaml` | One Ossie semantic model: datasets (tables with their source, keys and fields), relationships (as from/to columns), metrics with their Snowflake and Databricks SQL each under its own dialect, and `ai_context` holding instructions, the example questions, and key terms and entity names as synonyms (at 0.1.1 as one text of labelled lines, see below). Default filters and review state travel in one agami extension block (`custom_extensions` under `COMMON` in 0.1.1, `nonportable_extensions` under `AGAMI` in 0.3), outside Ossie's portable semantics. |

At 0.1.1 a field's expression is written under the `ANSI_SQL` dialect, and a metric carries only
`SNOWFLAKE` and `DATABRICKS` expressions. A metric bound for neither is left out.

At 0.1.1 every `ai_context` is one string: the instructions, then a `Synonyms:` line and an
`Example questions:` line, each followed by one `- ` line per entry. Ossie allows a string or an
object there; 0.1.1 writes the string form, and the report notes it once per file. At 0.3
`ai_context` stays an object with `instructions`, `synonyms` and `examples`.

## Applying the files

Nothing here is automated: the export writes files, and you run them in your own account with your
own credentials. Read a file before you run it.

### Snowflake

Run the files in this order; each names objects the one before creates.

1. **The view.** Open `<area>.sql`, add `TRUE` as a third argument to the call (its `verify_only`
   argument, which checks the view without creating it), and run it. Then run the file as written
   to create or replace the view in `<database>.<schema>`. With `--snowflake-certification-tags`,
   the role running it needs the `SNOWFLAKE.OOB_TAG_APPLY` application role.
2. **The agent.** Run `<area>.agent.sql`. Export with `--snowflake-warehouse NAME` to name the
   warehouse its SQL runs on. Without it the agent uses the caller's default warehouse, so a caller
   needs one. The file's header shows the `execution_environment` lines to add under `tool_resources.<view>` if you edit
   it instead.
3. **The evaluation data** (when the file exists). Run `<area>.evaluation.sql`. When it computes
   expected answers, the role running it needs SELECT on the tables the view reads. Each answer is
   as old as the run: it goes stale when the data changes. A dataset name registers once, and
   Snowflake documents no statement that drops a dataset, so to refresh the answers give the
   dataset a new name in three places: `<area>.evaluation.sql`, `<area>.evaluation_config.yaml`,
   and the copy of that configuration `<area>.evaluation_run.sql` stages (its `dataset_name`
   line); or edit the first two and stage the edited configuration with `PUT` instead of that copy.
   The three files run apart and the configuration is read from a stage, which no SQL variable
   reaches, so the name cannot be one definition. Items that get no computed answer are named in
   the report (`degraded` · `detail_lost`), with the reason: their SQL is not for Snowflake or does
   not parse, reads a column the view does not carry or a `*` anywhere, has result columns that
   share a name or more than one with none, takes parameters, holds more than one statement, or
   reads a table that cannot be placed on a physical table for certain (one defined by a query, one
   the view lacks, or one in another schema).
4. **The evaluation runs** (when the file exists: the area has golden items, or the view holds
   verified queries). Run `<area>.evaluation_run.sql`. It stages the configurations written, starts
   the agent run (when the area has golden items) and the Cortex Analyst run (when the view holds
   verified queries), checks each run's status and reads its results; repeat the status call until
   it reads `COMPLETED`, then read the results again. Each section lists the privileges its run
   needs. A run name is unique per object evaluated: change `agami-run-1` for each later run.
5. **Optionally, the MCP server.** Run `<area>.mcp_server.sql` after the view and the agent, then
   grant `USAGE` on the server (and the grants its header lists) to the role a client connects
   with. A replace keeps no grants, so grant them again after re-running it.

Verified queries are checked for syntax only: the export parses each one in Snowflake's dialect.
Run each one once on the target.

A semantic view does not refuse every unsafe pairing. A query that pairs a dimension from one table
with a metric from another along a many-to-many path can return numbers that do not add up to the
total. As an illustration, with made-up numbers: if one order of 100 has lines in two product
categories, revenue by category counts it once in each, 200 across categories against a total of
100. agami's pre-query checks refuse that join, and they do not travel.

### Databricks

1. **Create the metric views.** Run each `metric_views/*.sql` in a SQL editor or notebook attached to
   a SQL warehouse. They must exist before the Genie space that lists them and before the Unity
   Catalog script certifies them. A view holding a level-of-detail measure says so in its header;
   compare that measure with agami's answer before relying on it.
2. **Deploy the Genie space.** Copy `<out>/databricks/` into a bundle project whose `databricks.yml`
   has `include: [resources/*.yml]` (keep `resources/` and the `.geniespace.json` side by side; the
   resource names the space by the relative path `../<area>.geniespace.json`). A `genie_spaces`
   resource deploys only with the direct deployment engine (see Databricks' documentation for the
   CLI version that has it), so the same `databricks.yml` needs:

   ```yaml
   bundle:
     engine: direct
   ```

   Then run `databricks bundle deploy --var genie_warehouse_id=<YOUR_WAREHOUSE_ID>`. Keep
   `databricks.yml` outside `<out>/databricks/`: each export replaces that directory whole. When the
   space has benchmarks, the resource file's header shows the calls that start a benchmark run;
   the export never calls them. See Databricks' documentation for that API's current status.
3. **Submit the Pages.** Follow `pages/<area>__manifest.json`'s `steps` in order: create each
   domain's governed tag policy and then the domain (each only when none with its `tag_key` exists),
   put each returned domain id in place of its `{domain_id__…}` placeholder in the page files,
   create each page with its `request_id`, then send each of its `related_assets` (with
   `{metastore_id}` replaced) to the asset-link call. Without `--pages-publish` every page is a
   draft only its owner sees. Certification is not a Page field and no documented API sets it: a
   curator of the domain assigns it in Discover for the pages the manifest marks `certified`. A
   re-export is sent with the `update` call and each page's `update_mask`, which leaves the domain
   alone and changes a page's state only when this export publishes it.
4. **Optionally, the Unity Catalog script, last.** Run `unity_catalog/<area>.sql` on a SQL warehouse
   (or a Databricks Runtime that supports its statements; see Databricks' documentation) once the
   metric views exist, since its certification
   section tags them; comments and keys need only the base tables. Read each section's note first:
   it says what the section overwrites and which privileges it needs. If you re-create a metric
   view later, check that its tag is still there. With several subject areas, run their files in
   name order. `unity_catalog/<area>_undo.sql` reverses the script: run only the lines whose
   statement succeeded. Comments it clears cannot be restored, its `DROP NOT NULL` lines should be
   skipped for a column that was `NOT NULL` before, and its `UNSET TAG` lines are commented out:
   enable the line of each object whose `SET TAG` succeeded. If the certification check stops the
   section, it names the objects that already carry a status: delete each one's `SET TAG` line and
   its name in the check, then run the section again. The keys section runs once; to run it again,
   run the undo file's keys section first.

A sensitive column is left out of the space file, but the space lists the whole table that holds
it. The report counts such columns for each space (`degraded` · `sensitive`, without names).
`--genie-hide-sensitive` marks them hidden in the space; that does not prevent a query that names
one. Restricting who can read a column is done with a Unity Catalog column mask.

### Ossie

Load `<area>.ossie.yaml` with any tool that reads Ossie: Ossie's own validator and Python SDK in the
`apache/ossie` repository, or Snowflake's Ossie importer,
`SYSTEM$CREATE_SEMANTIC_VIEW_FROM_OSSIE_YAML`; see Snowflake's documentation for the Ossie version
it reads and what it keeps. 0.1.1 is the default. For a semantic view built
for Cortex Analyst, with its synonyms and verified queries, apply the Snowflake target's files.
Write `--ossie-version 0.3` only for a tool that reads that development version.

## Reading the report

`<out>/export-report.json` (also printed) has:

- `source`: the datasource, the model's engine and the subject areas exported;
- `options`: the flags the run used (a target's own flags appear under `target_args` only when
  given);
- `does_not_travel`: the fixed list of what never travels (pre-query checks, receipts, audit log
  rows, who signed off an item);
- `targets.<target>`: the `files` written, `carried`, `included_unreviewed` (items
  `--include-unreviewed` added), and `losses`.

`carried` counts, by kind, the items the eligibility rules let through to the target's writer,
less each item the writer then reported `dropped`, counted once however many of the target's files
report it. On a target that writes several files from one area (Databricks writes metric views, a
Genie space and Pages), an item one file drops while another holds it is counted as not carried, so
there `carried` is a floor: the files hold at least that many. For example, with `--pages-metrics`
a metric the metric views drop is counted as not carried even though its Page (and often a Genie
measure) holds it; the report's losses name each file that dropped it.

Each loss is `{area, kind, name, outcome, reason, detail}`. `outcome` is `dropped` (left out) or
`degraded` (carried with less). `area` is empty for an item that belongs to no one area. `detail`
says what happened in a sentence. Besides model items, `kind` can name a file-level note: `agent`,
`evaluation`, `unity_catalog`, `ai_context`, `sign_off`, `subject_area`. `reason` is one of a
closed set:

| Reason | Meaning |
|---|---|
| `not_approved` | Not approved; `--include-unreviewed` carries it. Also an item carried but not marked certified or published because no review of it is recorded: one carried unreviewed, a table holding a column carried unreviewed, a key term (which has no review state), or a Snowflake view that carries key terms or ambiguities. |
| `sensitive` | A column flagged sensitive, named by an export id, never its name; or, on a Genie space, the count of sensitive columns of the tables it lists that it does not mark hidden. |
| `depends_on_excluded` | Its SQL or text names a column or table this export leaves out, or it needs something the target file does not list. |
| `no_binding_for_target` | A metric with no binding for the target's engine. Add one by hand; SQL is never translated. |
| `sql_not_for_target` | SQL written for the model's engine, which is not the target's; the item travels without it (a question without its SQL) or not at all (a default filter, an `on:` join). |
| `unparseable_for_target` | SQL that does not expand or parse in the target's dialect, or a golden dataset file or case that does not load (kind `golden`, no area, named by its file and case). `sm validate` names the problem. |
| `no_home_in_target` | The target has no place for it. |
| `carried_as_text` | Carried, but as prose (an instruction, a description, an extension block) rather than as a construct the target acts on. |
| `detail_lost` | Carried without some of its detail (a join's type, a column's value transform), or a file-level choice left out (no expected answers, no Unity Catalog file). |
| `target_rule` | Carrying it as it is would break one of the target's documented rules. |
| `target_limit` | Past one of the target's documented size or count limits. |
| `name_collision` | Its emitted name is already taken by an earlier item in the same file. |
| `spans_subject_areas` | It belongs to no one subject area, and each area exports alone. |

A metric that no engine of the target can carry is one `dropped` entry whose `detail` names each
engine. On Ossie, a metric bound for one of its two engines is carried with a `degraded` entry for
the other.

## What does not carry over, per target

These tables are taken from the losses the code reports, not from the vendors' feature lists. The
rows that apply to every target come first.

**Every target** (decided once, before any target's writer runs):

| What | Outcome · reason |
|---|---|
| Items not approved | dropped · `not_approved` |
| Columns flagged sensitive, and their `choice_field` labels | dropped · `sensitive`, `depends_on_excluded` |
| Anything whose SQL or text names a column or table left home | dropped or degraded · `depends_on_excluded` |
| A metric with no binding for the target's engine, or one that does not parse | dropped (degraded on Ossie when the other engine carries it) · `no_binding_for_target`, `unparseable_for_target` |
| Example and golden SQL for another engine | degraded to the question only · `sql_not_for_target` |
| Default filters and `on:` joins written for another engine | dropped · `sql_not_for_target` |
| A table defined by a query written for another engine | dropped · `sql_not_for_target` |
| A table whose query reads a column or table left home, cannot be parsed, or projects `*` over a table with a column left home | dropped · `depends_on_excluded` |
| A description, caveat, label, synonym or other prose field that mentions a column or table left home | degraded (the field travels blank) · `depends_on_excluded` |
| A golden item's chart type, data shape, validation notes or tags that mention a column or table left home | degraded (the field travels blank) · `depends_on_excluded` |
| Cross-area relationships, entities and metrics; golden items no one area holds | dropped · `spans_subject_areas` |
| A golden dataset file or case that does not load | dropped · `unparseable_for_target` |
| A flag for a target or artifact the run does not select | the run is refused, before anything is written |
| A golden item's `match` (other than `exact`), `bounds`, `must_filter`, `tags`, `recorded`, and `expected.chart_type`, `data_shape` and `validation_notes`, less what the target carries (Snowflake carries `bounds`) | degraded · `detail_lost`, one entry per item naming the fields (on Ossie, which drops golden items, it stands beside the item's `dropped` entry) |
| Pre-query checks, receipts, audit rows, signers | never carried (`does_not_travel`) |

**Snowflake: the semantic view**

| What | Outcome · reason |
|---|---|
| A table defined by a query for another engine | dropped · `sql_not_for_target` |
| A column's `value_transform` (the view reads the stored value) | degraded · `detail_lost` |
| A relationship's join type, description and the questions it is for | degraded · `detail_lost` |
| A table, key or join column whose stored name needs quoting, where the YAML holds it bare | degraded · `target_rule` |
| A synonym an earlier item already has | degraded · `target_rule` |
| A default filter that does not parse in Snowflake | dropped · `unparseable_for_target` |
| A relationship whose `on:` is not an equality of columns, or that joins a table or column the view lacks | dropped · `no_home_in_target` |
| A relationship that does not point at a key (many-to-many), a relationship cycle | dropped · `target_rule` |
| A self-join, carried through a second logical table on the same base table | degraded · `target_rule` |
| A metric that is an aggregate of an aggregate at an inner grain (level-of-detail expressions are not written) | dropped · `no_home_in_target` |
| A metric that does not aggregate, names what the view lacks or a table in another schema, or reads tables that do not relate and cannot be a view-level metric (each aggregate of a view-level metric reads one table: `SUM(a.x * b.y)` over two unrelated tables is none) | dropped · `target_rule` |
| A view-level (derived) metric's non-additive dimensions; a non-additive dimension the view has no one dimension for; a semi-additive rule other than Snowflake's "latest value" | degraded · `detail_lost` |
| An example whose SQL does not parse, cannot be placed on the view's logical names for certain, reads a missing table or column or a table in another schema (or `"orders"` for the view's `ORDERS`), or maps to a taken name | degraded to an agent sample question · `unparseable_for_target`, `target_rule`, `no_home_in_target`, `name_collision` |
| A sign-off time that is not an ISO 8601 time (no `verified_at`) | degraded · `detail_lost` |
| An item, table, relationship or metric whose emitted name is taken | dropped · `name_collision` |
| Ambiguities, the fiscal year start, the default time window | degraded · `carried_as_text` (instructions) |
| An entity that maps to no table in the view | dropped · `no_home_in_target` |
| A subject area with no dimension or metric left | dropped · `target_rule` |
| Review state, without `--snowflake-certification-tags` | degraded · `detail_lost` |
| With that flag, time dimensions (no `tags` field) | degraded · `detail_lost` |
| With that flag, a table tagged `TO BE REVIEWED` because it carries an unreviewed column; the view tagged so because it carries key terms or ambiguities | degraded · `not_approved` |

**Snowflake: the agent, evaluation files and MCP server**

| What | Outcome · reason |
|---|---|
| Sample questions past the agent's 100,000-byte specification limit; the agent itself when its instructions alone pass it | dropped · `target_limit` |
| The agent's warehouse, when `--snowflake-warehouse` is not given | degraded · `target_rule` |
| Golden SQL that does not parse, in the evaluation data | degraded · `unparseable_for_target` |
| A golden item whose expected tool output keeps its physical names, because the rewrite onto the view's logical names is refused, and why | degraded · `target_rule`, `no_home_in_target` |
| A golden item that gets no computed expected answer, and why (among them: it reads a column the view does not carry, or a `*`) | degraded · `detail_lost` |
| Every expected answer, with `--snowflake-no-expected-answers` (kind `evaluation`) | degraded · `detail_lost` |

**Databricks: metric views**

| What | Outcome · reason |
|---|---|
| A metric no metric view can hold: it reads tables the views cannot reach by many-to-one joins, has a semi-additive rule other than first or last, or a non-additive dimension that is no field | dropped · `target_rule` |
| A second-order metric that a level-of-detail measure would not state exactly: bound other than `OUTERAGG({base})`, an outer aggregate other than SUM, AVG, MIN, MAX, COUNT or MEDIAN, semi-additive too, a base not in the export or itself second-order or semi-additive, a base over several tables, or an inner grain that is no field of the source table | dropped · `target_rule` |
| A metric whose metric view text would hold `$$` | dropped · `target_rule` |
| A measure or field whose emitted name is taken | dropped or degraded · `name_collision` |
| A join type other than LEFT (metric views join LEFT OUTER) | degraded · `detail_lost` |
| A relationship whose `on:` names a third table; a table a view already joins by another path | degraded · `target_rule` |
| More than 10 synonyms, or one over 255 characters | degraded · `target_limit` |

**Databricks: Genie space**

| What | Outcome · reason |
|---|---|
| A table defined by SQL | dropped · `no_home_in_target` |
| A table or metric view with no catalog (pass `--location`) | dropped · `target_rule` |
| Past 50 data sources, 100 instructions, 200 snippets, 500 benchmarks, or 120 entity-matching columns | dropped or degraded · `target_limit` |
| Ambiguities, the datasource description, the fiscal year start, the default time window, key terms | degraded · `carried_as_text` (the text instruction) |
| A semi-additive measure | degraded · `carried_as_text` |
| A join's type and the questions it is for (the join spec's instruction) | degraded · `carried_as_text` |
| A second-order metric as a measure snippet (the metric view holds it) | dropped · `target_rule` |
| An example whose SQL cannot be rewritten to `catalog.schema.table` for certain (among them, SQL naming a table in another schema or catalog), or reads a table the space does not list | degraded to a sample question · `target_rule`, `depends_on_excluded` |
| A golden item without Databricks SQL, or whose SQL cannot be rewritten so | dropped · `target_rule`, `depends_on_excluded` |
| A question already asked; SQL that does not parse | dropped · `name_collision`, `unparseable_for_target` |
| A self-join; a join or filter on a table the space does not list; a join condition reading other tables; a metric reading one | dropped · `target_rule`, `depends_on_excluded` |
| A measure alias already taken | dropped · `name_collision` |
| Sensitive columns of listed tables not marked hidden in the space, without `--genie-hide-sensitive` (a count) | degraded · `sensitive` |
| A subject area with no table or metric view to list | dropped · `target_rule` |

**Databricks: Pages**

| What | Outcome · reason |
|---|---|
| Certification (a curator assigns it in Discover) | degraded · `no_home_in_target` |
| An entity's columns (an asset link names a table) and parent entity | degraded · `carried_as_text` |
| An entity's `value_pattern`, `clarification_strictness` | degraded · `detail_lost` |
| A domain name a governed tag key refuses (it is renamed by the naming rule) | degraded · `target_rule` |
| An asset link with no catalog | degraded · `target_rule` |
| A mapping filter for another engine | degraded · `sql_not_for_target` |
| A title over 200 characters; more than 50 synonyms or one over 200 characters; a domain description over 4,096 characters | dropped or degraded · `target_limit` |
| A title its domain already has | dropped · `name_collision` |
| Pages left as drafts, without `--pages-publish` | degraded · `target_rule` |
| A key-term page: a draft, never published or marked certified (no review is recorded) | degraded · `not_approved` |

**Databricks: Unity Catalog script**

| What | Outcome · reason |
|---|---|
| The whole file, when no `--uc-*` flag is given | degraded · `detail_lost` |
| A table defined by a query (no catalog object) | degraded · `no_home_in_target` |
| A key whose constraint name is taken or too long | degraded · `name_collision`, `target_limit` |
| A foreign key: joined by an SQL condition, onto no primary key the file declares, between a table and itself or a query, a second one on the same column, or over columns agami types differently | degraded · `no_home_in_target`, `target_rule` |
| A table or metric view carried unreviewed, or a table carrying a column carried unreviewed (not marked certified) | degraded · `not_approved` |
| A metric view holding a level-of-detail measure (not marked certified) | degraded · `target_rule` |
| A table another subject area carries too (its statements are in the first area's file); a foreign key on such a table | degraded · `target_rule` |

**Ossie**

| What | Outcome · reason |
|---|---|
| A table defined by a query for another engine | dropped · `sql_not_for_target` |
| A relationship joined by an SQL condition; one with an end the file leaves out | dropped · `no_home_in_target`; `depends_on_excluded` |
| A relationship's cardinality and join type | degraded · `detail_lost` |
| A metric's non-additive dimensions | degraded · `carried_as_text` |
| Example SQL (Ossie examples are questions only) | degraded · `no_home_in_target` |
| Golden items (no Ossie construct) | dropped · `no_home_in_target` |
| Entities (their mappings and key) | degraded · `detail_lost`, carried as synonyms |
| Default filters, review state and sign-off time | degraded · `carried_as_text` (the agami extension block; Ossie does not apply a filter) |
| Two items with one emitted name | dropped · `name_collision` |
| A subject area with no dataset left | dropped · `target_rule` |
| At 0.1.1, `ai_context` written as text rather than lists (one entry per file, kind `ai_context`) | degraded · `carried_as_text` |

## What the tests check, and checking the files yourself

- **Checked offline, in CI.** Every file is checked against a structural validator written from each
  vendor's documentation, and every emitted SQL expression parses with sqlglot in the target's
  dialect. Ossie files are validated against Ossie's published JSON Schema (0.1.1, vendored at its
  tag; 0.3.0.dev0 at a pinned commit). `tests/test_context_export_e2e.py` exports the bundled sample
  to all three targets in one run and checks the file set, every validator, the report's reason
  codes, that all eight sample metrics reach every target, that no sensitive column's name appears in
  any emitted byte, and that a second run is byte-identical. Each target's own tests cover the
  flags above.
- **Optionally checked on open-source Spark.** When `pyspark` (a version with metric views) and a JVM
  are installed, that test also creates one exported metric view, reduced to what open-source Spark
  reads, and computes its measures. Neither is a dependency of agami, and CI does not install them.

A vendor can be stricter than its documentation, or change it. Check each vendor's documentation
for the current status of the features the files use before you rely on them.

To check the files on your own account or workspace (agami never does this for you):

1. Load the tables the model describes into the schema you pass as `--location`, and export.
2. Apply each view with the vendor's dry-run option first: for Snowflake, add `TRUE` as the third
   argument of the call in `<area>.sql` (`verify_only`), then run the file as written; for
   Databricks, run `databricks bundle validate` before `databricks bundle deploy`.
3. Query each metric through the view (a semantic view or a metric view) and compare it with the
   same metric computed on the base tables from its binding. The two numbers should be equal.
4. Run the exported evaluation and benchmark files (the Snowflake evaluation files, the Genie
   space's benchmarks) in the order "Applying the files" gives.

Note what fails, with the vendor's error text, in an issue: that is how the validators get corrected.
