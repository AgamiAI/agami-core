# Agami's OSI `custom_extensions` Conventions

The agami semantic model is **strictly OSI v0.1.1** ([osi-schema.json](osi-schema.json)). Anything Agami needs that isn't in the OSI core spec lives under `custom_extensions` with `vendor_name: COMMON`. This file is the authoritative list of every `custom_extensions` shape agami emits or reads. Validators MUST refuse any extension shape not listed here.

A vanilla OSI consumer (Snowflake, Tableau, dbt, etc.) can ignore these extensions and still load the model. An agami-aware consumer reads them to recover type info, enum semantics, and execution hints.

## Encoding

Per OSI: every `custom_extensions[]` entry is `{vendor_name, data}` where `data` is a **JSON string** (not a YAML object). Always quote-wrap the JSON.

```yaml
custom_extensions:
  - vendor_name: COMMON
    data: '{"agami": {"type": "decimal"}}'
```

All Agami extension payloads sit under a single top-level `agami` key inside the JSON, so a consumer can find them with one `JSON.parse(data).agami` access. If multiple `COMMON` extensions exist on the same node, agami only reads the one whose payload has an `agami` key.

---

## Field-level extensions

Attached to a `dataset.fields[]` entry. Optional in the strict OSI sense — agami emits them whenever the information is available.

### `agami.type` (always emitted on introspect)

