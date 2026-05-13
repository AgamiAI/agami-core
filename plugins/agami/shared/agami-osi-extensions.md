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

### Escaping quotes inside the JSON-in-YAML payload (HARD RULE)

The `data:` value is a YAML-single-quoted string that contains JSON. Two quoting systems are stacked — the YAML quotes wrap the JSON, the JSON quotes wrap individual keys and string values. **YAML single-quoted strings do NOT support backslash escapes.** The LLM keeps reaching for `\'` to embed a literal single quote (a C / Python / JSON instinct); YAML treats that as a literal backslash followed by an end-quote, and parsing fails.

**Real failure mode reported in production**: a `recommended_filters[].reason` field of `"WHERE test_load = 'true' or use WHERE NOT test_load"` got emitted as:

```yaml
# BROKEN — YAML parse error at line 677
data: '{"agami": {"performance_hints": {"recommended_filters": [
  {"column": "test_load", "reason": "WHERE test_load = \'true\' or use WHERE NOT test_load"}
]}}}'
```

The `\'` sequence is invalid in a YAML single-quoted context. Three valid ways to handle this:

**Option A — avoid embedded single quotes inside the JSON.** Easiest. JSON allows single quotes inside a JSON-string-literal *without* any escaping (only `"` and `\` need JSON-escaping). And inside a YAML single-quoted string, a literal single quote is escaped by **doubling it**: `''`. So write the reason as a JSON string that uses no extra quoting, and double the single quotes for YAML:

```yaml
# OK — single quotes inside the JSON string are doubled for YAML
data: '{"agami": {"performance_hints": {"recommended_filters": [
  {"column": "test_load", "reason": "WHERE test_load = ''true'' or use WHERE NOT test_load"}
]}}}'
```

**Option B — rephrase the prose to avoid quotes entirely.** Cleanest for `reason` / `description` / `definition_prose` fields where you control the wording:

```yaml
data: '{"agami": {"performance_hints": {"recommended_filters": [
  {"column": "test_load", "reason": "filter to test_load=TRUE or use WHERE NOT test_load"}
]}}}'
```

**Option C — use a YAML block scalar instead of a single-quoted string.** Works but loses inline readability:

```yaml
data: |-
  {"agami": {"performance_hints": {"recommended_filters": [
    {"column": "test_load", "reason": "WHERE test_load = 'true' or use WHERE NOT test_load"}
  ]}}}
```

**Never do this** (the failure mode):

```yaml
data: '{... "reason": "WHERE test_load = \'true\' ..."}'  # YAML parser fails
```

The LLM running agami-connect / agami-save-correction MUST follow Option A or B. If a string would contain `\'`, rephrase or double the inner quotes.

---

## Trust-layer extensions (universal — applies to fields, datasets, relationships, metrics)

These keys are emitted by `agami-connect` for every entry it produces, and updated by `agami-review` when a curator approves / rejects / edits an entry. They are the spine of the trust layer documented in [`/Users/ashwinramachandran/.claude/plans/we-have-the-claude-generic-stallman.md`](../../../../.claude/plans/we-have-the-claude-generic-stallman.md).

### `agami.confidence` (always emitted on introspect)

Number in `[0.0, 1.0]`. How sure the system is about this entry. Computed by `plugins/agami/scripts/compute_confidence.py` from the signal mix in `signal_breakdown`. See that script for the per-entity-type formulas.

### `agami.signal_breakdown` (always emitted on introspect)

Object whose keys are signal names and whose values are booleans or numbers. Records *which* signals contributed to the confidence score so a curator can audit the reasoning, not just the result. Free-form keys allowed; the script that produced the entry decides which signals are relevant.

