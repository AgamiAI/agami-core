"""The four `sm` verbs that grade a statement a PERSON supplied, part by part.

`agami-reconcile` is learning to take a trusted query as evidence rather than as the answer. The
skill decides meaning and talks to the person; everything deterministic about the statement lives in
the CLI, and these are those parts:

* `claims`          - where two statements differ, in the seven claims `golden_claims` already reads
* `compare-results` - whether two result sets say the same thing, through the golden comparator
* `join-probes`     - for every join the statement wrote: the receipt's own status for it, whether the
                      written key matches the declared one, and the probe SQL that would show whether
                      the keys really resolve
* `filter-values`   - for every value typed into a filter: is it a value the column holds, answered
                      from the semantic model's own list when it has one and from a probe otherwise

None of the four runs a probe. They emit SQL; the skill runs it through the same execution tier every
query takes, and `filter-values judge` reads what came back. Three properties this file holds still:
an open question is never rendered as a settled claim (a CTE that shadows a table, a `USING`, a
declared `on:` nobody could read); a probe that failed is never graded as an answer (a zero-byte CSV
reads `unresolved`); and a value that cannot be quoted the same way on every engine never reaches the
warehouse. Synthetic fixtures throughout: a `demo` shop over `orders`, `order_items` and `customers`.
"""

from __future__ import annotations

import contextlib
import io
import json
import sys
from pathlib import Path

import pytest

pytest.importorskip("pydantic")
pytest.importorskip("sqlglot")
yaml = pytest.importorskip("yaml")

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "plugins" / "agami" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from semantic_model import cli, dialects  # noqa: E402


def _model(root: Path, *, orders_rows: int | None = None, storage: str = "PostgreSQL",
           unreadable_on: bool = False, engines: tuple[str, ...] | None = None) -> None:
    """A three-table shop. `orders.status` carries a populated list of values, `orders.region` an
    EMPTY one (the not-yet-decoded state), `customers.email` is sensitive, `customers.id` is a
    declared key, and only the order_items -> orders join is declared. `unreadable_on` declares that
    join through an `on:` carrying a bind marker, which the receipt cannot reduce; `engines` declares
    several storage connections that disagree on the engine."""
    (root / "datasources").mkdir(parents=True)
    (root / "subject_areas" / "s" / "tables").mkdir(parents=True)
    connections = list(engines or (storage,))
    (root / "datasource.yaml").write_text(yaml.safe_dump({
        "datasource": "demo", "version": 1,
        "storage_connections": [{"name": f"c{i}", "ref": f"datasources/c{i}/storage.yaml"}
                                for i, _e in enumerate(connections)],
        "subject_areas": ["subject_areas/s"]}))
    for i, engine in enumerate(connections):
        (root / "datasources" / f"c{i}").mkdir()
        (root / "datasources" / f"c{i}" / "storage.yaml").write_text(
            yaml.safe_dump({"name": f"c{i}", "storage_type": engine}))
    (root / "subject_areas" / "s" / "subject_area.yaml").write_text(yaml.safe_dump({
        "name": "s", "tables": [
            {"storage_connection": "c0", "schema": "public", "table": "orders"},
            {"storage_connection": "c0", "schema": "public", "table": "order_items"},
            {"storage_connection": "c0", "schema": "public", "table": "customers"}]}))
    orders: dict = {
        "name": "orders", "schema": "public", "storage_connection": "c0", "grain": ["id"],
        "description": "o", "default_filters": ["{alias}.deleted_at IS NULL"],
        "columns": [{"name": "id", "type": "integer", "primary_key": True},
                    {"name": "customer_id", "type": "integer"},
                    {"name": "status", "type": "string",
                     "choice_field": {"pending": "Pending", "paid": "Paid"}},
                    {"name": "region", "type": "string", "choice_field": {}},
                    {"name": "severity", "type": "integer", "choice_field": {"1": "Low", "2": "High"}},
                    {"name": "deleted_at", "type": "timestamp"},
                    {"name": "order_date", "type": "date"},
                    {"name": "total", "type": "decimal"}]}
    if orders_rows is not None:
        orders["performance_hints"] = {"estimated_row_count": orders_rows}
    (root / "subject_areas" / "s" / "tables" / "orders.yaml").write_text(yaml.safe_dump(orders))
    (root / "subject_areas" / "s" / "tables" / "order_items.yaml").write_text(yaml.safe_dump({
        "name": "order_items", "schema": "public", "storage_connection": "c0", "grain": ["id"],
        "description": "oi",
        "columns": [{"name": "id", "type": "integer", "primary_key": True},
                    {"name": "order_id", "type": "integer"}, {"name": "qty", "type": "integer"}]}))
    (root / "subject_areas" / "s" / "tables" / "customers.yaml").write_text(yaml.safe_dump({
        "name": "customers", "schema": "public", "storage_connection": "c0", "grain": ["id"],
        "description": "cu",
        "columns": [{"name": "id", "type": "integer", "primary_key": True},
                    {"name": "name", "type": "string"},
                    {"name": "email", "type": "string", "sensitive": True}]}))
    rel: dict = {"from_table": "order_items", "to_table": "orders", "relationship": "many_to_one",
                 "review_state": "approved", "confidence": "confirmed",
                 "signed_off_by": "x", "signed_off_role": "system", "signed_off_at": "t"}
    if unreadable_on:
        rel["on"] = "order_items.order_id = orders.id AND orders.tenant = :tenant"
    else:
        rel.update({"from_column": "order_id", "to_column": "id"})
    (root / "subject_areas" / "s" / "relationships.yaml").write_text(
        yaml.safe_dump({"relationships": [rel]}))


