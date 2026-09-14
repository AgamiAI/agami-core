"""The report page's items come from the run's own files: one diff row per check, the differing
tokens named, the owner and the sentence templated. Nothing on the card is written by hand."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agami" / "scripts"))

import reconcile  # noqa: E402


def _part(part, verdict, evidence=None, note="", kind=None):
    return {"part": part, "verdict": verdict, "kind": kind, "depends_on": [], "evidence": evidence or {}, "note": note}


def _run(tmp_path: Path, records: list[dict]) -> Path:
    run = tmp_path / "20260913-101500"
    (run / "rows").mkdir(parents=True)
    (run / "rows.jsonl").write_text("".join(json.dumps(r) + "\n" for r in records))
    return run


SCALAR_MATCH = {"row": 1, "label": "Q3 revenue", "question": "What was total revenue in Q3 2025?", "expected": 412380.0, "actual": 412380.0,
    "delta_pct": 0.0, "match": True, "status": "match", "recorded": {"columns": ["revenue"], "rows": [[412380.0]]},
    "statement": "SELECT SUM(total_amount) AS revenue FROM orders WHERE status <> 'cancelled'",
    "statement_recorded": {"columns": ["revenue"], "rows": [[412380.0]]}, "provenance": {"shape": "d", "source": "the finance dashboard", "file": "tiles.csv", "line": 2},
    "ledger": {"rows": [_part("runs", "confirmed"), _part("default_filter:orders:status <> 'cancelled'", "confirmed"),
                        _part("metric:revenue", "confirmed", {"metric": "revenue"}), _part("question_fit", "confirmed", {"fit": "plausible"})],
               "verdict": "confirmed", "counts": {}}, "ledger_verdict": "confirmed", "comparison": {"scalar": {"match": True}},
    "claims": {"claims": [{"name": "tables", "status": "agrees", "generated": ["orders"], "golden": ["orders"]},
                          {"name": "limit", "status": "agrees", "generated": None, "golden": None}]}, "report_path": "rows/1/receipt.html"}

TABLE_DIFF = {"row": 2, "label": "Orders since June", "question": "List all orders placed from June this year, with their status, channel and region.",
    "expected": None, "actual": None, "delta_pct": None, "match": False, "status": "mismatch",
    "recorded": {"columns": ["number", "status", "region", "region_code", "placed_at"], "rows": []},
    "statement": "SELECT ...", "statement_recorded": {"columns": ["number", "status", "planned_ship_date", "placed_at", "delivered_at", "channel", "region", "region_code"], "row_count": 21},
    "provenance": {"shape": "b", "source": "your validation sheet", "file": "plan.csv", "line": 3},
    "ledger": {"rows": [_part("runs", "confirmed"), _part("question_fit", "confirmed", {"fit": "plausible"}),
                        _part("date_window", "unresolved", {"status": "unknown"}, note="a shape the claims reader does not fold"),
                        _part("prose:orders", "noted", {"prose": [{"about": "orders", "text": "shipped_at is the time anchor for order reporting."}]})],
               "verdict": "unresolved", "counts": {}}, "ledger_verdict": "unresolved",
    "comparison": {"result_set": {"accuracy": 1.0, "reason": None, "unmatched_golden_columns": ["planned_ship_date", "delivered_at", "channel"],
                                  "golden_row_count": 21, "generated_row_count": 21, "order_sensitive": False}},
    "claims": {"claims": [{"name": "tables", "status": "agrees", "generated": ["orders"], "golden": ["orders"]},
                          {"name": "filter_predicates", "status": "agrees", "generated": ["placed_at"], "golden": ["placed_at"]},
                          {"name": "date_window", "status": "unknown", "generated": {"column": "placed_at", "start": "2025-06-01", "start_inclusive": True, "end": None, "end_inclusive": False}, "golden": None}]}}

DEFECT = {"row": 3, "label": "Delivered share", "question": "What share of orders were delivered?", "expected": 0.0, "actual": 0.834, "delta_pct": None,
    "match": False, "status": "expected_doubtful", "recorded": {"columns": ["share"], "rows": [[0.834]]}, "statement": "SELECT ...",
    "statement_recorded": {"columns": ["share"], "rows": [[0.0]]}, "provenance": {"shape": "d", "source": "the finance dashboard", "file": "tiles.csv", "line": 4},
    "ledger": {"rows": [_part("runs", "confirmed"), _part("literal:orders.status='Delivered'", "query_defect", {"near_miss": "delivered"}, note="the data spells it delivered")],
               "verdict": "query_defect", "counts": {}}, "ledger_verdict": "query_defect", "comparison": {"scalar": {"match": False}}}

ERROR = {"row": 4, "label": None, "question": "Which category sold the most?", "expected": None, "actual": None, "status": "error", "error": "Could not extract a single scalar from the result.",
         "provenance": {"shape": "a", "file": "questions.txt", "line": 1}}


def test_a_scalar_match_becomes_held_rows_a_keep_owner_and_a_one_line_sentence(tmp_path):
    items = reconcile.report_items(_run(tmp_path, [SCALAR_MATCH]))
    item = items[0]
    keys = [r["key"] for r in item["diff"]]
    assert keys[0] == "answer" and item["diff"][0]["yours"] == "412,380" and item["diff"][0]["agami"] == "412,380"
    assert "default filter on orders" in keys and "metric revenue" in keys and "answers the question" in keys
    assert "limit" not in keys and "tables read" in keys  # an empty, agreeing claim is noise; a real one is a row
    assert {r["state"] for r in item["diff"]} == {"held"}
    assert item["owner"] == "keep" and item["single_cell"] is True and item["expected"] == "412,380"
    assert item["sentence"] == "The numbers match and every check passed."
    assert item["source"] == "the finance dashboard, tiles.csv:2, a number with the SQL behind it"


def test_a_table_that_differs_in_columns_names_the_extra_columns_and_blames_the_question(tmp_path):
    item = reconcile.report_items(_run(tmp_path, [TABLE_DIFF]))[0]
    rows = {r["key"]: r for r in item["diff"]}
    assert rows["rows"]["state"] == "held" and rows["rows"]["yours"] == "21 rows" and rows["rows"]["agami"] == "21 rows"
    assert rows["columns"]["state"] == "defect" and rows["columns"]["yours_hi"] == ["planned_ship_date", "delivered_at", "channel"] and rows["columns"].get("agami_hi") in (None, [])
    assert rows["values"]["state"] == "held" and rows["values"]["yours"] == "identical, row for row"
    assert rows["date window"]["state"] == "open" and rows["date window"]["agami"] == "placed_at ≥ 2025-06-01" and rows["date window"]["yours"] == "could not read"
    assert rows["date window"]["note"] == "a shape the claims reader does not fold"
    assert rows["caveats read"]["state"] == "noted" and item["words"] == ['orders: "shipped_at is the time anchor for order reporting."']
    # The same rows come back; only the columns the person's query returns differ: the query is what to change.
    assert item["owner"] == "you" and item["single_cell"] is False and item["expected"] == "21 rows"
    assert item["change"] == ["Your query returns columns the question did not ask for: planned_ship_date, delivered_at, channel. Remove them, or name them in the question."]
    assert rows["values"]["state"] == "held" and rows["values"]["yours"] == "identical, row for row"
    assert item["sentence"].startswith("The two answers do not match. What differs: columns")


def test_a_mistake_in_the_query_is_the_persons_and_the_near_miss_is_said(tmp_path):
    item = reconcile.report_items(_run(tmp_path, [DEFECT]))[0]
    rows = {r["key"]: r for r in item["diff"]}
    assert rows["answer"]["state"] == "defect" and rows["answer"]["yours_hi"] == ["0"] and rows["answer"]["agami_hi"] == ["0.83"]
    lit = rows["value orders.status='Delivered'"]
    assert lit["state"] == "defect" and lit["yours"] == "matches no rows; the data spells it delivered" and lit["note"] == "the data spells it delivered"
    assert item["owner"] == "you" and item["change"] == ["Fix your query: value orders.status='Delivered'. Then run this row again."]
    assert "so the number you expected is doubtful" in item["sentence"]


def test_an_error_row_and_the_cli(tmp_path, capsys):
    run = _run(tmp_path, [SCALAR_MATCH, ERROR])
    assert reconcile.main(["report-items", "--run-dir", str(run)]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["items"] == 2 and printed["by_status"] == {"match": 1, "error": 1} and printed["layout"] == "cards"
    items = json.loads((run / "report-items.json").read_text())
    err = items[1]
    assert err["diff"][0] == {"key": "answer", "state": "open", "yours": None, "agami": "failed", "note": "Could not extract a single scalar from the result."}
    assert err["owner"] == "agami" and err["question"] == "Which category sold the most?"
    (run / "rows.jsonl").unlink()
    assert reconcile.main(["report-items", "--run-dir", str(run)]) == 4
    assert reconcile.main(["report-items", "--run-dir", str(tmp_path / "nope")]) == 2



GAPS_AND_GRAIN = {"row": 5, "label": "Open requests", "question": "Show me all requests open from August that are still waiting on approval.",
    "expected": None, "actual": None, "match": False, "status": "mismatch", "statement": "SELECT r.number FROM requests r JOIN request_items i ON r.id = i.request_id WHERE r.state = 'Work in Progress' AND i.approval = 'Requested'",
    "sql": "SELECT i.request FROM request_items i WHERE i.approval = 'Requested' AND i.state NOT LIKE 'Closed%'",
    "recorded": {"columns": ["request"], "rows": []}, "statement_recorded": {"columns": ["number"], "row_count": 55},
    "provenance": {"shape": "b", "source": "your validation sheet", "file": "plan.csv", "line": 4},
    "ledger": {"rows": [_part("runs", "confirmed"), _part("join:request_items-requests", "confirmed"),
                        _part("question_fit", "unresolved", {"fit": "doubtful"}, note="the statement restricts the parent request to one state, a narrower notion of open than the question's"),
                        _part("values_declared:requests.state", "model_gap", {"declared": "absent", "distinct": "listed"}, note="the column holds 6 distinct values and the semantic model lists none of them", kind="description"),
                        _part("values_declared:request_items.approval", "model_gap", {"declared": "absent", "distinct": "listed"}, note="the column holds 4 distinct values and the semantic model lists none of them", kind="description")],
               "verdict": "model_gap", "counts": {}}, "ledger_verdict": "model_gap",
    "comparison": {"result_set": {"accuracy": 0.5, "reason": "no generated column carries the values of: number", "unmatched_golden_columns": ["number"], "golden_row_count": 55, "generated_row_count": 55}},
    "claims": {"claims": [{"name": "tables", "status": "differs", "generated": ["request_items"], "golden": ["request_items", "requests"]},
                          {"name": "filter_predicates", "status": "differs", "generated": ["eq(request_items.approval, 'Requested')", "not(like(request_items.state, 'Closed%'))"],
                           "golden": ["eq(request_items.approval, 'Requested')", "eq(requests.state, 'Work in Progress')", "gte(requests.opened, add(timestamptrunc(currentdate(), var(year)), interval('7', var(months))))"]}]}}


def test_gaps_the_ledger_measured_own_the_change_even_when_the_fit_is_doubtful_and_filters_read_as_words(tmp_path):
    item = reconcile.report_items(_run(tmp_path, [GAPS_AND_GRAIN]))[0]
    rows = {r["key"]: r for r in item["diff"]}
    assert item["owner"] == "model"
    assert item["change"] == ["The semantic model is missing: values declared on requests.state, values declared on request_items.approval. Add them through /agami-save-correction.",
                              "Also: the statement restricts the parent request to one state, a narrower notion of open than the question's"]
    assert rows["filters"]["yours"] == ["request_items.approval = 'Requested'", "requests.state = 'Work in Progress'",
                                        "requests.opened ≥ date_trunc(current_date, year) + interval 7 months"]
    assert rows["filters"]["yours_hi"] == ["requests.state = 'Work in Progress'", "requests.opened ≥ date_trunc(current_date, year) + interval 7 months"]
    assert rows["values"]["yours"] == "50% of the values match" and rows["values"]["agami"] is None
    assert item["sql_yours"].startswith("SELECT r.number") and item["sql_agami"].startswith("SELECT i.request")
    assert [r["key"] for r in item["diff"] if r["key"].startswith("metric")] == []


def test_readable_keys():
    assert reconcile._readable("eq(orders.region, 'EU')") == "orders.region = 'EU'"
    assert reconcile._readable("in(orders.status, 'a', 'b')") == "orders.status in ('a', 'b')"
    assert reconcile._readable("not(like(x.state, 'Closed%'))") == "not x.state like 'Closed%'"
    assert reconcile._readable("gte(o.d, add(datetrunc(var(year), currentdate()), interval('7', var(months))))") == "o.d ≥ date_trunc(year, current_date) + interval 7 months"
    assert reconcile._readable("plain text") == "plain text" and reconcile._readable(None) is None


def test_keep_is_the_owner_only_where_the_offer_can_be_made(tmp_path):
    """A matched table, or a matched number whose statement may not answer its question, is never
    offered as an example, so its owner is not keep and its words say why."""
    table = dict(TABLE_DIFF, row=6, status="match", comparison={"result_set": {"accuracy": 1.0, "reason": None, "unmatched_golden_columns": [], "golden_row_count": 21, "generated_row_count": 21}},
                 recorded={"columns": ["number", "status"], "rows": []}, statement_recorded={"columns": ["number", "status"], "row_count": 21})
    doubtful = json.loads(json.dumps(SCALAR_MATCH)); doubtful["row"] = 7
    doubtful["ledger"]["rows"] = [_part("runs", "confirmed"), _part("question_fit", "unresolved", {"fit": "doubtful"}, note="the grain differs")]
    items = {i["row"]: i for i in reconcile.report_items(_run(tmp_path, [SCALAR_MATCH, table, doubtful]))}
    assert items[1]["owner"] == "keep"
    assert items[6]["owner"] == "nothing" and items[6]["change"] == ["The two answers match. A table is not kept as an example; nothing to change."]
    # a doubtful fit on a matching number is the question's fix, and never kept
    assert items[7]["owner"] == "question" and items[7]["fix"] == "question" and items[7]["keep_allowed"] is False
    assert items[7]["change"][0].startswith("Reword the question, or change your query, so they ask the same thing. the grain differs")


def test_agamis_side_is_read_from_its_receipt_where_one_exists(tmp_path):
    """A default filter applied on agami's side and the metric it matched come from agami's receipt,
    found in the row directory or through the record's receipt_path when that is a JSON file."""
    run = _run(tmp_path, [SCALAR_MATCH, dict(SCALAR_MATCH, row=2)])
    receipt = {"tables": {"items": [{"qname": "public.orders", "ref": "orders", "filters": [{"expr": "status <> 'cancelled'", "status": "applied"}]}]},
               "columns": {"items": [{"kind": "output", "column": "revenue", "status": "matched", "name": "revenue"},
                                     {"kind": "metric", "name": "revenue"}]}}
    (run / "rows" / "1").mkdir(parents=True)
    (run / "rows" / "1" / "agami-receipt.json").write_text(json.dumps(receipt))
    elsewhere = tmp_path / "elsewhere.json"
    elsewhere.write_text(json.dumps(receipt))
    recs = [json.loads(l) for l in (run / "rows.jsonl").read_text().splitlines()]
    recs[1]["receipt_path"] = str(elsewhere)
    (run / "rows.jsonl").write_text("".join(json.dumps(r) + "\n" for r in recs))
    items = reconcile.report_items(run)
    for item in items:
        rows = {r["key"]: r for r in item["diff"]}
        assert rows["default filter on orders"]["agami"] == "applied"
        assert rows["metric revenue"]["yours"] == "matched revenue" and rows["metric revenue"]["agami"] == "matched revenue"


