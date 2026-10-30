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

- **The team server supports sign-in from 2026 MCP clients.** A client may identify itself with an
  https URL instead of registering (Client ID Metadata Documents), so a connection no longer adds a
  row to `oauth_client` each time. The server fetches that public document at most once an hour per
  client: https only, to a public address it checked, with no redirects, a 5 KB cap and one 3 s
  deadline on the whole fetch, lookup included. The request carries no customer data. Such a client
  may use only the redirect URIs its document lists, and the sign-in page names it by its redirect
  host. Authorization redirects now
  carry `iss`, `resource` is checked at authorize and token (`invalid_target`), and a loopback `http`
  callback matches on any port.

## [0.9.21] — 2026-10-29

### Fixed

- **`sm validate` and the query runner work when started from inside the plugin's `lib/` folder (#539).**
  The folder held a partial copy of the model package (`semantic_model/` with only `__init__.py` and
  `units.py`). With `lib/` as the working folder, or first on the import path, Python loaded that
  copy instead of the installed package: `python -m semantic_model.cli` failed with `No module named
  semantic_model.cli`, and the query runner refused every statement with `model_unavailable`. The
  folder now ships `units.py` as `_semantic_model_units.py`, so nothing there can stand in for the
  installed package. Runs from anywhere else are unchanged.

