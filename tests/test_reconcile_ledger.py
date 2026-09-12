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


def _agg(text: str, status: str, joins: list[str] = (), risks: list[str] = (),
         reason: str | None = None) -> dict:
    return {"aggregate": text, "scope": "main", "status": status, "joins": list(joins), "reason": reason,
            "findings": [{"risk": r, "reason": "x", "triggering_joins": list(joins), "aggregate": text}
                         for r in risks]}


def _receipt(*, tables=(), joins=(), columns=()) -> dict:
    return {"tables": {"items": list(tables), "undetermined": None},
            "joins": {"items": list(joins), "undetermined": None},
            "columns": {"items": list(columns), "undetermined": None}}


def _table(ref: str, filters: list[dict] = ()) -> dict:
    return {"ref": ref, "alias": ref, "qname": f"public.{ref}", "declared": True, "scope": "main",
            "filters": list(filters)}


def _output(column: str, status: str, source_tables: list[str] | None = None) -> dict:
    # The receipt carries the matched metric flattened as `name`, `area`, `expression` and, since
    # round 3, the metric's `source_tables`.
    return {"kind": "output", "column": column, "scope": "main", "status": status,
            "name": "revenue" if status == "matched" else None,
            "source_tables": source_tables if status == "matched" else None}


def _no_joins() -> dict:
    return {"joins": [], "joins_written": 0, "dropped": 0, "cardinality": {}, "unique_by_model": {},
            "unreadable": None}


def _no_literals() -> dict:
    return {"literals": [], "unreadable": None}


def _complete(row_dir: Path) -> None:
    """Every file a successful run leaves behind, each saying there was nothing to grade."""
    _ran_ok(row_dir)
    _write(row_dir, "statement-prepare.json", _prepare())
    _write(row_dir, "statement-receipt.json", _receipt())
    _write(row_dir, "join-probes.json", _no_joins())
    _write(row_dir, "filter-values.judge.json", _no_literals())


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
    entry = {"id": "join-1", "endpoints": [a, b], "predicate": f"{a}.{ac} = {b}.{bc}",
             "scope": "main", "status": status, "pairs": [[[ta, ca], [tb, cb]]],
             "declared_between_tables": declared_between, "written_matches_declared": matches,
             "declared_pairs": [[["order_items", "order_id"], ["orders", "id"]]] if declared_between else [],
             "too_big_to_probe": too_big, "probes": probes,
             "not_probed_because": "a table is over the size guard for probes" if too_big else None,
             "declared_cardinality": [], "dropped_rows_probe": None, "dropped_rows_not_emitted_because": None}
    if declared_between:
        # The sample edge: order_items.order_id -> orders.id, many_to_one, orders the one side.
        entry["declared_cardinality"] = [{"relationship": "many_to_one", "from": "order_items",
                                         "to": "orders", "one_side": ["orders"], "matched": matches}]
    return entry


def _with_dropped_probe(join: dict) -> dict:
    left, right = join["endpoints"]
    join["dropped_rows_probe"] = {"sql": "...", "left": left, "right": right, "on": join["predicate"]}
    return join


def _probes(*joins: dict, unique: dict | None = None) -> dict:
    return {**_no_joins(), "joins": list(joins), "joins_written": len(joins),
            "unique_by_model": unique or {}}


def _judged(verdict: str, literal: str = "Paid", column: str = "status") -> dict:
    return {"literals": [{"id": "lit-1", "table": "orders", "column": column, "literal": literal,
                          "tier": "choice_field", "verdict": verdict, "observed": None,
                          "near_miss": "paid" if verdict == "query_defect" else None,
                          "op": "=", "rows_with_value": 0, "note": "n"}]}


def _parts(result: dict) -> dict[str, dict]:
    return {row["part"]: row for row in result["rows"]}


# --- the run itself -----------------------------------------------------------


def test_a_statement_that_ran_clean_is_confirmed_on_every_part(tmp_path):
    _complete(tmp_path)
    _write(tmp_path, "statement-prepare.json", _prepare(_agg("SUM(total)", "not_multiplied")))
    _write(tmp_path, "statement-receipt.json", _receipt(
        tables=[_table("orders", [{"expr": "orders.deleted_at IS NULL", "status": "applied"}])],
        columns=[_output("total", "matched")]))
    result = ledger(tmp_path)
    assert _parts(result)["metric:total"]["evidence"] == {"metric": "revenue"}
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


