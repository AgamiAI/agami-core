# Format specs

Specs for every file `agami` reads or writes inside `~/.agami/`. Three formats:

1. **Credentials INI** — `~/.agami/credentials`
2. **Semantic model YAML** — `~/.agami/<dbname>.yaml`
3. **Examples library YAML** — `~/.agami/<dbname>-examples.yaml`

Plus three internal state files (`.config`, `.optins`, `.telemetry-queue.jsonl`) and an append-only log (`query_log.jsonl`).

## File layout

```
~/.agami/
├── credentials                      # INI, chmod 600 (user-edited)
├── credentials.example              # INI, chmod 644 (skill-written template)
├── <dbname>.yaml                    # YAML semantic model (skill-written, user-editable)
├── <dbname>-examples.yaml           # YAML few-shot examples (skill-written, append-only via /save-correction)
├── .config                          # JSON, chmod 600 (skill-managed: tier choice + telemetry consent + install_id)
├── .optins                          # JSON, chmod 600 (skill-managed: post-install email opt-in state)
├── .telemetry-queue.jsonl           # JSONL queue of pending events (skill-managed)
├── .telemetry-last-flush            # Unix timestamp of last successful flush
├── query_log.jsonl                  # JSONL append-only log (skill-written, never sent anywhere)
├── charts/<ts>.html                 # Chart.js charts (skill-written)
└── exports/<ts>.csv                 # CSV exports (skill-written)
```

`<dbname>` matches the profile name in credentials (default: `default`).

---

## 1. Credentials INI

See [`plugins/agami/shared/credentials-format.md`](../plugins/agami/shared/credentials-format.md) for the authoritative spec. Summary:

```ini
[default]
type     = postgres            # postgres | mysql | sqlite
host     = localhost
port     = 5432
database = mydb
user     = myuser
password = mypassword
```

`chmod 600` is required (skill refuses to read otherwise).

---

## 2. Semantic model YAML

See [`plugins/agami/shared/schema-reference.md`](../plugins/agami/shared/schema-reference.md) for the full spec. Top-level shape:

```yaml
database_name: shop
database_type: PostgreSQL
description: >-
  E-commerce shop database — customers, orders, products, order items.
fiscal_year_start_month: 1            # optional
glossary: {}                          # optional
upfront_queries:                      # optional
  - How many orders shipped last month?
  - Top 10 customers by revenue?

tables:
  - table_name: customers
    schema_name: public
    label: customers
    display_name: Customers
    description: ""
    columns:
      id:           { type: integer, description: "", primary_key: true }
      email:        { type: string,  description: "" }
      name:         { type: string,  description: "" }
      region:       { type: string,  description: "" }
      created_at:   { type: timestamp, description: "" }
    entities: []
    measures: {}
    relationships: []

  - table_name: orders
    schema_name: public
    label: orders
    display_name: Orders
    description: ""
    columns:
      id:           { type: integer, description: "", primary_key: true }
      customer_id:  { type: integer, description: "", foreign_key: { table: public.customers, column: id } }
      status:       { type: string,  description: "" }
      placed_at:    { type: timestamp, description: "" }
      shipped_at:   { type: timestamp, description: "" }
    entities: []
    measures: {}
    relationships:
      - { from_column: customer_id, to_table: public.customers, to_column: id, join_type: LEFT JOIN, description: "" }

metrics: {}
```

The skill auto-generates the skeleton during `connect`. You can hand-edit it any time — descriptions, entities, and measures are the highest-leverage things to fill in (the LLM uses them as NL→SQL hints).

Validation rules:
- Every FK target table must exist in the model
- Every FK column must exist in the target table
- No two tables share a `label`
- Every table has at least one column

---

## 3. Examples library YAML

