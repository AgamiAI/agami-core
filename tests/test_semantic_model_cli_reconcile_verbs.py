"""The four `sm` verbs that grade a statement a PERSON supplied, part by part.

`agami-reconcile` is learning to take a trusted query as evidence rather than as the answer. The
skill decides meaning and talks to the person; everything deterministic about the statement lives in
the CLI, and these are those parts:

* `claims`          — where two statements differ, in the seven claims `golden_claims` already reads
* `compare-results` — whether two result sets say the same thing, through the golden comparator
* `join-probes`     — for every join the statement wrote: is it declared, does it match the declared
                      key, and the probe SQL that would show whether the keys really resolve
* `filter-values`   — for every value typed into a filter: is it a value the column holds, answered
                      from the semantic model's own list when it has one and from a probe otherwise

None of the four runs a probe. They emit SQL; the skill runs it through the same execution tier every
query takes, and `filter-values judge` reads what came back. Synthetic fixtures throughout: a `demo`
shop over `orders`, `order_items` and `customers`.
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

from semantic_model import cli  # noqa: E402


def _model(root: Path, *, orders_rows: int | None = None) -> None:
    """A three-table shop. `orders.status` carries a populated list of values, `orders.region` an
    EMPTY one (the not-yet-decoded state), `customers.email` is sensitive, and only the
    order_items → orders join is declared."""
    (root / "datasources" / "c").mkdir(parents=True)
    (root / "subject_areas" / "s" / "tables").mkdir(parents=True)
    (root / "datasource.yaml").write_text(yaml.safe_dump({
        "datasource": "demo", "version": 1,
        "storage_connections": [{"name": "c", "ref": "datasources/c/storage.yaml"}],
        "subject_areas": ["subject_areas/s"]}))
    (root / "datasources" / "c" / "storage.yaml").write_text(
        yaml.safe_dump({"name": "c", "storage_type": "PostgreSQL"}))
    (root / "subject_areas" / "s" / "subject_area.yaml").write_text(yaml.safe_dump({
        "name": "s", "tables": [
            {"storage_connection": "c", "schema": "public", "table": "orders"},
            {"storage_connection": "c", "schema": "public", "table": "order_items"},
            {"storage_connection": "c", "schema": "public", "table": "customers"}]}))
    orders: dict = {
        "name": "orders", "schema": "public", "storage_connection": "c", "grain": ["id"],
        "description": "o", "default_filters": ["{alias}.deleted_at IS NULL"],
        "columns": [{"name": "id", "type": "integer", "primary_key": True},
                    {"name": "customer_id", "type": "integer"},
                    {"name": "status", "type": "string",
                     "choice_field": {"pending": "Pending", "paid": "Paid"}},
                    {"name": "region", "type": "string", "choice_field": {}},
                    {"name": "deleted_at", "type": "timestamp"},
                    {"name": "order_date", "type": "date"},
                    {"name": "total", "type": "decimal"}]}
    if orders_rows is not None:
        orders["performance_hints"] = {"estimated_row_count": orders_rows}
    (root / "subject_areas" / "s" / "tables" / "orders.yaml").write_text(yaml.safe_dump(orders))
    (root / "subject_areas" / "s" / "tables" / "order_items.yaml").write_text(yaml.safe_dump({
        "name": "order_items", "schema": "public", "storage_connection": "c", "grain": ["id"],
        "description": "oi",
        "columns": [{"name": "id", "type": "integer", "primary_key": True},
                    {"name": "order_id", "type": "integer"}, {"name": "qty", "type": "integer"}]}))
    (root / "subject_areas" / "s" / "tables" / "customers.yaml").write_text(yaml.safe_dump({
        "name": "customers", "schema": "public", "storage_connection": "c", "grain": ["id"],
        "description": "cu",
        "columns": [{"name": "id", "type": "integer", "primary_key": True},
                    {"name": "name", "type": "string"},
                    {"name": "email", "type": "string", "sensitive": True}]}))
    (root / "subject_areas" / "s" / "relationships.yaml").write_text(yaml.safe_dump({
        "relationships": [{"from_table": "order_items", "from_column": "order_id",
                           "to_table": "orders", "to_column": "id", "relationship": "many_to_one",
                           "review_state": "approved", "confidence": "confirmed",
                           "signed_off_by": "x", "signed_off_role": "system",
                           "signed_off_at": "t"}]}))


def _run(args):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = cli.main(args)
    return rc, buf.getvalue()


def _sql(tmp_path: Path, name: str, text: str) -> str:
    p = tmp_path / name
    p.write_text(text)
    return str(p)


# --- claims ---------------------------------------------------------------


def test_claims_names_the_window_that_differs_and_the_dialect_it_read(tmp_path):
    _model(tmp_path)
    a = _sql(tmp_path, "a.sql",
             "SELECT SUM(total) FROM orders WHERE order_date >= '2025-01-01' AND order_date < '2026-01-01'")
    b = _sql(tmp_path, "b.sql",
             "SELECT SUM(total) FROM orders WHERE order_date >= '2025-04-01' AND order_date < '2025-07-01'")
    rc, out = _run(["claims", str(tmp_path), "--sql-file", a, "--against-sql-file", b])
    d = json.loads(out)
    assert rc == 0, out
    by_name = {c["name"]: c["status"] for c in d["claims"]}
    assert by_name["date_window"] == "differs"
    assert by_name["tables"] == "agrees"
    assert d["dialect"] == "postgres"


def test_claims_agree_on_a_statement_compared_with_itself(tmp_path):
    _model(tmp_path)
    a = _sql(tmp_path, "a.sql", "SELECT COUNT(*) FROM orders WHERE status = 'paid'")
    rc, out = _run(["claims", str(tmp_path), "--sql-file", a, "--against-sql-file", a])
    d = json.loads(out)
    assert rc == 0, out
    by_name = {c["name"]: c["status"] for c in d["claims"]}
    # A statement with no date filter has no window to compare, and the module says `unknown`
    # rather than `agrees` for that: unresolved is not agreement any more than it is disagreement.
    assert by_name.pop("date_window") == "unknown"
    assert set(by_name.values()) == {"agrees"}, by_name


# --- compare-results ------------------------------------------------------


def _csv(tmp_path: Path, name: str, text: str) -> str:
    p = tmp_path / name
    p.write_text(text)
    return str(p)


def test_compare_results_scores_one_when_the_two_tables_say_the_same_thing(tmp_path):
    _model(tmp_path)
    g = _csv(tmp_path, "g.csv", "region,total\nEU,4.20\nUS,10\n")
    a = _csv(tmp_path, "a.csv", "total,region\n10,US\n4.2,EU\n")
    # The person's statement is the answer key here, and it wrote no ORDER BY — so the rows are a
    # multiset. Without it the comparator has to assume an ordering, which is the right default
    # for an answer key it cannot read and the wrong one for this call.
    s = _sql(tmp_path, "s.sql", "SELECT region, SUM(total) AS total FROM orders GROUP BY region")
    rc, out = _run(["compare-results", str(tmp_path), "--golden-csv", g, "--generated-csv", a,
                    "--match", "values", "--golden-sql-file", s])
    d = json.loads(out)
    # Columns are matched by VALUE, rows as a multiset, and "4.20" reads as the number 4.2 — the
    # CSV wire lost the types and the verb puts them back before the comparator sees the cells.
    assert rc == 0 and d["status"] == "scored" and d["accuracy"] == 1.0, d
    assert d["order_sensitive"] is False


def test_compare_results_scores_zero_when_a_value_differs(tmp_path):
    _model(tmp_path)
    g = _csv(tmp_path, "g.csv", "total\n10\n")
    a = _csv(tmp_path, "a.csv", "total\n11\n")
    rc, out = _run(["compare-results", str(tmp_path), "--golden-csv", g, "--generated-csv", a])
    d = json.loads(out)
    assert rc == 0 and d["status"] == "scored" and d["accuracy"] == 0.0, d


def test_compare_results_bounded_reads_a_band(tmp_path):
    _model(tmp_path)
    g = _csv(tmp_path, "g.csv", "total\n100\n")
    a = _csv(tmp_path, "a.csv", "total\n100.5\n")
    rc, out = _run(["compare-results", str(tmp_path), "--golden-csv", g, "--generated-csv", a,
                    "--match", "bounded", "--bounds",
                    json.dumps({"min_rows": 1, "max_rows": 1, "min_value": 99.0, "max_value": 101.0})])
    d = json.loads(out)
    assert rc == 0 and d["accuracy"] == 1.0, d


# --- join-probes ----------------------------------------------------------


def test_join_probes_recognises_the_declared_join_and_emits_no_verdict(tmp_path):
    _model(tmp_path)
    s = _sql(tmp_path, "s.sql",
             "SELECT COUNT(*) FROM order_items oi JOIN orders o ON oi.order_id = o.id")
    rc, out = _run(["join-probes", str(tmp_path), "--sql-file", s])
    d = json.loads(out)
    assert rc == 0, out
    (j,) = d["joins"]
    assert j["declared_between_tables"] is True
    assert j["written_matches_declared"] is True
    assert j["pair"] == [["order_items", "order_id"], ["orders", "id"]]
    # The verb describes and emits probes; the ledger grades. No verdict key here.
    assert "verdict" not in j


def test_join_probes_flags_a_join_on_the_wrong_key_between_declared_tables(tmp_path):
    _model(tmp_path)
    s = _sql(tmp_path, "s.sql",
             "SELECT COUNT(*) FROM order_items oi JOIN orders o ON oi.id = o.id")
    rc, out = _run(["join-probes", str(tmp_path), "--sql-file", s])
    (j,) = json.loads(out)["joins"]
    assert j["declared_between_tables"] is True and j["written_matches_declared"] is False
    assert j["declared_pairs"] == [[["order_items", "order_id"], ["orders", "id"]]]


def test_join_probes_emits_overlap_and_cardinality_sql_for_an_undeclared_join(tmp_path):
    _model(tmp_path)
    s = _sql(tmp_path, "s.sql",
             "SELECT COUNT(*) FROM orders o JOIN customers c ON o.customer_id = c.id")
    rc, out = _run(["join-probes", str(tmp_path), "--sql-file", s])
    (j,) = json.loads(out)["joins"]
    assert j["declared_between_tables"] is False and j["written_matches_declared"] is False
    assert j["too_big_to_probe"] is False
    probes = j["probes"]
    # The overlap probe is the one introspection already trusts: 50 sampled distinct values from
    # one side, counted against the other. Both directions, because the statement does not say
    # which side is the key.
    assert len(probes["overlap"]) == 2
    for p in probes["overlap"]:
        assert "SELECT DISTINCT" in p["sql"] and "LIMIT 50" in p["sql"] and "EXISTS" in p["sql"]
    assert {p["from"] for p in probes["overlap"]} == {"orders.customer_id", "customers.id"}
    # One cardinality probe per endpoint, in the dialect's own count-distinct shape.
    assert set(probes["cardinality"]) == {"orders.customer_id", "customers.id"}
    assert "COUNT(DISTINCT" in probes["cardinality"]["customers.id"]


def test_join_probes_declines_to_probe_a_table_over_the_size_guard(tmp_path):
    _model(tmp_path, orders_rows=4_000_000)
    s = _sql(tmp_path, "s.sql",
             "SELECT COUNT(*) FROM orders o JOIN customers c ON o.customer_id = c.id")
    rc, out = _run(["join-probes", str(tmp_path), "--sql-file", s])
    (j,) = json.loads(out)["joins"]
    assert j["too_big_to_probe"] is True
    assert j["probes"]["overlap"] == [] and j["probes"]["cardinality"] == {}


def test_join_probes_reads_nothing_from_an_unparseable_statement(tmp_path):
    _model(tmp_path)
    s = _sql(tmp_path, "s.sql", "SELECT FROM WHERE")
    rc, out = _run(["join-probes", str(tmp_path), "--sql-file", s])
    d = json.loads(out)
    assert rc == 0 and d["joins"] == [] and d["unreadable"], d


# --- filter-values plan ---------------------------------------------------


def _plan(tmp_path: Path, sql: str) -> dict:
    s = _sql(tmp_path, "s.sql", sql)
    rc, out = _run(["filter-values", "plan", str(tmp_path), "--sql-file", s])
    assert rc == 0, out
    return json.loads(out)


def test_plan_answers_a_populated_list_in_process_and_names_the_near_miss(tmp_path):
    _model(tmp_path)
    d = _plan(tmp_path, "SELECT COUNT(*) FROM orders WHERE status = 'Paid'")
    (lit,) = d["literals"]
    assert lit["table"] == "orders" and lit["column"] == "status" and lit["literal"] == "Paid"
    assert lit["choice_field"] == "populated"
    assert lit["in_choice_field"] is False
    # Case and whitespace fold only. 'Paid' folds to the listed 'paid'; nothing guesses further.
    assert lit["choice_near_miss"] == "paid"
    assert lit["sensitive"] is False


def test_plan_reads_an_empty_list_as_not_yet_decoded_and_emits_probes(tmp_path):
    _model(tmp_path)
    d = _plan(tmp_path, "SELECT COUNT(*) FROM orders WHERE region = 'EU'")
    (lit,) = d["literals"]
    assert lit["choice_field"] == "empty" and lit["in_choice_field"] is None
    probes = lit["probes"]
    assert "COUNT(DISTINCT" in probes["cardinality"]
    # 26 = ENUM_MAX_DISTINCT + 1, so an overflow is visible as a 26th row.
    assert "SELECT DISTINCT" in probes["distinct"] and "LIMIT 26" in probes["distinct"]
    assert "'EU'" in probes["exists"] and "COUNT(*)" in probes["exists"]
    assert "LOWER" in probes["exists_folded"] and "TRIM" in probes["exists_folded"]


def test_plan_gives_each_member_of_an_in_list_its_own_row(tmp_path):
    _model(tmp_path)
    d = _plan(tmp_path, "SELECT COUNT(*) FROM orders WHERE status IN ('pending', 'Paid', 'Hold')")
    lits = {lit["literal"]: lit for lit in d["literals"]}
    assert set(lits) == {"pending", "Paid", "Hold"}
    assert lits["pending"]["in_choice_field"] is True
    assert lits["Paid"]["in_choice_field"] is False and lits["Paid"]["choice_near_miss"] == "paid"
    assert lits["Hold"]["in_choice_field"] is False and lits["Hold"]["choice_near_miss"] is None


def test_plan_refuses_to_probe_values_of_a_sensitive_column(tmp_path):
    _model(tmp_path)
    d = _plan(tmp_path, "SELECT COUNT(*) FROM customers WHERE email = 'a@example.com'")
    (lit,) = d["literals"]
    assert lit["sensitive"] is True and lit["choice_field"] == "absent"
    # No probe may confirm or deny a private value one guess at a time. Only the count of
    # distinct values, which names nothing, is still emitted.
    assert lit["probes"]["distinct"] is None and lit["probes"]["exists"] is None
    assert lit["probes"]["exists_folded"] is None
    assert lit["probes"]["cardinality"] is not None


def test_plan_reads_literals_from_inner_join_ons_and_qualified_columns(tmp_path):
    _model(tmp_path)
    d = _plan(tmp_path,
              "SELECT COUNT(*) FROM order_items oi JOIN orders o ON oi.order_id = o.id "
              "AND o.status = 'paid' WHERE o.region <> 'EU'")
    lits = {(lit["column"], lit["literal"]): lit for lit in d["literals"]}
    assert ("status", "paid") in lits and ("region", "EU") in lits
    assert lits[("status", "paid")]["table"] == "orders"


def test_plan_reports_a_column_it_cannot_place_rather_than_guessing(tmp_path):
    _model(tmp_path)
    d = _plan(tmp_path,
              "SELECT COUNT(*) FROM orders o JOIN customers c ON o.customer_id = c.id "
              "WHERE nickname = 'x'")
    (lit,) = d["literals"]
    assert lit["table"] is None and lit["column"] == "nickname"
    assert lit["choice_field"] == "absent" and lit["probes"] == {}


# --- filter-values judge --------------------------------------------------


def _judge(tmp_path: Path, plan: dict, results: dict[str, str]) -> dict:
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps(plan))
    res = tmp_path / "res"
    res.mkdir(exist_ok=True)
    for name, text in results.items():
        (res / name).write_text(text)
    rc, out = _run(["filter-values", "judge", str(tmp_path), "--plan", str(plan_path),
                    "--results", str(res)])
    assert rc == 0, out
    return json.loads(out)


def test_judge_confirms_a_listed_value_without_any_probe(tmp_path):
    _model(tmp_path)
    plan = _plan(tmp_path, "SELECT COUNT(*) FROM orders WHERE status = 'paid'")
    (v,) = _judge(tmp_path, plan, {})["literals"]
    assert v["tier"] == "choice_field" and v["verdict"] == "confirmed"


def test_judge_grades_a_miscased_listed_value_as_a_defect_naming_the_near_miss(tmp_path):
    _model(tmp_path)
    plan = _plan(tmp_path, "SELECT COUNT(*) FROM orders WHERE status = 'Paid'")
    lit_id = plan["literals"][0]["id"]
    (v,) = _judge(tmp_path, plan, {f"{lit_id}.exists.csv": "n\n0\n"})["literals"]
    assert v["verdict"] == "query_defect" and v["near_miss"] == "paid"
    assert v["rows_contributed"] == 0


def test_judge_calls_the_list_stale_when_the_warehouse_holds_the_value_anyway(tmp_path):
    _model(tmp_path)
    plan = _plan(tmp_path, "SELECT COUNT(*) FROM orders WHERE status = 'refunded'")
    lit_id = plan["literals"][0]["id"]
    (v,) = _judge(tmp_path, plan, {f"{lit_id}.exists.csv": "n\n12\n"})["literals"]
    # The list says no, the data says yes: that is a gap in the semantic model, not in the query.
    assert v["verdict"] == "model_gap" and v["rows_contributed"] == 12


def test_judge_reads_an_empty_list_as_unresolved_until_a_probe_answers(tmp_path):
    _model(tmp_path)
    plan = _plan(tmp_path, "SELECT COUNT(*) FROM orders WHERE region = 'EU'")
    (v,) = _judge(tmp_path, plan, {})["literals"]
    assert v["verdict"] == "unresolved" and v["tier"] == "none"
    assert "no probe" in v["note"]


def test_judge_uses_the_distinct_probe_on_a_low_cardinality_column(tmp_path):
    _model(tmp_path)
    plan = _plan(tmp_path, "SELECT COUNT(*) FROM orders WHERE region = 'eu'")
    lit_id = plan["literals"][0]["id"]
    (v,) = _judge(tmp_path, plan, {
        f"{lit_id}.distinct.csv": "v\nEU\nUS\nAPAC\n",
        f"{lit_id}.exists.csv": "n\n0\n",
    })["literals"]
    assert v["tier"] == "distinct" and v["verdict"] == "query_defect"
    assert v["near_miss"] == "EU" and v["observed"] == ["APAC", "EU", "US"]


def test_judge_falls_back_to_the_existence_probe_when_the_distinct_probe_overflows(tmp_path):
    _model(tmp_path)
    plan = _plan(tmp_path, "SELECT COUNT(*) FROM orders WHERE region = 'EU'")
    lit_id = plan["literals"][0]["id"]
    overflow = "v\n" + "\n".join(f"r{i}" for i in range(26)) + "\n"
    (v,) = _judge(tmp_path, plan, {
        f"{lit_id}.distinct.csv": overflow,
        f"{lit_id}.exists.csv": "n\n7\n",
    })["literals"]
    assert v["tier"] == "exists" and v["verdict"] == "confirmed" and v["rows_contributed"] == 7
    # The overflowed list is not reported as the column's values: it is not all of them.
    assert v["observed"] is None


def test_judge_reports_a_sensitive_column_as_unresolved_and_says_why(tmp_path):
    _model(tmp_path)
    plan = _plan(tmp_path, "SELECT COUNT(*) FROM customers WHERE email = 'a@example.com'")
    (v,) = _judge(tmp_path, plan, {})["literals"]
    assert v["verdict"] == "unresolved" and "sensitive" in v["note"]