def _run(args):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = cli.main(args)
    return rc, buf.getvalue()


def _write(tmp_path: Path, name: str, text: str) -> str:
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return str(p)


# --- claims ---------------------------------------------------------------


def test_claims_names_the_window_that_differs_and_the_dialect_it_read(tmp_path):
    _model(tmp_path)
    a = _write(tmp_path, "a.sql",
               "SELECT SUM(total) FROM orders WHERE order_date >= '2025-01-01' AND order_date < '2026-01-01'")
    b = _write(tmp_path, "b.sql",
               "SELECT SUM(total) FROM orders WHERE order_date >= '2025-04-01' AND order_date < '2025-07-01'")
    rc, out = _run(["claims", str(tmp_path), "--sql-file", a, "--against-sql-file", b])
    d = json.loads(out)
    assert rc == 0, out
    by_name = {c["name"]: c["status"] for c in d["claims"]}
    assert by_name["date_window"] == "differs" and by_name["tables"] == "agrees"
    assert d["dialect"] == "postgres"
    assert d["unreadable"] == {"sql_file": None, "against_sql_file": None}


def test_claims_agree_on_a_statement_compared_with_itself(tmp_path):
    _model(tmp_path)
    a = _write(tmp_path, "a.sql", "SELECT COUNT(*) FROM orders WHERE status = 'paid'")
    rc, out = _run(["claims", str(tmp_path), "--sql-file", a, "--against-sql-file", a])
    d = json.loads(out)
    assert rc == 0, out
    by_name = {c["name"]: c["status"] for c in d["claims"]}
    # A statement with no date filter has no window to compare, and the module says `unknown`
    # rather than `agrees` for that: unresolved is not agreement any more than it is disagreement.
    assert by_name.pop("date_window") == "unknown"
    assert set(by_name.values()) == {"agrees"}, by_name


def test_claims_says_which_side_could_not_be_read(tmp_path):
    _model(tmp_path)
    a = _write(tmp_path, "a.sql", "SELECT COUNT(*) FROM orders")
    b = _write(tmp_path, "b.sql", "SELECT FROM WHERE")
    rc, out = _run(["claims", str(tmp_path), "--sql-file", a, "--against-sql-file", b])
    d = json.loads(out)
    assert rc == 0 and d["unreadable"]["sql_file"] is None and d["unreadable"]["against_sql_file"]
    assert {c["status"] for c in d["claims"]} == {"unknown"}


# --- compare-results ------------------------------------------------------