```yaml
# ~/.agami/shop-examples.yaml
# Few-shot NL→SQL examples loaded by query-database.
# New corrections are appended by /save-correction.

examples:
  - question: How many orders are there?
    sql: SELECT COUNT(*) AS order_count FROM orders
    source: seed
    created_at: 2026-05-06T12:00:00Z

  - question: Orders by status
    sql: |-
      SELECT status, COUNT(*) AS count
      FROM orders
      GROUP BY status
      ORDER BY count DESC
    source: seed
    created_at: 2026-05-06T12:00:00Z

  - question: Top 5 customers by spend
    sql: |-
      SELECT c.name, SUM(i.quantity * i.unit_price) AS spend
      FROM customers c
      JOIN orders o ON o.customer_id = c.id
      JOIN order_items i ON i.order_id = o.id
      GROUP BY c.id, c.name
      ORDER BY spend DESC
      LIMIT 5
    source: correction
    created_at: 2026-05-07T18:30:00Z
    confirmed: true
    confirmed_at: 2026-05-07T18:30:00Z
```

Fields:

| Field | Type | Required | Description |
|---|---|---|---|
| `question` | string | yes | The NL question that triggers this example |
| `sql` | string | yes | The corrected SQL |
| `source` | enum | yes | `seed` (auto-generated by `connect`) or `correction` (saved by `/save-correction`) |
| `created_at` | ISO8601 | yes | When the example was added |
| `confirmed` | bool | no | `true` if the user explicitly confirmed it (e.g., demo-query "Yes") |
| `confirmed_at` | ISO8601 | no | When confirmed |

The query-database skill loads at most 50 entries (newest `created_at` first) into each query's prompt context. Older entries stay in the file but stop being loaded — when you save a 51st correction, it pushes out the oldest seed.

---

## 4. Internal state files

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

Hand-edit if you want to flip `analytics_consent` from true to false (or vice versa).

### `~/.agami/.optins`

```json
{
  "schema_version": 1,
  "email_optin": true,
  "email": "alice@example.com",
  "ts": "2026-05-07T18:30:00Z"
}
```

Existence of this file gates the post-install email prompt — once written, the skill never re-prompts.

### `~/.agami/.telemetry-queue.jsonl`

One JSON event per line, each conforming to the allowlist in [`plugins/agami/shared/telemetry-payload.md`](../plugins/agami/shared/telemetry-payload.md). Flushed via `curl` to `analytics.agami.ai/v1/events` once per day. After a successful flush, the queue is truncated.

### `~/.agami/query_log.jsonl`

```jsonl
{"ts":"2026-05-06T15:14:00Z","question":"how many orders shipped in May","sql":"SELECT ...","row_count":4,"execution_ms":250,"tier":"cli","risk":"LOW","error_kind":null,"feedback":"good"}
{"ts":"2026-05-06T15:15:30Z","question":"top customers","sql":"SELECT ...","row_count":5,"execution_ms":310,"tier":"cli","risk":"LOW","error_kind":null,"feedback":null}
```

**Local-only** — never sent anywhere. Records every query, the SQL, the latency, the tier used, the risk class, the error kind (if any), and inferred feedback (drill-down → `good`, rephrase → `bad`).

You can grep / aggregate this file in your own pipelines if you want personal analytics. The skill doesn't touch it beyond appending.

---

## 5. Chart artifacts (`~/.agami/charts/<ts>.html`)

Self-contained HTML files with Chart.js v4 (loaded from CDN at view time). Each file is the rendered output of `plugins/agami/shared/chart-template.html` with placeholders substituted. Open in any browser.

```html
<!-- structure (abbreviated) -->
<!DOCTYPE html>
<html>
<head>
  <title>Top customers — agami</title>
  <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
</head>
<body>
  <h1>Top customers</h1>
  <canvas id="chart"></canvas>
  <script>
    const labels = ["Carol Chen", "Bob Brown", "Dave Davis"];
    const datasets = [{ label: "Spend", data: [148.95, 45.0, 39.98] }];
    const chartType = "bar";
    new Chart(document.getElementById('chart'), { type: chartType, data: { labels, datasets } });
  </script>
</body>
</html>
```

## 6. CSV exports (`~/.agami/exports/<ts>.csv`)

Standard RFC 4180 CSV. Header row first, then body. Encoding UTF-8 with no BOM.
