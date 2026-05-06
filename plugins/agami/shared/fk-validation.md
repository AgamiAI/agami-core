# FK Inference & Live-Join Validation

Helper used by `semantic-model` (G3 introspection / G6 validation) and `evaluate-queries` (relationship sanity checks). Cross-references:

- [connection-reference.md](connection-reference.md) — how to actually issue the queries.
- [dialect-rules.md](dialect-rules.md) — dialect-specific syntax for `LEFT JOIN`, type casts, `NULLIF`.
- [schema-reference.md](schema-reference.md) — where inferred FKs land in the YAML (`relationships:` block on each table).

---

## Why this exists

Some databases — the schemas users hand us most often — declare zero foreign keys at the database level. When the introspection step finds 0 declared FKs, the historical behaviour was to silently emit empty `relationships:` blocks. Every downstream NL-to-SQL query that needed a JOIN then failed (or worse, produced subtly wrong results from a cross join).

The fix is **two-pass**:

1. **Heuristic candidate generation** — propose FKs from naming conventions.
2. **Live join validation** — actually query the database to check whether the proposed join produces orphans. The DB knows the truth; ask it.

This converts "guess and ship empty" into "guess, verify, ship validated joins (or drop and audit)."

---

## Step 1 — Candidate generation (naming heuristics)

For every column that looks like a foreign-key reference, propose one or more candidate target tables.

### Patterns to match

For each table `T` with column `C`:

| Pattern | Candidate target table | Candidate target column | Confidence |
|---|---|---|---|
| `C` ends in `_id` and `C` minus `_id` is a known table name | `<C minus _id>` (singular or plural) | `id` | High |
| `C` ends in `_id` and `C` minus `_id` is a singular form of a known table name | the plural form | `id` | High |
| `C` is exactly `<known_table>_id` | `<known_table>` | `id` | High |
| `C` ends in `_key` or `_fk` | same lookup as above | the matching column | Medium |
| `C` matches `<known_table>_<known_pk_column>` (e.g. `customer_external_id` → `customers.external_id`) | `<known_table>` | `<known_pk_column>` | Medium |
| `C` is named `parent_id` / `owner_id` / `created_by` and `T` has no obvious self-ref | search all tables for an `id` column with type-compatible PK | `id` | Low |

### What to exclude

- Tables in audit / framework / migration schemas (already filtered in G3 — see semantic-model SKILL.md Phase G3).
- Columns with type `text`/`json`/`array` — only consider integer-ish or uuid-ish candidate FK columns.
- Self-references unless the column name explicitly hints at it (`parent_id`, `manager_id`).

### Output

A list of candidate tuples:

```python
[
  {"from_table": "orders", "from_column": "customer_id",
   "to_table":   "customers", "to_column":   "id",
   "confidence": "high"},
  ...
]
```

If multiple candidates exist for the same `(from_table, from_column)` (e.g. `created_by` → `users.id` *or* `employees.id`), include all of them — the validation step below will pick the survivor.

---

## Step 2 — Live join validation

For each candidate, issue a `LEFT JOIN` orphan-count query against the live database. If it produces few or no orphans, the relationship is real.

### The query template

```sql
SELECT
  COUNT(*)                                                          AS total_left,
  SUM(CASE WHEN r.<to_column> IS NULL THEN 1 ELSE 0 END)             AS orphans,
  ROUND(100.0 * SUM(CASE WHEN r.<to_column> IS NULL THEN 1 ELSE 0 END)
        / NULLIF(COUNT(*), 0), 2)                                   AS orphan_pct
FROM <from_table> l
LEFT JOIN <to_table> r ON l.<from_column> = r.<to_column>
WHERE l.<from_column> IS NOT NULL;
```

**Sampling for huge tables.** If `<from_table>` has > 1M rows (per the row-count hint collected in G3), wrap the left side in a sampling subquery to keep the validation under ~10 seconds:

```sql
FROM (SELECT * FROM <from_table> ORDER BY <pk> LIMIT 100000) l
```

Use `TABLESAMPLE` or `SAMPLE` if the dialect supports it (Snowflake: `SAMPLE (100000 ROWS)`; PostgreSQL 9.5+: `TABLESAMPLE SYSTEM`). Plain `LIMIT` without a sort can give skewed samples on partitioned tables — pick a deterministic sort by PK.

### Decision rule (initial validation — first time we see this candidate)

| `orphan_pct` | Confidence | Interactive mode | Balanced mode | Fast mode |
|---|---|---|---|---|
| `< 1%` | **Validated FK** | Show ✓ in summary, write silently | Write silently | Write silently |
| `1% – 10%` | **Probably valid** (some legitimate nulls / stale rows) | Show stats, ask "include?" | Show stats, ask "include?" | Write **with TODO comment** noting the orphan rate |
| `> 10%` | **Inference is wrong** | Show stats, ask "drop / override?" | Drop, no ask | Drop, no ask |
| Query errors (type mismatch, table missing, permission denied) | **Not a real FK** | Drop, ask if a synthetic relationship is wanted | Drop silently | Drop silently |