def test_an_undecided_aggregate_over_a_statement_with_no_join_is_confirmed(tmp_path):
    """`COUNT(*)` has no column for the pre-flight to trace, so it comes back `undetermined`. When
    `sm join-probes` has listed no join at all, nothing can multiply it, and the grade says so."""
    _ran_ok(tmp_path)
    _write(tmp_path, "join-probes.json", _no_joins())
    _write(tmp_path, "statement-prepare.json", _prepare(_agg("COUNT(*)", "undetermined")))
    row = _parts(ledger(tmp_path))["fan_out:COUNT(*)"]
    assert row["verdict"] == "confirmed" and "no join" in row["note"]


def test_an_undecided_aggregate_beside_a_written_join_stays_open(tmp_path):
    _ran_ok(tmp_path)
    _write(tmp_path, "join-probes.json", {**_no_joins(), "joins_written": 1, "joins": [
        _join_probe("orders", "id", "order_items", "order_id", declared_between=True, matches=True)]})
    _write(tmp_path, "statement-prepare.json", _prepare(_agg("COUNT(*)", "undetermined")))
    assert _parts(ledger(tmp_path))["fan_out:COUNT(*)"]["verdict"] == "unresolved"
    # A verb that could not read the statement settles nothing either.
    _write(tmp_path, "join-probes.json", {**_no_joins(), "unreadable": "the statement could not be read"})
    assert _parts(ledger(tmp_path))["fan_out:COUNT(*)"]["verdict"] == "unresolved"


def test_an_undecided_aggregate_stays_open_when_the_joins_were_never_listed(tmp_path):
    _ran_ok(tmp_path)
    _write(tmp_path, "statement-prepare.json", _prepare(_agg("COUNT(*)", "undetermined")))
    assert _parts(ledger(tmp_path))["fan_out:COUNT(*)"]["verdict"] == "unresolved"


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


def test_no_date_window_on_either_readable_side_is_not_a_disagreement(tmp_path):
    """`sm claims` reads the window as `unknown` unless both statements wrote one. Two statements
    that both parsed and neither of which filters on a date have nothing to disagree about, so the
    part is confirmed; the same `unknown` with one side unreadable stays open."""
    _ran_ok(tmp_path)
    window = {"name": "date_window", "status": "unknown", "generated": None, "golden": None}
    readable = {"sql_file": None, "against_sql_file": None}
    _write(tmp_path, "claims.json", {"claims": [window], "gates": [], "gated": False, "unreadable": readable,
                                     "temporal_predicates": {"sql_file": 0, "against_sql_file": 0}})
    row = _parts(ledger(tmp_path, with_claims=True))["date_window"]
    assert row["verdict"] == "confirmed" and "neither statement" in row["note"]
    _write(tmp_path, "claims.json", {"claims": [window], "gates": [], "gated": False,
                                     "unreadable": {"sql_file": None, "against_sql_file": "syntax"},
                                     "temporal_predicates": {"sql_file": 0, "against_sql_file": None}})
    assert _parts(ledger(tmp_path, with_claims=True))["date_window"]["verdict"] == "unresolved"


def test_a_date_filter_the_reader_could_not_fold_keeps_the_window_open(tmp_path):
    """`CURRENT_DATE - INTERVAL '30 days'` is a window the resolver does not fold, so the claim reads
    `unknown` with both sides null; the temporal count says a date filter was written, and the part
    stays open rather than reading as agreement."""
    _ran_ok(tmp_path)
    window = {"name": "date_window", "status": "unknown", "generated": None, "golden": None}
    _write(tmp_path, "claims.json", {"claims": [window], "gates": [], "gated": False,
                                     "unreadable": {"sql_file": None, "against_sql_file": None},
                                     "temporal_predicates": {"sql_file": 1, "against_sql_file": 0}})
    row = _parts(ledger(tmp_path, with_claims=True))["date_window"]
    assert row["verdict"] == "unresolved" and "does not fold" in row["note"]


def test_a_window_read_on_one_side_only_stays_open(tmp_path):
    _ran_ok(tmp_path)
    _write(tmp_path, "claims.json", {"claims": [
        {"name": "date_window", "status": "unknown", "generated": None,
         "golden": {"column": "orders.created_at", "start": "2026-01-01", "end": "2026-02-01"}}],
        "gates": [], "gated": False, "unreadable": {"sql_file": None, "against_sql_file": None}})
    assert _parts(ledger(tmp_path, with_claims=True))["date_window"]["verdict"] == "unresolved"


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
    _complete(d)
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


