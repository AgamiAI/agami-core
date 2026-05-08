# Semantic Model — OSI v0.1.1 Reference

The agami semantic model is **strictly conformant to Open Semantic Interchange (OSI) v0.1.1**. The canonical spec lives at [github.com/open-semantic-interchange/OSI](https://github.com/open-semantic-interchange/OSI). The JSON schema is bundled at [`osi-schema.json`](osi-schema.json) so the validator works offline.

This document is the reference the agami skills (connect, query-database, save-correction) read. It documents (a) the OSI v0.1.1 shape, applied to agami's single-database use case, and (b) where Agami-specific data lives — under `custom_extensions` per [`agami-osi-extensions.md`](agami-osi-extensions.md).

The validator at [`plugins/agami/scripts/validate_semantic_model.py`](../scripts/validate_semantic_model.py) is the source of truth — every model is validated before being written to disk. **No OSI-breaking change is ever persisted.**

## Contents

- [File layout](#file-layout)
- [Top-level shape](#top-level-shape)
- [Datasets](#datasets)
- [Fields](#fields)
- [Relationships](#relationships)
- [Metrics](#metrics)
- [`custom_extensions`](#custom_extensions)
- [Validation rules](#validation-rules)
- [Worked example](#worked-example)

---

## File layout

agami's state splits across two directories — secrets in `~/.agami/`, sharable artifacts in `<artifacts_dir>/` (default `~/agami-artifacts/`, configurable per [`file-layout.md`](file-layout.md)).

```
~/.agami/                                # secrets — NEVER commit
├── credentials                          # INI, chmod 600
├── .pgpass / .mysql.cnf / .snowsql.cnf
├── .config                              # JSON, chmod 600 (incl. artifacts_dir)
├── .optins, query_log.jsonl, charts/, exports/
└── ...

<artifacts_dir>/                         # sharable — can be committed to a team repo
├── USER_MEMORY.md                       # cross-database preferences
└── <profile>/
    ├── index.yaml                       # TOC + cross-schema relationships
    ├── <schema1>.yaml                   # OSI semantic model for schema1
    ├── <schema2>.yaml                   # OSI semantic model for schema2
    ├── examples.yaml                    # NL→SQL examples (agami-bespoke)
    └── ORGANIZATION.md                  # domain context for this database
```

`<profile>` matches the section name in `~/.agami/credentials` (default: `default`). One *directory* per profile, single-user. Each `<schema>.yaml` is a standalone OSI v0.1.1 document for that schema's datasets — same shape as a single-file model, just narrower.

The examples library (`examples.yaml`) is a **bespoke agami format** for few-shot prompts and is documented in [`format-spec.md`](../../../docs/format-spec.md). It is intentionally not OSI — OSI doesn't model NL→SQL examples.

The domain context (`ORGANIZATION.md`) is documented in [`organization-context-format.md`](organization-context-format.md). Free-form Markdown, loaded into every SQL-generation prompt.

### `index.yaml`

The slim TOC the `agami-query-database` skill loads first. It lists which schemas exist, where each schema's yaml lives, and any cross-schema relationships (relationships within a single schema live in that schema's yaml, not here).

```yaml
version: "0.1.1"
profile: finbud
db_type: postgres
schemas:
  - name: public
    file: public.yaml
    table_count: 12
    description: Core OLTP — accounts, transactions, sessions.
  - name: analytics
    file: analytics.yaml
    table_count: 47
    description: Aggregated analytics views, refreshed nightly.
cross_schema_relationships:
  - name: analytics_balance_to_public_accounts
    from: analytics.daily_balance        # qualified <schema>.<dataset>
    to: public.accounts                  # qualified <schema>.<dataset>
    from_columns: [account_id]
    to_columns: [id]
    description: Analytics balance rows reference the public accounts table.
introspect_meta:
  introspected_at: 2026-05-08T12:00:00Z
  tier: cli
  source_db_version: PostgreSQL 16.2
```

| Field | Required | Description |
|---|---|---|
| `version` | yes | `"0.1.1"` |
| `profile` | yes | Matches the credentials profile name |
| `db_type` | yes | `postgres` / `redshift` / `mysql` / `snowflake` / `sqlite` |
| `schemas[]` | yes | One entry per introspected schema |
| `schemas[].name` | yes | Schema name as it appears in the database |
| `schemas[].file` | yes | Filename of the schema yaml, relative to the profile dir |
| `schemas[].table_count` | no | Convenience for the two-pass retrieval cost estimate |
| `schemas[].description` | no | One-line summary of what's in the schema |
| `cross_schema_relationships[]` | no | Relationships whose `from`/`to` span schemas. Endpoints **must** be qualified `<schema>.<dataset>` |
| `introspect_meta` | no | Same allowlist as the per-schema yaml's model-level `agami.introspect_meta` |

The validator at [`validate_semantic_model.py`](../scripts/validate_semantic_model.py) gains a `--directory <profile_dir>` mode that reads `index.yaml`, validates every referenced schema yaml, and runs cross-schema checks (cross-rel endpoints resolve to real datasets, schema yamls' `agami.schema` matches their index entry).

### Per-schema yaml

Each `<schema>.yaml` is a standalone OSI v0.1.1 document. The model-level `custom_extensions` carry an additional `agami.schema` field naming which schema the file represents:

```yaml
version: "0.1.1"

semantic_model:
  - name: finbud
    description: Public schema — core OLTP tables.
    custom_extensions:
      - vendor_name: COMMON
        data: '{"agami": {"profile": "finbud", "db_type": "postgres", "schema": "public"}}'
    datasets:
      - <only public.<table> entries>
    relationships:
      - <only relationships whose from + to are both in public>
```

`agami.schema` must equal the schema's `name` in `index.yaml`. Cross-schema relationships go in `index.yaml.cross_schema_relationships[]`, not in any individual schema yaml.

---

## Top-level shape

```yaml
version: "0.1.1"

semantic_model:
  - name: shop                                # required, unique identifier
    description: E-commerce shop database.    # optional
    ai_context:                               # optional, string OR structured
      instructions: "Use this for sales analytics across orders, customers, products."
      synonyms: [shop, store]

    datasets:                                 # required, ≥ 1
      - <see Datasets section>

    relationships:                            # optional
      - <see Relationships section>

    metrics:                                  # optional
      - <see Metrics section>

    custom_extensions:                        # optional — per-model agami metadata
      - vendor_name: COMMON
        data: '{"agami": {"profile": "default", "db_type": "postgres", "introspect_meta": {"introspected_at": "2026-05-06T12:00:00Z", "tier": "cli", "source_db_version": "PostgreSQL 16.2"}}}'
```

| Field | Required | Description |
|---|---|---|
| `version` | yes | Always `"0.1.1"` (string, quoted) |
| `semantic_model` | yes | **Array** with exactly one element in agami's case (OSI permits multiple models per file; agami uses one) |
| `semantic_model[].name` | yes | Identifier (matches the credential profile name) |
| `semantic_model[].description` | no | Plain English summary the LLM uses as domain context |
| `semantic_model[].ai_context` | no | String, OR `{instructions, synonyms[], examples[]}` |
| `semantic_model[].datasets` | yes | List of datasets (≥ 1) |
| `semantic_model[].relationships` | no | List of named relationships |
| `semantic_model[].metrics` | no | List of cross-dataset metrics |
| `semantic_model[].custom_extensions` | no | Agami metadata — see [`agami-osi-extensions.md`](agami-osi-extensions.md) |

---

## Datasets

A dataset is a fact or dimension table.

```yaml
datasets:
  - name: orders                          # required, unique within model
    source: shop.public.orders            # required: database.schema.table
    primary_key: [id]                     # optional, supports composite
    unique_keys:                          # optional
      - [order_number]
      - [customer_id, placed_at]
    description: Customer orders.         # optional
    ai_context:                           # optional, string or structured
      synonyms: [orders, purchases, sales]
    fields:                               # optional, see Fields section
      - <field>
    custom_extensions:                    # optional — per-dataset agami extensions
      - vendor_name: COMMON
        data: '{"agami": {"performance_hints": {"estimated_row_count": 6, "indexes": [["customer_id"], ["placed_at"]]}}}'
```

| Field | Required | Description |
|---|---|---|
| `name` | yes | Identifier (use the source table name verbatim) |
| `source` | yes | `database.schema.table` (or a SQL view name) — the physical reference |
| `primary_key` | no | Array of column names (single or composite) |
| `unique_keys` | no | Array of column-list arrays (each can be simple or composite) |
| `description` | no | Plain English summary — high leverage for NL→SQL |
| `ai_context` | no | Synonyms, instructions, examples — the LLM reads these |
| `fields` | no | Row-level attributes (see [Fields](#fields)) |
| `custom_extensions` | no | Agami performance hints, etc. (see [agami-osi-extensions.md](agami-osi-extensions.md)) |

---

## Fields

Fields are row-level attributes — what other systems call columns. Each field is **expression-based** (OSI requires this even for plain column references). The expression supports multiple dialects.

```yaml
fields:
  - name: amount                          # required, unique within dataset
    expression:                           # required
      dialects:
        - dialect: ANSI_SQL               # required: ANSI_SQL | SNOWFLAKE | MDX | TABLEAU | DATABRICKS
          expression: amount              # required: scalar SQL expression (no aggregations)
    dimension:                            # optional
      is_time: false
    label: Amount                         # optional, free-form category label
    description: Order amount in dollars. # optional
    ai_context:
      synonyms: [order amount, total]
    custom_extensions:                    # ← Agami stores type info here per agami-osi-extensions.md
      - vendor_name: COMMON
        data: '{"agami": {"type": "decimal", "unit": "dollars"}}'
```

### Simple column reference

```yaml
- name: customer_id
  expression:
    dialects:
      - dialect: ANSI_SQL
        expression: customer_id
  custom_extensions:
    - vendor_name: COMMON
      data: '{"agami": {"type": "integer"}}'
```

### Computed field

```yaml
- name: full_name
  expression:
    dialects:
      - dialect: ANSI_SQL
        expression: first_name || ' ' || last_name
  description: Customer full name.
  custom_extensions:
    - vendor_name: COMMON
      data: '{"agami": {"type": "string"}}'
```

### Time dimension

```yaml
- name: placed_at
  expression:
    dialects:
      - dialect: ANSI_SQL
        expression: placed_at
  dimension:
    is_time: true
  custom_extensions:
    - vendor_name: COMMON
      data: '{"agami": {"type": "timestamp"}}'
```

### Multi-dialect expression

When the same logical field needs different SQL syntax per dialect:

```yaml
- name: email_normalized
  expression:
    dialects:
      - dialect: ANSI_SQL
        expression: LOWER(email)
      - dialect: SNOWFLAKE
        expression: LOWER(email)::VARCHAR
  custom_extensions:
    - vendor_name: COMMON
      data: '{"agami": {"type": "string"}}'
```

### Choice field (enum-like)

Stored values mapped to display labels via the `agami.choice_field` extension:

```yaml
- name: status
  expression:
    dialects:
      - dialect: ANSI_SQL
        expression: status
  custom_extensions:
    - vendor_name: COMMON
      data: '{"agami": {"type": "string", "choice_field": {"pending": "Pending", "shipped": "Shipped", "delivered": "Delivered", "cancelled": "Cancelled"}}}'
```

| Field | Required | Description |
|---|---|---|
| `name` | yes | Unique within the dataset |
| `expression` | yes | `{dialects: [{dialect, expression}]}` — at least one dialect entry |
| `dimension` | no | `{is_time: bool}` — flag for time-based filtering |
| `label` | no | Free-form category label |
| `description` | no | Plain English |
| `ai_context` | no | Synonyms / instructions for the LLM |
| `custom_extensions` | no | **Agami stores `type`, `choice_field`, `unit`, `original_type` here** |

---

## Relationships

Foreign-key joins between datasets. **Top-level under `semantic_model[]`** (not nested under datasets like in some other formats).

```yaml
relationships:
  - name: orders_to_customers              # required, unique within model
    from: orders                            # required: dataset name on the many side
    to: customers                           # required: dataset name on the one side
    from_columns: [customer_id]             # required: array, FK column(s) in `from`
    to_columns: [id]                        # required: array, PK/UK column(s) in `to`
    ai_context:
      synonyms: ["who placed the order"]
    custom_extensions:                      # ← Agami stores live FK validation here
      - vendor_name: COMMON
        data: '{"agami": {"fk_validation": {"validated_at": "2026-05-06T12:00:00Z", "orphan_count": 0, "total_rows": 6, "orphan_ratio": 0.0}}}'
```

Composite-key example:

```yaml
- name: order_lines_to_products
  from: order_lines
  to: products
  from_columns: [product_id, variant_id]
  to_columns: [id, variant_id]
```

| Field | Required | Description |
|---|---|---|
| `name` | yes | Unique within model |
| `from` | yes | Dataset name (must exist in `datasets[]`) |
| `to` | yes | Dataset name (must exist in `datasets[]`) |
| `from_columns` | yes | Array, ≥ 1 element, same length as `to_columns` |
| `to_columns` | yes | Array, ≥ 1 element |
| `ai_context` | no | Synonyms / instructions |
| `custom_extensions` | no | Agami's FK validation result (see [agami-osi-extensions.md](agami-osi-extensions.md)) |

---

## Metrics

Cross-dataset aggregations. Top-level under `semantic_model[]`.

```yaml
metrics:
  - name: total_revenue                    # required, unique within model
    expression:                            # required, dialect-aware
      dialects:
        - dialect: ANSI_SQL
          expression: SUM(orders.amount)
    description: Total revenue from all orders.
    ai_context:
      synonyms: [total sales, revenue, gross]
```

```yaml
- name: revenue_per_customer
  expression:
    dialects:
      - dialect: ANSI_SQL
        expression: SUM(orders.amount) / NULLIF(COUNT(DISTINCT customers.id), 0)
  description: Average revenue per customer.
```

OSI metrics span multiple datasets. **Reference fields by `dataset_name.field_name`** in the SQL expression. The expression is a complete SQL fragment (with aggregation), unlike field expressions which must be scalar.

| Field | Required | Description |
|---|---|---|
| `name` | yes | Unique within model |
| `expression` | yes | Same shape as field expression, but allows aggregations |
| `description` | no | What it measures |
| `ai_context` | no | Synonyms / examples (the LLM matches "revenue", "total sales", etc. to this metric) |
| `custom_extensions` | no | Reserved for future agami metric metadata |

---

## `custom_extensions`

OSI's exit hatch. Per-vendor JSON-string payloads. Agami uses `vendor_name: COMMON` with a top-level `agami` key inside the JSON. The full list of agami extension keys lives in [`agami-osi-extensions.md`](agami-osi-extensions.md). The validator refuses any agami extension key not listed there.

Other vendors' extensions (`SNOWFLAKE`, `DBT`, `SALESFORCE`, `DATABRICKS`) pass through untouched — agami doesn't read them but doesn't strip them either.

```yaml
custom_extensions:
  - vendor_name: COMMON
    data: '{"agami": {"type": "decimal"}}'        # agami reads this
  - vendor_name: DBT
    data: '{"materialized": "table", "tags": ["daily"]}'   # agami preserves but ignores
```

---

## Validation rules

Run by `plugins/agami/scripts/validate_semantic_model.py` before any write. **The validator's verdict is binding.** A model that fails validation is never persisted.

1. **JSON Schema** — the entire document must validate against [`osi-schema.json`](osi-schema.json). Structural breaches (missing required fields, wrong types, unknown top-level keys) abort.
2. **Unique names** — within one model: dataset names unique; field names unique within each dataset; metric names unique; relationship names unique.
3. **Relationship references** — every `relationships[].from` and `to` must match a `datasets[].name`. `from_columns` and `to_columns` must be the same length.
4. **Field references in metrics** — every `dataset_name.field_name` token in a metric expression must resolve. (Best-effort regex match — full SQL parsing is out of scope for v1.)
5. **Choice field shape** — if `agami.choice_field` is present, all keys and values must be strings. Quote numeric stored values: `"1": "Critical"`.
6. **`agami.type` value** — must be one of `string | integer | decimal | timestamp | date | boolean`.
7. **Unknown agami extension keys** — any sub-key of the `agami` JSON object not listed in [`agami-osi-extensions.md`](agami-osi-extensions.md) is rejected.

Optional (warnings, not errors): SQL expression parses cleanly via `sqlglot` if installed.

---

## Worked example

A small "shop" database — what `agami-connect/SKILL.md` writes after introspecting the integration-test fixture at `tests/integration/fixtures/postgres-init.sql`.

```yaml
version: "0.1.1"

semantic_model:
  - name: shop
    description: E-commerce shop — customers, orders, products, order items.
    ai_context:
      instructions: "Use this model for sales analytics. Most questions ask about orders, customers, or revenue."
      synonyms: [shop, store, ecommerce]

    custom_extensions:
      - vendor_name: COMMON
        data: '{"agami": {"profile": "default", "db_type": "postgres", "introspect_meta": {"introspected_at": "2026-05-06T12:00:00Z", "tier": "cli", "source_db_version": "PostgreSQL 16.2"}}}'

    datasets:
      - name: customers
        source: shop.public.customers
        primary_key: [id]
        unique_keys:
          - [email]
        description: People who buy things.
        fields:
          - name: id
            expression: { dialects: [{ dialect: ANSI_SQL, expression: id }] }
            custom_extensions:
              - vendor_name: COMMON
                data: '{"agami": {"type": "integer"}}'
          - name: email
            expression: { dialects: [{ dialect: ANSI_SQL, expression: email }] }
            custom_extensions:
              - vendor_name: COMMON
                data: '{"agami": {"type": "string"}}'
          - name: name
            expression: { dialects: [{ dialect: ANSI_SQL, expression: name }] }
            custom_extensions:
              - vendor_name: COMMON
                data: '{"agami": {"type": "string"}}'
          - name: region
            expression: { dialects: [{ dialect: ANSI_SQL, expression: region }] }
            custom_extensions:
              - vendor_name: COMMON
                data: '{"agami": {"type": "string", "choice_field": {"NA": "North America", "EU": "Europe", "APAC": "Asia-Pacific"}}}'
          - name: is_active
            expression: { dialects: [{ dialect: ANSI_SQL, expression: is_active }] }
            custom_extensions:
              - vendor_name: COMMON
                data: '{"agami": {"type": "boolean"}}'
          - name: created_at
            expression: { dialects: [{ dialect: ANSI_SQL, expression: created_at }] }
            dimension: { is_time: true }
            custom_extensions:
              - vendor_name: COMMON
                data: '{"agami": {"type": "timestamp"}}'

      - name: orders
        source: shop.public.orders
        primary_key: [id]
        description: Customer orders.
        fields:
          - name: id
            expression: { dialects: [{ dialect: ANSI_SQL, expression: id }] }
            custom_extensions:
              - vendor_name: COMMON
                data: '{"agami": {"type": "integer"}}'
          - name: customer_id
            expression: { dialects: [{ dialect: ANSI_SQL, expression: customer_id }] }
            custom_extensions:
              - vendor_name: COMMON
                data: '{"agami": {"type": "integer"}}'
          - name: status
            expression: { dialects: [{ dialect: ANSI_SQL, expression: status }] }
            custom_extensions:
              - vendor_name: COMMON
                data: '{"agami": {"type": "string", "choice_field": {"pending": "Pending", "shipped": "Shipped", "delivered": "Delivered", "cancelled": "Cancelled"}}}'
          - name: placed_at
            expression: { dialects: [{ dialect: ANSI_SQL, expression: placed_at }] }
            dimension: { is_time: true }
            custom_extensions:
              - vendor_name: COMMON
                data: '{"agami": {"type": "timestamp"}}'
          - name: shipped_at
            expression: { dialects: [{ dialect: ANSI_SQL, expression: shipped_at }] }
            dimension: { is_time: true }
            custom_extensions:
              - vendor_name: COMMON
                data: '{"agami": {"type": "timestamp"}}'

      - name: products
        source: shop.public.products
        primary_key: [id]
        unique_keys:
          - [sku]
        fields:
          - name: id
            expression: { dialects: [{ dialect: ANSI_SQL, expression: id }] }
            custom_extensions:
              - vendor_name: COMMON
                data: '{"agami": {"type": "integer"}}'
          - name: sku
            expression: { dialects: [{ dialect: ANSI_SQL, expression: sku }] }
            custom_extensions:
              - vendor_name: COMMON
                data: '{"agami": {"type": "string"}}'
          - name: name
            expression: { dialects: [{ dialect: ANSI_SQL, expression: name }] }
            custom_extensions:
              - vendor_name: COMMON
                data: '{"agami": {"type": "string"}}'
          - name: category
            expression: { dialects: [{ dialect: ANSI_SQL, expression: category }] }
            custom_extensions:
              - vendor_name: COMMON
                data: '{"agami": {"type": "string"}}'
          - name: unit_price
            expression: { dialects: [{ dialect: ANSI_SQL, expression: unit_price }] }
            custom_extensions:
              - vendor_name: COMMON
                data: '{"agami": {"type": "decimal", "unit": "dollars"}}'
          - name: is_active
            expression: { dialects: [{ dialect: ANSI_SQL, expression: is_active }] }
            custom_extensions:
              - vendor_name: COMMON
                data: '{"agami": {"type": "boolean"}}'

      - name: order_items
        source: shop.public.order_items
        primary_key: [id]
        fields:
          - name: id
            expression: { dialects: [{ dialect: ANSI_SQL, expression: id }] }
            custom_extensions:
              - vendor_name: COMMON
                data: '{"agami": {"type": "integer"}}'
          - name: order_id
            expression: { dialects: [{ dialect: ANSI_SQL, expression: order_id }] }
            custom_extensions:
              - vendor_name: COMMON
                data: '{"agami": {"type": "integer"}}'
          - name: product_id
            expression: { dialects: [{ dialect: ANSI_SQL, expression: product_id }] }
            custom_extensions:
              - vendor_name: COMMON
                data: '{"agami": {"type": "integer"}}'
          - name: quantity
            expression: { dialects: [{ dialect: ANSI_SQL, expression: quantity }] }
            custom_extensions:
              - vendor_name: COMMON
                data: '{"agami": {"type": "integer"}}'
          - name: unit_price
            expression: { dialects: [{ dialect: ANSI_SQL, expression: unit_price }] }
            custom_extensions:
              - vendor_name: COMMON
                data: '{"agami": {"type": "decimal", "unit": "dollars"}}'

    relationships:
      - name: orders_to_customers
        from: orders
        to: customers
        from_columns: [customer_id]
        to_columns: [id]

      - name: order_items_to_orders
        from: order_items
        to: orders
        from_columns: [order_id]
        to_columns: [id]

      - name: order_items_to_products
        from: order_items
        to: products
        from_columns: [product_id]
        to_columns: [id]

    metrics:
      - name: total_revenue
        expression:
          dialects:
            - dialect: ANSI_SQL
              expression: SUM(order_items.quantity * order_items.unit_price)
        description: Total revenue across all order items.
        ai_context:
          synonyms: [revenue, total sales, gross]

      - name: total_customers
        expression:
          dialects:
            - dialect: ANSI_SQL
              expression: COUNT(DISTINCT customers.id)
        description: Distinct customer count.
```

This validates clean against `osi-schema.json` and the agami extension rules.

---

## Migration note

Versions of agami before 1.0 used a bespoke `tables` / `columns` / `entities` / `measures` schema. As of v1.0 (this release) the format is OSI-only. Existing pre-OSI models are not auto-migrated — say "reload the schema" (or "re-introspect my database") and the agami-connect skill regenerates in the new format. The semantic content (descriptions, choice fields, hand-edits) is recovered from the source DB and the user's prior `<profile>-examples.yaml` corrections.