### Decision rule (re-validation — `semantic-model` G6 re-checks an already-existing relationship)

A relationship that previously validated under 1% can drift over time — the schema may have changed, data quality may have degraded, or a new ETL bug may be producing orphans. The re-validation rule is stricter on the high end (because a previously-clean FK breaking is a stronger drift signal) and adds an explicit middle band that the initial-validation rule didn't need:

| `orphan_pct` (re-validation) | Action |
|---|---|
| `< 1%` | Silent — relationship still healthy. |
| `1% – 10%` | Log to `<org_path>/local/.fk_inference_log.json` as `outcome: drift_acceptable`; keep relationship; print one-line warning in G6 summary. |
| `10% – 50%` | **Drift queue.** Log as `outcome: drift_warning`. Keep the relationship in the active YAML but stamp a TODO comment above the entry: `# DRIFT: orphan_pct re-validated at <X>% on <date> (was <1% at original write).`. In Interactive / Balanced, ask the user once whether to keep, fix, or drop. In Fast, keep with the TODO and surface in the G6 summary. |
| `> 50%` | **Hard fail.** G6 blocks the YAML write — see `semantic-model` SKILL.md G6. The data has clearly drifted; treat the same as a brand-new dangling reference. |
| Query errors | Same as initial — drop the relationship. |

When multiple candidates exist for the same source column and more than one validates with `orphan_pct < 1%`:

- **Interactive / Balanced**: ask the user which one is the intended target. (Real ambiguity — both joins technically work; only the user knows the intent.)
- **Fast**: pick the candidate with the lower `orphan_pct`. If tied, pick the one whose target table name has the strongest naming match (longest common prefix). Log the decision to the inference log so it can be reviewed later.

### Type / collation mismatches

If the candidate's `from_column` and `to_column` have incompatible types (e.g. `varchar` vs `bigint`), the `LEFT JOIN` will error or implicitly cast. Catch these errors as "Not a real FK" — don't try to be clever with explicit casts. If the user genuinely needs a cross-type join they can declare it manually.

---

## Step 3 — Audit log

Every inference attempt — validated, dropped, or asked-about — is appended to `<org_path>/local/.fk_inference_log.json`:

```json
{
  "timestamp": "2026-04-24T15:42:00Z",
  "datasource": "crm",
  "from_table": "orders",
  "from_column": "customer_id",
  "candidates": [
    {"to_table": "customers", "to_column": "id", "orphan_pct": 0.4, "outcome": "validated"},
    {"to_table": "leads",     "to_column": "id", "orphan_pct": 87.2, "outcome": "dropped:high_orphans"}
  ],
  "decision": "validated:customers.id",
  "decided_by": "auto:balanced",
  "mode": "balanced"
}
```

This is the single place to look when someone asks "why does (or doesn't) my model have this relationship?". It's gitignored (lives under `local/`) but is the source of truth for what was tried.

---

## Step 4 — TODO breadcrumbs in YAML

When validation declines a candidate or all candidates are dropped, leave a comment at the top of the affected table YAML so future-you understands why `relationships:` is empty (or shorter than expected):

```yaml
# FK inference: 0 declared FKs in source DB. Heuristic proposed 3 candidates;
# all dropped (see local/.fk_inference_log.json for details). To add manually,
# append entries to relationships: below.
relationships: []
```

---

## What this is NOT

- **Not a constraint enforcer.** We never `ALTER TABLE ... ADD CONSTRAINT` on the user's database. This is read-only; we only describe relationships in the YAML.
- **Not a metric.** The orphan_pct is a confidence signal, not a data-quality KPI to track.
- **Not a substitute for declared FKs.** If the user later adds real FK constraints to the schema, the next `/agami-data-admin:semantic-model` UPDATE run will pick them up and supersede the heuristic.

---

## Reused by

- `semantic-model` G3 (introspection): runs Step 1 + Step 2 for the new datasource. Default behaviour gated by `onboarding_mode` from `<org_path>/local/.config.json`.
- `semantic-model` G6 (validation): re-runs Step 2 for any `relationships:` entry already in the YAML to confirm it still validates against the live DB. Treats a previously-validated FK that now has `orphan_pct > 50%` as a hard-fail in G6 (the schema or data has drifted).
- `evaluate-queries` Phase 6 (failure analysis): when a generated SQL fails on a `JOIN`, check whether the relationship the SQL relied on still validates — if not, suggest fixing the YAML.
