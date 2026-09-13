"""The report page's items come from the run's own files: one diff row per check, the differing
tokens named, the owner and the sentence templated. Nothing on the card is written by hand."""

from __future__ import annotations

import json
import sys
from pathlib import Path

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
    assert rows["values"]["yours"] == "100% of the values match" or rows["values"]["state"] == "held"
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
