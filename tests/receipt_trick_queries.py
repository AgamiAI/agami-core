"""Every query that has ever been read as a false match, in one place (ACE-177 criterion 11).

A false `matched` is the defect the receipt's `columns` section exists to prevent, so each one found
stays a test, here, rather than in whichever battery happened to find it. The lists are DATA: the
batteries assert what each reads as, and `receipt_truth` executes every one the receipt calls
`matched` and checks the claim against the numbers.

The SQL is written against the synthetic `acme` model of `tests/test_ace177_filters_on_a_matched_metric.py`
(`orders`, `customers`, `payments`, `change_requests`) unless a list says otherwise.
"""

from __future__ import annotations

# #655 / #658: two spellings of one metric that can return different rows, over the `t (g, p, x)`
# table of `tests/test_receipt_equivalent_spellings.py`. None may match.
# (binding, written, why it is not the same number, witness). Each witness is
# (engine, dataset, grouped) on which the two return different rows.
SPELLINGS_DIFFERENT = [
    (
        "SUM(x) FILTER (WHERE p = 'paid')",
        "SUM(CASE WHEN p = 'paid' THEN x ELSE 0 END)",
        "no matching row: NULL against 0",
        ("sqlite", "none_paid", False),
    ),
    (
        "AVG(x) FILTER (WHERE p = 'paid')",
        "AVG(CASE WHEN p = 'paid' THEN x ELSE 0 END)",
        "the zeros are averaged in",
        ("sqlite", "full", False),
    ),
    (
        "COUNT(*) FILTER (WHERE p = 'paid')",
        "SUM(CASE WHEN p = 'paid' THEN 1 ELSE 0 END)",
        "an empty table: 0 against NULL",
        ("sqlite", "empty", False),
    ),
    (
        "COUNT(*) FILTER (WHERE p = 'paid')",
        "SUM(CASE WHEN p = 'paid' THEN 1 END)",
        "a group with no match: 0 against NULL",
        ("sqlite", "full", True),
    ),
    (
        "COUNT(*)",
        "NULLIF(COUNT(*), 0)",
        "an empty table: 0 against NULL",
        ("sqlite", "empty", False),
    ),
    # Widening for division. A numeric cast keeps integer division on SQLite, and on Postgres a
    # quotient past 10^16 keeps one digit fewer than `* 1.0` gives (not reproducible on this data).
    (
        "SUM(x) * 1.0 / COUNT(*)",
        "CAST(SUM(x) AS NUMERIC) / COUNT(*)",
        "SQLite: a NUMERIC cast of an integer is still an integer",
        ("sqlite", "full", False),
    ),
    # Dropping NULLIF from a denominator: DuckDB returns infinity for 1/0, not NULL.
    (
        "SUM(x) / NULLIF(COUNT(*) FILTER (WHERE p = 'zzz'), 0)",
        "SUM(x) / COUNT(*) FILTER (WHERE p = 'zzz')",
        "DuckDB divides by zero to infinity",
        ("duckdb", "full", False),
    ),
    # A FILTER can leave a group's aggregate with no input, so the grouped-only rules do not hold
    # inside one: group 1 has no row with g > 1, giving NULL against 0. Both directions.
    (
        "COUNT(CASE WHEN p = 'paid' THEN 1 END) FILTER (WHERE g > 1)",
        "SUM(CASE WHEN p = 'paid' THEN 1 ELSE 0 END) FILTER (WHERE g > 1)",
        "a FILTER that empties group 1: 0 against NULL",
        (("sqlite", "duckdb"), "full", True),
    ),
    (
        "SUM(CASE WHEN p = 'paid' THEN 1 ELSE 0 END) FILTER (WHERE g > 1)",
        "COUNT(CASE WHEN p = 'paid' THEN 1 END) FILTER (WHERE g > 1)",
        "a FILTER that empties group 1: NULL against 0",
        (("sqlite", "duckdb"), "full", True),
    ),
    # A CASE result carries no column collation, so over a collated text column DISTINCT, MIN and
    # MAX compare differently in the CASE form: SQLite, `x TEXT COLLATE NOCASE`.
    (
        "COUNT(DISTINCT x) FILTER (WHERE p = 'paid')",
        "COUNT(DISTINCT CASE WHEN p = 'paid' THEN x END)",
        "'a' and 'A' are one value under NOCASE, two in the CASE form",
        ("sqlite_nocase", "nocase", False),
    ),
    (
        "MIN(x) FILTER (WHERE p = 'paid')",
        "MIN(CASE WHEN p = 'paid' THEN x END)",
        "NOCASE orders 'a' first, BINARY orders 'A' first",
        ("sqlite_nocase", "nocase", False),
    ),
    (
        "MAX(x) FILTER (WHERE p = 'paid')",
        "MAX(CASE WHEN p = 'paid' THEN x END)",
        "NOCASE orders 'B' last, BINARY orders 'a' last",
        ("sqlite_nocase", "nocase", False),
    ),
    # SQLite compares two columns of different collations with the LEFT one's, so `=` and `<>` are
    # not symmetric there.
    (
        "COUNT(*) FILTER (WHERE x = p)",
        "COUNT(*) FILTER (WHERE p = x)",
        "'a' = 'A' under the left column's NOCASE, not under BINARY",
        ("sqlite_nocase", "nocase", False),
    ),
    (
        "COUNT(*) FILTER (WHERE x <> p)",
        "COUNT(*) FILTER (WHERE p <> x)",
        "'a' <> 'A' only under the left column's BINARY",
        ("sqlite_nocase", "nocase", False),
    ),
    (
        "COUNT(*) FILTER (WHERE g > 1)",
        "NULLIF(COUNT(*) FILTER (WHERE g > 1), 0)",
        "a FILTER that empties group 1: 0 against NULL",
        (("sqlite", "duckdb"), "full", True),
    ),
]


