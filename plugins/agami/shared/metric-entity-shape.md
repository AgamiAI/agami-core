# Metric & Entity YAML shape — the canonical reference

This is the **sanctioned source of truth** for the shape of a metric or entity
YAML when building them during onboarding/enrichment. The data below is
**synthetic** (a made-up `widgets`/`shipments` store) — it exists to show the
*structure*, never to be copied as content.

> **HARD RULE — never read another profile to learn a shape.**
> Do **not** glob or read other profiles' artifacts (e.g.
> `find <artifacts_dir> -path '*/metrics/*.yaml'`, or reading
> `<artifacts_dir>/<some-other-profile>/...`) to "copy the binding shape." That
> crosses the profile boundary: in a hosted / multi-tenant deployment it is a
> **tenant-data leak** (onboarding customer A must never read customer B's
> model), and even locally it risks lifting another profile's *business
> definitions* (filters, calculation text) into the one you're building.
>
> You don't need to. The packaged `sm add` command validates every item against
> the `Metric` / `Entity` Pydantic model and reverts the batch on failure — so
> build the JSON from this reference (and the profile's own schema) and let the
> engine validate it. The schema, not a sibling profile, is the authority.

## Metric

Fields: `name`, `description`, `calculation` (prose intent — **required**,
provider-portable), `bindings` (per-dialect SQL keyed by `StorageType`),
`source_tables`, `other_names`, `unit` (optional: a currency ISO code, optionally scaled and/or a rate — `USD`, `USD_millions`, `USD/day`, `USD_millions/year` — or a plain label),
`subject_areas` (optional; only for cross-cutting metrics), `caveats` (optional; what the
formula does not cover, served with the metric as a warning). Trust:
`confidence` + `review_state` (declared → `inferred`/`unreviewed`; pure guess →
`proposed`/`unreviewed`).

```yaml
name: completed shipments
description: Shipments that reached a delivered state.
calculation: Count of shipments whose status is 'delivered'.
bindings:
  PostgreSQL: "COUNT(*) FILTER (WHERE status = 'delivered')"
  BigQuery:   "COUNTIF(status = 'delivered')"
source_tables:
  - shipments
other_names:
  - delivered shipments
  - fulfilled orders
confidence: inferred
review_state: unreviewed
```

A limit of the formula goes in `caveats`, not in the description or calculation, so it is served
as a warning rather than read as part of the definition:

```yaml
name: average resolution days
description: Days from opening to resolution, averaged over resolved tickets.
calculation: Average of resolved_at minus opened_at, in days.
bindings:
  PostgreSQL: "AVG(resolved_at::date - opened_at::date)"
source_tables:
  - tickets
caveats:
  - Gives no value for a ticket with no opened_at.
confidence: inferred
review_state: unreviewed
```

A metric carrying a currency output adds a `unit`:

```yaml
name: gross revenue
description: Total invoiced amount across all orders.
calculation: Sum of order line totals.
bindings:
  PostgreSQL: "SUM(line_total)"
unit: USD
source_tables:
  - order_lines
confidence: inferred
review_state: unreviewed
```

**Semi-additive metrics (`non_additive_dimensions` + `semi_additive_agg`) — set these whenever
the measure is a STOCK, not a flow.** A balance, inventory level, headcount, or point-in-time
subscriber count is summable across *entities* (accounts, warehouses, regions) but **NOT across
time** — summing an account balance over 90 days multiplies it ~90×. Name the dimension(s) it
can't be summed over (`time` is the usual shorthand for any date/time grain) and how to collapse
over them (`last` = period-end value, `average`, `min`, `max`). Flow metrics (revenue, counts,
quantities) leave both empty. Tell-tale: the metric's source column has `aggregation: additive`
but is a *level/balance/on-hand* (a stock), or its name is balance/inventory/headcount/AUM/etc.

```yaml
name: total balance
description: End-of-period balance summed across accounts.
calculation: Sum of account balances at the period end (NOT across days within the period).
bindings:
  PostgreSQL: "SUM(balance)"
unit: USD
source_tables:
  - daily_balances
non_additive_dimensions:
  - time
semi_additive_agg: last     # over time take the period-end balance, then sum across accounts
confidence: inferred
review_state: unreviewed
```

**Derived metrics (compose other metrics — don't re-derive).** When a metric is a function of
OTHER metrics, reference them by name with `{…}` placeholders and list them in `base_metrics`.
Define `revenue` once; everything downstream tracks it (no drift).

```yaml
name: average order value
description: Revenue per order.
calculation: Total revenue divided by order count.
base_metrics: [revenue, order count]
bindings:
  PostgreSQL: "{revenue} / {order count}"   # expands to (SUM(amount)) / (COUNT(DISTINCT order_id))
source_tables:
  - orders
```

**Second-order statistics (an aggregate OF an aggregate).** A metric like *average daily revenue*
or *peak monthly orders* aggregates a finer-grain aggregate — `AVG` of a daily `SUM`. Don't write
`AVG(SUM(...))` (illegal SQL). Declare it: bind as `OUTERAGG({base_metric})` and set `inner_grain`
to the dimension(s) the base is grouped by first. The engine synthesizes the CTE deterministically.

```yaml
name: average daily revenue
description: Mean of each day's total revenue.
calculation: Average over days of the daily revenue total.
base_metrics: [daily revenue]
bindings:
  PostgreSQL: "AVG({daily revenue})"   # → (SELECT AVG(v) FROM (SELECT order_date, SUM(amount) AS v FROM orders GROUP BY order_date) _i)
inner_grain:
  - order_date
source_tables:
  - orders
```

A base metric over several tables works too: the engine joins the table it measures to each other
source table by the one relationship declared directly between them, and refuses the metric when
there is no such edge, more than one, or a join that fans out the anchor's rows. The measured
table is the base's `primary_table` (it must be one of its `source_tables`, with or without the
schema), else the one table that declares every column the base's aggregates read; when those
columns span tables (or it reads none, as in `COUNT(*)`), set `primary_table`. With several
tables, write each `inner_grain` entry as a column only one of them declares, or as
`table.column` naming a source table and a column it declares; the tables must not share a name
across schemas. Table and column
names are quoted where the engine needs it, so a mixed-case table such as `"OrderLines"` works on
PostgreSQL.

`sm add` and `sm curate` refuse a new or edited binding that puts an aggregate inside another in
the same SELECT, including in a `WITHIN GROUP (ORDER BY ...)` or `FILTER (WHERE ...)` clause
(`PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY SUM(amount))`), or a window function inside an
aggregate (`AVG(SUM(amount) OVER ())`), because the database refuses every query that uses it. An
aggregate over a subquery that groups first is fine, and so is a window over an aggregate
(`SUM(SUM(amount)) OVER ()`). An aggregate in a scalar subquery counts as the outer query's when it
reads only the outer query's columns, so `AVG((SELECT SUM(orders.amount)))` is refused too, while
`AVG((SELECT SUM(o2.amount) FROM orders o2 WHERE o2.customer_id = orders.customer_id))`, which sums
the subquery's own rows, is accepted. The one exception is an Oracle binding: Oracle runs two levels of
aggregate in the argument of a SELECT that has its own GROUP BY, so
`(SELECT AVG(SUM(amount)) FROM orders GROUP BY customer_id)` is accepted there. A bare
`AVG(SUM(amount))` has no GROUP BY of its own and is still refused on Oracle, as is a third level,
and the same binding for any other engine is refused. A metric already in the model that nests
aggregates gets a `metric_binding_nested_aggregate` warning instead.

**An average of the values that exist.** A missing value is not a zero, so don't fill it before
averaging. For one column write `AVG(col)`: it already leaves missing values out. For several
columns, divide the sum of the present values by the count of the present values, without a
`VALUES` list or a per-row subquery:

```yaml
bindings:
  PostgreSQL: "SUM(COALESCE(score_a, 0) + COALESCE(score_b, 0)) * 1.0 / NULLIF(COUNT(score_a) + COUNT(score_b), 0)"
```

The `* 1.0` keeps the average fractional: without it an integer sum divided by a count is integer
division on PostgreSQL, SQLite and SQL Server (scores 1 and 2 give 1, not 1.5). The zero added for a
missing score is not counted in the denominator, so it changes nothing, and `metric_null_filled`
does not warn on this shape: a `SUM` of `COALESCE(col, 0)` terms in the numerator whose denominator
counts only the present values of the same columns, written the same way (`a.score` is not
`b.score`, and on PostgreSQL, Snowflake and Oracle a quoted `"Score"` is not `score`), with
`COUNT(col)` or the searched `SUM(CASE WHEN col IS NOT NULL THEN 1 ELSE 0 END)`. Any other
fallback than 0, another aggregate such as `AVG(COALESCE(col, 0))` (it averages the invented zeros),
or a denominator term such as `COUNT(*)` that counts the missing rows still warns.

## Entity

Fields: `name`, `plural`, `other_names`, `description`, `maps_to` (one entry per
table/column the entity is identified by; at most one `primary: true`),
optionally `value_pattern` (provider-neutral regex for opaque literals).

```yaml
name: customer
plural: customers
other_names:
  - buyer
  - account
description: A person or org that places orders.
maps_to:
  - table: customers
    column: customer_id
    primary: true
  - table: orders
    column: customer_id
confidence: inferred
review_state: unreviewed
```

An entity is written `unreviewed`, which is also the default when `review_state` is left out. It
waits in the review queue until a person approves it in `/agami-model` (or `sm approve-queue`),
which records who signed it off and when.

## How to write them

Build a JSON array and apply it ONCE with the packaged command — never
hand-author each YAML, never write a throwaway loop script:

```bash
bash "$AGAMI_PLUGIN_ROOT/scripts/sm" add "$ROOT" --kind metric --area <area> --file "<artifacts_dir>/local/tmp/agami-metrics.json"
```

It validates each item against the schema, writes
`subject_areas/<area>/metrics/<slug>.yaml` (or `.../entities/<slug>.yaml`),
validates the whole model, and reverts the batch on any failure.