def test_owner_branches_a_number_only_mismatch_differing_tables_and_open_parts(tmp_path):
    number_only = {"row": 1, "label": "Orders", "question": "How many orders?", "expected": 100.0, "actual": 90.0, "delta_pct": -0.1, "match": False,
                   "status": "mismatch", "recorded": {"columns": ["n"], "rows": [[90.0]]}, "provenance": {"shape": "c", "source": "a tile"}}
    tables_differ = dict(TABLE_DIFF, row=2, recorded={"columns": ["number"], "rows": []}, statement_recorded={"columns": ["number"], "row_count": 21},
                         comparison={"result_set": {"accuracy": 0.5, "reason": "rows differ", "unmatched_golden_columns": [], "golden_row_count": 21, "generated_row_count": 21}},
                         claims={"claims": [{"name": "tables", "status": "differs", "generated": ["orders"], "golden": ["orders", "shipments"]}]},
                         ledger={"rows": [_part("runs", "confirmed"), _part("question_fit", "confirmed", {"fit": "plausible"})], "verdict": "confirmed", "counts": {}})
    open_only = dict(SCALAR_MATCH, row=3, status="match_unverified", ledger={"rows": [_part("runs", "confirmed"), _part("join:orders-payments", "unresolved", note="could not probe"),
                     _part("question_fit", "confirmed", {"fit": "plausible"})], "verdict": "unresolved", "counts": {}}, claims=None)
    items = {i["row"]: i for i in reconcile.report_items(_run(tmp_path, [number_only, tables_differ, open_only]))}
    assert items[1]["owner"] == "question" and items[1]["sentence"] == "The two answers do not match, and no check explains why."
    assert items[1]["diff"][0]["note"] == "agami is -10.0% from your number" and items[1]["diff"][0]["yours_hi"] == ["100"]
    assert items[2]["fix"] == "examples" and items[2]["owner"] == "agami" and items[2]["sentence"] == "The two answers do not match. What differs: tables read."
    assert items[3]["owner"] == "nothing" and items[3]["change"] == ["Nothing to change. Some checks could not run against the database, so this row is not offered as an example."]
    assert items[3]["sentence"] == "The numbers match, but these checks could not be confirmed: join orders to payments."