def test_compare_results_scores_one_when_the_two_tables_say_the_same_thing(tmp_path):
    _model(tmp_path)
    g = _write(tmp_path, "g.csv", "region,total\nEU,4.20\nUS,10\n")
    a = _write(tmp_path, "a.csv", "total,region\n10,US\n4.2,EU\n")
    # The person's statement is the answer key here, and it wrote no ORDER BY, so the rows are a
    # multiset. Without it the comparator has to assume an ordering, which is the right default
    # for an answer key it cannot read and the wrong one for this call.
    s = _write(tmp_path, "s.sql", "SELECT region, SUM(total) AS total FROM orders GROUP BY region")
    rc, out = _run(["compare-results", str(tmp_path), "--golden-csv", g, "--generated-csv", a,
                    "--match", "values", "--golden-sql-file", s])
    d = json.loads(out)
    # Columns are matched by VALUE, rows as a multiset, and "4.20" reads as the number 4.2: the
    # CSV wire lost the types and the reader puts them back before the comparator sees the cells.
    assert rc == 0 and d["status"] == "scored" and d["accuracy"] == 1.0, d
    assert d["order_sensitive"] is False


def test_compare_results_defaults_to_the_comparators_own_level_and_scores_zero_on_a_difference(tmp_path):
    _model(tmp_path)
    g = _write(tmp_path, "g.csv", "total\n10\n")
    a = _write(tmp_path, "a.csv", "total\n11\n")
    rc, out = _run(["compare-results", str(tmp_path), "--golden-csv", g, "--generated-csv", a])
    d = json.loads(out)
    assert rc == 0 and d["status"] == "scored" and d["accuracy"] == 0.0, d


def test_compare_results_keeps_a_padded_id_as_text(tmp_path):
    _model(tmp_path)
    g = _write(tmp_path, "g.csv", "postal\n02134\n")
    a = _write(tmp_path, "a.csv", "postal\n2134\n")
    rc, out = _run(["compare-results", str(tmp_path), "--golden-csv", g, "--generated-csv", a,
                    "--match", "values"])
    # A leading zero is a text fact. Reading `02134` as the number 2134 would make a postal code
    # equal to a different postal code.
    assert rc == 0 and json.loads(out)["accuracy"] == 0.0, out


def test_compare_results_bounded_reads_a_band(tmp_path):
    _model(tmp_path)
    g = _write(tmp_path, "g.csv", "total\n100\n")
    a = _write(tmp_path, "a.csv", "total\n100.5\n")
    rc, out = _run(["compare-results", str(tmp_path), "--golden-csv", g, "--generated-csv", a,
                    "--match", "bounded", "--bounds",
                    json.dumps({"min_rows": 1, "max_rows": 1, "min_value": 99.0, "max_value": 101.0})])
    assert rc == 0 and json.loads(out)["accuracy"] == 1.0, out


def test_compare_results_refuses_an_empty_csv_rather_than_scoring_zero_rows(tmp_path):
    _model(tmp_path)
    g = _write(tmp_path, "g.csv", "total\n100\n")
    a = _write(tmp_path, "a.csv", "")
    rc, out = _run(["compare-results", str(tmp_path), "--golden-csv", g, "--generated-csv", a])
    # The tier writes CSV only on success: an empty file is a statement that failed, not an answer
    # of zero rows, and scoring it would turn a failed run into a wrong answer.
    assert rc == 2 and json.loads(out)["error"] == "unreadable_csv"


def test_compare_results_reports_a_bad_band_as_json_not_a_traceback(tmp_path):
    _model(tmp_path)
    g = _write(tmp_path, "g.csv", "total\n100\n")
    rc, out = _run(["compare-results", str(tmp_path), "--golden-csv", g, "--generated-csv", g,
                    "--match", "bounded", "--bounds", "{}"])
    assert rc == 2 and json.loads(out)["error"] == "bad_bounds"


# --- join-probes ----------------------------------------------------------


def _joins(tmp_path: Path, sql: str) -> dict:
    s = _write(tmp_path, "s.sql", sql)
    rc, out = _run(["join-probes", str(tmp_path), "--sql-file", s])
    assert rc == 0, out
    return json.loads(out)


def test_join_probes_recognises_the_declared_join_and_emits_no_verdict(tmp_path):
    _model(tmp_path)
    d = _joins(tmp_path, "SELECT COUNT(*) FROM order_items oi JOIN orders o ON oi.order_id = o.id")
    (j,) = d["joins"]
    assert j["status"] == "declared"
    assert j["declared_between_tables"] is True and j["written_matches_declared"] is True
    assert j["pairs"] == [[["order_items", "order_id"], ["orders", "id"]]]
    assert j["probes"] == {"overlap": [], "cardinality": []}
    assert "verdict" not in j and d["joins_written"] == 1 and d["dropped"] == 0