Common signal keys (not exhaustive):
- `fk_declared` (boolean) — FK metadata in the source DB
- `dba_column_comment` (boolean) — column comment present in `pg_description` / `INFORMATION_SCHEMA`
- `unique_index_match` (boolean) — target column has a unique index
- `column_type_match` (boolean) — source and target column types match exactly
- `column_name_similarity` (number 0–1) — Jaccard similarity between source and target column names
- `pk_overlap` (boolean) — both endpoints are primary keys
- `plural_pattern_match` (boolean) — `<table>.<col>` matches `<plural-of-table>.<col>` pattern
- `well_known_measure_pattern` (boolean) — column name matches a known measure regex
- `numeric_type` (boolean) — column type is numeric
- `aggregate_friendly_distribution` (boolean) — sample distribution looks like a measure
- `synonym_match` (boolean) — name matches an already-approved entry's synonyms
- `enum_like_distribution` (boolean) — distinct values look enum-like
- `llm_inferred` (boolean) — entry came from LLM-only inference with no other signal

### `agami.review_state` (always emitted)

Enum: one of:
- `unreviewed` — the default after introspect for entries that don't auto-approve
- `approved` — a human (or, for auto-approve cases, the introspect process) has signed off
- `rejected` — explicitly excluded; runtime model loader skips it
- `stale` — was approved but a schema-drift event has invalidated it; runtime refuses dependent queries until reconciled
- `not_applicable` — there is nothing to review on this entry. Reserved for fields whose `description` is empty: the agami extension still records type / name / signal_breakdown, but the review dashboard skips the card because no description was proposed. Allowed only when paired with `origin: no_description`.

### `agami.origin` (always emitted on introspect)

Enum: one of:
- `fk` — derived from FK metadata in the source DB
- `introspect_heuristic` — derived from introspect rules (column-name match, unique-index match, etc.)
- `column_comment` — derived from a DBA-authored column comment
- `llm_suggested` — proposed by an LLM during introspect with no stronger signal
- `human_authored` — written by a human in the YAML directly (or via the dashboard)
- `no_description` — the introspect step had nothing to propose for this field's `description` (DB returned no column comment, LLM was not used). Pairs with `review_state: not_applicable`.

### `agami.signed_off_by` (required when `review_state: approved`)

String. Either an email address (for human approvals) or `agami_introspect_v1` (for auto-approved entries from the introspect step). When `review_state: unreviewed` or `rejected`, this MUST be `null`.

### `agami.signed_off_at` (required when `review_state: approved`)

ISO-8601 UTC timestamp string. The moment of approval. When `review_state: unreviewed` or `rejected`, this MUST be `null`.

### `agami.signed_off_role` (required when entry is a metric or named_filter with `review_state: approved`)

Enum: one of `cfo`, `cto`, `data_lead`, `engineer`, `analyst`, `other`, `system`. The role under which the sign-off was given — `system` for auto-approved entries; the rest for humans. Optional for non-Rule-1 entries (joins, descriptions, etc.) but the dashboard always writes it.

### Example (relationship)

```yaml
- name: orders_to_customers
  from: orders
  to: customers
  from_columns: [customer_id]
  to_columns: [id]
  custom_extensions:
    - vendor_name: COMMON
      data: '{"agami": {"confidence": 1.0, "review_state": "approved", "origin": "fk", "signed_off_by": "agami_introspect_v1", "signed_off_at": "2026-05-10T14:23:11Z", "signed_off_role": "system", "signal_breakdown": {"fk_declared": true, "column_type_match": true}}}'
```

### Example (low-confidence relationship awaiting review)

```yaml
- name: orders_to_customers
  from: orders
  to: customers
  from_columns: [customer_id]
  to_columns: [customer_id]
  custom_extensions:
    - vendor_name: COMMON
      data: '{"agami": {"confidence": 0.62, "review_state": "unreviewed", "origin": "introspect_heuristic", "signed_off_by": null, "signed_off_at": null, "signed_off_role": null, "signal_breakdown": {"fk_declared": false, "unique_index_match": true, "column_type_match": true, "column_name_similarity": 1.0, "plural_pattern_match": true}}}'
```

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

Drives the risk-assessment banner in `agami-query-database/SKILL.md` Phase 2d. The skill consults `estimated_row_count` and `recommended_filters` to flag HIGH/MEDIUM/LOW risk on each query.