def test_display_helpers_cover_every_shape():
    assert reconcile._fmt(True) == "true" and reconcile._fmt(3.14159) == "3.14" and reconcile._fmt("x") == "x" and reconcile._fmt(1234567) == "1,234,567"
    assert reconcile._recorded_display({"columns": ["a"], "rows": [[1], [2]]}) == ("2 rows", False)
    assert reconcile._recorded_display({"columns": ["a"]}) == (None, False) and reconcile._recorded_display("no") == (None, False)
    assert reconcile._part_key("literal:*") == "values" and reconcile._part_key("fan_out:*") == "fan-out" and reconcile._part_key("join:a-b") == "join a to b"
    assert reconcile._part_key("default_filter:orders:status <> 'x'") == "default filter on orders" and reconcile._part_key("mystery:z") == "mystery:z"
    assert reconcile._claim_text({"column": "orders.d", "start": "2025-01-01", "start_inclusive": False, "end": "2025-02-01", "end_inclusive": True}) == "orders.d > 2025-01-01 ≤ 2025-02-01"
    assert reconcile._claim_text([[["orders", "id"], ["payments", "order_id"]]]) == ["orders.id = payments.order_id"]
    assert reconcile._claim_text([["placed_at", "desc"]]) == ["placed_at desc"] and reconcile._claim_text(5) == "5" and reconcile._claim_text([]) is None
    assert reconcile._readable("between(o.d, '2025-01-01', '2025-02-01')") == "o.d between '2025-01-01' and '2025-02-01'"
    assert reconcile._readable("cast(o.d, date)") == "o.d as date" and reconcile._readable("and(a, b, c)") == "a and b and c"
    assert reconcile._readable("paren(eq(a, 'x, y'))") == "(a = 'x, y')" and reconcile._readable(["eq(a, 1)"]) == ["a = 1"]