# --- review fixes: nothing confident from nothing -----------------------------------


def test_an_input_missing_after_a_successful_run_leaves_that_part_open(tmp_path):
    """A verb that crashed leaves no file, or a zero-byte one, or one JSON error line. Each is a part
    of the statement that was NOT checked, and the ledger says so instead of grading the rest clean."""
    _ran_ok(tmp_path)
    result = ledger(tmp_path)
    assert result["verdict"] == "unresolved"
    opened = {p: row for p, row in _parts(result).items() if p.endswith(":*")}
    assert set(opened) == {"fan_out:*", "receipt:*", "join:*", "literal:*"}
    assert all(row["evidence"]["problem"] == "was not written" for row in opened.values())
    _write(tmp_path, "join-probes.json", "")
    _write(tmp_path, "statement-prepare.json", {"error": "no_model"})
    parts = _parts(ledger(tmp_path))
    assert parts["join:*"]["evidence"]["problem"] == "carries an error (empty_file)"
    assert parts["fan_out:*"]["evidence"]["problem"] == "carries an error (no_model)"


def test_a_run_that_failed_expects_no_later_files(tmp_path):
    _write(tmp_path, "run.json", {"status": "failed", "rule": None, "kind": "timeout", "detail": None})
    assert set(_parts(ledger(tmp_path))) == {"runs"}


def test_a_complete_clean_row_has_no_open_part(tmp_path):
    _complete(tmp_path)
    result = ledger(tmp_path)
    assert result["verdict"] == "confirmed" and set(_parts(result)) == {"runs", "scope"}


def test_an_output_column_the_receipt_could_not_settle_is_open_not_a_gap(tmp_path):
    _ran_ok(tmp_path)
    _write(tmp_path, "statement-receipt.json", _receipt(columns=[_output("x", "undetermined")]))
    row = _parts(ledger(tmp_path))["metric:x"]
    assert row["verdict"] == "unresolved" and row["kind"] is None


def test_cardinality_headers_are_read_whatever_their_case(tmp_path):
    """One tier upper-cases CSV headers. Read case-sensitively, `TOTAL` fell back to the first column
    for every field and a unique key read as a repeating one."""
    _ran_ok(tmp_path)
    _write(tmp_path, "join-probes.json", {"joins": [
        _join_probe("orders", "customer_id", "customers", "id", declared_between=False, matches=False)],
        "unreadable": None})
    _write(tmp_path, "join-1.overlap.0.csv", "MATCHED\n50\n")
    _write(tmp_path, "cardinality.customers.id.csv", "TOTAL,DISTINCT_COUNT,NULL_COUNT\n1000,1000,0\n")
    _write(tmp_path, "cardinality.orders.customer_id.csv", "TOTAL,DISTINCT_COUNT,NULL_COUNT\n4000,900,10\n")
    parts = _parts(ledger(tmp_path))
    assert parts["join:customers-orders"]["verdict"] == "model_gap"
    card = parts["cardinality:customers-orders"]
    assert card["verdict"] == "confirmed" and card["evidence"]["one_side"] == "customers.id"


def test_a_failed_overlap_probe_beside_a_zero_is_not_a_defect(tmp_path):
    _ran_ok(tmp_path)
    _write(tmp_path, "join-probes.json", {"joins": [
        _join_probe("orders", "customer_id", "customers", "id", declared_between=False, matches=False)],
        "unreadable": None})
    _write(tmp_path, "join-1.overlap.0.csv", "")
    _write(tmp_path, "join-1.overlap.1.csv", "matched\n0\n")
    parts = _parts(ledger(tmp_path))
    assert parts["join:customers-orders"]["verdict"] == "unresolved"
    assert "empty" in parts["join:customers-orders"]["note"]
    assert parts["join_key:customers-orders"]["verdict"] == "unresolved"


