# Semantic Model — YAML Format Reference

This document defines the YAML format for the `~/.agami/<dbname>.yaml` semantic model written by the `connect` skill. One file per database, single-user. The format is intentionally compact — all table, entity, measure, metric, and relationship information lives inline in one file.

If you provide your own format (sample files, a spec, or a different structure like dbt / JSON Schema), the skill uses your format instead and does not reference this document.

## Contents
- File Structure
- Top-level fields
- Tables
- Columns
- Entities
- Measures (per-table)
- Metrics (cross-table)
- Relationships
- Performance hints
- Validation Rules

---

## File Structure

```
~/.agami/
├── credentials                         # connection details (chmod 600)
├── <dbname>.yaml                       # semantic model — this document's spec
├── <dbname>-examples.yaml              # NL→SQL few-shot examples (managed by skill)
├── charts/<ts>.html                    # rendered charts
├── exports/<ts>.csv                    # CSV exports
└── query_log.jsonl                     # local query log
```

`<dbname>` matches the profile name in `~/.agami/credentials` (default: `default`).

---

## Top-level fields

```yaml
database_name: salesforce
database_type: PostgreSQL
description: >-
  Salesforce CRM data: accounts, contacts, opportunities, leads, tasks, events.
  Used for donor management and pipeline tracking.
fiscal_year_start_month: 7        # optional, default 1
glossary:                         # optional
  CRM: Customer Relationship Management
  ARR: Annual Recurring Revenue

upfront_queries:                  # optional — example questions surfaced to the user
  - How many open opportunities do we have?
  - What is the total pipeline value by stage?
  - Show me the top 10 accounts by revenue.

tables:
  - <see Tables section below>

metrics:
  <see Metrics section below>
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `database_name` | string | yes | Logical name (matches credentials profile) |
| `database_type` | string | yes | `PostgreSQL`, `MySQL`, `SQLite`, etc. |
| `description` | string | yes | What this database contains — helps the LLM understand the domain |
| `fiscal_year_start_month` | int | no | 1-12, default 1 (January) |
| `glossary` | dict | no | Abbreviation/term → full form |
| `upfront_queries` | list[string] | no | Example questions for the user UI |
| `tables` | list | yes | Table definitions |
| `metrics` | dict | no | Cross-table derived metrics |

---

## Tables

Each table is one entry in the top-level `tables` list.

```yaml
tables:
  - table_name: opportunities
    schema_name: public
    label: opportunities
    display_name: Opportunities
    description: >-
      Tracks sales opportunities in the CRM pipeline. Each record represents a
      potential deal with a dollar amount, stage, and expected close date.
      Opportunities are linked to accounts via account_id.

    columns:
      id:
        type: string
        description: Unique identifier for the opportunity record.
        primary_key: true
      account_id:
        type: string
        description: Foreign key linking to the accounts table.
        foreign_key:
          table: public.accounts
          column: id
      stage_name:
        type: string
        description: Current stage in the sales pipeline.
        choice_field:
          Prospecting: Prospecting
          Qualification: Qualification
          Closed Won: Closed Won
          Closed Lost: Closed Lost
      amount:
        type: decimal
        description: Dollar value of the opportunity.
      is_won:
        type: boolean
        description: Whether the opportunity has been won.
      created_date:
        type: timestamp
        description: When the record was created.
      close_date:
        type: date
        description: Expected or actual close date.

    entities:
      - name: Opportunity
        plural: Opportunities
        description: A potential sales deal in the pipeline.
        status: active
        maps_to: [id]
        aggregated: false

    measures:
      total_opportunities:
        description: Total number of unique opportunities.
        other_names: [opportunity count, deal count]
        calculation: COUNT(DISTINCT id)
        tracked_by: Date
        units: opportunities
        aggregation: count
      total_pipeline_value:
        description: Sum of dollar amounts across all opportunities.
        other_names: [pipeline value, total deal value]
        calculation: SUM(amount)
        tracked_by: Date
        units: dollars
        currency: USD
        aggregation: sum
      win_rate:
        description: Percentage of closed opportunities that were won.
        other_names: [close rate, conversion rate]
        calculation: >-
          SUM(CASE WHEN is_won = true THEN 1 ELSE 0 END) * 100.0 /
          NULLIF(SUM(CASE WHEN stage_name IN ('Closed Won','Closed Lost') THEN 1 ELSE 0 END), 0)
        tracked_by: Date
        units: "%"
        aggregation: ratio

    relationships:
      - from_column: account_id
        to_table: public.accounts
        to_column: id
        join_type: LEFT JOIN
        description: Links each opportunity to its parent account.