def test_join_probes_flags_a_join_on_the_wrong_key_between_declared_tables(tmp_path):
    _model(tmp_path)
    d = _joins(tmp_path, "SELECT COUNT(*) FROM order_items oi JOIN orders o ON oi.id = o.id")
    (j,) = d["joins"]
    assert j["status"] == "wrong_key"
    assert j["declared_between_tables"] is True and j["written_matches_declared"] is False
    assert j["declared_pairs"] == [[["order_items", "order_id"], ["orders", "id"]]]
    assert j["probes"]["overlap"] == []


def test_join_probes_emits_overlap_and_cardinality_sql_for_an_undeclared_join(tmp_path):
    _model(tmp_path)
    d = _joins(tmp_path, "SELECT COUNT(*) FROM orders o JOIN customers c ON o.customer_id = c.id")
    (j,) = d["joins"]
    assert j["status"] == "undeclared" and j["declared_between_tables"] is False
    assert j["too_big_to_probe"] is False
    # `customers.id` is a declared key, so sampling IT and asking whether orders holds every value
    # proves nothing about the join: a customer with no orders is not a broken key. Only the other
    # direction tests whether the written keys resolve.
    (probe,) = j["probes"]["overlap"]
    assert probe["from"] == "orders.customer_id" and probe["into"] == "customers.id"
    assert "SELECT DISTINCT" in probe["sql"] and "LIMIT 50" in probe["sql"] and "EXISTS" in probe["sql"]
    # Cardinality is a fact about a COLUMN, emitted once per column at the top level; a declared key
    # needs no scan because the semantic model already says it is unique.
    assert j["probes"]["cardinality"] == ["customers.id", "orders.customer_id"]
    assert d["cardinality"]["customers.id"] is None and d["unique_by_model"]["customers.id"] is True
    assert "COUNT(DISTINCT" in d["cardinality"]["orders.customer_id"]


def test_join_probes_declines_to_probe_a_table_over_the_size_guard(tmp_path):
    _model(tmp_path, orders_rows=4_000_000)
    d = _joins(tmp_path, "SELECT COUNT(*) FROM orders o JOIN customers c ON o.customer_id = c.id")
    (j,) = d["joins"]
    assert j["status"] == "undeclared" and j["too_big_to_probe"] is True
    assert j["probes"] == {"overlap": [], "cardinality": []}
    assert "size guard" in j["not_probed_because"]


def test_join_probes_leaves_a_cte_shadowing_a_declared_table_open(tmp_path):
    """The statement bound `orders` to a result of its own. No declaration can be about it, and a
    probe against the real `orders` would test a table this join never read."""
    _model(tmp_path)
    d = _joins(tmp_path, "WITH orders AS (SELECT id FROM order_items) "
                         "SELECT COUNT(*) FROM order_items oi JOIN orders o ON oi.order_id = o.id")
    (j,) = d["joins"]
    assert j["status"] == "undeclarable"
    assert j["declared_between_tables"] is False and j["written_matches_declared"] is False
    assert j["probes"] == {"overlap": [], "cardinality": []}
    assert "computed" in j["not_probed_because"]


def test_join_probes_leaves_a_using_join_open_rather_than_calling_it_the_wrong_key(tmp_path):
    _model(tmp_path)
    d = _joins(tmp_path, "SELECT COUNT(*) FROM order_items JOIN orders USING (id)")
    (j,) = d["joins"]
    assert j["status"] == "undetermined" and j["written_matches_declared"] is False
    assert j["declared_between_tables"] is False


def test_join_probes_leaves_a_join_open_when_the_declaration_itself_cannot_be_read(tmp_path):
    """The semantic model declares the edge through an `on:` carrying a bind marker. That is our
    failure to read the declaration, not the person's wrong key, and the receipt says so too."""
    _model(tmp_path, unreadable_on=True)
    d = _joins(tmp_path, "SELECT COUNT(*) FROM order_items oi JOIN orders o ON oi.order_id = o.id")
    (j,) = d["joins"]
    assert j["status"] == "undetermined"
    assert "could not be read" in j["not_probed_because"]


