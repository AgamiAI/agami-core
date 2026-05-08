# Format specs

agami's state splits across two directories. See [`plugins/agami/shared/file-layout.md`](../plugins/agami/shared/file-layout.md) for the full design rationale; this page is the per-file format reference.

## `~/.agami/` — secrets + per-user state — **NEVER commit**

| File | Format | Owner |
|---|---|---|
| `~/.agami/credentials` | INI (chmod 600) | User-edited |
| `~/.agami/.pgpass`, `.mysql.cnf`, `.snowsql.cnf` | Provider-native auth files (chmod 600) | Skill-written by `setup_pgauth.py` |
| `~/.agami/.config` | JSON (chmod 600) — telemetry consent, install_id, active_profile, **artifacts_dir**, tool_paths | Skill-managed |
| `~/.agami/.optins` | JSON (chmod 600) | Skill-managed |
| `~/.agami/.duckdb_init_<id>.sql` | SQL (chmod 600, ephemeral) | `build_duckdb_attach.py`, deleted after the query |
| `~/.agami/.telemetry-queue.jsonl` | JSONL | Skill-managed |
| `~/.agami/query_log.jsonl` | JSONL append-only | Skill-written, never sent, personal record |
| `~/.agami/charts/<ts>.html` | Chart.js HTML | Skill-written |
| `~/.agami/exports/<ts>.csv` | RFC 4180 CSV | Skill-written |

## `<artifacts_dir>/` — sharable, can be committed (default `~/agami-artifacts/`)

| File | Format | Owner |
|---|---|---|
| `<artifacts_dir>/USER_MEMORY.md` | Free-form Markdown — cross-database preferences | Seeded by `agami-init`, edited by user or appended by `agami-save-correction` |
| `<artifacts_dir>/<profile>/index.yaml` | Agami-bespoke YAML (top-level TOC: schemas + cross-schema relationships + introspect_meta) | Skill-written, user-editable |
| `<artifacts_dir>/<profile>/<schema>/_schema.yaml` | Agami-bespoke YAML (per-schema slim TOC: tables list + within-schema relationships + multi-table metrics) | Skill-written, user-editable |
| `<artifacts_dir>/<profile>/<schema>/<table>.yaml` | **Open Semantic Interchange (OSI) v0.1.1** YAML — one dataset per file | Skill-written, user-editable |
| `<artifacts_dir>/<profile>/examples.yaml` | Agami-bespoke YAML | Skill-written (seeds) + append-only via `agami-save-correction` |
| `<artifacts_dir>/<profile>/ORGANIZATION.md` | Free-form Markdown — per-database domain context | Seeded by `agami-connect`, edited by user or appended by `agami-save-correction` |
| `~/.agami/cross_profile_relationships.yaml` | Agami-bespoke YAML — declared JOIN paths across profiles for federation. **Lives in `~/.agami/` because it spans profiles** and isn't tied to one team's repo | User-edited (optional) |

`USER_MEMORY.md` is **distinct** from Claude Code's auto-memory at `~/.claude/projects/<workspace>/memory/MEMORY.md`. The auto-memory is host-managed and project-scoped; `USER_MEMORY.md` is agami-managed, lives in the artifacts dir, and persists across hosts (CLI / Cowork / Desktop) the same way the rest of `<artifacts_dir>/` does.

`USER_MEMORY.md` covers **user preferences that apply across every database** (default time windows, display preferences, exclude rules). The per-database **`ORGANIZATION.md`** at `<artifacts_dir>/<profile>/ORGANIZATION.md` covers **domain knowledge for that specific database** (terminology, key metrics, what the data represents). See [`plugins/agami/shared/organization-context-format.md`](../plugins/agami/shared/organization-context-format.md).

`<profile>` matches the section name in `~/.agami/credentials` (default: `default`). One *directory* per profile under `<artifacts_dir>/`. The `agami-connect` skill auto-migrates v1.0 (single-file) and v1.1 (under `~/.agami/`) installs on first run after upgrade.

## Why the split

Three concrete wins (full design in [`shared/file-layout.md`](../plugins/agami/shared/file-layout.md)):

1. **Zero credential-leak risk on commit.** `~/.agami/` is gitignored by default; `<artifacts_dir>/` is the only place anything goes when teams share.
2. **Team workflows just work.** `cd ~/code/myteam/data && git add agami/` commits everyone's tuned semantic model, examples, ORGANIZATION.md, and USER_MEMORY.md preferences.
3. **Power users override per-environment.** Set `AGAMI_ARTIFACTS_DIR=/path/to/staging-models` for an experimental session.