# A filtered statement against UNFILTERED metrics (`change_count`, `total_sales`, `order_count`):
# none may read `matched` with nothing listed. Includes every two-table condition the review found
# being dropped as a "join" (OR, function, tuple, CASE, concatenation, non-equal comparison), and
# the same filter one step back (a CTE, a derived table), which reads `undetermined`.
NOT_A_PLAIN_MATCH = [
    "SELECT COUNT(*) AS n FROM orders WHERE NOT (status = 'cancelled')",
    "SELECT COUNT(*) AS n FROM orders WHERE status <> 'cancelled'",
    "SELECT COUNT(*) AS n FROM orders WHERE status IN ('paid', 'shipped')",
    "SELECT COUNT(*) AS n FROM orders WHERE status NOT IN ('cancelled')",
    "SELECT SUM(total_amount) AS n FROM orders WHERE total_amount BETWEEN 10 AND 100",
    "SELECT COUNT(*) AS n FROM orders WHERE status = 'paid' OR status = 'shipped'",
    "SELECT COUNT(*) AS n FROM orders WHERE status IS NOT NULL",
    "SELECT COUNT(*) AS n FROM orders WHERE status LIKE 'p%'",
    "SELECT COUNT(*) AS n FROM orders o JOIN customers c ON c.id = o.customer_id "
    "AND c.region = 'EU'",
    "SELECT COUNT(*) AS n FROM orders o JOIN customers c ON c.id = o.customer_id "
    "WHERE c.region = 'EU'",
    "SELECT COUNT(*) AS n FROM orders o, customers c WHERE c.id = o.customer_id "
    "AND c.region = 'EU'",
    "SELECT COUNT(*) AS n FROM orders o WHERE EXISTS (SELECT 1 FROM customers c "
    "WHERE c.id = o.customer_id AND c.region = 'EU')",
    "SELECT COUNT(*) AS n FROM orders WHERE customer_id IN (SELECT id FROM customers "
    "WHERE region = 'EU')",
    "SELECT COUNT(*) AS n FROM orders o WHERE o.opened = o.closed",
    "SELECT COUNT(*) AS n FROM orders WHERE 1 = 0",
    "SELECT COUNT(*) AS n FROM orders WHERE TRUE AND status = 'paid'",
    "SELECT COUNT(*) FILTER (WHERE status = 'paid') AS n FROM orders",
    "SELECT SUM(CASE WHEN status = 'paid' THEN total_amount END) AS n FROM orders",
    "SELECT COUNT(CASE WHEN status = 'paid' THEN 'x' END) AS n FROM orders",
    "SELECT COUNT(*) FILTER (WHERE status = 'paid') AS n FROM orders WHERE customer_id = 3",
    "SELECT COUNT(*) AS n FROM orders WHERE ((O.STATUS = 'paid'))",
    "SELECT customer_id, COUNT(*) AS n FROM orders WHERE status = 'paid' "
    "GROUP BY customer_id HAVING COUNT(*) > 1",
    "SELECT COUNT(DISTINCT number) AS n FROM change_requests WHERE opened >= DATE '2026-01-01'",
    "SELECT COUNT(*) AS n FROM orders WHERE " + " AND ".join(
        f"status <> 's{i}'" for i in range(12)),
    # A condition naming two references that is not a bare `col = col` restricts rows (review).
    "SELECT COUNT(*) AS n FROM orders o JOIN customers c ON c.id = o.customer_id "
    "WHERE o.status = 'x' OR c.region = 'EU'",
    "SELECT COUNT(*) AS n FROM orders o JOIN customers c ON c.id = o.customer_id "
    "WHERE COALESCE(o.status, c.status) = 'x'",
    "SELECT COUNT(*) AS n FROM orders o JOIN customers c ON c.id = o.customer_id "
    "WHERE (o.status, c.region) IN (('x', 'EU'))",
    "SELECT COUNT(*) AS n FROM orders o JOIN customers c ON c.id = o.customer_id OR c.region = 'EU'",
    "SELECT COUNT(*) AS n FROM orders o JOIN customers c ON c.id = o.customer_id "
    "WHERE (o.status = 'paid' OR c.id IS NULL)",
    "SELECT COUNT(*) AS n FROM orders o JOIN customers c ON c.id = o.customer_id "
    "WHERE CASE WHEN c.id IS NULL THEN TRUE ELSE o.status = 'x' END",
    "SELECT COUNT(*) AS n FROM orders o JOIN orders o2 ON o2.id = o.id "
    "AND (o2.status = 'paid' OR o.id < 0)",
    "SELECT COUNT(*) AS n FROM orders o JOIN customers c ON c.id = o.customer_id "
    "WHERE o.status || c.region = 'paid' || c.region",
    "SELECT SUM(o.total_amount) AS n FROM orders o JOIN payments p ON p.order_id = o.id "
    "WHERE p.amount > o.total_amount",
    # The same filter one step back: ACE-178 follows the step and lists it.
    "WITH paid AS (SELECT id FROM orders WHERE status = 'paid') SELECT COUNT(*) AS n FROM paid",
    "SELECT COUNT(*) AS n FROM (SELECT id FROM orders WHERE status = 'paid') x",
    "SELECT SUM(p.total_amount) AS n FROM (SELECT total_amount FROM orders "
    "WHERE status = 'paid') p",
]