def test_two_joins_between_the_same_tables_are_two_parts(tmp_path):
    _ran_ok(tmp_path)
    first = _join_probe("orders", "id", "order_items", "id", declared_between=True, matches=False)
    second = _join_probe("orders", "id", "order_items", "order_id", declared_between=True, matches=True)
    second["id"] = "join-2"
    _write(tmp_path, "join-probes.json", {"joins": [first, second], "unreadable": None})
    _write(tmp_path, "statement-prepare.json", _prepare(
        _agg("SUM(total)", "not_multiplied", joins=["orders - order_items"])))
    parts = _parts(ledger(tmp_path))
    assert parts["join:order_items-orders"]["verdict"] == "query_defect"
    assert parts["join:order_items-orders#2"]["verdict"] == "confirmed"
    fan = parts["fan_out:SUM(total)"]
    assert fan["verdict"] == "unresolved" and "join:order_items-orders" in fan["depends_on"]


def test_a_statement_the_verbs_could_not_read_opens_every_join_and_literal(tmp_path):
    _ran_ok(tmp_path)
    _write(tmp_path, "join-probes.json", {**_no_joins(), "unreadable": "the statement could not be read"})
    _write(tmp_path, "filter-values.judge.json", {"literals": [], "unreadable": "the statement could not be read"})
    parts = _parts(ledger(tmp_path))
    assert parts["join:*"]["verdict"] == "unresolved" and parts["literal:*"]["verdict"] == "unresolved"


def test_a_row_with_an_open_part_or_no_ledger_is_never_an_example(tmp_path):
    run = _run_dir(tmp_path, [
        {"row": 1, "question": "q1", "statement": "s1", "expected": 1, "status": "mismatch"},
        {"row": 2, "question": "q2", "statement": "s2", "expected": 2, "status": "mismatch"},
        {"row": 3, "question": "q3", "statement": None, "expected": 3, "status": "mismatch"}])
    _write(run / "rows" / "1", "run.json", {"status": "failed", "rule": None, "kind": "timeout", "detail": None})
    _ran_ok(run / "rows" / "2")  # ran, but nothing after it was written: four parts stay open
    keys = {f["key"] for f in findings(run)["findings"]}
    assert not any(k.startswith("example:") for k in keys), keys


def test_one_declared_filter_seen_through_two_aliases_is_one_finding(tmp_path):
    run = _run_dir(tmp_path, [
        {"row": 1, "question": "q1", "statement": "s1", "expected": 1, "status": "mismatch"},
        {"row": 2, "question": "q2", "statement": "s2", "expected": 2, "status": "mismatch"}])
    for n, expr in ((1, "o.status != 'cancelled'"), (2, "orders.status != 'cancelled'")):
        d = run / "rows" / str(n)
        _ran_ok(d)
        _write(d, "statement-receipt.json", _receipt(
            tables=[_table("orders", [{"expr": expr, "status": "omitted"}])]))
    keys = [f["key"] for f in findings(run)["findings"]]
    assert keys == ["filter:orders:status != 'cancelled'"]


# --- round 3: what the semantic model knows about a declared join reaches the aggregate -------


def test_a_count_through_declared_many_to_one_joins_is_confirmed(tmp_path):
    """`COUNT(*)` names no column, so the pre-flight cannot bind it and says `undetermined`. When every
    join the statement writes is the declared one and brings in one row at most (its right side is the
    one side of the relationship), nothing can multiply the count, and the ledger says so."""
    _ran_ok(tmp_path)
    join = _join_probe("order_items", "order_id", "orders", "id", declared_between=True, matches=True)
    _write(tmp_path, "join-probes.json", _probes(join, unique={"orders.id": True, "order_items.order_id": False}))
    _write(tmp_path, "statement-prepare.json", _prepare(
        _agg("COUNT(*)", "undetermined", reason="the aggregate names no column")))
    parts = _parts(ledger(tmp_path))
    assert parts["join:order_items-orders"]["evidence"]["one_row_on_right"] is True
    fan = parts["fan_out:COUNT(*)"]
    assert fan["verdict"] == "confirmed" and "one row at most" in fan["note"]
    assert fan["depends_on"] == ["join:order_items-orders"]


def test_the_one_row_stamp_also_comes_from_a_unique_written_column(tmp_path):
    """An undeclared join onto a declared key: no relationship to match, but the model says the right
    column is unique, which is the same fact."""
    _ran_ok(tmp_path)
    join = _join_probe("orders", "customer_id", "customers", "id", declared_between=False, matches=False)
    join["probes"] = {"overlap": [], "cardinality": []}
    join["status"] = "declared"
    _write(tmp_path, "join-probes.json", _probes(join, unique={"customers.id": True, "orders.customer_id": False}))
    _write(tmp_path, "statement-prepare.json", _prepare(_agg("COUNT(*)", "undetermined")))
    assert _parts(ledger(tmp_path))["fan_out:COUNT(*)"]["verdict"] == "confirmed"