---

## 1. Credentials INI

See [`plugins/agami/shared/credentials-format.md`](../plugins/agami/shared/credentials-format.md). `chmod 600` is enforced by the `agami-init` skill.

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

Few-shot NL→SQL pairs loaded by `query-database` (most-recent 50). Appended to by the `agami-save-correction` skill.

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
| `source` | enum | yes | `seed` (auto-generated by `connect`) or `correction` (saved by the `agami-save-correction` skill) |
| `created_at` | ISO8601 | yes | When the example was added |
| `confirmed` | bool | no | `true` if the user explicitly confirmed it |
| `confirmed_at` | ISO8601 | no | When confirmed |

**Why bespoke?** OSI doesn't model NL→SQL examples. The examples library is an agami implementation detail and intentionally not OSI.

---

## 4. User memory (free-form Markdown)

`~/.agami/USER_MEMORY.md` holds free-form preferences and policies that don't belong in the OSI semantic model — default filters, domain vocabulary, display preferences, hard avoids. Every agami skill loads this file on each invocation and applies what's in it to SQL generation, formatting, and follow-up suggestions.

Seeded by `init` on first run with section hints (HTML comments). User edits by hand, OR the `agami-save-correction` skill appends a bullet when it classifies a correction as `user_preference` ("from now on, always exclude test users where email matches @example.com").

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
  "github_star_asked": true,
  "github_star_response": "yes_opened",
  "ts": "2026-05-07T18:30:00Z"
}
```

`github_star_response` is one of `yes_opened` (user clicked through to GitHub), `maybe_later`, or `already_starred`. Existence of the file is the never-re-prompt gate — we ask exactly once, after the user's first successful query.

### `~/.agami/.telemetry-queue.jsonl`

One JSON event per line, each conforming to the allowlist in [`plugins/agami/shared/telemetry-payload.md`](../plugins/agami/shared/telemetry-payload.md). Flushed daily.

### `~/.agami/query_log.jsonl`

```jsonl
{"ts":"2026-05-07T15:14:00Z","question":"how many orders shipped in May","sql":"SELECT ...","row_count":4,"execution_ms":250,"tier":"cli","risk":"LOW","error_kind":null,"feedback":"good","chart_path":"/Users/me/.agami/charts/20260507-141500.html"}
```

Fields per line:

| Field | Type | Description |
|---|---|---|
| `ts` | ISO8601 UTC | When the query ran |
| `question` | string | The user's NL question |
| `sql` | string | The executed SQL |
| `row_count` | integer | Rows returned (post-filter, pre-truncation) |
| `execution_ms` | integer | Wall-clock latency |
| `tier` | enum | Connection method that ran the query: `cli` (native CLI), `duckdb`, `python` (Python driver). Field name is `tier` for backward-compatibility with v1.0 logs. |
| `risk` | enum | `LOW` / `MEDIUM` / `HIGH` (large-table risk classifier) |
| `error_kind` | enum or null | Set when execution failed; one of the 9 classifier kinds |
| `feedback` | enum or null | `good` / `bad` / null (set retroactively by follow-up signals) |
| `chart_path` | string or null | Absolute path of the HTML report from Phase 4e, or null if the query returned a 1×1 scalar that didn't get a report. Read by `query-database`'s reopen-intent flow (Phase 2a.1) |
| `tables_used` | array of strings | Qualified `<schema>.<table>` names the SQL FROMs/JOINs. For two-pass retrieval (Phase 2b large mode), this is what Pass 1 picked; for small mode, parsed from the SQL. |
| `retrieval_mode` | enum | `small` or `large` — which Phase 2b branch ran. Useful for tuning the 50-table threshold. |

**Local-only** — never sent. Records every query. Grep / aggregate it in your own tooling if you want personal analytics.

---

## 6. Chart artifacts (`~/.agami/charts/<ts>.html`)

Self-contained Chart.js v4 HTML, rendered from [`plugins/agami/shared/chart-template.html`](../plugins/agami/shared/chart-template.html) with placeholders substituted (`{{TITLE}}`, `{{CHART_TYPE}}`, `{{LABELS}}`, `{{DATASETS}}`, `{{GENERATED_AT}}`, `{{SQL}}`). Open in any browser.

## 7. CSV exports (`~/.agami/exports/<ts>.csv`)

Standard RFC 4180 CSV. UTF-8, no BOM.