- **`sm remove-example` names each example in full and says it keeps them (#550).** It cut each
  question to about 50 characters, so two that opened the same way couldn't be told apart; its
  `rejected` labels and the curation log now carry the whole question. The output also adds an
  `examples` list (`area`, `question`, `id` when the example has one, `status`) and a `note` saying
  they were marked rejected (kept for audit), not deleted; the existing keys are unchanged. Its
  help says the same. `sm curate --help` no longer says `--signer`/`--role` are for approve ops only:
  they are recorded in the curation log on every op, and an approve (or an edit of an approved
  join's tables, columns or cardinality) also stamps them on the item as its sign-off.

- **Sensitive-column detection reads whole words, checks sampled values and looks inside JSON
  (#516).** A new build split column names into words, so `phoneme_error_rate`, `mobile_sessions`,
  `headphones_sold`, `adobe_campaign_id` and `ip_address` are no longer flagged (they stay in the
  suspected list), while `customer_name`, `contact_name`, `first_name`, `customer_ssn`, `zip_code`,
  `pan_number` and `e_mail` now are. Every other name the old rule flagged is still flagged. The
  table's one sample (now taken for any table with text or JSON columns) also flags a column whose
  values are mostly email addresses, lists phone-number-shaped and birth-date-shaped columns as
  suspected (with a caveat on the column, shown by `sm sensitive` and the explorer's PII tab), and
  flags a JSON column whose keys hold personal data, with a caveat naming the key paths
  (`Contains personal data at: email, contact.phone`); a key that may itself be data (a person's
  name used as a key) is shown as `*`. Postgres JSON read through the CLI is recognised too. A column
  holding emails or phone numbers (digit-only ones included), or flagged or suspected as personal,
  gets no value list, whether read from the data or declared by an enum type or CHECK; a list
  with even one key that is a phone number is dropped, even when the sample is empty or unreadable. No sampled
  value is written to the model or the report. A birth date stored as a timestamp at midnight is
  recognised with or without a time zone (`Z`, `+05:30`), and digit-only values in an identifier
  column (`customerId`, `trackingCode`, `id_number`) are not read as phone numbers. `ipv4_address`/`IPv4Address` stay suspected-only. An existing
  model keeps its flags on refresh; no model field is added.

- **`metric_binding_unknown_column` resolves schema-qualified source tables (#500).** The check
  looked a metric's `source_tables` up by bare table name, so `s.orders` matched nothing and a typo'd
  column in its binding was never reported, and `sales.orders` and `archive.orders` shared one column
  set, so a column on the other schema's table hid a typo. A qualified source now finds that schema's
  table (or a schema-less definition of the name), a bare one every table of that name, and a table
  whose name embeds its schema (`name: public.orders`) is found either way. It stays a warning.

### Changed

- **A wide database's proposed subject areas follow its joins, and keep the plural (#513).** A
  single-schema database of more than 24 tables used to get one area per table-name prefix, with
  any name starting with a shorter one's prefix pulled in (`pro_x` with `products`) and names
  singularised by dropping an "s" (`supplie`, `statu`). Tables are now grouped where they join each
  other most densely (greedy modularity over the join graph), with no target number of areas, so a
  hub table that half the database joins doesn't pull everything into one area; no group passes the
  area size limit, and ties go to the tables sharing more words in their names and descriptions.
  Tables with no joins go with tables sharing their whole first name word. Area names are the
  tables' own words, and the grouping no longer depends on the order the tables arrive in. Only
  proposals change: a curated model is never regrouped, while a model whose areas are still exactly
  the engine's (a batched first build, or one built by an earlier release and never edited) is
  regrouped by `introspect --append`. A join whose two tables end up in one area is written as that
  area's join, and a refresh that would write a file the model can't load back is refused, with the
  model left as it was.

- **`sm suggest-metrics` no longer proposes a share-of-true rate for every boolean column (#530).**
  On a large onboarding these `<table>_<column>_rate` metrics were 698 of the 706 it suggested and
  about half of all metrics in the models, while the business's own definitions named one. They are
  now left out by default; the output carries one `flag_rates` summary instead (how many boolean
  columns could have one, up to 20 of them by name, and how to ask). `--flag-rates
  <table.column>[,...]` proposes the named ones (matched case-insensitively, `schema.table.column`
  when the table name is in more than one schema; a name that matches no boolean column is reported
  in `errors`, and one that matches more than one table is refused before anything is written, with
  the matches listed), and `--flag-rates all` proposes one for every flag column
  as before. `--cite "<document>"` records where a requested rate is defined: its description reads
  `Defined in: <document>.`. Requested rates go through the same dry run, `--write`, `--names` and
  `--profile` test run as every other proposal. Rate metrics already in a model are not changed; the
  explorer's Review tab can reject them in bulk. The agami-connect skill requests a rate only when the
  customer's definitions name it.

### Fixed

- **`sm add` refuses a subject area the model doesn't list, and takes `--area _cross` (#513).**
  The loader reads only the areas `datasource.yaml` lists, so a metric added under any other name
  was reported applied and validated and then never served. The error now lists the model's areas.
  `--area _cross` writes the datasource's cross-area list (`cross_subject_area_metrics.yaml`, or
  `_entities.yaml`), validated like any other add, and those items can be signed off through
  `sm curate`, where `"area": "_cross"` reads only the cross-area lists and otherwise acts as an op
  with no area does (a missing name, or one in both places, fails the batch). Re-adding an item that `datasource.yaml` holds inline replaces it there, so one name is
  never defined twice.

- **A foreign key over several columns is now one join on all of them, and a query joining on only
  some of them is flagged (#510).** Introspection wrote each column of a composite key as its own
  approved many-to-one join, and either one used alone repeats rows (981 became 2,502 in one
  database). Every dialect's foreign-key query now returns the constraint name and column position,
  and the generic `information_schema` query (Redshift, Snowflake, DuckDB, Databricks) and BigQuery's
  match the referenced column by position, which they used to pair with every column of the key.
  Where a catalog can't run the generic one (an older Redshift may lack
  `position_in_unique_constraint`), introspection falls back to the query it used before, keeps its
  single-column keys as before, and leaves out a composite key with a note rather than guess how its
  columns pair; the report says when the fallback was used. A
  key is written as one `on:` join, its cardinality read from the whole key; single-column joins are
  unchanged. Where a catalog gives no constraint name, the columns are still kept apart but left for
  review. A refresh replaces the split joins introspection approved for such a key with the one
  join and lists that in the preview; if a person signed off any of them, nothing changes for that
  key and the preview says so. On the receipt, a join written on part of a declared composite key
  makes an aggregate on either side `multiplied`, with a `fan_trap` naming the missing columns; the
  query still runs. Code that read only `from_column`/`to_column` now reads every column of an
  `on:` join too (adding reference joins no longer raises on one), and the validator warns when an
  `on:` names a column its table doesn't define. A join that is a key equality plus a fixed value
  (`... AND customers_curr.current_flag = 'Y'`) is not a composite key: its receipt and refresh are
  as before, its key column counts as a referenced column, and hiding that column now hides the
  join. BigQuery and Oracle keys are kept apart per dataset/owner, a BigQuery key into another
  dataset keeps its single-column join, and an `on:` now also parses in the engine's own grammar
  (`` `OrderId` ``). A refresh keeps a single-column FK the catalog declares beside a composite
  one, and only an `on:` join introspection signed goes back to review when a column is retyped.
  The partial-key check reads each join on its own (an aliased self-join and a `USING` join too),
  skips SEMI/ANTI joins and a join another declaration names whole, and counts only the equalities
  between the join's two tables. A column an earlier `USING` or `NATURAL` join merged counts for
  every table it merged, and one whose table can't be told leaves the aggregate `undetermined`. A
  key column or table named after a reserved word (`order`) is quoted in the `on:`, and a refresh
  matches a split join by its schema even when the schema is written in the table name
  (`crm.shipments`), and keeps such a join in its own schema's area only.
  A refresh's relabelling
  from the database's keys (#511) labels a composite join from all its columns, and a split join the
  refresh replaces is not also listed as relabelled.

### Fixed

- **Column scope checks an unqualified column that sits beside a CTE or subquery (#339).** When a
  query read a declared table and a CTE or derived table in the same `FROM`, an unqualified column was
  skipped, since the CTE might have supplied it. A column the model excludes was then returned:
  `WITH c AS (SELECT 1 AS n) SELECT secret_col FROM orders, c` ran, while the same statement without
  `c` was refused. The gate now reads what each CTE, derived table, `LATERAL` subquery, `VALUES` list
  and table function names in its projection or alias. A column one of them names passes. Otherwise it
  is refused when that scope reads a declared table, and a scope with no source supplying the name
  defers to the enclosing query as a correlated reference does, so `SELECT (SELECT secret_col) FROM
  orders` is refused too. An output with no readable name (`SELECT count(*)` with no alias) does not
  count, and the refusal says to name it with `AS` or to qualify the column with the source's alias.
  Alias column lists rename by position, a quoted output name keeps its case where the engine does,
  and a CTE inside a scalar subquery still sees the outer query. A key a `USING` join compares must
  be declared on the side of that join it comes from, and a name a `NATURAL` join compares from a
  CTE or subquery must be declared on the tables of that join. An output alias stands for itself
  only as a whole `ORDER BY` or `GROUP BY` item, or as a window-function alias in `QUALIFY` (DuckDB,
  Snowflake, BigQuery, Databricks), where engines read the alias. Anywhere else, including
  `HAVING`, expressions and `FILTER`, the engine reads the input column of that name, so `SELECT
  secret_col AS secret_col FROM orders` is refused, and so is an alias referenced in `HAVING`; that
  refusal says to write the expression out, and the SQL rules and server instructions now tell the
  model to. A parenthesised join is read like the `FROM` around it: the sources inside it name their
  columns, its `ON`, `USING` and `NATURAL` are checked, and its alias (`(...) AS w`) reaches only
  the columns its tables declare, so `w.secret_col` and `ON secret_col = ...` inside one are
  refused where they ran before.

### Added

- **A table can move to another subject area, keeping its joins, metrics and entities (#513).**
  The curate op `{"op":"move","kind":"table","name":"<table>","area":"<from>","to_area":"<to>"}`
  (`"schema"` too when two schemas share the name) moves the table and its area entry, and creates
  the target area when the model doesn't have it. A join that now spans two areas becomes a
  cross-area join, and a cross-area join whose two tables now share an area moves into it. Metrics
  and entities on that table move with it when every table they read is now in the new area; one
  that also reads a table outside it stays, and the result's `notes` names it; one left in another
  area moves too once all its tables have reached the new area by separate moves. A join that names
  its table by a bare name shared by two schemas is resolved by the area it sits in and gets the
  schema written; a move with a join on the table whose other end matches tables in two schemas is
  refused, with nothing written. A join to a table no area defines stays where it is, and `notes`
  says so. A join the target area already holds is kept once: a copy a person signed off over one
  the engine approved, and that over an unreviewed one, taking a description only the other copy
  has; `notes` says which was kept. A move is refused when two copies a person signed off disagree.
  A composite-key `on:` join moves intact beside another join between the same tables. The batch is
  validated and undone on failure (folders it created included, and no `notes` reported), logged
  with both areas, and committed when the model is its own git repository. The model explorer has a
  **Move to area** control on every table that queues the op.

- **A metric can carry `caveats` (#524).** Tables and columns could, metrics could not, so a limit
  of a metric's formula ("gives no value when the start date is missing") went into its description
  or calculation, where it reads as part of the definition. A metric now takes an optional
  `caveats` list, validated like a column's (no empty entries) and editable through `sm curate`
  and the model explorer. It is served wherever the metric is: the metric's entry in
  `get_datasource_schema`, metric search, the admin model page, the explorer's metric card and the
  review queue. The validator reads it too: a metric caveat saying a missing value counts as 0
  silences `metric_null_filled` the way the calculation saying so already did.

  **Reading a model:** a metric file that sets `caveats` is refused by a release older than this
  one, which does not know the key; files with no caveats stay readable, because the key is left
  out of a metric file when the list is empty. Upgrade every server that reads the model (including
  deploy images) before writing metric caveats.

### Added

- **A customer's data dictionary is applied as `metadata` in one command (#522).** `sm describe-file`
  stamped every description `source: ai`, so a dictionary the customer wrote could only land as their
  own text through a script that built `curate` ops, which agami-connect forbids. It now takes
  `--source metadata` and reads a CSV (`table`, `column`, `description`, optional `area` / `schema`
  header; quoted text may hold commas and newlines) or JSON (a list of those objects, or an object
  mapping `"table.column"` to a description) as well as the TSV it already read, by file extension or
  `--format`. Each row is matched to a model column, exactly first and then ignoring case, and lands
  on the model's spelling. A `SCHEMA.TABLE` in the table column is read as that schema's table when
  the model has the schema, and listed under `schema_prefix_stripped`; a schema the model doesn't
  have is reported as such. A row that can't be applied comes back under `unmatched` with a reason
  (`no_table`, `no_column`, `ambiguous_table`, `ambiguous_column`, `malformed`, `empty_description`),
  where a TSV's bad lines used to be skipped without a word; a CSV row with more fields than the
  header is `malformed`, and broken quoting fails the whole file (exit 2) rather than merging rows. A
  folder with no semantic model is refused (exit 2). A dictionary row never replaces a description a
  person wrote; those are listed under `kept_human`. A `metadata` description is taken as written
  from the customer's dictionary; it is not checked against the data (see #564). Without the flag
  the command stamps `ai` as before. agami-connect applies a dictionary file this way and reports
  the unmatched rows.

- **A column's list of allowed values is the whole list, or none (#517).** Introspection built each
  `choice_field` from the first 500 rows the engine returned, so on a table of a few thousand rows a
  status with 14 values could list 6, and the list read as complete: a question about a missing value
  was written against the list, and the reconcile probe graded a correct literal as a query defect.
  Values the catalog declares now come first, on a table of any size and with no table scan: a
  Postgres enum type, a MySQL/MariaDB `ENUM(...)` column, and a CHECK constraint that only lists one
  column's values (`col IN ('a','b')`, `col = 'a' OR col = 'b'`), read on Postgres, MySQL 8, SQL
  Server, Oracle, SQLite and DuckDB; a declared list is kept as declared, up to 200 values, integer
  columns included. Only a CHECK the engine enforces on every stored row counts (not Postgres `NOT
  VALID` or `NO INHERIT`, MySQL `NOT ENFORCED`, a disabled or untrusted SQL Server CHECK, or an
  Oracle one that is disabled or `NOVALIDATE`), a CHECK whose name another table also uses is skipped,
  and one that casts the column to a non-string type, or casts a listed number in a way that can change
  it (`CAST(1.9 AS INTEGER)`), declares nothing. Two columns whose names differ only by case (Postgres
  `status` and `"STATUS"`) get no declared list. A declared list that can't be kept (over 200
  values, two CHECKs that disagree, or a value the column's type can't hold) gives the column no list
  at all. On SQL Server and a SQLite `COLLATE NOCASE` column a string list also gets a caveat that the
  database compares values case-insensitively; on MySQL/MariaDB, case- and accent-insensitively. Postgres shows a CHECK's text only to the table's owner (the function that shows
  it to any role is refused by the recon gate), so a SELECT-only role reads enums and no CHECKs. When nothing is declared, a small table (the same size rule as before) reads the
  list with one bounded `SELECT DISTINCT` per column, at most 1,000 in a run; more than 25 values
  gives no list rather than part of one. Keys (`*_key`, `*_sk`, `*_fk`), measures and years get no
  list from the data. Values are listed exactly as stored: 'Open ' is no longer trimmed into 'Open',
  and a column whose values differ only by spacing or case gets a caveat naming them; loading a model
  no longer trims a `choice_field` key either. A refresh leaves an existing column's list alone, so
  labels a person filled in are kept; new columns get the new lists.

  **Reading a model:** a release older than this one trims `choice_field` keys as it loads them, so it
  reads a list holding both 'Open' and 'Open ' as one 'Open'.

### Fixed

- **A table is identified by its schema as well as its name (#258, #337).**
  - **Storage.** The served model stored tables keyed by subject area and bare name, so
    `sales_data.orders` and `staging.orders` in one area landed on one key and the deploy failed.
    Migration `028_model_table_identity` adds the schema to `model_table`'s key. Rows
    already stored copy across unchanged and still load, and the next deploy writes them in full.
    One area defining the same table twice is now the validator error `duplicate_table_identity`,
    which includes `public.orders` written next to `orders` with `schema: public`.
  - **`dataset_names`.** It used to strip the schema, so `public.orders` could return
    `archive.orders`. It now returns the table in the schema it names. A name declared under two
    schemas returns both tables, each keyed `schema.table`. A schema the name isn't declared in is
    answered with the spellings that do resolve.
  - **Routing map.** On `mode="index"` it publishes `schema.table` where a name is ambiguous, and
    passing that name back returns that table.
  - **Name carrying its schema.** A table defined as `name: public.orders` can now be selected as
    either `orders` or `public.orders`, and the scope gates match it.
  - **Joins.** A join in `joins_to_other_tables` whose schema the returned table can't confirm now
    stays whole, so it no longer reads `from: orders` with the schema dropped.
  - **Scope gates.**
    - A model's tables live in the connection's own database. A reference qualified with another
      database or project is refused once the credentials say which one the connection is in. When
      the credentials don't name one, the qualifier is refused. On Databricks the connection is
      always in the workspace's default catalog, so a catalog prefix is refused there; write
      `schema.table`. A Snowflake database in the credentials is compared as Snowflake reads it,
      upper-cased unless quoted, so `database: analytics` accepts `"ANALYTICS".SALES.ORDERS`.
    - A four-part name (`server.database.schema.table`) never matches a declared table.
    - `database..table` is refused, saying to write the schema: it reads the connecting user's
      default schema, which on SQL Server can be `dbo.orders` beside a declared `sales.orders`.
    - On Postgres, Snowflake and Oracle, a quoted name has to be the declared spelling exactly, so
      `"Orders"` no longer passes for `orders`, nor `"ORDERS"` for a declared `orders` on Snowflake.
      Unquoted names still match without regard to case there. Where case alone tells two declared
      tables apart, column scope reads the columns of the one the statement names.
    - A dot inside a table name is part of the name when the model gives the schema separately, so
      Postgres `public."orders.v2"` is one table.
    - On MySQL a table or database name has to match the declared case exactly, quoted or not:
      on Linux, its default, `sales.Orders` and `sales.orders` are two different tables.
    - The other engines match names without regard to case, as they resolve them.

- **A join on a unique key is labelled one-to-one, so a metric over it no longer carries a false
  fan-trap or chasm-trap finding (#511).** Introspection labelled a join from the primary keys
  alone, so a join onto a UNIQUE column, such as `passports.person_id → users.id`, came out
  many-to-one. Unique keys are now read from the catalog on PostgreSQL (unique constraints and
  unique indexes), MySQL, SQLite, SQL Server, Oracle and DuckDB, and kept on the table as
  `unique_keys`. Snowflake and Redshift declare unique keys without enforcing them, so one is kept
  only when the data bears it out; BigQuery, Databricks and Trino have none. A join is one-to-one
  when its columns are the whole of a key on both sides, and the same rule now labels a join a
  query writes that the model doesn't declare, a join to a grouped CTE, and the references
  enrichment adds. When no key settles a join but its many side turns out unique in the data, the
  label is kept and the join waits for review with a note saying so, since data that is unique
  today is not a promise. A refresh relabels each join introspection approved itself from the
  database's keys and lists it under `joins corrected from the database's constraints`; a join a
  person signed off is never changed, and a person's edit of a join's cardinality, columns, `on` or
  tables in the explorer makes the sign-off theirs (with no signer recorded, the engine's sign-off
  is cleared and the join waits for review), so a refresh leaves the edit alone. Only enabled,
  valid keys count: an invalid PostgreSQL index, a disabled SQL Server index and a disabled or
  unvalidated Oracle constraint are not read, a SQLite unique index over an expression is not
  shortened to its plain columns, and a SQLite unique index written with its own `COLLATE` is not
  trusted. Labelling reads each table's own keys, so two tables of one name in two schemas no longer
  lend each other a key, at introspection or at query time. At query time a join the model doesn't
  declare counts as one-to-one by its keys only when its `ON` is on its own table, that table is read
  once in the query, and every other join beside it is one-to-one too; otherwise its numbers stay
  `undetermined`. A refresh that can't read a table's unique keys (a failed catalog read or data
  check) keeps the stored ones and leaves that table's joins alone, with a note
  `unique keys not re-read for <table>`.

  **Reading a model:** a table that sets `unique_keys` is refused by a release older than this
  one, which does not know the key. Upgrade every server that reads the model (including deploy
  images) before re-introspecting with this release. A model with no unique keys stays readable by
  older releases. CI that validates the model with a newer release won't catch this.

### Fixed

### Fixed

- **`sm suggest-metrics` prints its proposals before writing, and its bindings run and mean what
  they say (#514).** It used to write straight into the model, and every proposal kept from it on a
  large onboarding had to be fixed by hand. It is now a dry run by default: it prints each metric's
  binding, calculation, source table and flags, and `--write` saves them (`--names` keeps only some,
  or a flagged one a person chose). `--profile <name>` test-runs every binding first, one bounded
  read per table (opt-in, as a pay-per-scan warehouse bills it): a binding that errors is never
  written, and a constant, all-zero or NULL result is flagged `degenerate` and left out unless named.
  Proposals on a table under 50 rows are flagged `low_confidence` (from the test run's own row
  count when there is one). A table defined by SQL is test-run over that SQL; when that read fails
  its proposals are flagged `not_test_run` and each proposal says whether it was test-run. The SQL itself: a mixed-case
  column is quoted where the engine folds case (PostgreSQL, Redshift, Snowflake, Oracle), and a
  column named after a word the engine reserves (`index` on SQLite, `key` on MySQL) is quoted; a pair of
  DATE columns subtracts to days on PostgreSQL and DuckDB, where `EXTRACT(EPOCH ...)` of the integer
  failed, and uses `DATE_DIFF` on BigQuery; BigQuery and Oracle timestamp pairs no longer fail; a
  flag rate leaves NULL rows out instead of counting them false, and a T-SQL `BIT` flag is compared
  rather than used as a predicate. A duration leaves out rows where the end is before the start and
  its calculation says so (compared directly on engines whose day difference rounds a reversed
  same-day pair to 0), pairs a planned start only with a planned end (and actual with actual),
  uses an audit column such as `sys_created_on` only when nothing else fits, and offers an ambiguous
  pairing flagged rather than picking one (its reason names up to three of the others). A storage type with no SQL dialect now fails clearly
  instead of being written as PostgreSQL.

- **A curate op on a join, entity or metric that doesn't name an area is resolved, or fails the batch (#538).**
  An op on a join between two subject areas was skipped unless it named one side's area, so the batch
  reported success with that change missing. A metric or entity defined across areas could not be
  curated at all, so `sm approve-queue` skipped every cross-area metric. An op with no area is now
  searched for in every area's files and in the cross-area joins, entities and metrics. Exactly one
  match is applied; an entity or metric whose declared name equals the op's name exactly wins over
  others that only share its file slug (`revenue ($)` and `revenue (%)`). No match, or more than one (two joins between the same tables), fails the whole
  batch with an error naming the op and the candidates, and nothing in the batch is written;
  `from_column` / `to_column` (or `area`) picks one. A join op whose named area doesn't hold the join
  also finds it in another area. An op that names an area behaves as before otherwise: a missing
  target is reported under `skipped` and the rest of the batch applies.

### Fixed

- **A second-order metric can sit on a base metric that spans several tables, and its table names
  are quoted for the engine (#525).** `OUTERAGG({base})` with `inner_grain` used to be refused when
  the base metric read more than one table, and the synthesized statement wrote table and column
  names bare, so a mixed-case table failed on PostgreSQL. The synthesized inner query now joins the
  table the base metric measures to each other source table by the one relationship declared
  directly between them. That table is the base's `primary_table` (matched to its source table
  with or without the schema), else the one table that declares every column the base's
  aggregates read; a `primary_table` that is not a source table, or a base whose measured columns
  span tables or can't be placed, is refused and asks for `primary_table`. It is also refused,
  with the reason, when there is no such edge, more than one, or the join would fan out the base
  aggregate's rows. A bare `inner_grain` column must be declared by exactly one of the tables; a
  `table.column` entry (`orders.order date`, `line items.id`) must name a source table and a
  column it declares. Identifiers are quoted only where the engine needs it (including words one
  engine reserves, such as `index` on SQLite or `key` on MySQL), so a plain lower-case name is
  emitted as before; a reserved lower-case name on Snowflake or Oracle is quoted in upper case
  (`"LIMIT"`), the name those engines store it under.
  Tables with the same name from different schemas in one base are refused. An `inner_grain` entry
  naming a declared column (`order date`) is that column, quoted; the base metric's own subject
  area is read first for the table's columns.

### Fixed

- **The receipt names the table behind a subquery or CTE alias, and reads a `FILTER` clause (#521).**
  `COUNT(*) FILTER (WHERE s.x > 0)` over a subquery `s` listed the reference as `s.x`, and the
  aggregate said it names no column. A column qualified by a derived table or a CTE is now followed
  through a projection that passes one column through unchanged (bare or renamed) to its table, so
  the reference reads `public.t.x`. A computed projection (`x / id AS r`) keeps the alias it was
  written with. A qualifier is resolved in the query it was written in, so an alias reused in a
  nested query no longer borrows the other query's table. An aggregate's `FILTER (WHERE …)` columns
  count as columns it reads, the same as a `CASE` inside it, so a single-table `COUNT(*) FILTER (…)`
  is resolved and one over a join can report a fan. A CTE name binds as the scope gate binds it (a
  schema-qualified table, a later sibling or a non-recursive self-reference is the table), and a
  column list (`WITH s(y, x)`, `AS s(y, x)`) renames outputs by position. A subquery's columns are
  not credited to the outer query's aliases: a `FILTER` holding a subquery, an aggregate holding
  one and an aggregate computed inside a nested SELECT report `undetermined` (a fan proven on the
  outer columns still reports `multiplied`). A body with a star keeps the written alias.
  What the scope gate refuses is unchanged, and a value read from a subquery is still not matched to
  a metric.

### Added

- **The model explorer can reject in bulk, beside every bulk approve (#531).** Each "✓ Approve all N
  proposed" and "✓ Approve N" now has a "✕ Reject all N proposed" / "✕ Reject N" next to it. It acts
  on exactly the items shown (the sub-tab, kind group, status filter and search), skipping anything
  already approved or rejected, confirms with the count and kind ("Reject 104 metrics?") and asks for
  one reason, which is required. Each queued reject op carries that `reason`, which reaches
  `curation_log.jsonl` with the op; the model itself does not store it. Like approvals, the rejects
  are queued and applied only when the feedback block is submitted. A single reject still carries no
  reason.

### Fixed

- **"Approve all" on the Metrics tab names the kind on each op (#531).** The Metrics tab's bulk
  approve queued ops with no `kind`, so the Queued tab showed "approve undefined" and the curation
  engine skipped them as "op needs kind + name". They now say `metric`, as the Review tab's do.

### Fixed

- **The interpreter resolver no longer picks a Python that can never be brought up to date (#529).**
  `connect_resolve.py` checks each candidate for pip and for the agami-core version it carries, read
  from installed metadata. A candidate with no pip and an agami-core that is missing or older than the
  plugin is skipped, with a `reason` in `candidates_scored`; a pip-less one whose library is current is
  still chosen, and `$AGAMI_PYTHON` is never skipped but gets a warning. The output says
  `chosen_because`. `sm` and `py` apply the same rule to the Pythons on PATH, so an activated
  pip-less venv no longer shadows a working one; a configured interpreter that fails it gives way to
  a PATH one that passes, and is kept (so `sm` can say what is wrong) when none does. When the library a Python runs is older than the plugin, `connect_resolve`
  (as a `library_older_than_plugin` anomaly), `py` and the scripts' library loader warn on stderr,
  naming both versions, the interpreter and the fix (`sm install`, or `AGAMI_PYTHON`). `sm` says
  "<python> has no pip" instead of a bare "couldn't install". The plugin version is read from
  `plugin.json` by one shared helper, with the cache folder's name as the fallback.

### Fixed

- **Validator: fewer false positives, entity mappings checked everywhere, collision messages name
  the table (#520).** `metric_binding_unknown_column` read the aliases of a binding's own subquery
  as missing columns: `SELECT AVG(s.r) FROM (SELECT …, SUM(pts) / SUM(maxpts) AS r …) s` warned
  about `r`. It now leaves out a column a derived table or CTE the SELECT reads projects (through
  a `SELECT *` too, when the star is over another derived table) and a select-list alias used in
  the same SELECT's HAVING or ORDER BY; a typo inside the subquery, one read through a star over a
  base table, and one an unused CTE happens to project still warn. The loader's rule
  for hiding a metric that reads an excluded or stale column is unchanged. An entity mapping to a
  table outside its own area was never checked, nor were `cross_subject_area_entities`; both now
  resolve against the whole model, with new warnings `entity_mapping_unknown_table` (no area
  defines the table) and `entity_mapping_column_not_found` (the table has no such column). They are
  warnings so a model that validated before still writes. `cross_area_entity_collision` printed
  `?.?.<table>` for a table the area only listed, and so warned when two areas mapped the same
  table; it now resolves the table across the model (on the connection the area lists it on, when
  two connections define it), compares the resolved table, and prints
  `schema.table` with each area's name (the connection too when there is more than one).

### Fixed

- **A subject area named where a table was wanted also names that area's tables like it (#567).**
  `get_datasource_schema(dataset_names=["orders"])` answered "'orders' is a subject area, not a
  table: pass it as `area`" and nothing else, so in a model whose tables carry a suffix (`orders_v`)
  the agent spent a second call finding the table. The hint now ends "or did you mean 'orders_v'?"
  with the same names in `did_you_mean`, matched against that area's own tables only, so a
  like-named table elsewhere is never guessed and never crowds the area's own out. The activity
  log's `missed` entry for a name answered with any wrong-kind hint (an area or a column) now
  carries `hinted: true`, so an empty suggestion list no longer reads as "the caller was given
  nothing".

- **The forked transport no longer relays a child's whole stderr on an `other` failure (#583).**
  Every sanitized failure is rebuilt by the parent from the exit code, because stderr is shared with
  anything a library logs during the run. `other` (exit 6) had no fixed sentence, so it fell through
  to the relay meant for the authored configuration errors (exit 2 and 3), and a library's line,
  values or paths included, reached `failure.message`. It now gets the generic sentence, or a
  SQLSTATE sentence (#561) relayed only as an exact match; a stream holding neither goes to the
  server log.

- **A datasource.yaml key this agami-core does not read is reported, not silently dropped (#575).**
  The loader reads a fixed list of top-level keys, so a model written for a newer agami-core that
  declared a field the running one did not know lost it without a word, and a deploy fed by that
  load stored the model without it. The model still loads and the field is still ignored, but `sm
  validate` lists the keys as an `unknown_datasource_key` warning and the deploy prints them, saying
  the model may need a newer agami-core. The load itself stays silent, since the executor loads the
  model in a child process whose stderr is its output.

- **On Redshift, discovery finds the tables of a read-only user (#558).** `sm discover`, and an
  introspection run without a table list, listed schemas from `information_schema.schemata`, which on
  Redshift shows only the schemas the user owns. A read-only role owns none, so discovery reported
  "no tables discovered" for a catalog it could read in full; passing `--tables` worked, which hid
  it. Redshift now lists the schemas the user has USAGE on (`pg_namespace` with
  `has_schema_privilege`), leaving out the system schemas. Other engines are unchanged.

## [0.9.20] — 2026-10-26

### Changed

- **Units can carry a scale and a rate, and a guessed currency is never applied without review
  (#515).** A `unit` now reads `<ISO code>[_<scale>][/<period>]`: `USD_millions` shows 12.5 as
  `$12.5M` (scales `thousands`, `millions`, `billions`, `lakhs`, `crores`), `USD/day` shows 120 as
  `$120.00/day`, and `USD_millions/year` combines both, in answers and charts alike. There is no new
  field: a release older than this one reads these strings as a plain label, so the model still loads
  everywhere. `suggest-units` now splits a name into whole words, so `total_minutes`, `credit_hours`,
  `points_balance` and `total_weight` are no longer taken for money, while `gross_usd`, `value_usd`
  and `fx_dollar` now are; each row gains a `suggested_unit` read from the name
  (`revenue_usd_millions` → `USD_millions`). `set-units` without `--columns` prints its plan and
  writes nothing until `--yes`, and gives a column whose name states a currency that unit, with
  `--columns` too (so a subset of the plan is written as planned). A bare
  `units` column (`reading_units`) is no longer classed additive at introspection; `units_sold` and
  the like still are, and classes already in a model are left as they are.

### Fixed

- **A model outside git keeps a reviewable history of its edits (#528).** agami commits a curation
  only when the profile folder is its own git repository, and it never creates one, so on a fresh
  artifacts folder an edit left nothing a reviewer could compare: `curation_log.jsonl` listed the ops
  with no time and no prior value, and the datasource description and glossary writes weren't logged
  at all. Every model write now logs each change with its UTC time (`at`), the model version it
  produced (`version`) and, for an edit or a review-state change, the value it replaced (`before`)
  beside the new one. The description and glossary writes are logged too, and a curate batch logs
  only the ops that took, not the ones it skipped. A new `sm history <root> [--limit N]` prints the
  log newest first, and a curate result outside git points to it. Older log lines without the new
  keys read as before. The docs no longer say onboarding runs `git init` or that a failed edit is
  reverted through git; it is restored from backups.

### Fixed

- **A column's `value_transform` and a join's `on:` parse in the datasource's own SQL dialect, and a
  text column of ISO dates gets a date format whatever it is called (#518).** Both fragments were
  parsed with sqlglot's generic grammar, so a valid Postgres transform such as
  `CASE WHEN code ~ '^[0-9]+$' THEN code::int END` failed validation. A fragment is now accepted when
  it parses in the engine's dialect or in the generic grammar, so nothing that validated before
  starts failing; a datasource whose engine can't be resolved keeps the generic parse, and the
  incremental validation cache includes the dialect. A field inside a JSON column is reached by a
  transform on the JSON column that extracts the key itself (`(profile->>'signup_date')::date` on
  Postgres, `json_extract(profile, '$.signup_date')` on SQLite); agami-connect now says so.
  Introspection set `iso8601` only on columns with a date-like name; it now also sets it on a text
  column with any name when at least 20 sampled values are present and every one is a real ISO date
  or timestamp. Such a column no longer gets a `choice_field` skeleton.

- **The model explorer counts subject areas as "subject areas", not "schemas" (#527).** The totals
  line at the top of the page and the line the renderer prints said "3 schemas" for three subject
  areas, which reads as database schemas. Labels that really are about database schemas, such as a
  cross-schema join, are unchanged.

### Fixed

- **A metric built on a rejected metric is no longer served (#571).** The loader left out a derived
  metric whose base was dropped only when a table or column was also hidden, so with just a metric
  rejected, a metric built on it stayed in the served model and validation reported "references
  unknown base metric". It now runs on every load, per area and cross-area, and repeats down a
  chain of derived metrics. Only a base that was rejected or hidden drops a metric: a base the model
  never had is a mistake in the model, and stays for the validator to report. An ambiguity naming a
  dropped metric loses that metric, as for any metric not served. The curation view is unchanged.

- **A statement the database rejected says so, by its SQLSTATE (#561).** An execution error none of
  the classifier's patterns read came back as `other` with "an unexpected error on the server", so a
  client could not tell a statement it should rewrite from a broken server, and guessed. The
  database's own text still never travels, since it can carry a value from a row. Its SQLSTATE now
  does: five characters, no values, and a class that means the same on every engine that reports
  one, so one table covers them all without a rule per database. Class `42` (the statement broke a
  rule), `0A` (unsupported feature) and `22` (a value did not fit an operation) come back as `syntax`
  with the code and "rewrite it"; `53`/`54` (too heavy) and `40` (a conflict, retry) with the code
  and what to do; `42501` as `permission`, `08` as `network` and `28` as `auth` with their usual
  sentences; any other class as the code beside the usual sentence. The
  code is read from the driver's exception (`sqlstate`, `pgcode`, or `redshift_connector`'s `C`
  field), never from its message, and an injected executor can pass it as
  `ExecutorError(sqlstate=…)`. An engine that reports no code is unchanged, and a dialect rule (#566)
  still answers first. On the forked transport a `syntax` sentence is relayed only as an exact match;
  the others travel as every `other` failure does today.

- **`sm enrich-metadata` exits 0 when no metadata preset applies (#526).** Most databases carry no
  in-database dictionary, and the command reported that as a failure (exit 1), which stopped a
  script running it. It now says no preset applies and exits 0. Naming a preset with `--preset` whose
  source tables are not in the model is still an error.

### Fixed

- **A SUM with a zero fallback on the many side no longer reads as a fan trap (#579).** The
  receipt flagged `SUM(CASE WHEN orders.status = 'shipped' THEN COALESCE(order_items.quantity, 0)
  END)` over `orders JOIN order_items` as multiplied: the `0` belongs to no table, so the check that
  decides which table the summed value comes from found none, and the `orders` column in the
  condition made the join look like it repeated the value. A `0` or `NULL` adds nothing to a sum
  however often its row repeats, so directly under a `SUM` (through `CASE`, `COALESCE`, `IF`,
  parentheses and casts) it is now left out. `COUNT`, `AVG` and arithmetic are unchanged, and
  `SUM(CASE WHEN … THEN 1 ELSE 0 END)`, which counts the joined rows, is still reported.

- **An entity is unreviewed until a person approves it (#508).** The entity model defaulted to
  `review_state: approved`, so an entity written during enrichment without the field came out
  signed off, with no signer, and was trusted at query time although no one had looked at it. The
  default is now `unreviewed`: such an entity waits in the review queue, and approving it in
  `/agami-model` or with `sm approve-queue` records who signed it off and when. The shape reference
  agami-connect follows showed `review_state: approved` in its entity example; it now shows
  `unreviewed`.

  **Existing models:** an entity file that states `review_state: approved` stays approved. Only one
  that leaves the field out now loads as unreviewed and appears in the review queue.

### Fixed

- **A join only the metadata preset assumes is not engine-approved (#509).** `sm enrich-metadata`
  adds reference joins from two sources: the database's own dictionary, and the preset's list of
  standard joins for that platform. A standard join whose values overlapped in the data was written
  confirmed and approved by the engine, although this database never declared it. Such a join is
  now written inferred and unreviewed, says it is the preset's standard join and not declared, and
  waits in the review queue. A reference the database's dictionary declares is still
  engine-approved once its values overlap, as a declared foreign key is during introspection.
  Introspection already wrote every join it inferred from names and values unreviewed. New tests
  now pin that for a database with no declared foreign keys.

## [0.9.19] — 2026-10-25

### Fixed

- **A local `get_prompt_examples` ranks and caps the library, as a served one does (#573).** It returned every
  area's examples file verbatim whatever the question (tens of thousands of characters a call on a
  large library, rejected examples included), in a different shape from a served deployment. It now
  ranks by word overlap with `query`, returns at most `top_k` (default 10) within the same character
  budget, drops rejected examples, and replies in the served shape with each example's `id`. A
  served deployment is unchanged. Measured on a large local library: about 52,000 characters a call
  became about 18,000.

- **A refresh rewrites only the files whose content changed (#472).** `sm introspect --append --tables
  <one>` changed one table and re-dumped every file in the model, with keys reordered, empty defaults
  added and lines re-wrapped, so a one-table refresh was a whole-model diff. `write_tree` now compares
  what it would write with what is on disk, as parsed data (a null value or an empty list counts as
  absent), and leaves an equal file alone, keeping its line endings and formatting. The introspection report
  says how many files were already up to date. On a real 87-file model, rewriting it unchanged now
  touches no files.
- **Renaming a column group carries the ambiguity anchors that name it (#487).** An anchor such as
  `orders.dates` used to fail validation after `dates` was renamed, rolling the whole edit back. A
  rename (a new group with exactly the old group's columns) now moves the anchor; a split or merged
  group is still left for validation to reject, naming the ambiguity.

- **A join rejected at sign-off between two subject areas is no longer served (#543).** Rejected joins
  inside one area were already dropped on load, but the cross-area loaders never applied the filter,
  so a rejected cross-area join (often the single-column piece of a composite key, which repeats
  rows) reached `sm context`, the schema reply and a deploy with no review status, and looked usable.
  Rejected cross-area entities and metrics had the same gap. The curation views still see them. A
  deployment keeps serving them until its model is redeployed.

- **A statement the engine rejects for a known dialect gap comes back with the rule that fixes it
  (#566).** The engine rules (Redshift: no `STRING_AGG`, unit-first `DATEADD`, `LISTAGG` beside a
  `DISTINCT` aggregate, boolean casts, …) reached a client only through `list_datasources`, and a client
  that already knew its datasource skipped that call and wrote the PostgreSQL the rules warn against.
  The database's error is still never relayed; the classifier now recognises the constructs a rule
  covers and answers with that rule as a `syntax` failure. Before, `function string_agg(...) does not
  exist` read as a missing column (telling the client to re-introspect) or as an unclassified failure. The forked transport relays the
  rule only as an exact match against the messages built from the rules themselves.

### Security

- **The server's CA bundle no longer trusts the compromised eMudhra (emSign) intermediates (#504).**
  Their private keys were weak enough to forge a certificate for any host, which opens the server's
  outbound HTTPS (OIDC discovery and token exchange) to a man-in-the-middle. The `server` extra now
  requires `certifi>=2026.10.19`, so every install resolves the fixed bundle rather than whichever one
  it happened to get. The image upgrades pip to 26.2.2 from a hash-pinned requirement before
  installing, because the pip bundled with `python:3.12-slim` vendors an affected certifi of its own.

## [0.9.18] — 2026-10-21

### Added

- **A missing value is not filled with 0 by default (#478).** Enrichment wrote metric bindings like
  `COALESCE(staff_count, 0)` on columns whose description says NULL means "not recorded", which gives
  a row a result it should not have and pulls averages and rates toward zero. A new
  `metric_null_filled` warning fires when a binding fills a column with a numeric constant through
  `COALESCE`, `IFNULL`, `NVL` or `ISNULL` and nothing gives a reason: neither the column's
  description and caveats saying a missing value means zero, nor the metric's calculation saying the
  definition it follows counts a missing value as 0 and naming that definition ("the KB formula counts
  a missing percentage as 0"). A bare "(null as 0)" names no source and still warns, and so does a
  statement that denies the zero ("NULL is not zero", "the definition never treats it as 0"). Source
  tables written with their schema (`s.operations`) resolve to that table only. A total over no rows (`COALESCE(SUM(x), 0)`), a fallback column and a
  text label are left alone. Every model's derived context now carries the row-keeping convention:
  a row whose value cannot be calculated stays in a list, ranking or grouping, shown empty and last,
  and counts in neither the numerator nor the denominator of an average, total or rate.
  agami-connect now says to let NULL propagate, and to tell a recorded value that is missing (left
  empty) from no related rows at all (often a real zero, decided per metric).
- **A metric about today measures to today (#482).** Enrichment sometimes bound "days since start"
  or "age" to the latest date found in the data, or to a fixed date, so the figure was reproducible.
  When the data is not loaded up to today that silently changes the answer and which rows qualify.
  A new `metric_today_not_current_date` warning fires when a metric's calculation or description
  says today, the current date, the run date or "until now", but its binding has no current date in it: it uses
  a fixed date, a `MAX` over the data in a subquery, or neither. Every engine's form of today is
  recognised (`CURRENT_DATE`, `NOW()`, `GETDATE()`, `SYSDATE`, `CURDATE()`, `today()`, SQLite's
  `'now'` as a date function's argument, not as a compared label), and a date literal beside it,
  such as a placeholder end date, is not flagged. agami-connect now says to bind `CURRENT_DATE` and
  to keep how far the data lags today out of model text, since the receipt carries it.
- **The validator warns on an average of per-row ratios of two amounts that add up (#480).** A metric
  bound as `AVG(online_sales / total_sales)` weighs a store with 100 sales the same as one with 1,000,
  so for a group it is not the share of the total and disagrees with the totals beside it. A new
  `metric_avg_of_ratio` warning fires when both sides of the ratio are `additive` columns, and
  suggests `SUM(numerator) / NULLIF(SUM(denominator), 0)`. A ratio of aggregates, a ratio computed per
  group in a subquery, a division used only as a condition (`CASE WHEN a / b > 0.5`), columns of
  unknown class, and columns whose copies in different subject areas disagree on their class are left
  alone; source tables written with their schema resolve to that table, and a metric whose calculation
  says it is an average of each row's ratio is not flagged. agami-connect now says to bind a share of
  a total as a ratio of totals — also when the definition gives the formula for one row, since a
  binding with no aggregate leaves the query to average the rows — and to mark amount columns
  `additive` so the check can see them.

- **The validator warns on figures measured on the data in model text (#479).** Enrichment wrote
  build-time counts into metric calculations and descriptions and into table, column and entity
  caveats ("covers only 57 of the 231 operations", "427 of 670 accounts have a value"). After the
  next load these are wrong, but they read as part of the definition, and sessions repeat them in
  answers. A new `measured_figure` warning, one for the whole model, names each such text and the
  phrase found: `<n> of <m>` (with a denominator of 10 or more), "covers only <n>", "<n>% of
  rows" or "<n>% null", "<n> rows", and "NULL on <n>". Thresholds, weights and unit factors are
  not flagged. agami-connect now says to state the lasting fact, not its current count.

### Changed

- **agami-connect binds a value as its label and unit say, even when the data looks implausible
  (#481).** Enrichment sometimes decided a stated meaning was wrong because the values did not look
  as expected — reading a field stored as `'0.73%'` as a 0–1 ratio because every value was below 1,
  or swapping a documented column for a neighbour whose values looked more plausible — and stated the
  guess as a rule that every answer then inherited. The skill now says to keep the stated meaning,
  record the oddity as a caveat, and ask the user which reading is right before changing a binding.

### Fixed

- **`get_prompt_examples` serves the same examples on every read (#203).** Examples were read in
  no set order, so when more of them tied on word overlap than the top-10 cap admits, which ones
  were served could change from one process to the next. Ties are now broken by example id, the
  order the store's primary-key index already returned in practice, on SQLite and Postgres alike.

- **Introspection escapes a backslash in a string literal where the engine reads it as an escape
  (#439).** MySQL, Snowflake, BigQuery and Databricks treat `\` inside a string literal as an escape
  by default, and introspection only doubled single quotes, so a schema or table name ending in a
  backslash could end the literal early. Those four now double the backslash too. The other
  engines are unchanged, since doubling it there would change the name being looked up.


## [0.9.17] — 2026-10-18

### Changed

- **A schema reply is one JSON document (#477).** `get_datasource_schema` used to append its domain
  context (the `datasource.md` narrative, the derived model summary and `USER_MEMORY.md`) as text
  after the JSON object, so a client that saved a large reply to a file and parsed it as JSON failed
  with "Extra data". The same text now rides inside, as `domain_context`. **Clients:** read
  `domain_context` instead of the text after the object; the golden-eval runner reads either shape.

- **A table reply sends joins to tables you didn't name compactly (#477).** A `dataset_names` reply
  keeps every join between the tables it names in full, and lists each join to another table under
  `joins_to_other_tables`: one entry per pair of tables, with its keys as `"named_col=other_col"`
  (or its own condition, verbatim), its cardinality and outer-join direction from the named table's side (a `LEFT` join read from
  its other end is `RIGHT`), and its schema or join type only when they differ from the usual. Those joins were the largest part of a table
  reply, and a statement almost always uses only the tables it named. Replaying a 51-call question
  run on a 48-table model: 13% fewer schema characters, with no join dropped.
- **Ambiguities are disclosed progressively (#477).** An entry can be tied to `columns`
  (`table.column`), `column_groups` (`table.group`) or `metrics` as well as `applies_to` tables.
  The full entry arrives with the reply that opens one of them; any other reply that reaches it
  carries it under `ambiguity_signposts` as `{name, when, open}`, where `open` is
  `{datasource, ambiguity_names: [name]}`. The new `ambiguity_names` argument returns the named
  entries in full; alone, the reply is just those entries, so following a signpost never depends
  on how a table, group or area would resolve. The overview no longer carries table-tied entries in full. An entry whose
  anchors are all hidden, excluded, stale or rejected is dropped rather than widened. The new fields are left out of a model's
  dump when empty, so a model that doesn't use them still loads on older servers. A model that does
  use them needs this version: older servers refuse the unknown fields, so upgrade the server before
  adding them to a model.

## [0.9.16] — 2026-10-15

### Added

- **Declared ambiguities (#473).** `datasource.yaml` can list `ambiguities`: kinds of question the
  model owner knows can mean more than one thing. Each entry has:
  - `when`, the kind of question it applies to;
  - `options`, each with a `label` and the SQL `how`, with at most one `default`;
  - `behaviour`, either `ask` (ask the user before running SQL) or `answer_and_offer` (answer with
    the default, say so, offer the others);
  - `applies_to`, the tables it concerns.

  `get_datasource_schema` returns the entries relevant to its scope, with an `ambiguities_note`
  beside them saying how to act on them. That note takes precedence over a rule or example that
  picks one option, and it still arrives when a client trims the server instructions. Validation checks the options and that every table exists,
  and a refresh keeps the list. Models without it are unchanged. Guidance, not enforcement.

## [0.9.15] — 2026-10-14

### Changed

- **A wide table's schema reply sends its column groups, not every column in full (#449).** For a
  table with column groups, a `dataset_names` reply of `get_datasource_schema` lists each group (its
  description and column names) and sends full detail only for the grain, the columns a metric in
  the reply binds, and columns with a caveat, a value transform or a sensitive flag. The new
  `column_groups` argument (`"group"` or `"table.group"`) brings whole groups in full; unknown names
  come back in `unknown_column_groups`. Every column stays named, so nothing becomes unreachable.
  In the same reply, lean joins (to tables not requested) between the same two tables are merged
  into one entry with `column_pairs`; `get_table_context` and the CLI context commands are
  unchanged. On a 150-column fact table, replies were about 15% smaller in a live check, with
  the agent picking the right groups from their descriptions alone.

- **`/agami-connect` describes the column groups it creates, on every build route (#465).** A wide
  table's groups are regrouped by meaning, but kept the engine's descriptions ("Columns named exp*
  (grouped by prefix)"), and a model built from a spec workbook skipped the regroup step entirely.
  The step now writes a one-line description per group, saying what it holds and which questions
  need it, and runs after a spec too. A group still carrying the engine's description is the sign
  that a table hasn't been regrouped.
- **Regrouping a table shown by several subject areas works.** A curate `column_groups` edit
  updated only the owning area's `expose_column_groups`; another area showing the table kept the
  old group names, so validation failed and the batch rolled back. Every area is now updated, by
  columns: it shows the new groups that hold any column it showed before. A new group that spans
  the old boundary adds its other columns, instead of the area widening to every column.

### Fixed

- **A regroup no longer rewrites another area's own copy of the table.** The curate `column_groups`
  edit from #467 updated `expose_column_groups` in every subject area that listed the table,
  including an area that defines its own copy, whose groups are independent. That could widen the
  copy to every column. Such areas are now left alone; areas that only reference the table are still
  updated.
- **Receipts flag a sensitive column whatever case the statement writes it in (#456).** With
  `orders.ssn` declared sensitive, `SELECT ORDERS.SSN FROM ORDERS` or `SELECT O.ssn FROM orders o`
  reported no sensitive column projected, because names were compared exactly. Table, alias and
  column names now match ignoring case, in the check and in the receipt's column list. Nothing is
  refused; this changes only what the receipt reports.

## [0.9.14] — 2026-10-13

### Added

- **The model build asks which date a large table is filtered on (#451).** Introspection suggests
  every date column of a large table as its `recommended_filters`, and on a warehouse many of those
  are load or audit timestamps. The build report now lists each large table with its row estimate
  and date columns, `/agami-connect` asks which date a question would mean, and the new
  `recommended_filters` curate edit changes the list without touching the row count beside it.
- **Receipts flag a large table pulled in from the outer side of a join, and a timeout points at the
  join (#451).** Each table entry on the receipt carries `scan_risk`, a sentence or null. It is set
  when the table is large (about a million rows or more, by the model's row estimate) and the
  statement reads it from the outer side of a join from tables the model sizes as small
  (`small LEFT JOIN large`, which keeps every small row and stops the large table being narrowed
  first). When a statement with an outer join hits the time limit, the refusal now describes that
  join in the statement's own table names and suggests the inner-join shape, ahead of the generic
  advice. It names no model fact, as no refusal may. Advisory only; nothing is refused for it.

### Fixed

- **Views and never-analysed tables get a row count, so large ones are flagged (#451).** The model
  sizes each table from the database's statistics, and a view has none: Redshift's `svv_table_info`
  lists only tables, and Postgres reports a view or an unanalysed table as `0` or `-1`, depending on the version. A fact
  view with millions of rows therefore never reached `large_tables` or got its date columns as
  `recommended_filters`, so nothing warned about a query scanning all of it. When the statistics
  give no usable figure, the model build now counts the rows instead, with a read-only query under
  the normal statement time limit. If the count fails or times out, the size is unknown (a
  Postgres `-1` is no longer stored as a size). MySQL and BigQuery are left out until introspection reads carry their own byte and
  server-side time limits (#459). The counts share a 10-minute budget per build; tables past it stay
  unsized and the build report says how many. Refresh a model built from views to pick the counts up.
- **Receipts find a fan-out when a column is qualified in a different case from its table (#453).**
  `SUM(ORDERS.total) FROM orders JOIN order_items …` reported the inflated total as
  `not_multiplied`, because the qualifier matched no table in the statement. The fan and chasm
  checks now match a qualifier to its table case-insensitively.
- **The "summing a rate" and "summing a balance over time" warnings no longer depend on case.** On a
  model whose names are stored in capitals, `SUM(o.rate) FROM orders o` passed without the warning,
  and so did `SUM(O.rate) FROM orders o` on any model. Tables, aliases and columns in those checks
  now match case-insensitively. The receipt's sensitive-projection fact is unchanged.
- **Fan and chasm findings name tables as the model declares them** (`ORDERS`, not `orders`),
  undoing the lowercase naming 0.9.13 introduced.
- **A connection string with no `://` is never shown as a database type.** The local listing could
  echo the whole string, password included; it now reports the type as unknown.

## [0.9.13] — 2026-10-12

### Fixed

- **`list_datasources` reports the database type the model declares (#420).** `database_type` was
  read from the connection string, which went wrong three ways: it never looked for a tenant's own
  connection setting, so on a multi-tenant server it was usually empty; a Redshift warehouse
  reached over a `postgresql://` connection read as `postgres`, steering the agent to SQL Redshift
  rejects; and a per-user warehouse login has no connection string to read. On a served deployment
  it now always matches `engine`, and is empty only when the model declares no single engine. A
  local profile still falls back to its credentials when no model declares a single engine.
- **Receipts no longer clear a fan-out written in capitals (#201).** `FROM ORDERS JOIN ORDER_ITEMS`
  against a model that says `orders` and `order_items` matched no declared join, and the receipt
  reported the inflated total as `not_multiplied`. The fan and chasm checks now compare table
  names case-insensitively on both sides, as the table scope check already did, so a model whose
  names are stored in capitals is covered too. Their findings name the tables in lowercase.
- **Receipts say when a fan-out cannot change the number (#450).** Each aggregate now carries
  `result_affected`. A `MIN`, `MAX`, boolean fold or `DISTINCT` aggregate over a join that repeats
  rows keeps `status: multiplied`, because the rows really were repeated, and now says
  `result_affected: false`, so neither the agent nor the receipt panel flags a correct number. It
  is `true` whenever a fan or chasm trap is found (a `MAX` under a chasm included), and `null`
  when undetermined, including an immune aggregate that also reads a subquery or CTE.
  `/agami-reconcile`'s part ledger now follows it, so that `null` case stays unresolved there
  instead of being confirmed.
- **A chasm-trap finding names the same shared table on every run (#202).** When two tables
  qualified, which one was named depended on Python's set order and could differ between processes.
- **The skills' own commands work on Windows (#362).** Claude follows the skill instructions to the
  letter, and three of their habits broke under Git Bash, which is what Claude Code uses on Windows:
  - **Bare `python3`,** often the Microsoft Store stub there, which runs nothing. Every plugin
    script and Python check now runs through the new `scripts/py`, which finds the interpreter
    agami-connect configured exactly as `sm` does. The two now share one resolver (`_python.sh`).
    It runs in UTF-8 mode, and the `$PY` bootstrap no longer needs a Python to find the right one.
  - **Scratch files in `/tmp`,** which Claude's file tools and Git Bash place in different
    folders on Windows. They now go in `<artifacts_dir>/local/tmp/`, git-ignored with the rest of
    `local/`, which `sm` and `py` create.
  - **The `open || xdg-open || start` chain,** which could never reach Windows' `start`. Charts,
    the model explorer and links now open through `scripts/open_in_browser`.
  - The credentials permission check is skipped on Windows, where NTFS has no Unix modes and the
    check refused every install. `tests/test_skills_cross_platform.py` keeps all three habits out.

## [0.9.12] — 2026-10-10

### Fixed

- **Refreshing a model after the database changed works, and keeps what people curated.** Asking
  agami-connect to pick up new columns failed three ways:
  - **It couldn't read the database's column list once a model existed (#428).** The reads that
    build the model went through the check that confines a query to the model's own tables, so
    every table in a second batch or a refresh came back with "no readable columns". Those reads now
    skip that check; the always-on read-only gate still applies, and seed examples and enrichment
    stay confined. That check also now finds the model in a folder chosen through the saved pointer,
    where before it found none and stayed off.
  - **Re-reading a table threw away its curation (#437).** It was rebuilt from scratch, and the
    subject areas were re-proposed. Now an existing table keeps every description, sensitivity
    flag, caveat and sign-off. A new column is added; one the database dropped is kept on disk,
    marked `stale` and no longer offered to the agent (a query naming it would fail at the
    database), and restored if the database has it again; a changed type is updated. Subject areas,
    entities, metrics and existing joins are left as they are once anyone has curated them; a new
    table joins the area named for its schema, never one already holding a table of the same name.
    A bare `--tables customers` refreshes the stored `main.customers` instead of adding it twice. A
    merge that doesn't validate isn't written, and the report says so first. `--dry-run` previews
    the change as `changed <table>: added …; dropped …; retyped …; key …`, and agami-connect shows
    that before writing. A first build in `--append` batches still ends with the same subject areas
    as a one-shot build.
  - **What a refresh does with each kind of database change.** A changed primary key moves the key
    flags, and the grain when the engine had set it (a grain a person or a model spec stated is
    kept). A changed type re-derives its date encoding, timezone and (engine-set) aggregation,
    resets a value list that no longer fits, and sends confirmed joins on that column back to
    review. A table that crosses the deep-table size gets column groups. A table the database
    dropped is kept, marked `stale`, and no longer served; it is restored if it comes back. Row
    counts are refreshed. A new table never pushes an area past 30 tables, never shares an area
    with a same-named table, and carries the model's own connection. A SQL-defined table is never
    merged with a database table of the same name; the refresh stops and says why.
  - **What the agent sees leaves out anything naming a column or table that isn't served** —
    excluded by a curator or stale after a refresh: joins, grain columns, table references, default
    filters, entity mappings and metrics on it. They stay on disk. This also closes the same gap
    for columns and tables curators excluded before.
  - **Curation never noticed a model kept inside a team's git repository (#436).** It looked for
    git only in the model's own folder, so it said `committed: false` with no reason. It still
    commits when that folder is its own repository. Inside a larger one it never commits (that
    would stage unrelated files on whatever branch is checked out), and every result now carries a
    `commit_note` saying why nothing was committed and which folder changed. A folder that
    repository ignores counts as not in git, and an exported `GIT_DIR` can't send the commit elsewhere. Rollback never depended
    on git, and agami-model's skill no longer says it did.

## [0.9.11] — 2026-10-10

### Fixed

- **On Windows, a query's result no longer arrives with a blank row after every real one.** The query
  step writes CSV, whose rows end `\r\n`; a piped stdout on Windows turned the `\n` in that into
  `\r\n` again, so the local MCP server and `sm seed-validate` read an empty row between each pair —
  a 10-row answer reported 21 rows, and a preview showed half its rows. The stream now writes UTF-8
  and leaves the rows as csv wrote them, and every caller decodes it as UTF-8 (each Python child runs with `PYTHONUTF8=1` and
  `PYTHONIOENCODING=utf-8`). The CSV itself is
  byte-for-byte unchanged on every platform (#362).
- **Windows: text is read and written as UTF-8 everywhere, and a test keeps it that way.** 54 reads
  and writes, the six credentials-file reads among them, used the ANSI code page: a template with an
  arrow stopped three dashboards rendering, and a non-ASCII password failed to authenticate.
  `tests/test_windows_text_io.py` now fails on any text read, write or child process that names no
  encoding, across the package, the plugin's scripts and the vendored `lib/`. `sm`'s CLI writes UTF-8
  too, so `sm --help` and an amount in rupees print instead of raising, and the query step's error
  text reaches its caller intact (#362).
- **`sm` finds the interpreter agami-connect configured, even when `python3` is the Microsoft Store
  stub.** It read `.config` by running `python3` — the stub, on the machines that need the configured
  one most — and then picked the stub. It now reads the path in plain shell, and never takes a
  `WindowsApps` stub from PATH (#362).
- **Windows:** the model explorer and prune pages render (`%-d` is glibc-only); a credentials
  template saved by Notepad or PowerShell with a byte-order mark is promoted as UTF-8 rather than
  crashing; a credentials file is no longer reported world-readable, which NTFS reports for every file;
  re-running agami-connect no longer fails moving onto an earlier legacy backup; and a schema named
  `con`, `aux` or another reserved device name gets a usable area folder — on every platform, so a
  model moves between machines; an existing area with such a name becomes `aux_` on the next
  introspection (#362).
- **Claude Desktop from the Microsoft Store gets its MCP entry.** `setup_desktop_mcp.py` wrote to
  `%APPDATA%\Claude`, which the Store build never reads; it now writes to the package's own folder
  when that exists, sets `PYTHONUTF8=1` for the server, prints this platform's restart steps and log
  path, says so when it repoints an entry at another profile, and leaves a config that is not UTF-8
  untouched instead of calling it invalid JSON (#362).
- **`sm seed-validate`'s preview shows a year as `2022`, not `2,022`.** It formatted each cell alone
  and skipped the label rule answers use; both now share `units.format_rows` (#362).
- **agami-connect no longer needs `TodoWrite`** to show its progress checklist (#362).

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

- **Access tokens carry and require an audience.** Every token is minted with
  `aud = <PUBLIC_BASE_URL>/mcp`, and `/mcp` answers a token without it with the usual 401 challenge.
  Access tokens issued before the upgrade stop working at once, so connected clients renew through
  their refresh token (which keeps working) or sign in again. A script that hand-mints a bearer must
  add the claim.
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

## [0.9.5] — 2026-09-19

### Fixed

- **A caller's `datasource` is bounded before it reaches the audit row (#370).** The name is
  caller-written text and arrives at `_record_execution` before anything establishes that it names a
  datasource we serve, so an arbitrarily long value was stored once per call. The statement beside
  it has always been capped; this column was missed. It now shares `LOG_DATASOURCE_MAX_CHARS` with
  the refusal log line, so the line and the row cannot disagree about what was sent. Every real name
  is far inside the limit — a value that is cut was never one.

### Internal

- **The wheel's contents are asserted (#122).** Every test runs against an editable install, where
  `migrations/` and `static/` resolve into the source tree whatever the packaging config says — so
  that config was the one part of the repo the suite could not see, and it has shipped broken once
  (a missing `migrations/` globbed to nothing and the server booted on an empty schema with no
  error). A new test builds a wheel and looks inside it; `build` is declared in the test
  dependencies so the guard cannot quietly skip.

- **The test suite no longer reads the developer's own artifacts directory (#293).** `artifacts_dir()`
  resolves a pointer at `~/.config/agami/path` and then `~/agami-artifacts`, one level earlier than
  the fixtures that isolate files inside that directory — so `organization.yaml`'s real minted
  `org_id` leaked into tests asserting `local`. 41 tests failed on a maintainer's machine and none
  in CI. Both are now redirected per test; `AGAMI_ARTIFACTS_DIR` is left alone, because a test that
  sets it is saying which directory it wants.