# Against the full model, which also declares the curated twin `paid_sales`: a condition on a
# reference the number does not read (a self-join's other side, an unqualified column in a
# two-reference SELECT, another table's same-named column) may never stand for the twin's own.
TWIN_TRICKS = [
    "SELECT SUM(a.total_amount) AS s FROM orders a JOIN orders b ON b.id = a.customer_id "
    "WHERE b.status = 'paid'",
    "SELECT SUM(a.total_amount) FILTER (WHERE b.status = 'paid') AS s FROM orders a "
    "JOIN orders b ON b.id = a.customer_id",
    "SELECT SUM(o.total_amount) AS s FROM orders o JOIN payments p ON p.order_id = o.id "
    "WHERE status = 'paid'",
    # The curated twin's condition written on the OTHER table of a join.
    "SELECT SUM(o.total_amount) AS s FROM orders o JOIN customers c ON c.id = o.customer_id "
    "WHERE c.status = 'paid'",
]

# The same rows as `paid_sales`, spelled every way #658 accepts: each is the twin, nothing extra.
CURATED_SPELLINGS = [
    "SELECT SUM(total_amount) AS s FROM orders WHERE status = 'paid'",
    "SELECT SUM(total_amount) FILTER (WHERE status = 'paid') AS s FROM orders",
    "SELECT SUM(CASE WHEN status = 'paid' THEN total_amount END) AS s FROM orders",
    "SELECT SUM(o.total_amount) AS s FROM orders o WHERE ((O.STATUS = 'paid'))",
    "SELECT SUM(total_amount) FILTER (WHERE status = 'paid') AS s FROM orders o "
    "WHERE o.status = 'paid'",
]