def test_join_probes_survives_a_self_join_on_one_column(tmp_path):
    _model(tmp_path)
    d = _joins(tmp_path, "SELECT COUNT(*) FROM orders o1 JOIN orders o2 ON o1.id = o2.id")
    (j,) = d["joins"]
    assert j["status"] == "undeclared" and j["probes"]["overlap"] == []
    assert "itself" in j["not_probed_because"]


def test_join_probes_emits_nothing_when_the_datasource_has_no_single_engine(tmp_path):
    _model(tmp_path, engines=("PostgreSQL", "Snowflake"))
    d = _joins(tmp_path, "SELECT COUNT(*) FROM orders o JOIN customers c ON o.customer_id = c.id")
    (j,) = d["joins"]
    assert d["dialect"] is None and j["probes"]["overlap"] == []
    assert "engine" in j["not_probed_because"]


def test_join_probes_reads_nothing_from_an_unparseable_statement(tmp_path):
    _model(tmp_path)
    d = _joins(tmp_path, "SELECT FROM WHERE")
    assert d["joins"] == [] and d["unreadable"]


# --- the row-limit helper the probes lean on ---------------------------------


def test_limited_puts_top_after_distinct_on_sql_server():
    sql = dialects.get_dialect("SQLServer").limited('SELECT DISTINCT "region" AS v FROM "t"', 26)
    assert sql.startswith("SELECT DISTINCT TOP 26 ")
    assert dialects.get_dialect("PostgreSQL").limited("SELECT DISTINCT x FROM t", 26).endswith(" LIMIT 26")


# --- filter-values plan ---------------------------------------------------


def _plan(tmp_path: Path, sql: str) -> dict:
    s = _write(tmp_path, "s.sql", sql)
    rc, out = _run(["filter-values", "plan", str(tmp_path), "--sql-file", s])
    assert rc == 0, out
    return json.loads(out)


def test_plan_answers_a_populated_list_in_process_and_names_the_near_miss(tmp_path):
    _model(tmp_path)
    d = _plan(tmp_path, "SELECT COUNT(*) FROM orders WHERE status = 'Paid'")
    (lit,) = d["literals"]
    assert lit["table"] == "orders" and lit["column"] == "status" and lit["literal"] == "Paid"
    assert lit["column_key"] == "orders.status" and lit["op"] == "="
    assert lit["choice_field"] == "populated" and lit["in_choice_field"] is False
    # Case and whitespace fold only. 'Paid' folds to the listed 'paid'; nothing guesses further.
    assert lit["choice_near_miss"] == "paid" and lit["sensitive"] is False


def test_plan_reads_an_empty_list_as_not_yet_decoded_and_emits_probes(tmp_path):
    _model(tmp_path)
    d = _plan(tmp_path, "SELECT COUNT(*) FROM orders WHERE region = 'EU'")
    (lit,) = d["literals"]
    assert lit["choice_field"] == "empty" and lit["in_choice_field"] is None
    assert "'EU'" in lit["probes"]["exists"] and "COUNT(*)" in lit["probes"]["exists"]
    assert "LOWER" in lit["probes"]["exists_folded"] and "TRIM" in lit["probes"]["exists_folded"]
    column = d["columns"]["orders.region"]
    # 26 = ENUM_MAX_DISTINCT + 1, recorded on the plan so the judge reads the number the plan used.
    assert "SELECT DISTINCT" in column["distinct"] and "LIMIT 26" in column["distinct"]
    assert column["distinct_limit"] == 26


def test_plan_gives_each_member_of_an_in_list_its_own_row_and_one_column_probe(tmp_path):
    _model(tmp_path)
    d = _plan(tmp_path, "SELECT COUNT(*) FROM orders WHERE status IN ('pending', 'Paid', 'Hold')")
    lits = {lit["literal"]: lit for lit in d["literals"]}
    assert set(lits) == {"pending", "Paid", "Hold"}
    assert lits["pending"]["in_choice_field"] is True and lits["pending"]["op"] == "in"
    assert lits["Paid"]["in_choice_field"] is False and lits["Paid"]["choice_near_miss"] == "paid"
    assert lits["Hold"]["in_choice_field"] is False and lits["Hold"]["choice_near_miss"] is None
    # Three values, one column: the column's own probe is emitted once.
    assert list(d["columns"]) == ["orders.status"]