```

### Table fields

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `table_name` | string | yes | Exact table name in the database |
| `schema_name` | string | yes | Exact schema name (e.g., `public`) |
| `label` | string | yes | Internal reference (usually = table_name) |
| `display_name` | string | yes | Human-readable name (Title Case) |
| `description` | string | yes | Business description (empty string OK for skeleton) |
| `columns` | dict | yes | Column definitions keyed by column name |
| `entities` | list | no | Business entity classifications (empty list OK) |
| `measures` | dict | no | Aggregation definitions (empty dict OK) |
| `relationships` | list | no | FK joins to other tables (empty list OK) |
| `performance_hints` | dict | no | See Performance hints section |

---

## Columns

Each column is keyed under `columns`:

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `type` | string | yes | `string`, `integer`, `decimal`, `timestamp`, `date`, `boolean` |
| `description` | string | yes | What this column stores |
| `primary_key` | bool | no | `true` if primary key |
| `foreign_key` | dict | no | `{table: "schema.table", column: "col"}` |
| `choice_field` | dict | no | `{stored_value: display_label}` for enum-like fields |

---

## Entities

A list of business-level concepts mapped to columns. Use entities to give the LLM richer NL → SQL hints.

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `name` | string | yes | Entity name (singular) |
| `plural` | string | yes | Plural form |
| `description` | string | yes | What this entity represents |
| `status` | string | yes | `active` or `inactive` |
| `maps_to` | list[string] | yes | Column names in this table |
| `aggregated` | bool | yes | `false` for record-level, `true` for grouping (e.g., status, category) |

---

## Measures (per-table)

Per-table aggregations expressed in SQL. Keyed by measure name.

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `description` | string | yes | What the measure calculates |
| `other_names` | list[string] | no | Aliases for NLU matching |
| `calculation` | string | yes | SQL expression (no `FROM`/`WHERE`) |
| `tracked_by` | string | no | Usually `Date` |
| `units` | string | yes | Unit label (`dollars`, `%`, `incidents`, …) |
| `currency` | string | no | `USD` for monetary measures |
| `aggregation` | string | yes | `count`, `sum`, `average`, `ratio` |
| `cuts` | dict | no | Dimensional breakdowns with allowed values |

---

## Metrics (cross-table)

Top-level `metrics` dict for derivations that span multiple tables.

```yaml
metrics:
  contacts_per_account:
    metric_type: derived
    description: Average number of contacts associated with each account.
    other_names: [contact density, avg contacts per account]
    tracked_by: Date
    calculation: COUNT(DISTINCT contacts.id) / COUNT(DISTINCT accounts.id)
    base_metrics: [total_contacts, total_accounts]
    units: contacts
    aggregation: average

  revenue_per_account:
    metric_type: derived
    description: Average opportunity revenue per account.
    tracked_by: Date
    calculation: SUM(opportunities.amount) / COUNT(DISTINCT accounts.id)
    base_metrics: [total_pipeline_value, total_accounts]
    units: dollars
    currency: USD
    aggregation: average
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `metric_type` | string | yes | Always `derived` |
| `description` | string | yes | What this metric calculates |
| `other_names` | list[string] | no | Aliases for NLU matching |
| `tracked_by` | string | no | Usually `Date` |
| `calculation` | string | yes | SQL expression with `table.column` refs |
| `base_metrics` | list[string] | yes | Per-table measures this depends on |
| `units` | string | yes | Unit label |
| `currency` | string | no | `USD` for monetary metrics |
| `aggregation` | string | yes | `count`, `sum`, `average`, `ratio` |

---

## Relationships

Inside each table's `relationships` list. The skill auto-detects FKs during introspection and validates them via live `LEFT JOIN` orphan checks (see [`fk-validation.md`](fk-validation.md)).

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `from_column` | string | yes | Column on this table |
| `to_table` | string | yes | `schema.table` reference |
| `to_column` | string | yes | Column on target table |
| `join_type` | string | yes | `LEFT JOIN`, `INNER JOIN`, `RIGHT JOIN` |
| `description` | string | no | What the join expresses |

---

## Performance hints

Optional `performance_hints` per table, populated during introspection for large tables.

```yaml
performance_hints:
  estimated_row_count: 50000000
  recommended_filters:
    - column: created_date
      reason: partition key
    - column: account_id
      reason: indexed FK
  selective_filters:
    - is_active = true
  indexes:
    - [created_date]
    - [account_id, stage_name]
```

The `query-database` skill consults these hints to flag HIGH/MEDIUM/LOW risk per query and prompt for filters when missing.

---

## Validation Rules

Enforced by the `connect` skill before writing the file:

1. Every `foreign_key.table` must reference an existing `schema.table_name` in the model
2. Every `foreign_key.column` must exist in the referenced table's columns
3. Every relationship's `to_table` must exist; `from_column`/`to_column` must exist in respective tables
4. Every entity's `maps_to` entries must be valid column names in that table
5. No two tables can have the same `label` value
6. Every table must have at least one column
7. Metric `base_metrics` should reference existing per-table measure names
8. Choice field keys must be strings (quote numeric values: `"1": Critical`)

Validation failures abort the write — the skill surfaces specific errors so you can fix them before committing the model to disk.
