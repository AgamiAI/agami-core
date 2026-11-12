"""`reconcile.py own-examples`: what a run started from "Add my own" adds, asks, and sends back (#645).

The run is an ordinary reconcile run: every statement the person brought was checked part by part and
agami answered the same question cold. This verb only reads the report items that run produced and
sorts each row into one list. A row whose query the data proved wrong is never written as an example;
a row that disagrees with a definition is asked about as "which should be the default?" and kept as an
open question in the shape of a declared ambiguity. Synthetic run directories, neutral names.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agami" / "scripts"))

import reconcile  # noqa: E402

from test_reconcile_report_items import DEFECT, ERROR, SCALAR_MATCH, UNGRADED_ROW, _part, _run  # noqa: E402


def _claims(*cs):
    return {"claims": [{"name": n, "status": st, "golden": y, "generated": g} for n, st, y, g in cs]}


# Agami counts every arrival, the person's dashboard the first one: the answers differ on a filter.
DEFINITION = dict(SCALAR_MATCH, row=6, question="How many visitors arrived in May?", status="mismatch", match=False,
                  actual=980.0, delta_pct=-0.2, recorded={"columns": ["visitors"], "rows": [[980.0]]},
                  statement="SELECT COUNT(*) AS visitors FROM arrivals WHERE is_first = 1",
                  sql="SELECT COUNT(*) AS visitors FROM arrivals",
                  claims=_claims(("tables", "agrees", ["arrivals"], ["arrivals"]),
                                 ("filter_predicates", "differs", ["eq(arrivals.is_first, 1)"], [])))
# The person's query counts cancelled orders; the semantic model leaves them out by default.
OMITS_DEFAULT = dict(SCALAR_MATCH, row=7, question="How many orders are there in each channel?", status="match_unverified",
                     statement="SELECT channel, COUNT(*) AS n FROM orders GROUP BY channel",
                     ledger={"rows": [_part("runs", "confirmed"), _part("question_fit", "confirmed"),
                                      _part("default_filter:orders:orders.status != 'cancelled'", "model_gap",
                                            {"table": "orders", "expr": "orders.status != 'cancelled'"}, kind="filter")],
                             "verdict": "model_gap", "counts": {}}, ledger_verdict="model_gap")
# A metric the semantic model does not define is a gap to fill, not a choice between two meanings.
NO_METRIC = dict(SCALAR_MATCH, row=8, question="How many units shipped?", status="match_unverified",
                 statement="SELECT SUM(quantity) AS units FROM order_items",
                 ledger={"rows": [_part("runs", "confirmed"), _part("question_fit", "confirmed"),
                                  _part("metric:units", "model_gap", note="the output matches no metric the semantic model defines")],
                         "verdict": "model_gap", "counts": {}}, ledger_verdict="model_gap")


def _receipt(run: Path, row: int, *tables: str) -> None:
    d = run / "rows" / str(row)
    d.mkdir(parents=True, exist_ok=True)
    (d / "statement-receipt.json").write_text(json.dumps({"tables": {"items": [{"ref": t} for t in tables]}}))


def _model(tmp_path: Path) -> Path:
    root = tmp_path / "acme"
    for area, table in (("sales", "orders"), ("sales", "order_items"), ("traffic", "arrivals")):
        (root / "subject_areas" / area / "tables").mkdir(parents=True, exist_ok=True)
        (root / "subject_areas" / area / "tables" / f"{table}.yaml").write_text(f"name: {table}\n")
    return root


def _plan_run(tmp_path: Path) -> Path:
    run = _run(tmp_path, [SCALAR_MATCH, DEFECT, dict(ERROR, row=4), dict(UNGRADED_ROW, row=5),
                          DEFINITION, OMITS_DEFAULT, NO_METRIC])
    for row, table in ((1, "orders"), (3, "orders"), (6, "arrivals"), (7, "orders"), (8, "order_items")):
        _receipt(run, row, table)
    return run


def test_each_row_lands_in_exactly_one_list(tmp_path):
    plan = reconcile.own_examples(_plan_run(tmp_path), root=_model(tmp_path), now="2026-11-12T00:00:00Z")
    rows = {k: [e["row"] for e in plan[k]] for k in ("add", "ask", "fix", "reword", "not_checked")}
    assert rows == {"add": [1, 8], "ask": [6, 7], "fix": [3], "reword": [], "not_checked": [4, 5]}
    assert plan["counts"] == {"add": 2, "ask": 2, "fix": 1, "reword": 0, "not_checked": 2}


def test_a_query_the_data_proved_wrong_is_never_written_as_an_example(tmp_path):
    # A live run wrote such a row: the fix branch fell through to the write.
    run = _plan_run(tmp_path)
    plan = reconcile.own_examples(run, root=_model(tmp_path))
    written = [e["question"] for f in plan["examples_files"].values() for e in json.loads(Path(f).read_text())]
    assert DEFECT["question"] not in written
    assert sorted(written) == sorted([SCALAR_MATCH["question"], NO_METRIC["question"]])


def test_examples_are_filed_by_area_signed_later_and_marked_as_the_persons_own(tmp_path):
    run = _plan_run(tmp_path)
    plan = reconcile.own_examples(run, root=_model(tmp_path), now="2026-11-12T00:00:00Z")
    assert set(plan["examples_files"]) == {"sales"} and plan["needs_area"] is None
    (first, second) = json.loads(Path(plan["examples_files"]["sales"]).read_text())
    assert first == {"question": SCALAR_MATCH["question"], "sql": SCALAR_MATCH["statement"], "tables": ["orders"],
                     "source": "own", "status": "confirmed", "created_at": "2026-11-12T00:00:00Z"}
    assert second["tables"] == ["order_items"]


def test_a_table_no_area_claims_is_written_apart_for_the_skill_to_ask(tmp_path):
    run = _plan_run(tmp_path)
    plan = reconcile.own_examples(run, root=None)
    assert plan["examples_files"] == {} and Path(plan["needs_area"]).name == "own-examples.add.no-area.json"


def test_an_open_question_has_the_shape_of_a_declared_ambiguity(tmp_path):
    plan = reconcile.own_examples(_plan_run(tmp_path), root=_model(tmp_path))
    asks = {a["row"]: a for a in plan["ask"]}
    definition = asks[6]["candidate"]
    assert definition["when"] == "How many visitors arrived in May?" and definition["behaviour"] == "ask"
    assert definition["name"] == "how_many_visitors_arrived_in_may"
    assert definition["options"] == [{"label": "as your query has it", "how": DEFINITION["statement"]},
                                     {"label": "as agami answered", "how": DEFINITION["sql"]}]
    assert definition["applies_to"] == ["arrivals"] and asks[6]["differs_in"] == ["filters"]
    # what the card names: the checks where the two sides part, both sides given
    checks = {d["check"]: d for d in asks[6]["differences"]}
    assert "filters" in checks and checks["filters"]["yours"] != checks["filters"]["agami"]
    default = asks[7]["candidate"]["options"]
    assert default[1] == {"label": "as the semantic model declares", "how": "apply the declared filter orders.status != 'cancelled'"}
    assert all(a["status"] == "open" and a["settled_as"] is None for a in plan["ask"])
    # the plan is on disk beside the run, for a later step to read
    assert json.loads((tmp_path / "20260913-101500" / "own-examples.json").read_text())["ask"] == plan["ask"]


def test_card_answers_settle_questions_and_leave_rows_out(tmp_path):
    run = _plan_run(tmp_path)
    decisions = [{"row": 1, "decision": "nothing"}, {"row": 6, "decision": "change", "words": "first arrival is the default"},
                 {"row": 7, "decision": "example"}]
    plan = reconcile.own_examples(run, root=_model(tmp_path), decisions=decisions)
    asks = {a["row"]: (a["status"], a["settled_as"]) for a in plan["ask"]}
    assert asks == {6: ("settled", "yours"), 7: ("settled", "this_question")}
    assert [e["row"] for e in plan["add"]] == [8]
    assert {"row": 1, "question": SCALAR_MATCH["question"], "source": "the finance dashboard, tiles.csv:2, a number with the SQL behind it",
            "why": "left out on the card"} in plan["not_checked"]
    written = [e["question"] for e in json.loads(Path(plan["examples_files"]["sales"]).read_text())]
    # "keep mine for this question" adds it; "make mine the default" goes to save-correction instead
    assert written == [OMITS_DEFAULT["question"], NO_METRIC["question"]]


def test_a_rerun_leaves_no_stale_examples_file(tmp_path):
    run = _plan_run(tmp_path)
    reconcile.own_examples(run, root=None)
    assert (run / "own-examples.add.no-area.json").exists()
    reconcile.own_examples(run, root=_model(tmp_path))
    assert not (run / "own-examples.add.no-area.json").exists()


def test_the_cli_applies_only_the_parsers_ok_answer_for_this_run(tmp_path, capsys):
    run = _plan_run(tmp_path)
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"ok": False, "data": {"run": run.name, "decisions": []}}))
    assert reconcile.main(["own-examples", "--run-dir", str(run), "--decisions", str(bad)]) == 2
    other = tmp_path / "other.json"
    other.write_text(json.dumps({"ok": True, "data": {"run": "another-run", "decisions": []}}))
    assert reconcile.main(["own-examples", "--run-dir", str(run), "--decisions", str(other)]) == 2
    capsys.readouterr()
    good = tmp_path / "good.json"
    good.write_text(json.dumps({"ok": True, "data": {"run": run.name, "decisions": [{"row": 6, "decision": "fix"}]}}))
    assert reconcile.main(["own-examples", "--run-dir", str(run), "--root", str(_model(tmp_path)), "--decisions", str(good)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert {a["row"]: a["settled_as"] for a in out["ask"]}[6] == "agami"


def test_the_cli_refuses_a_run_that_has_not_finished(tmp_path, capsys):
    assert reconcile.main(["own-examples", "--run-dir", str(tmp_path)]) == 2
    assert "finish the run first" in capsys.readouterr().err