def test_plan_reads_a_numeric_literal_against_a_coded_column(tmp_path):
    _model(tmp_path)
    d = _plan(tmp_path, "SELECT COUNT(*) FROM orders WHERE severity = 3")
    (lit,) = d["literals"]
    assert lit["quoted"] is False and lit["literal"] == "3"
    assert lit["choice_field"] == "populated" and lit["in_choice_field"] is False
    assert lit["probes"]["exists"].endswith("= 3") and lit["probes"]["exists_folded"] is None


def test_plan_refuses_to_probe_values_of_a_sensitive_column(tmp_path):
    _model(tmp_path)
    d = _plan(tmp_path, "SELECT COUNT(*) FROM customers WHERE email = 'a@example.com'")
    (lit,) = d["literals"]
    assert lit["sensitive"] is True and lit["choice_field"] == "absent"
    # No probe may confirm or deny a private value one guess at a time, and no probe may list them.
    assert lit["probes"] == {"exists": None, "exists_folded": None}
    assert d["columns"]["customers.email"]["distinct"] is None


def test_plan_never_sends_a_value_engines_quote_differently(tmp_path):
    _model(tmp_path)
    d = _plan(tmp_path, r"SELECT COUNT(*) FROM orders WHERE region = 'a\\b'")
    (lit,) = d["literals"]
    # A backslash is an escape on some engines and a character on others; the doubled quote that
    # makes a literal safe on one is a breakout on another. The value is still checked in-process.
    assert lit["probes"] == {"exists": None, "exists_folded": None}
    assert "backslash" in lit["note"]


def test_plan_emits_only_the_existence_probe_over_the_size_guard(tmp_path):
    _model(tmp_path, orders_rows=4_000_000)
    d = _plan(tmp_path, "SELECT COUNT(*) FROM orders WHERE region = 'EU'")
    (lit,) = d["literals"]
    assert lit["too_big_to_probe"] is True
    assert lit["probes"]["exists"] is not None and lit["probes"]["exists_folded"] is None
    assert d["columns"]["orders.region"]["distinct"] is None


def test_plan_reads_literals_from_inner_join_ons_and_qualified_columns(tmp_path):
    _model(tmp_path)
    d = _plan(tmp_path,
              "SELECT COUNT(*) FROM order_items oi JOIN orders o ON oi.order_id = o.id "
              "AND o.status = 'paid' WHERE o.region <> 'EU'")
    lits = {(lit["column"], lit["literal"]): lit for lit in d["literals"]}
    assert lits[("status", "paid")]["table"] == "orders"
    assert lits[("region", "EU")]["op"] == "<>"


def test_plan_reports_a_column_it_cannot_place_rather_than_guessing(tmp_path):
    _model(tmp_path)
    d = _plan(tmp_path, "SELECT COUNT(*) FROM orders o JOIN customers c ON o.customer_id = c.id "
                        "WHERE nickname = 'x'")
    (lit,) = d["literals"]
    assert lit["table"] is None and lit["column_key"] is None
    assert lit["probes"] == {"exists": None, "exists_folded": None}
    assert "declares" in lit["note"]


def test_plan_does_not_attribute_a_ctes_column_to_the_real_table(tmp_path):
    _model(tmp_path)
    d = _plan(tmp_path, "WITH orders AS (SELECT 'x' AS status) "
                        "SELECT COUNT(*) FROM orders o WHERE o.status = 'paid'")
    (lit,) = d["literals"]
    assert lit["table"] is None and lit["in_choice_field"] is None
    assert "computed" in lit["note"] and d["columns"] == {}


def test_plan_reports_a_like_pattern_and_a_range_without_probing_them(tmp_path):
    _model(tmp_path)
    d = _plan(tmp_path, "SELECT COUNT(*) FROM orders WHERE region LIKE 'E%' "
                        "AND order_date BETWEEN '2025-01-01' AND '2025-12-31' AND status IS NULL")
    (lit,) = d["literals"]
    assert lit["pattern"] is True and lit["op"] == "like"
    assert lit["probes"] == {"exists": None, "exists_folded": None}
    assert {s["op"] for s in d["skipped"]} == {"between", "is"}


def test_plan_spells_the_distinct_probe_for_sql_server(tmp_path):
    _model(tmp_path, storage="SQLServer")
    d = _plan(tmp_path, "SELECT COUNT(*) FROM orders WHERE region = 'EU'")
    assert d["columns"]["orders.region"]["distinct"].startswith("SELECT DISTINCT TOP 26 ")