def test_a_join_that_brings_the_many_side_in_leaves_the_count_open_and_names_the_cause(tmp_path):
    """`FROM orders JOIN order_items`: the right side is the many side, so each order row can become
    several. The count stays open, and the note repeats the pre-flight's reason instead of blaming the join."""
    _ran_ok(tmp_path)
    join = _join_probe("orders", "id", "order_items", "order_id", declared_between=True, matches=True)
    _write(tmp_path, "join-probes.json", _probes(join, unique={"orders.id": True, "order_items.order_id": False}))
    _write(tmp_path, "statement-prepare.json", _prepare(
        _agg("COUNT(*)", "undetermined", reason="the aggregate names no column (COUNT(*) counts rows of every joined table)")))
    parts = _parts(ledger(tmp_path))
    assert parts["join:order_items-orders"]["evidence"]["one_row_on_right"] is False
    fan = parts["fan_out:COUNT(*)"]
    assert fan["verdict"] == "unresolved"
    assert "could not bind this aggregate to one table: the aggregate names no column" in fan["note"]
    assert fan["evidence"]["reason"].startswith("the aggregate names no column")


def test_the_many_to_one_rule_needs_every_join_listed_and_confirmed(tmp_path):
    _ran_ok(tmp_path)
    good = _join_probe("order_items", "order_id", "orders", "id", declared_between=True, matches=True)
    unique = {"orders.id": True, "order_items.order_id": False}
    # A join dropped at the cap: one written join is unlisted, so nothing is known about it.
    _write(tmp_path, "join-probes.json", {**_probes(good, unique=unique), "joins_written": 2, "dropped": 1})
    _write(tmp_path, "statement-prepare.json", _prepare(_agg("COUNT(*)", "undetermined")))
    assert _parts(ledger(tmp_path))["fan_out:COUNT(*)"]["verdict"] == "unresolved"
    # A join on the wrong key beside the good one: the defect blocks the rule.
    bad = _join_probe("order_items", "id", "orders", "id", declared_between=True, matches=False)
    bad["id"] = "join-2"
    _write(tmp_path, "join-probes.json", _probes(good, bad, unique=unique))
    assert _parts(ledger(tmp_path))["fan_out:COUNT(*)"]["verdict"] == "unresolved"
    # A self-join has no right side to speak of.
    selfjoin = _join_probe("orders", "id", "orders", "id", declared_between=False, matches=False)
    selfjoin["status"] = "declared"
    selfjoin["probes"] = {"overlap": [], "cardinality": []}
    _write(tmp_path, "join-probes.json", _probes(selfjoin, unique={"orders.id": True}))
    assert _parts(ledger(tmp_path))["fan_out:COUNT(*)"]["verdict"] == "unresolved"


def test_dropped_rows_are_noted_and_never_graded(tmp_path):
    """The left table's rows with no partner: a fact the run states. It never decides the verdict,
    never blocks an example, and a probe that did not run is noted as such rather than held open."""
    _complete(tmp_path)
    join = _with_dropped_probe(
        _join_probe("order_items", "order_id", "orders", "id", declared_between=True, matches=True))
    _write(tmp_path, "join-probes.json", _probes(join, unique={"orders.id": True}))
    _write(tmp_path, "join-1.dropped_rows.csv", "total,dropped\n4000,12\n")
    result = ledger(tmp_path)
    noted = _parts(result)["dropped_rows:order_items-orders"]
    assert noted["verdict"] == "noted"
    assert noted["evidence"] == {"total": 4000, "dropped": 12, "left": "order_items", "right": "orders"}
    assert "12 of 4000 order_items rows have no orders partner" in noted["note"]
    assert result["verdict"] == "confirmed" and result["counts"]["noted"] == 1
    _write(tmp_path, "join-1.dropped_rows.csv", "")
    noted = _parts(ledger(tmp_path))["dropped_rows:order_items-orders"]
    assert noted["verdict"] == "noted" and "nothing is claimed" in noted["note"]
    _write(tmp_path, "join-1.dropped_rows.csv", "total,dropped\n4000,0\n")
    assert "no order_items row is dropped" in _parts(ledger(tmp_path))["dropped_rows:order_items-orders"]["note"]


