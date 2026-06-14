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
`source_tables`, `other_names`, `unit` (optional, e.g. a currency ISO code),
`subject_areas` (optional; only for cross-cutting metrics). Trust:
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
confidence: confirmed
review_state: approved
```

## How to write them

Build a JSON array and apply it ONCE with the packaged command — never
hand-author each YAML, never write a throwaway loop script:

```bash
bash "$AGAMI_PLUGIN_ROOT/scripts/sm" add "$ROOT" --kind metric --area <area> --file /tmp/agami-metrics.json
```

It validates each item against the schema, writes
`subject_areas/<area>/metrics/<slug>.yaml` (or `.../entities/<slug>.yaml`),
validates the whole model, and reverts the batch on any failure.