The simple type the field maps to. Used by query-database to:
- Pick chart types (numeric → bar/line, string → pie/doughnut, date → line)
- Format numbers (currency, integers, percentages)
- Apply SQL safety rules (don't `SUM` a string)

Allowed values: `string`, `integer`, `decimal`, `timestamp`, `date`, `boolean`.

```yaml
- name: amount
  expression:
    dialects:
      - dialect: ANSI_SQL
        expression: amount
  description: Order amount.
  custom_extensions:
    - vendor_name: COMMON
      data: '{"agami": {"type": "decimal"}}'
```

### `agami.choice_field` (optional)

For enum-like columns where stored values map to display labels. Helps NL→SQL translation ("show me closed-won deals" → `WHERE stage_name = 'Closed Won'`).

```yaml
- name: stage_name
  expression:
    dialects:
      - dialect: ANSI_SQL
        expression: stage_name
  custom_extensions:
    - vendor_name: COMMON
      data: '{"agami": {"type": "string", "choice_field": {"Prospecting": "Prospecting", "Closed Won": "Closed Won", "Closed Lost": "Closed Lost"}}}'
```

Shape: `choice_field: {<stored_value>: <display_label>}`. Both string. Quote numeric stored values (`"1": "Critical"`).

### `agami.unit` (optional)

Unit hint for numeric fields. Helps result rendering and downstream measure construction. Free-form string but use these when applicable: `dollars`, `cents`, `percent`, `count`, `seconds`, `bytes`, `rows`.

```yaml
custom_extensions:
  - vendor_name: COMMON
    data: '{"agami": {"type": "decimal", "unit": "cents"}}'
```

### `agami.original_type` (optional, emitted on introspect)

The DB-native type before mapping. Useful when the user wants to know "is this `numeric(10,2)` or `bigint`?". Free-form.

```yaml
custom_extensions:
  - vendor_name: COMMON
    data: '{"agami": {"type": "integer", "original_type": "bigint"}}'
```

---

## Dataset-level extensions

Attached to a `datasets[]` entry.

### `agami.performance_hints` (optional, emitted on introspect for tables > 100k rows)

Drives the risk-assessment banner in `query-database/SKILL.md` Phase 2d. The skill consults `estimated_row_count` and `recommended_filters` to flag HIGH/MEDIUM/LOW risk on each query.

```yaml
- name: orders
  source: shop.public.orders
  custom_extensions:
    - vendor_name: COMMON
      data: '{"agami": {"performance_hints": {"estimated_row_count": 50000000, "recommended_filters": [{"column": "placed_at", "reason": "partition key"}, {"column": "customer_id", "reason": "indexed FK"}], "selective_filters": ["is_active = true"], "indexes": [["placed_at"], ["customer_id", "status"]]}}}'
```

Sub-fields:
- `estimated_row_count` (integer, optional)
- `recommended_filters` (array of `{column, reason}`)
- `selective_filters` (array of free-form WHERE-clause snippets the LLM should consider)
- `indexes` (array of column-list arrays; first column is the leading column)

---

## Model-level extensions

Attached to the `semantic_model[i]` object.

### `agami.profile` (always emitted on connect)

The credential profile the model belongs to. Mirrors the section name in `~/.agami/credentials`. Used by query-database to confirm the model matches the active connection.

```yaml
custom_extensions:
  - vendor_name: COMMON
    data: '{"agami": {"profile": "default", "db_type": "postgres"}}'
```

### `agami.introspect_meta` (always emitted on connect)

When and how the model was introspected. Used by `connect reintrospect` to detect drift.

```yaml
custom_extensions:
  - vendor_name: COMMON
    data: '{"agami": {"introspect_meta": {"introspected_at": "2026-05-06T12:00:00Z", "tier": "cli", "source_db_version": "PostgreSQL 16.2"}}}'
```

---

## Relationship-level extensions

Attached to a `relationships[]` entry.

### `agami.fk_validation` (optional, emitted by connect after live join check)

Records the result of the orphan-ratio check from [`fk-validation.md`](fk-validation.md). Skill drops relationships with > 5% orphans during introspection, but a hand-added relationship that's never been validated has no `fk_validation` entry.

```yaml
- name: orders_to_customers
  from: orders
  to: customers
  from_columns: [customer_id]
  to_columns: [id]
  custom_extensions:
    - vendor_name: COMMON
      data: '{"agami": {"fk_validation": {"validated_at": "2026-05-06T12:00:00Z", "orphan_count": 0, "total_rows": 2403, "orphan_ratio": 0.0}}}'
```

---

## Reading conventions for the skills

- `query-database/SKILL.md` reads `agami.type` and `agami.choice_field` to inform SQL generation and chart rendering. If absent, fall back to inferring from the SQL expression.
- `connect/SKILL.md` is the canonical writer of every extension above.
- `save-correction/SKILL.md` may **add** new extensions (e.g., a clarified `agami.unit` after the user says "amount is in cents") but never invents an extension whose shape isn't listed here.

---

## Hard rules — what the validator enforces

The agami validator (`plugins/agami/scripts/validate_semantic_model.py`) refuses to write a model if any of the following are true:

1. The model fails the OSI JSON schema validation (any structural breach).
2. A `custom_extensions[]` entry on any node has `vendor_name: COMMON` with a JSON payload that **starts with an `agami` key** but contains a sub-key not listed in this document. Unknown extensions = breaking change.
3. The `agami.type` value is outside `{string, integer, decimal, timestamp, date, boolean}`.
4. A relationship's `from` / `to` doesn't match an existing dataset's `name`.
5. A relationship's `from_columns` / `to_columns` arrays differ in length (OSI also checks this; we surface it as a separate error for clarity).
6. Two datasets share a `name`, or two fields within one dataset share a `name`, or two metrics share a `name`, or two relationships share a `name`.

Other vendors' extensions (`SNOWFLAKE`, `DBT`, etc.) pass through untouched — the validator only inspects `vendor_name: COMMON` payloads with an `agami` top-level key.

---

## Adding a new extension key

1. Add a section to this document with its name, when it's emitted, and its full shape.
2. Add the key to the validator's `KNOWN_AGAMI_KEYS` set in `validate_semantic_model.py`.
3. Add a test in `tests/test_semantic_model_validator.py` that constructs a model using the new key and asserts the validator accepts it.
4. Update any skill that produces or consumes the new key.

If you skip step 2 or 3, your model with the new extension will be rejected by the validator. That's deliberate — it forces extensions to be reviewed and documented before they ship.