def test_the_checkpoint_reader_and_the_cli_error_paths(tmp_path, capsys):
    run = tmp_path / "run"; run.mkdir()
    (run / "intake.json").write_text(json.dumps([{"question": "q1"}, {"question": "q2"}]))  # a bare list, rows numbered on read
    (run / "rows.jsonl").write_text("\n" + json.dumps({"row": 1, "status": "match"}) + "\n\n")
    out = reconcile.next_chunk(run)
    assert out["chunk_rows"] == [2] and out["done"] == [1]
    (run / "rows.jsonl").write_text("[1, 2]\n")
    assert reconcile._done_rows(run)[1] == ["line 1"]
    assert reconcile.main(["next-chunk", "--run-dir", str(run), "--size", "0"]) == 2
    assert reconcile.main(["next-chunk", "--run-dir", str(tmp_path / "fresh"), "--rows-file", str(tmp_path / "nope.json")]) in (2,)
    assert reconcile.main(["report-items", "--run-dir", str(run)]) == 2
    assert "cannot be read" in capsys.readouterr().err


def test_the_remaining_row_shapes_a_failed_statement_a_wide_delta_and_the_join_facts(tmp_path):
    failed = {"row": 1, "label": "Refunds", "question": "How many refunds?", "expected": None, "actual": 12.0, "delta_pct": 4.5, "match": False, "status": "expected_doubtful",
              "recorded": None, "statement": "SELECT COUNT(*) FROM refundz", "statement_recorded": None, "provenance": {"shape": "b"},
              "ledger": {"rows": [_part("runs", "query_defect", {"kind": "table_not_found"}, note="the statement failed: table not found"),
                                  _part("cardinality:orders-payments", "confirmed", {"one_side": "orders", "sides": {}}),
                                  _part("dropped_rows:orders-payments", "noted", {"total": 4000, "dropped": 12, "left": "orders", "right": "payments"})],
                         "verdict": "query_defect", "counts": {}}, "ledger_verdict": "query_defect",
              "claims": {"claims": [{"name": "date_window", "status": "unknown", "generated": None, "golden": None}]}}
    item = reconcile.report_items(_run(tmp_path, [failed]))[0]
    rows = {r["key"]: r for r in item["diff"]}
    assert rows["answer"]["yours"] == "failed" and rows["answer"]["agami"] == "12" and rows["answer"]["state"] == "defect"
    assert rows["answer"]["note"] == "agami is +450.0% from your number"  # delta_pct is a fraction; always shown as a percent
    assert rows["date window"] == {"key": "date window", "state": "open", "yours": "could not read", "agami": "could not read",
                                   "note": "the window could not be read from one of the two queries"}
    assert rows["one row per key, orders to payments"]["yours"] == "one row per key on orders"
    assert rows["rows dropped by join orders to payments"]["yours"] == "12 of 4,000 orders rows" and rows["rows dropped by join orders to payments"]["state"] == "noted"
    assert item["delta_pct"] == 450.0 and item["owner"] == "you"