# (storage type, statement): a SEMI or ANTI join keeps or drops rows by what another relation
# holds, so it is listed as the join itself.
SEMI_ANTI = [
    ("DuckDB", "SELECT SUM(o.total_amount) AS n FROM orders o SEMI JOIN payments p "
               "ON p.order_id = o.id"),
    ("DuckDB", "SELECT SUM(o.total_amount) AS n FROM orders o ANTI JOIN payments p "
               "ON p.order_id = o.id"),
    ("Databricks", "SELECT SUM(o.total_amount) AS n FROM orders o LEFT ANTI JOIN payments p "
                   "ON p.order_id = o.id AND p.status = 'x'"),
]

# (storage type, statement): a sampled table, or a table read as of another time, is not the metric
# over the table and reads `undetermined`.
UNSEEN_ROWS = [
    ("PostgreSQL", "SELECT SUM(total_amount) AS n FROM orders TABLESAMPLE SYSTEM (10)"),
    ("DuckDB", "SELECT SUM(total_amount) AS n FROM orders USING SAMPLE 10%"),
    ("Snowflake", "SELECT SUM(total_amount) AS n FROM orders AT(TIMESTAMP => '2026-01-01')"),
    ("BigQuery", "SELECT SUM(total_amount) AS n FROM orders "
                 "FOR SYSTEM_TIME AS OF TIMESTAMP '2026-01-01'"),
]