def test_plan_emits_no_probe_when_the_datasource_has_no_single_engine(tmp_path):
    _model(tmp_path, engines=("PostgreSQL", "Snowflake"))
    d = _plan(tmp_path, "SELECT COUNT(*) FROM orders WHERE region = 'EU'")
    (lit,) = d["literals"]
    assert d["dialect"] is None and lit["probes"] == {"exists": None, "exists_folded": None}
    assert "engine" in lit["note"]


# --- filter-values judge --------------------------------------------------


def _judge(tmp_path: Path, plan: dict, results: dict[str, str]) -> dict:
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps(plan))
    res = tmp_path / "res"
    res.mkdir(exist_ok=True)
    for name, text in results.items():
        (res / name).write_text(text, encoding="utf-8")
    rc, out = _run(["filter-values", "judge", str(tmp_path), "--plan", str(plan_path),
                    "--results", str(res)])
    assert rc == 0, out
    return json.loads(out)


def test_judge_confirms_a_listed_value_without_any_probe(tmp_path):
    _model(tmp_path)
    plan = _plan(tmp_path, "SELECT COUNT(*) FROM orders WHERE status = 'paid'")
    (v,) = _judge(tmp_path, plan, {})["literals"]
    assert v["tier"] == "choice_field" and v["verdict"] == "confirmed"


def test_judge_grades_a_miscased_listed_value_as_a_defect_once_the_probe_agrees(tmp_path):
    _model(tmp_path)
    plan = _plan(tmp_path, "SELECT COUNT(*) FROM orders WHERE status = 'Paid'")
    (v,) = _judge(tmp_path, plan, {"lit-1.exists.csv": "n\n0\n"})["literals"]
    assert v["verdict"] == "query_defect" and v["near_miss"] == "paid"
    assert v["rows_with_value"] == 0 and "did you mean 'paid'" in v["note"]


def test_judge_leaves_an_unlisted_value_open_until_the_probe_answers(tmp_path):
    """The list says no. Without the existence probe nothing can tell a mistake in the query from a
    list that has gone stale, so the grade stays open and names the near miss."""
    _model(tmp_path)
    plan = _plan(tmp_path, "SELECT COUNT(*) FROM orders WHERE status = 'Paid'")
    (v,) = _judge(tmp_path, plan, {})["literals"]
    assert v["verdict"] == "unresolved" and v["near_miss"] == "paid" and "stale" in v["note"]


def test_judge_calls_the_list_stale_when_the_warehouse_holds_the_value_anyway(tmp_path):
    _model(tmp_path)
    plan = _plan(tmp_path, "SELECT COUNT(*) FROM orders WHERE status = 'refunded'")
    (v,) = _judge(tmp_path, plan, {"lit-1.exists.csv": "n\n12\n"})["literals"]
    assert v["verdict"] == "model_gap" and v["rows_with_value"] == 12


def test_judge_reads_an_empty_list_as_unresolved_until_a_probe_answers(tmp_path):
    _model(tmp_path)
    plan = _plan(tmp_path, "SELECT COUNT(*) FROM orders WHERE region = 'EU'")
    (v,) = _judge(tmp_path, plan, {})["literals"]
    assert v["verdict"] == "unresolved" and v["tier"] == "none" and "no probe" in v["note"]


def test_judge_uses_the_distinct_probe_on_a_low_cardinality_column(tmp_path):
    _model(tmp_path)
    plan = _plan(tmp_path, "SELECT COUNT(*) FROM orders WHERE region = 'eu'")
    (v,) = _judge(tmp_path, plan, {
        "orders.region.distinct.csv": "v\nEU\nUS\nAPAC\n",
        "lit-1.exists.csv": "n\n0\n",
    })["literals"]
    assert v["tier"] == "distinct" and v["verdict"] == "query_defect"
    assert v["near_miss"] == "EU" and v["observed"] == ["APAC", "EU", "US"]


def test_judge_confirms_a_value_the_distinct_probe_lists(tmp_path):
    _model(tmp_path)
    plan = _plan(tmp_path, "SELECT COUNT(*) FROM orders WHERE region = 'EU'")
    (v,) = _judge(tmp_path, plan, {"orders.region.distinct.csv": "v\nEU\nUS\n"})["literals"]
    assert v["tier"] == "distinct" and v["verdict"] == "confirmed"