def test_an_intake_without_a_row_list_is_refused(tmp_path):
    run = tmp_path / "run"; run.mkdir()
    (run / "intake.json").write_text(json.dumps({"rows": "nope"}))
    with pytest.raises(ValueError, match="no list of rows"):
        reconcile.next_chunk(run)


def test_the_first_reviews_findings(tmp_path):
    """Extra columns on agami's side are noticed, never the person's mistake; a claim that differs is a
    difference, not a mistake; an unreadable statement is one open row; a row recorded twice is one
    row with its last status; a record without a status is an error on the card too."""
    # 2 + 3: agami returned more columns than asked, and the values still match
    agami_extra = dict(TABLE_DIFF, row=1, status="match", recorded={"columns": ["number", "status", "extra"], "rows": []},
                       statement_recorded={"columns": ["number", "status"], "row_count": 21},
                       comparison={"result_set": {"accuracy": 1.0, "reason": None, "unmatched_golden_columns": [], "golden_row_count": 21, "generated_row_count": 21}},
                       claims=None, ledger={"rows": [_part("runs", "confirmed"), _part("question_fit", "confirmed", {"fit": "plausible"})], "verdict": "confirmed", "counts": {}})
    # 4: a typed value the data proved wrong, beside a tables claim that merely differs
    doubtful = json.loads(json.dumps(DEFECT)); doubtful["row"] = 2
    doubtful["claims"] = {"claims": [{"name": "tables", "status": "differs", "generated": ["orders"], "golden": ["orders", "shipments"]}]}
    # 5: an unreadable statement: seven unknown claims with nothing on either side
    unreadable = dict(SCALAR_MATCH, row=3, status="match_unverified", ledger_verdict="unresolved",
                      ledger={"rows": [_part("runs", "confirmed"), _part("question_fit", "confirmed", {"fit": "plausible"})], "verdict": "confirmed", "counts": {}},
                      claims={"claims": [{"name": n, "status": "unknown", "generated": None, "golden": None} for n in ("tables", "filter_predicates", "date_window", "group_keys", "join_keys", "ordering", "limit")]})
    # 11: no status at all
    no_status = {"row": 4, "question": "Which category?", "error": "boom"}
    # 6: row 5 recorded twice, mismatch then match
    twice_a = dict(SCALAR_MATCH, row=5, status="mismatch", match=False, actual=1.0); twice_b = dict(SCALAR_MATCH, row=5)
    run = _run(tmp_path, [agami_extra, doubtful, unreadable, no_status, twice_a, twice_b])
    items = {i["row"]: i for i in reconcile.report_items(run)}
    cols = next(r for r in items[1]["diff"] if r["key"] == "columns")
    assert cols["state"] == "noted" and cols["agami_hi"] == ["extra"] and cols["note"] is None  # the tokens carry it, diff-style
    assert items[1]["owner"] != "you" and "every check passed" in items[1]["sentence"]
    assert items[2]["change"][0] == "Fix your query: value orders.status='Delivered'. Then run this row again."
    assert "tables read" not in items[2]["sentence"] and "value orders.status='Delivered'" in items[2]["sentence"]
    claim_rows = [r for r in items[3]["diff"] if r["key"] in ("claims", "tables read", "filters", "limit")]
    assert [r["key"] for r in claim_rows] == ["claims"] and claim_rows[0]["state"] == "open"
    assert "limit" not in items[3]["sentence"] and "claims" in items[3]["sentence"]
    assert items[4]["owner"] == "agami" and items[4]["sentence"] == "agami's query failed: boom." and items[4]["status"] == "error"
    assert len(items) == 5 and items[5]["status"] == "match" and items[5]["owner"] == "keep"
    # 2 again, the other way: a difference in values beside extra columns of yours is not just the query
    both = dict(TABLE_DIFF, row=6, comparison={"result_set": {"accuracy": 0.4, "reason": "values differ", "unmatched_golden_columns": [], "golden_row_count": 21, "generated_row_count": 21}})
    item = reconcile.report_items(_run(tmp_path / "b", [both]))[0]
    assert item["owner"] != "you" and item["fix"] == "examples" and item["result"]["label"] == "different answer"