# ACE-178: a restriction hidden one or more steps back — in a CTE, a subquery in FROM, a step's join,
# a lookup step's value, a filter on a step's group key — over the same synthetic model, against
# UNFILTERED metrics. None may read a plain `matched`: each lists the restriction, names a join the
# model does not declare, or is not `matched`. A false match found in ACE-178's reviews joins here.
STEP_TRICKS = [
    # B. join forms through a step
    "WITH s AS (SELECT o.total_amount FROM orders o JOIN customers c ON c.id = o.customer_id "
    "AND c.region = 'EU') SELECT SUM(total_amount) AS n FROM s",
    "WITH s AS (SELECT o.total_amount FROM orders o JOIN customers c ON c.id = o.customer_id "
    "WHERE c.region = 'EU') SELECT SUM(total_amount) AS n FROM s",
    "SELECT SUM(s.total_amount) AS n FROM (SELECT o.total_amount FROM orders o, customers c "
    "WHERE c.id = o.customer_id AND c.region = 'EU') s",
    "SELECT SUM(o.total_amount) AS n FROM orders o JOIN (SELECT id FROM customers "
    "WHERE region = 'EU') c ON c.id = o.customer_id",
    # C. step-specific
    "WITH g AS (SELECT status, COUNT(*) AS n FROM orders GROUP BY status) "
    "SELECT n FROM g WHERE status = 'paid'",
    "WITH g AS (SELECT status AS st, COUNT(*) AS n FROM orders GROUP BY 1) "
    "SELECT n FROM g WHERE st = 'paid'",
    "WITH a AS (SELECT MAX(opened) AS d FROM orders) SELECT COUNT(*) AS n FROM orders o, a "
    "WHERE o.opened >= a.d",
    "WITH z AS (SELECT 1 AS k WHERE FALSE) SELECT COUNT(*) AS n FROM orders o CROSS JOIN z",
    "WITH orders AS (SELECT id, total_amount FROM public.orders WHERE status = 'paid') "
    "SELECT SUM(total_amount) AS n FROM orders",
    "SELECT COUNT(*) AS n FROM (WITH o AS (SELECT id FROM orders WHERE status = 'paid') "
    "SELECT id FROM o) x",
    "WITH p AS (SELECT id, customer_id FROM orders WHERE status = 'paid') "
    "SELECT COUNT(*) AS n FROM p a JOIN p b ON a.id = b.id",
    "WITH a AS (SELECT id FROM s), s AS (SELECT id FROM orders WHERE status = 'paid') "
    "SELECT COUNT(*) AS n FROM a",
    "WITH p AS (SELECT id FROM orders WHERE status = 'paid') SELECT COUNT(*) AS n FROM \"p\"",
    "WITH r AS (SELECT total_amount, ROW_NUMBER() OVER (ORDER BY id) AS rn FROM orders) "
    "SELECT SUM(total_amount) AS n FROM r WHERE rn <= 10",
    "WITH t AS (SELECT total_amount FROM orders ORDER BY total_amount DESC LIMIT 10) "
    "SELECT SUM(total_amount) AS n FROM t",
    "WITH d AS (SELECT DISTINCT customer_id, total_amount FROM orders) "
    "SELECT SUM(total_amount) AS n FROM d",
    "WITH u AS (SELECT total_amount FROM orders WHERE status = 'paid' UNION ALL "
    "SELECT total_amount FROM orders) SELECT SUM(total_amount) AS n FROM u",
    "WITH paid AS (SELECT id FROM orders WHERE status = 'paid') "
    "SELECT COUNT(*) AS n FROM orders WHERE id IN (SELECT id FROM paid)",
    "WITH s AS (SELECT COUNT(*) FILTER (WHERE status = 'paid') AS n FROM orders) SELECT n FROM s",
    "WITH s AS (SELECT SUM(CASE WHEN status = 'paid' THEN total_amount END) AS n FROM orders) "
    "SELECT s.n AS n FROM s",
    # Ten steps, the filter only in the innermost.
    "WITH s0 AS (SELECT id, total_amount FROM orders WHERE status = 'paid'), "
    + ", ".join(f"s{i} AS (SELECT id, total_amount FROM s{i - 1})" for i in range(1, 10))
    + " SELECT SUM(total_amount) AS n FROM s9",
    "WITH c AS (SELECT o.total_amount, c.status FROM orders o JOIN customers c "
    "ON c.id = o.customer_id) SELECT SUM(total_amount) AS n FROM c WHERE status = 'paid'",
    "WITH g AS (SELECT customer_id, COUNT(*) AS c FROM orders GROUP BY customer_id "
    "HAVING COUNT(*) > 5) SELECT COUNT(*) AS n FROM orders o JOIN g ON g.customer_id = "
    "o.customer_id",
    "WITH g AS (SELECT customer_id, SUM(total_amount) AS t FROM orders WHERE status = 'paid' "
    "GROUP BY customer_id) SELECT SUM(t) AS n FROM g",
    "WITH RECURSIVE r AS (SELECT id, total_amount FROM orders WHERE status = 'paid' UNION ALL "
    "SELECT id, total_amount FROM r WHERE id < 0) SELECT SUM(total_amount) AS n FROM r",
    "WITH s AS (SELECT o.total_amount FROM orders o LEFT JOIN customers c ON c.id = "
    "o.customer_id WHERE c.region = 'EU') SELECT SUM(total_amount) AS n FROM s",
    "WITH x AS (SELECT * FROM orders WHERE status = 'paid') SELECT SUM(x.total_amount) AS n FROM x",
]