def test_a_noted_part_does_not_stop_an_example_finding(tmp_path):
    run = _run_dir(tmp_path, [{"row": 1, "question": "How many items?", "statement": "s",
                               "expected": 100, "status": "mismatch"}])
    d = run / "rows" / "1"
    _complete(d)
    join = _with_dropped_probe(
        _join_probe("order_items", "order_id", "orders", "id", declared_between=True, matches=True))
    _write(d, "join-probes.json", _probes(join, unique={"orders.id": True}))
    _write(d, "join-1.dropped_rows.csv", "total,dropped\n4000,12\n")
    assert [f["kind"] for f in findings(run)["findings"]] == ["example"]


# --- round 3: a column nobody declared values for is one gap, said once -----------------------


def _judge_columns(**columns: dict) -> dict:
    return {"literals": [], "unreadable": None,
            "columns": {key: {"table": key.split(".")[0], "column": key.split(".")[1], "sensitive": False,
                              "observed_count": None, **fact} for key, fact in columns.items()}}


def test_an_undeclared_low_cardinality_column_is_a_gap_in_the_semantic_model(tmp_path):
    _ran_ok(tmp_path)
    judge = _judge_columns(**{"categories.slug": {"declared": "absent", "distinct": "listed", "observed_count": 8}})
    # Two literals on the column, one part about the column.
    judge["literals"] = [{"id": f"lit-{i}", "table": "categories", "column": "slug", "literal": v, "tier": "distinct",
                          "verdict": "confirmed", "observed": None, "near_miss": None, "op": "=",
                          "rows_with_value": 3, "note": "n", "declared": "absent"} for i, v in enumerate(("a", "b"), 1)]
    _write(tmp_path, "filter-values.judge.json", judge)
    parts = _parts(ledger(tmp_path))
    gap = parts["values_declared:categories.slug"]
    assert gap["verdict"] == "model_gap" and gap["kind"] == "description"
    assert "8 distinct values and the semantic model lists none" in gap["note"]
    assert sum(1 for p in parts if p.startswith("values_declared:")) == 1
    assert reconcile._finding_key(gap) == "description:categories.slug"


def test_the_values_declared_part_reads_each_state_of_the_column(tmp_path):
    _ran_ok(tmp_path)
    _write(tmp_path, "filter-values.judge.json", _judge_columns(**{
        "orders.status": {"declared": "populated", "distinct": "not_run"},
        "orders.region": {"declared": "empty", "distinct": "listed", "observed_count": 3},
        "customers.full_name": {"declared": "absent", "distinct": "overflow", "observed_count": 26},
        "orders.notes": {"declared": "absent", "distinct": "not_run"},
        "customers.email": {"declared": "absent", "distinct": "not_run", "sensitive": True},
    }))
    parts = _parts(ledger(tmp_path))
    assert parts["values_declared:orders.status"]["verdict"] == "confirmed"
    region = parts["values_declared:orders.region"]
    assert region["verdict"] == "model_gap" and "nobody decoded" in region["note"]
    assert parts["values_declared:customers.full_name"]["verdict"] == "noted"
    assert parts["values_declared:orders.notes"]["verdict"] == "unresolved"
    assert parts["values_declared:customers.email"]["verdict"] == "noted"


# --- round 3: a metric matched by shape on a table the statement never reads -----------------


def test_a_bare_aggregate_matched_to_a_metric_on_another_table_is_open(tmp_path):
    _ran_ok(tmp_path)
    _write(tmp_path, "statement-receipt.json", _receipt(
        tables=[_table("payments")], columns=[_output("total_refunds", "matched", source_tables=["refunds"])]))
    row = _parts(ledger(tmp_path))["metric:total_refunds"]
    assert row["verdict"] == "unresolved"
    assert "defined on refunds, which this statement does not read" in row["note"]
    assert row["evidence"]["tables_read"] == ["payments"]
    # On the table the metric is defined over, or with no tables named, the match stands.
    _write(tmp_path, "statement-receipt.json", _receipt(
        tables=[_table("orders")], columns=[_output("revenue", "matched", source_tables=["orders"])]))
    assert _parts(ledger(tmp_path))["metric:revenue"]["verdict"] == "confirmed"
    _write(tmp_path, "statement-receipt.json", _receipt(
        tables=[_table("payments")], columns=[_output("revenue", "matched")]))
    assert _parts(ledger(tmp_path))["metric:revenue"]["verdict"] == "confirmed"