def test_small_numbers_keep_their_digits_and_never_read_minus_zero():
    assert reconcile._fmt(0.004) == "0.004" and reconcile._fmt(-0.004) == "-0.004" and reconcile._fmt(-0.0000001) != "-0"
    assert reconcile._fmt(0.5) == "0.5" and reconcile._fmt(-0.0) == "0"


def test_the_checkpoint_dedupes_and_the_seed_is_validated(tmp_path, capsys):
    run = tmp_path / "run"; run.mkdir()
    (run / "intake.json").write_text(json.dumps({"rows": [{"row": 1}, {"row": 2}, {"row": 3}]}))
    (run / "rows.jsonl").write_text("".join(json.dumps(r) + "\n" for r in (
        {"row": 1, "status": "mismatch"}, {"row": "1", "status": "match"}, {"row": 99, "status": "match"})))
    out = reconcile.next_chunk(run)
    assert out["progress"] == {"match": 1} and out["finished"] == 1 and out["chunk_rows"] == [2, 3]
    fresh = tmp_path / "fresh"; fresh.mkdir()
    bad = tmp_path / "bad.json"; bad.write_text("{not json")
    assert reconcile.main(["next-chunk", "--run-dir", str(fresh), "--rows-file", str(bad)]) == 2 and not (fresh / "intake.json").exists()
    bad.write_text(json.dumps({"rows": ["q1", "q2"]}))
    assert reconcile.main(["next-chunk", "--run-dir", str(fresh), "--rows-file", str(bad)]) == 2 and not (fresh / "intake.json").exists()
    (fresh / "intake.json").write_text(json.dumps({"rows": ["q1"]}))
    assert reconcile.main(["next-chunk", "--run-dir", str(fresh)]) == 2
    assert "not an object" in capsys.readouterr().err


