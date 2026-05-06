# Format specs

Specs for every file `agami` reads or writes inside `~/.agami/`.

| File | Format | Owner |
|---|---|---|
| `~/.agami/credentials` | INI (chmod 600) | User-edited |
| `~/.agami/<profile>.yaml` | **Open Semantic Interchange (OSI) v0.1.1** YAML | Skill-written, user-editable |
| `~/.agami/<profile>-examples.yaml` | Agami-bespoke YAML | Skill-written (seeds) + append-only via `/save-correction` |
| `~/.agami/USER_MEMORY.md` | Free-form Markdown | Seeded by `init`, edited by user or appended by `/save-correction` |
| `~/.agami/.config` | JSON (chmod 600) | Skill-managed |
| `~/.agami/.optins` | JSON (chmod 600) | Skill-managed |
| `~/.agami/.telemetry-queue.jsonl` | JSONL | Skill-managed |
| `~/.agami/query_log.jsonl` | JSONL append-only | Skill-written, never sent |
| `~/.agami/charts/<ts>.html` | Chart.js HTML | Skill-written |
| `~/.agami/exports/<ts>.csv` | RFC 4180 CSV | Skill-written |

`USER_MEMORY.md` is **distinct** from Claude Code's auto-memory at `~/.claude/projects/<workspace>/memory/MEMORY.md`. The auto-memory is host-managed and project-scoped; `USER_MEMORY.md` is agami-managed, lives alongside credentials, and persists across hosts (CLI / Cowork / Desktop) the same way credentials do.

`<profile>` matches the section name in `~/.agami/credentials` (default: `default`).

---

## 1. Credentials INI

See [`plugins/agami/shared/credentials-format.md`](../plugins/agami/shared/credentials-format.md). `chmod 600` is enforced by the `init` skill.

```ini
[default]
type     = postgres
host     = localhost
port     = 5432
database = mydb
user     = myuser
password = mypassword
```

---

## 2. Semantic model — OSI v0.1.1