```yaml
- name: orders
  source: shop.public.orders
  custom_extensions:
    - vendor_name: COMMON
      data: '{"agami": {"performance_hints": {"estimated_row_count": 50000000, "recommended_filters": [{"column": "placed_at", "kind": "range", "reason": "partition key"}, {"column": "customer_id", "kind": "equality", "reason": "indexed FK"}], "selective_filters": ["is_active = true"], "indexes": [["placed_at"], ["customer_id", "status"]]}}}'
```

Sub-fields:
- `estimated_row_count` (integer, optional)
- `recommended_filters` (array of `{column, kind, reason}`). `kind` is one of `"equality"` (PK / FK / choice_field — `WHERE col = ?`) or `"range"` (time / date columns — `WHERE col BETWEEN ? AND ?`). `reason` is a free-form short prose explanation. Phase 2d's risk classifier checks the user's WHERE clause against `column` + `kind` to downgrade HIGH risk → LOW risk for queries that already include a recommended filter. Without this field, queries against `>1M` row tables stay HIGH risk regardless of how well-filtered they are.
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

### `agami.named_filters` (optional, written by agami-review when curators approve filter definitions)

OSI v0.1.1 has no native concept for a *named, sign-off'd predicate*. We add it here as a model-level extension so the LLM in `agami-query-database` can resolve fuzzy business terms like "active customer" to a single approved definition rather than guessing between several plausible predicates (Dimension 4 — Filter Correctness).

Shape: an array of `named_filter` objects. Each carries:

- `name` (string, required) — unique within the model
- `expression` (string, required) — a valid SQL predicate (no leading `WHERE`, no trailing semicolon)
- `definition_prose` (string, required when `review_state: approved`) — plain-English sentence describing the filter
- `synonyms` (array of strings, optional) — phrases the LLM should match against
- The full set of trust-layer keys (`confidence`, `review_state`, `origin`, `signed_off_by`, `signed_off_at`, `signed_off_role`, `signal_breakdown`)

A model-level `custom_extensions` entry can carry both `agami.profile` / `agami.introspect_meta` AND `agami.named_filters` on the same `agami` object — they coexist.

```yaml
custom_extensions:
  - vendor_name: COMMON
    data: '{"agami": {"profile": "default", "db_type": "postgres", "named_filters": [{"name": "active_customer", "expression": "customers.last_purchase_at >= NOW() - INTERVAL ''90 days''", "definition_prose": "Made a purchase in the last 90 days.", "synonyms": ["active customers", "engaged customers"], "confidence": 1.0, "review_state": "approved", "origin": "human_authored", "signed_off_by": "data.lead@example.com", "signed_off_at": "2026-04-01T15:30:00Z", "signed_off_role": "data_lead"}]}}'
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

## Metric-level extensions

Attached to a `metrics[]` entry. The trust-layer keys (`confidence`, `review_state`, etc.) apply here too — see the universal section above. Below are the keys specific to metrics, which encode the *meaning* of a measure (Dimension 1 — Semantic Correctness).

A metric without these is a number with no defensible definition. A CFO challenging "where did 47.3 come from" gets the SQL receipt; the metric extension keys explain what 47.3 means before the SQL even ran.

### `agami.definition_prose` (required for `review_state: approved`)

Free-form prose. Plain-English sentence(s) that describe what the metric measures, written for a non-technical reader (CFO / VP Finance / etc.). The receipt surfaces this on every answer that uses the metric. Example: *"Net revenue, gross of refunds, in USD converted at the transaction's invoice date. Excludes trial subscriptions and internal test accounts."*

### `agami.assumptions` (optional)

Array of strings. Each string is one assumption baked into the metric's definition. The CFO can scan this and either nod or push back. Example: `["FX rate is the daily close from xrates.org as of invoice date", "Trial accounts identified by customers.is_trial = true"]`.

### `agami.excludes` (optional)

Array of strings. Each string is one scenario explicitly excluded from the measure. Same audience as `assumptions`. Example: `["refunds (we are gross-of-refunds)", "trial subscription revenue"]`.

### Example (fully-defined metric)

```yaml
- name: revenue
  expression:
    dialects:
      - dialect: ANSI_SQL
        expression: SUM(orders.amount_usd_at_invoice_date)
  description: Net revenue.
  custom_extensions:
    - vendor_name: COMMON
      data: '{"agami": {"definition_prose": "Net revenue, gross of refunds, in USD converted at the transaction''s invoice date. Excludes trial subscriptions and internal test accounts.", "assumptions": ["FX rate is the daily close from xrates.org as of invoice date", "Trial accounts identified by customers.is_trial = true"], "excludes": ["refunds (we are gross-of-refunds)", "trial subscription revenue"], "confidence": 1.0, "review_state": "approved", "origin": "human_authored", "signed_off_by": "jane.smith@example.com", "signed_off_at": "2026-03-15T10:00:00Z", "signed_off_role": "cfo"}}'