def test_keep_allowed_on_the_items_equals_the_parsers_keep_gate(tmp_path):
    import parse_reconcile_report as pr
    no_fit = json.loads(json.dumps(SCALAR_MATCH)); no_fit["row"] = 2
    no_fit["ledger"]["rows"] = [_part("runs", "confirmed"), _part("metric:revenue", "confirmed", {"metric": "revenue"})]  # no question_fit part at all
    number_only = {"row": 3, "label": "Orders", "question": "How many orders?", "expected": 100.0, "actual": 100.0, "delta_pct": 0.0, "match": True,
                   "status": "match", "recorded": {"columns": ["n"], "rows": [[100.0]]}, "provenance": {"shape": "c"}}
    run = _run(tmp_path, [SCALAR_MATCH, no_fit, number_only])
    for rec in (SCALAR_MATCH, no_fit, number_only):
        d = run / "rows" / str(rec["row"]); d.mkdir(parents=True, exist_ok=True)
        if rec.get("ledger"):
            (d / "ledger.json").write_text(json.dumps(rec["ledger"]))
    items = reconcile.report_items(run)
    keepable = pr.keepable_rows(run)
    assert {i["row"]: i["keep_allowed"] for i in items} == {r: (r in keepable) for r in (1, 2, 3)} == {1: True, 2: False, 3: True}