The semantic model file is **strictly conformant to Open Semantic Interchange v0.1.1**. The OSI spec lives at [github.com/open-semantic-interchange/OSI](https://github.com/open-semantic-interchange/OSI); the JSON schema is bundled at [`plugins/agami/shared/osi-schema.json`](../plugins/agami/shared/osi-schema.json) so validation works offline.

Every write to this file is gated by the validator at [`plugins/agami/scripts/validate_semantic_model.py`](../plugins/agami/scripts/validate_semantic_model.py). **No OSI-breaking change is ever persisted.**

For the full reference: [`plugins/agami/shared/schema-reference.md`](../plugins/agami/shared/schema-reference.md).
For Agami's `custom_extensions` conventions: [`plugins/agami/shared/agami-osi-extensions.md`](../plugins/agami/shared/agami-osi-extensions.md).

### Worked example — minimal OSI-conformant model

```yaml
version: "0.1.1"

semantic_model:
  - name: shop
    description: E-commerce shop database.
    ai_context:
      instructions: "Use this for sales analytics across orders, customers, and products."
      synonyms: [shop, store, ecommerce]

    custom_extensions:
      - vendor_name: COMMON
        data: '{"agami": {"profile": "default", "db_type": "postgres", "introspect_meta": {"introspected_at": "2026-05-06T12:00:00Z", "tier": "cli", "source_db_version": "PostgreSQL 16.2"}}}'

    datasets:
      - name: customers
        source: shop.public.customers
        primary_key: [id]
        unique_keys: [[email]]
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
          - name: region
            expression: { dialects: [{ dialect: ANSI_SQL, expression: region }] }
            custom_extensions:
              - vendor_name: COMMON
                data: '{"agami": {"type": "string", "choice_field": {"NA": "North America", "EU": "Europe", "APAC": "Asia-Pacific"}}}'
          - name: created_at
            expression: { dialects: [{ dialect: ANSI_SQL, expression: created_at }] }
            dimension: { is_time: true }
            custom_extensions:
              - vendor_name: COMMON
                data: '{"agami": {"type": "timestamp"}}'

      - name: orders
        source: shop.public.orders
        primary_key: [id]
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
                data: '{"agami": {"type": "string", "choice_field": {"pending": "Pending", "shipped": "Shipped", "delivered": "Delivered"}}}'
          - name: amount
            expression: { dialects: [{ dialect: ANSI_SQL, expression: amount }] }
            custom_extensions:
              - vendor_name: COMMON
                data: '{"agami": {"type": "decimal", "unit": "dollars"}}'

    relationships:
      - name: orders_to_customers
        from: orders
        to: customers
        from_columns: [customer_id]
        to_columns: [id]

    metrics:
      - name: total_revenue
        expression:
          dialects:
            - dialect: ANSI_SQL
              expression: SUM(orders.amount)
        description: Total revenue across all orders.
        ai_context:
          synonyms: [revenue, total sales, gross]
```

### Why OSI

- **Vendor-neutral.** A model written by agami can be loaded by Snowflake, Tableau, dbt, or any other OSI-aware consumer.
- **Stable spec.** OSI v0.1.1 was finalized in January 2026 by Snowflake, Salesforce, dbt Labs, and others. We track it.
- **Type info via extensions.** OSI itself doesn't model column types — agami stores them under `custom_extensions[].vendor_name=COMMON` with a top-level `agami` key. Vanilla OSI consumers ignore the extensions; agami reads them. See [`agami-osi-extensions.md`](../plugins/agami/shared/agami-osi-extensions.md).

### Validation rules

Run by [`plugins/agami/scripts/validate_semantic_model.py`](../plugins/agami/scripts/validate_semantic_model.py) before any write:

1. **JSON Schema** (Layer 1) — entire document validates against `osi-schema.json`. Missing required fields, wrong types, unknown keys, bad enum values all fail here.
2. **Unique names** (Layer 2) — datasets unique within model; fields unique within dataset; metrics unique; relationships unique.
3. **Relationship refs** (Layer 2) — `from`/`to` match real datasets; `from_columns`/`to_columns` same length.
4. **Agami extension allowlist** (Layer 2) — every key under `custom_extensions[].vendor_name=COMMON.agami` must be documented in `agami-osi-extensions.md`. Unknown keys fail.
5. **`agami.type` value** (Layer 2) — must be one of `string | integer | decimal | timestamp | date | boolean`.
6. **Choice field shape** (Layer 2) — `choice_field` keys and values must be strings.
7. **SQL parse** (Layer 3, optional) — warning, not error. Requires `sqlglot` installed.

The validator's verdict is **binding**. Exit 0 → write. Exit 1 → don't write.

---

## 3. Examples library YAML (agami-bespoke, NOT OSI)

Few-shot NL→SQL pairs loaded by `query-database` (most-recent 50). Appended to by `/save-correction`.

```yaml
# ~/.agami/<profile>-examples.yaml

examples:
  - question: How many orders are there?
    sql: SELECT COUNT(*) AS order_count FROM orders
    source: seed
    created_at: 2026-05-06T12:00:00Z

  - question: Top 5 customers by spend
    sql: |-
      SELECT c.name, SUM(o.amount) AS spend
      FROM customers c
      JOIN orders o ON o.customer_id = c.id
      GROUP BY c.id, c.name
      ORDER BY spend DESC
      LIMIT 5
    source: correction
    created_at: 2026-05-07T18:30:00Z
    confirmed: true
    confirmed_at: 2026-05-07T18:30:00Z
```

| Field | Type | Required | Description |
|---|---|---|---|
| `question` | string | yes | NL question that triggers this example |
| `sql` | string | yes | Reference SQL |
| `source` | enum | yes | `seed` (auto-generated by `connect`) or `correction` (saved by `/save-correction`) |
| `created_at` | ISO8601 | yes | When the example was added |
| `confirmed` | bool | no | `true` if the user explicitly confirmed it |
| `confirmed_at` | ISO8601 | no | When confirmed |

**Why bespoke?** OSI doesn't model NL→SQL examples. The examples library is an agami implementation detail and intentionally not OSI.

---

## 4. User memory (free-form Markdown)

`~/.agami/USER_MEMORY.md` holds free-form preferences and policies that don't belong in the OSI semantic model — default filters, domain vocabulary, display preferences, hard avoids. Every agami skill loads this file on each invocation and applies what's in it to SQL generation, formatting, and follow-up suggestions.

Seeded by `init` on first run with section hints (HTML comments). User edits by hand, OR `/save-correction` appends a bullet when it classifies a correction as `user_preference` ("from now on, always exclude test users where email matches @example.com").

Full spec: [`plugins/agami/shared/user-memory-format.md`](../plugins/agami/shared/user-memory-format.md).

~~~markdown
# agami user memory

## Default filters
- Exclude rows where customers.email LIKE '%@example.com'
- Default time window: last 30 days unless the question specifies otherwise

## Naming and synonyms
- "active" means is_active = true AND status = 'live'
- "MRR" = SUM(price) WHERE plan_type = 'subscription'

## Display preferences
- Currency: USD with 2 decimals
- Dates: ISO format (2026-05-06), not relative ("today")

## Avoid
- Don't query the _audit schema
~~~

## 5. Internal state files

### `~/.agami/.config`

```json
{
  "schema_version": 1,
  "analytics_consent": true,
  "install_id": "f47ac10b-58cc-4372-a567-0e02b2c3d479",
  "tier": "cli",
  "host": "claude-code-cli",
  "consent_ts": "2026-05-06T12:00:00Z"
}
```

### `~/.agami/.optins`

```json
{
  "schema_version": 1,
  "email_optin": true,
  "email": "alice@example.com",
  "ts": "2026-05-07T18:30:00Z"
}
```

### `~/.agami/.telemetry-queue.jsonl`

One JSON event per line, each conforming to the allowlist in [`plugins/agami/shared/telemetry-payload.md`](../plugins/agami/shared/telemetry-payload.md). Flushed daily.

### `~/.agami/query_log.jsonl`

```jsonl
{"ts":"2026-05-06T15:14:00Z","question":"how many orders shipped in May","sql":"SELECT ...","row_count":4,"execution_ms":250,"tier":"cli","risk":"LOW","error_kind":null,"feedback":"good"}
```

**Local-only** — never sent. Records every query. Grep / aggregate it in your own tooling if you want personal analytics.

---

## 6. Chart artifacts (`~/.agami/charts/<ts>.html`)

Self-contained Chart.js v4 HTML, rendered from [`plugins/agami/shared/chart-template.html`](../plugins/agami/shared/chart-template.html) with placeholders substituted (`{{TITLE}}`, `{{CHART_TYPE}}`, `{{LABELS}}`, `{{DATASETS}}`, `{{GENERATED_AT}}`, `{{SQL}}`). Open in any browser.

## 7. CSV exports (`~/.agami/exports/<ts>.csv`)

Standard RFC 4180 CSV. UTF-8, no BOM.
