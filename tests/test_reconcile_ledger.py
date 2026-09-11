"""`reconcile.py ledger` and `reconcile.py findings` — one grade per part of a supplied statement,
and the findings a person reads afterwards.

The ledger reads fixed filenames in a row directory: what happened when the statement ran, what
`sm prepare` and `sm receipt` said about it, what `sm join-probes` and `sm filter-values judge`
reported, and the probe CSVs the execution tier returned. It applies one set of rules and writes one
grade per part: `confirmed`, `model_gap`, `query_defect` or `unresolved`. The rules have a dependency
in them, and that is the property this file exists to hold still: a join that could not be graded
leaves the fan-out check on its aggregate `unresolved`, said out loud, never silently clean.

Every fixture below is written by the test itself, in the shapes the real verbs emit. Synthetic
throughout: a `demo` shop over `orders`, `order_items` and `customers`.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agami" / "scripts"))

import reconcile  # noqa: E402
from reconcile import findings, ledger  # noqa: E402

# --- fixture builders -------------------------------------------------------


def _write(row_dir: Path, name: str, payload) -> None:
    row_dir.mkdir(parents=True, exist_ok=True)
    text = payload if isinstance(payload, str) else json.dumps(payload)
    (row_dir / name).write_text(text, encoding="utf-8")


def _ran_ok(row_dir: Path) -> None:
    _write(row_dir, "run.json", {"status": "ok", "rule": None, "kind": None, "detail": None})


def _prepare(*aggregates: dict, unchecked: str | None = None) -> dict:
    return {"aggregates": list(aggregates), "findings": [f for a in aggregates for f in a["findings"]],
            "unchecked": unchecked}


def _agg(text: str, status: str, joins: list[str] = (), risks: list[str] = ()) -> dict:
    return {"aggregate": text, "scope": "main", "status": status, "joins": list(joins),
            "findings": [{"risk": r, "reason": "x", "triggering_joins": list(joins), "aggregate": text}
                         for r in risks]}


def _receipt(*, tables=(), joins=(), columns=()) -> dict:
    return {"tables": {"items": list(tables), "undetermined": None},
            "joins": {"items": list(joins), "undetermined": None},
            "columns": {"items": list(columns), "undetermined": None}}


def _table(ref: str, filters: list[dict] = ()) -> dict:
    return {"ref": ref, "alias": ref, "qname": f"public.{ref}", "declared": True, "scope": "main",
            "filters": list(filters)}


def _output(column: str, status: str) -> dict:
    return {"kind": "output", "column": column, "scope": "main", "status": status,
            "metric": None if status == "unmatched" else {"name": "revenue"}}


def _join_probe(a: str, ac: str, b: str, bc: str, *, declared_between: bool, matches: bool,
                too_big: bool = False) -> dict:
    (ta, ca), (tb, cb) = sorted([(a, ac), (b, bc)])
    status = "declared" if matches else ("wrong_key" if declared_between else "undeclared")
    keys = [f"{ta}.{ca}", f"{tb}.{cb}"]
    probes: dict = {"overlap": [], "cardinality": []}
    if status == "undeclared" and not too_big:
        probes = {"overlap": [{"from": keys[0], "into": keys[1], "sql": "..."},
                              {"from": keys[1], "into": keys[0], "sql": "..."}],
                  "cardinality": keys}
    return {"id": "join-1", "endpoints": [a, b], "predicate": f"{a}.{ac} = {b}.{bc}",
            "scope": "main", "status": status, "pairs": [[[ta, ca], [tb, cb]]],
            "declared_between_tables": declared_between, "written_matches_declared": matches,
            "declared_pairs": [[["order_items", "order_id"], ["orders", "id"]]] if declared_between else [],
            "too_big_to_probe": too_big, "probes": probes,
            "not_probed_because": "a table is over the size guard for probes" if too_big else None}


def _judged(verdict: str, literal: str = "Paid", column: str = "status") -> dict:
    return {"literals": [{"id": "lit-1", "table": "orders", "column": column, "literal": literal,
                          "tier": "choice_field", "verdict": verdict, "observed": None,
                          "near_miss": "paid" if verdict == "query_defect" else None,
                          "op": "=", "rows_with_value": 0, "note": "n"}]}


def _parts(result: dict) -> dict[str, dict]:
    return {row["part"]: row for row in result["rows"]}


# --- the run itself -----------------------------------------------------------


def test_a_statement_that_ran_clean_is_confirmed_on_every_part(tmp_path):
    _ran_ok(tmp_path)
    _write(tmp_path, "statement-prepare.json", _prepare(_agg("SUM(total)", "not_multiplied")))
    _write(tmp_path, "statement-receipt.json", _receipt(
        tables=[_table("orders", [{"expr": "orders.deleted_at IS NULL", "status": "applied"}])],
        columns=[_output("total", "matched")]))
    result = ledger(tmp_path)
    assert result["verdict"] == "confirmed"
    assert {row["verdict"] for row in result["rows"]} == {"confirmed"}
    assert set(_parts(result)) == {"runs", "scope", "fan_out:SUM(total)", "aggregation:SUM(total)",
                                   "default_filter:orders:orders.deleted_at IS NULL", "metric:total"}


def test_a_statement_the_database_rejected_for_a_missing_column_is_a_query_defect(tmp_path):
    _write(tmp_path, "run.json", {"status": "failed", "rule": None, "kind": "column_not_found",
                                  "detail": "d"})
    result = ledger(tmp_path)
    assert _parts(result)["runs"]["verdict"] == "query_defect"
    assert result["verdict"] == "query_defect"


def test_a_scope_refusal_is_a_finding_about_the_semantic_model_not_a_crash(tmp_path):
    _write(tmp_path, "run.json", {"status": "refused", "rule": "table_scope", "kind": None,
                                  "detail": "d"})
    parts = _parts(ledger(tmp_path))
    assert parts["scope"]["verdict"] == "model_gap" and parts["scope"]["kind"] == "scope"
    # The statement itself is not graded wrong for wanting a table nobody exposed.
    assert parts["runs"]["verdict"] == "unresolved"


def test_select_star_is_the_persons_defect(tmp_path):
    _write(tmp_path, "run.json", {"status": "refused", "rule": "select_star", "kind": None,
                                  "detail": "d"})
    assert _parts(ledger(tmp_path))["runs"]["verdict"] == "query_defect"


def test_a_connection_failure_leaves_the_run_unresolved_and_says_so(tmp_path):
    _write(tmp_path, "run.json", {"status": "failed", "rule": None, "kind": "auth", "detail": "d"})
    runs = _parts(ledger(tmp_path))["runs"]
    assert runs["verdict"] == "unresolved" and "auth" in runs["note"]


def test_a_missing_run_record_is_unresolved_never_confirmed(tmp_path):
    tmp_path.mkdir(exist_ok=True)
    runs = _parts(ledger(tmp_path))["runs"]
    assert runs["verdict"] == "unresolved" and "no run record" in runs["note"]


# --- joins ----------------------------------------------------------------------


def test_a_join_on_the_declared_key_is_confirmed_without_a_probe(tmp_path):
    _ran_ok(tmp_path)
    _write(tmp_path, "join-probes.json", {"joins": [
        _join_probe("order_items", "order_id", "orders", "id", declared_between=True, matches=True)],
        "unreadable": None})
    parts = _parts(ledger(tmp_path))
    assert parts["join:order_items-orders"]["verdict"] == "confirmed"
    # No probe was run, and none was needed; the declaration stands on its own.
    assert "join_key:order_items-orders" not in parts


def test_a_join_on_a_different_key_than_the_declared_one_is_the_persons_defect(tmp_path):
    _ran_ok(tmp_path)
    _write(tmp_path, "join-probes.json", {"joins": [
        _join_probe("order_items", "id", "orders", "id", declared_between=True, matches=False)],
        "unreadable": None})
    _write(tmp_path, "join-1.overlap.0.csv", "matched\n50\n")   # overlap does not rescue it
    row = _parts(ledger(tmp_path))["join:order_items-orders"]
    assert row["verdict"] == "query_defect"
    assert row["evidence"]["declared_pairs"] == [[["order_items", "order_id"], ["orders", "id"]]]


def test_an_undeclared_join_whose_keys_overlap_is_a_gap_in_the_semantic_model(tmp_path):
    _ran_ok(tmp_path)
    _write(tmp_path, "join-probes.json", {"joins": [
        _join_probe("orders", "customer_id", "customers", "id", declared_between=False, matches=False)],
        "unreadable": None})
    _write(tmp_path, "join-1.overlap.0.csv", "matched\n50\n")
    _write(tmp_path, "join-1.overlap.1.csv", "matched\n0\n")
    _write(tmp_path, "cardinality.customers.id.csv",
           "total,distinct_count,null_count\n1000,1000,0\n")
    _write(tmp_path, "cardinality.orders.customer_id.csv",
           "total,distinct_count,null_count\n4000,900,10\n")
    parts = _parts(ledger(tmp_path))
    assert parts["join:customers-orders"]["verdict"] == "model_gap"
    assert parts["join:customers-orders"]["kind"] == "relationship"
    assert parts["join_key:customers-orders"]["verdict"] == "confirmed"
    card = parts["cardinality:customers-orders"]
    assert card["verdict"] == "confirmed" and card["evidence"]["one_side"] == "customers.id"


def test_an_undeclared_join_whose_keys_never_meet_is_the_persons_defect(tmp_path):
    _ran_ok(tmp_path)
    _write(tmp_path, "join-probes.json", {"joins": [
        _join_probe("orders", "customer_id", "customers", "id", declared_between=False, matches=False)],
        "unreadable": None})
    _write(tmp_path, "join-1.overlap.0.csv", "matched\n0\n")
    _write(tmp_path, "join-1.overlap.1.csv", "matched\n0\n")
    parts = _parts(ledger(tmp_path))
    assert parts["join:customers-orders"]["verdict"] == "query_defect"
    assert parts["join_key:customers-orders"]["verdict"] == "query_defect"


def test_an_undeclared_join_nobody_probed_is_unresolved(tmp_path):
    _ran_ok(tmp_path)
    _write(tmp_path, "join-probes.json", {"joins": [
        _join_probe("orders", "customer_id", "customers", "id", declared_between=False, matches=False,
                    too_big=True)], "unreadable": None})
    parts = _parts(ledger(tmp_path))
    assert parts["join:customers-orders"]["verdict"] == "unresolved"
    assert "probe" in parts["join:customers-orders"]["note"]


def test_two_repeating_sides_mean_the_join_multiplies_rows(tmp_path):
    _ran_ok(tmp_path)
    _write(tmp_path, "join-probes.json", {"joins": [
        _join_probe("orders", "customer_id", "customers", "id", declared_between=False, matches=False)],
        "unreadable": None})
    _write(tmp_path, "join-1.overlap.0.csv", "matched\n50\n")
    _write(tmp_path, "cardinality.customers.id.csv",
           "total,distinct_count,null_count\n1000,700,0\n")
    _write(tmp_path, "cardinality.orders.customer_id.csv",
           "total,distinct_count,null_count\n4000,900,10\n")
    assert _parts(ledger(tmp_path))["cardinality:customers-orders"]["verdict"] == "query_defect"


# --- aggregates, and the dependency rule ------------------------------------------


def test_a_multiplied_total_over_a_confirmed_join_is_the_persons_defect(tmp_path):
    _ran_ok(tmp_path)
    _write(tmp_path, "join-probes.json", {"joins": [
        _join_probe("order_items", "order_id", "orders", "id", declared_between=True, matches=True)],
        "unreadable": None})
    _write(tmp_path, "statement-prepare.json", _prepare(
        _agg("SUM(orders.total)", "multiplied", joins=["order_items → orders"], risks=["fan_trap"])))
    row = _parts(ledger(tmp_path))["fan_out:SUM(orders.total)"]
    assert row["verdict"] == "query_defect" and "fan_trap" in row["note"]


def test_a_multiplication_the_aggregate_cannot_feel_is_confirmed_with_a_note(tmp_path):
    _ran_ok(tmp_path)
    _write(tmp_path, "join-probes.json", {"joins": [
        _join_probe("order_items", "order_id", "orders", "id", declared_between=True, matches=True)],
        "unreadable": None})
    _write(tmp_path, "statement-prepare.json", _prepare(
        _agg("COUNT(DISTINCT orders.id)", "multiplied", joins=["order_items → orders"],
             risks=["fan_out_invariant"])))
    row = _parts(ledger(tmp_path))["fan_out:COUNT(DISTINCT orders.id)"]
    assert row["verdict"] == "confirmed" and "cannot move" in row["note"]


def test_a_fan_out_check_over_an_unresolved_join_is_unresolved_never_clean(tmp_path):
    """The dependency rule. `sm prepare` had no cardinality for a join the semantic model does not
    declare and nobody probed, so its clean verdict on the total is blind, not clean."""
    _ran_ok(tmp_path)
    _write(tmp_path, "join-probes.json", {"joins": [
        _join_probe("orders", "customer_id", "customers", "id", declared_between=False, matches=False,
                    too_big=True)], "unreadable": None})
    _write(tmp_path, "statement-prepare.json", _prepare(
        _agg("SUM(orders.total)", "not_multiplied", joins=["orders → customers"])))
    row = _parts(ledger(tmp_path))["fan_out:SUM(orders.total)"]
    assert row["verdict"] == "unresolved"
    assert row["depends_on"] == ["join:customers-orders"]
    assert "customers" in row["note"]


def test_a_preflight_that_could_not_run_leaves_every_aggregate_unresolved(tmp_path):
    _ran_ok(tmp_path)
    _write(tmp_path, "statement-prepare.json", _prepare(unchecked="sqlglot is not installed"))
    _write(tmp_path, "statement-receipt.json", _receipt(columns=[_output("total", "matched")]))
    result = ledger(tmp_path)
    assert result["verdict"] == "unresolved"
    assert any(r["part"] == "fan_out:*" and r["verdict"] == "unresolved" for r in result["rows"])


def test_a_sum_over_a_column_that_cannot_be_summed_is_the_persons_defect(tmp_path):
    _ran_ok(tmp_path)
    _write(tmp_path, "statement-prepare.json", _prepare(
        _agg("SUM(rate)", "not_multiplied", risks=["bad_aggregation"])))
    assert _parts(ledger(tmp_path))["aggregation:SUM(rate)"]["verdict"] == "query_defect"


# --- filters, metrics, values -------------------------------------------------------


def test_an_omitted_declared_filter_is_a_gap_of_kind_filter(tmp_path):
    _ran_ok(tmp_path)
    _write(tmp_path, "statement-receipt.json", _receipt(
        tables=[_table("orders", [{"expr": "orders.deleted_at IS NULL", "status": "omitted"}])]))
    row = _parts(ledger(tmp_path))["default_filter:orders:orders.deleted_at IS NULL"]
    assert row["verdict"] == "model_gap" and row["kind"] == "filter"


def test_an_output_that_matches_no_metric_is_a_gap_of_kind_metric(tmp_path):
    _ran_ok(tmp_path)
    _write(tmp_path, "statement-prepare.json", _prepare(_agg("SUM(total)", "not_multiplied")))
    _write(tmp_path, "statement-receipt.json", _receipt(columns=[_output("total", "unmatched")]))
    row = _parts(ledger(tmp_path))["metric:total"]
    assert row["verdict"] == "model_gap" and row["kind"] == "metric"


def test_a_bare_count_is_exempt_from_the_metric_rule(tmp_path):
    _ran_ok(tmp_path)
    _write(tmp_path, "statement-prepare.json", _prepare(_agg("COUNT(*)", "not_multiplied")))
    _write(tmp_path, "statement-receipt.json", _receipt(columns=[_output("n", "unmatched")]))
    row = _parts(ledger(tmp_path))["metric:n"]
    assert row["verdict"] == "confirmed" and "count" in row["note"]


def test_a_typed_value_carries_the_judges_grade(tmp_path):
    _ran_ok(tmp_path)
    _write(tmp_path, "filter-values.judge.json", _judged("query_defect"))
    row = _parts(ledger(tmp_path))["literal:orders.status=Paid"]
    assert row["verdict"] == "query_defect" and row["evidence"]["near_miss"] == "paid"


def test_a_stale_list_of_values_is_a_gap_about_the_column(tmp_path):
    _ran_ok(tmp_path)
    _write(tmp_path, "filter-values.judge.json", _judged("model_gap", literal="refunded"))
    row = _parts(ledger(tmp_path))["literal:orders.status=refunded"]
    assert row["verdict"] == "model_gap" and row["kind"] == "description"


# --- claims, precedence, idempotence ---------------------------------------------


def test_claims_are_read_only_when_asked_and_a_difference_is_named_not_judged(tmp_path):
    _ran_ok(tmp_path)
    _write(tmp_path, "claims.json", {"claims": [
        {"name": "filter_predicates", "status": "differs", "generated": ["a"], "golden": ["b"]},
        {"name": "date_window", "status": "unknown", "generated": None, "golden": None},
        {"name": "tables", "status": "agrees", "generated": ["orders"], "golden": ["orders"]}],
        "gates": [], "gated": False})
    assert "predicates" not in _parts(ledger(tmp_path))
    parts = _parts(ledger(tmp_path, with_claims=True))
    assert parts["predicates"]["verdict"] == "unresolved" and parts["predicates"]["evidence"]["generated"] == ["a"]
    assert parts["date_window"]["verdict"] == "unresolved"


def test_the_weakest_part_decides_the_verdict(tmp_path):
    _ran_ok(tmp_path)
    _write(tmp_path, "statement-receipt.json", _receipt(
        tables=[_table("orders", [{"expr": "orders.deleted_at IS NULL", "status": "omitted"}])]))
    _write(tmp_path, "filter-values.judge.json", _judged("query_defect"))
    result = ledger(tmp_path)
    # model_gap and query_defect both present: the defect wins, and the counts say what else was there.
    assert result["verdict"] == "query_defect"
    assert result["counts"]["model_gap"] == 1 and result["counts"]["query_defect"] == 1


def test_the_ledger_is_the_same_when_run_twice(tmp_path):
    _ran_ok(tmp_path)
    _write(tmp_path, "statement-prepare.json", _prepare(_agg("SUM(total)", "not_multiplied")))
    _write(tmp_path, "statement-receipt.json", _receipt(columns=[_output("total", "matched")]))
    assert ledger(tmp_path) == ledger(tmp_path)


def test_the_verb_writes_ledger_json_beside_the_inputs(tmp_path, capsys):
    _ran_ok(tmp_path)
    assert reconcile.main(["ledger", "--row-dir", str(tmp_path)]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert json.loads((tmp_path / "ledger.json").read_text()) == printed


# --- findings -----------------------------------------------------------------------


def _run_dir(tmp_path: Path, rows: list[dict]) -> Path:
    run = tmp_path / "run"
    (run / "rows").mkdir(parents=True)
    with (run / "rows.jsonl").open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")
    return run


def test_the_same_missing_join_in_either_order_is_one_finding(tmp_path):
    run = _run_dir(tmp_path, [
        {"row": 1, "question": "q1", "statement": "s1", "expected": 1, "status": "mismatch"},
        {"row": 2, "question": "q2", "statement": "s2", "expected": 2, "status": "mismatch"}])
    for n, (a, ac, b, bc) in ((1, ("orders", "customer_id", "customers", "id")),
                              (2, ("customers", "id", "orders", "customer_id"))):
        d = run / "rows" / str(n)
        _ran_ok(d)
        _write(d, "join-probes.json", {"joins": [
            _join_probe(a, ac, b, bc, declared_between=False, matches=False)], "unreadable": None})
        _write(d, "join-1.overlap.0.csv", "matched\n50\n")
    out = findings(run)
    keys = [f["key"] for f in out["findings"]]
    assert keys == ["relationship:customers-orders"]
    (f,) = out["findings"]
    assert f["kind"] == "relationship" and [e["row"] for e in f["evidence"]] == [1, 2]
    assert json.loads((run / "findings.json").read_text())["findings"][0]["key"] == keys[0]


def test_a_filter_gap_and_a_metric_gap_never_share_a_key(tmp_path):
    run = _run_dir(tmp_path, [{"row": 1, "question": "q", "statement": "s", "expected": 1,
                               "status": "mismatch"}])
    d = run / "rows" / "1"
    _ran_ok(d)
    _write(d, "statement-prepare.json", _prepare(_agg("SUM(total)", "not_multiplied")))
    _write(d, "statement-receipt.json", _receipt(
        tables=[_table("orders", [{"expr": "total > 0", "status": "omitted"}])],
        columns=[_output("total", "unmatched")]))
    keys = {f["key"] for f in findings(run)["findings"]}
    assert keys == {"filter:orders:total > 0", "metric:total"}


def test_a_clean_statement_the_ai_got_wrong_is_a_finding_of_kind_example(tmp_path):
    run = _run_dir(tmp_path, [{"row": 1, "question": "What is total revenue?", "statement": "s",
                               "expected": 100, "status": "mismatch",
                               "claims": {"claims": [{"name": "filter_predicates", "status": "differs",
                                                      "generated": [], "golden": ["status <> 'cancelled'"]}]}}])
    d = run / "rows" / "1"
    _ran_ok(d)
    (f,) = findings(run)["findings"]
    assert f["kind"] == "example" and f["key"] == "example:what is total revenue?"
    assert f["evidence"][0]["claims"]["claims"][0]["name"] == "filter_predicates"


def test_the_persons_defects_are_listed_apart_from_the_findings(tmp_path):
    run = _run_dir(tmp_path, [{"row": 1, "question": "q", "statement": "s", "expected": 1,
                               "status": "expected_doubtful"}])
    d = run / "rows" / "1"
    _ran_ok(d)
    _write(d, "filter-values.judge.json", _judged("query_defect"))
    out = findings(run)
    assert out["findings"] == []
    (defect,) = out["query_defects"]
    assert defect["row"] == 1 and defect["part"] == "literal:orders.status=Paid"
    assert json.loads((run / "query_defects.json").read_text()) == out["query_defects"]
    assert set(json.loads((run / "ledger.json").read_text())) == {"1"}


def test_the_findings_verb_exits_four_when_there_is_nothing_to_read(tmp_path, capsys):
    run = tmp_path / "run"
    run.mkdir()
    assert reconcile.main(["findings", "--run-dir", str(run)]) == 4
    assert "no rows" in capsys.readouterr().err