```

---

## Reading conventions for the skills

- `agami-query-database/SKILL.md` reads `agami.type` and `agami.choice_field` to inform SQL generation and chart rendering. If absent, fall back to inferring from the SQL expression.
- `agami-connect/SKILL.md` is the canonical writer of every extension above.
- `agami-save-correction/SKILL.md` may **add** new extensions (e.g., a clarified `agami.unit` after the user says "amount is in cents") but never invents an extension whose shape isn't listed here.

---

## Hard rules — what the validator enforces

The agami validator (`plugins/agami/scripts/validate_semantic_model.py`) refuses to write a model if any of the following are true:

1. The model fails the OSI JSON schema validation (any structural breach).
2. A `custom_extensions[]` entry on any node has `vendor_name: COMMON` with a JSON payload that **starts with an `agami` key** but contains a sub-key not listed in this document. Unknown extensions = breaking change.
3. The `agami.type` value is outside `{string, integer, decimal, timestamp, date, boolean}`.
4. A relationship's `from` / `to` doesn't match an existing dataset's `name`.
5. A relationship's `from_columns` / `to_columns` arrays differ in length (OSI also checks this; we surface it as a separate error for clarity).
6. Two datasets share a `name`, or two fields within one dataset share a `name`, or two metrics share a `name`, or two relationships share a `name`. Two `named_filters` share a `name` within one model.
7. **Trust-layer enum violations.** `agami.review_state` outside `{unreviewed, approved, rejected, stale, not_applicable}`; `agami.origin` outside `{fk, introspect_heuristic, column_comment, llm_suggested, human_authored, no_description}`; `agami.signed_off_role` outside `{cfo, cto, data_lead, engineer, analyst, other, system}`; `agami.confidence` outside `[0, 1]`. `review_state: not_applicable` requires `origin: no_description` and an empty `description`.
8. **Rule 1 — high-blast-radius sign-off.** A `metric` or a model-level `named_filter` with `agami.review_state: approved` MUST have non-null `agami.signed_off_by`, `agami.signed_off_at`, AND `agami.signed_off_role`. A metric with `review_state: approved` MUST also have non-empty `agami.definition_prose`. Reject the YAML write otherwise.
9. **Rule 2 — sign-off completeness.** Any other entry (field / dataset / relationship) with `agami.review_state: approved` MUST have non-null `agami.signed_off_by` and `agami.signed_off_at`. Reject the YAML write otherwise.
10. **Sign-off coherence.** When `agami.review_state` is `unreviewed` or `rejected`, `agami.signed_off_by`, `agami.signed_off_at`, and `agami.signed_off_role` MUST all be `null` (preserves audit-log clarity — only approved entries carry sign-off attribution).

Other vendors' extensions (`SNOWFLAKE`, `DBT`, etc.) pass through untouched — the validator only inspects `vendor_name: COMMON` payloads with an `agami` top-level key.

---

## Adding a new extension key

1. Add a section to this document with its name, when it's emitted, and its full shape.
2. Add the key to the validator's `KNOWN_AGAMI_KEYS` set in `validate_semantic_model.py`.
3. Add a test in `tests/test_semantic_model_validator.py` that constructs a model using the new key and asserts the validator accepts it.
4. Update any skill that produces or consumes the new key.

If you skip step 2 or 3, your model with the new extension will be rejected by the validator. That's deliberate — it forces extensions to be reviewed and documented before they ship.