def test_judge_falls_back_to_the_existence_probe_when_the_distinct_probe_overflows(tmp_path):
    _model(tmp_path)
    plan = _plan(tmp_path, "SELECT COUNT(*) FROM orders WHERE region = 'EU'")
    overflow = "v\n" + "\n".join(f"r{i}" for i in range(26)) + "\n"
    (v,) = _judge(tmp_path, plan, {
        "orders.region.distinct.csv": overflow,
        "lit-1.exists.csv": "n\n7\n",
    })["literals"]
    assert v["tier"] == "exists" and v["verdict"] == "confirmed" and v["rows_with_value"] == 7
    # The overflowed list is not reported as the column's values: it is not all of them.
    assert v["observed"] is None


def test_judge_names_the_near_miss_the_folded_probe_found(tmp_path):
    _model(tmp_path)
    plan = _plan(tmp_path, "SELECT COUNT(*) FROM orders WHERE region = 'eu'")
    overflow = "v\n" + "\n".join(f"r{i}" for i in range(26)) + "\n"
    (v,) = _judge(tmp_path, plan, {
        "orders.region.distinct.csv": overflow,
        "lit-1.exists.csv": "n\n0\n",
        "lit-1.exists_folded.csv": "v,n\nEU,40\n",
    })["literals"]
    assert v["tier"] == "exists" and v["verdict"] == "query_defect" and v["near_miss"] == "EU"


def test_judge_treats_an_empty_probe_file_as_a_probe_that_failed(tmp_path):
    """The tier writes CSV only on success, so a zero-byte file is a refused or failed probe. It
    must never read as "the column holds no values"."""
    _model(tmp_path)
    plan = _plan(tmp_path, "SELECT COUNT(*) FROM orders WHERE region = 'EU'")
    (v,) = _judge(tmp_path, plan, {"orders.region.distinct.csv": "", "lit-1.exists.csv": "n\n12\n"})["literals"]
    assert v["tier"] == "exists" and v["verdict"] == "confirmed"
    (v,) = _judge(tmp_path, plan, {"lit-1.exists.csv": ""})["literals"]
    assert v["verdict"] == "unresolved" and "likely failed" in v["note"]


def test_judge_carries_the_operator_so_a_count_is_not_misread(tmp_path):
    _model(tmp_path)
    plan = _plan(tmp_path, "SELECT COUNT(*) FROM orders WHERE region NOT IN ('EU')")
    (v,) = _judge(tmp_path, plan, {"lit-1.exists.csv": "n\n40\n"})["literals"]
    # 40 rows HOLD the value; the filter excludes them. The count is named for what it is.
    assert v["op"] == "not in" and v["rows_with_value"] == 40 and v["verdict"] == "confirmed"


def test_judge_reports_a_sensitive_column_as_unresolved_and_says_why(tmp_path):
    _model(tmp_path)
    plan = _plan(tmp_path, "SELECT COUNT(*) FROM customers WHERE email = 'a@example.com'")
    (v,) = _judge(tmp_path, plan, {})["literals"]
    assert v["verdict"] == "unresolved" and "sensitive" in v["note"]


def test_judge_propagates_an_unreadable_plan_and_refuses_a_file_that_is_not_a_plan(tmp_path):
    _model(tmp_path)
    plan = _plan(tmp_path, "SELECT FROM WHERE")
    assert plan["unreadable"]
    out = _judge(tmp_path, plan, {})
    assert out["literals"] == [] and out["unreadable"] == plan["unreadable"]
    not_a_plan = tmp_path / "other.json"
    not_a_plan.write_text(json.dumps({"joins": []}))
    (tmp_path / "res").mkdir(exist_ok=True)
    rc, printed = _run(["filter-values", "judge", str(tmp_path), "--plan", str(not_a_plan),
                        "--results", str(tmp_path / "res")])
    assert rc == 2 and json.loads(printed)["error"] == "bad_plan"


def test_judge_refuses_a_results_directory_that_does_not_exist(tmp_path):
    _model(tmp_path)
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps({"literals": [], "columns": {}}))
    rc, out = _run(["filter-values", "judge", str(tmp_path), "--plan", str(plan_path),
                    "--results", str(tmp_path / "missing")])
    assert rc == 2 and json.loads(out)["error"] == "no_results_dir"
