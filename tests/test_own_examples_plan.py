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

from test_reconcile_report_items import (  # noqa: E402
    DEFECT,
    ERROR,
    SCALAR_MATCH,
    UNGRADED_ROW,
    _part,
    _run,
)


def _claims(*cs):
    return {
        "claims": [{"name": n, "status": st, "golden": y, "generated": g} for n, st, y, g in cs]
    }


# Agami counts every arrival, the person's dashboard the first one: the answers differ on a filter.
DEFINITION = dict(
    SCALAR_MATCH,
    row=6,
    question="How many visitors arrived in May?",
    status="mismatch",
    match=False,
    actual=980.0,
    delta_pct=-0.2,
    recorded={"columns": ["visitors"], "rows": [[980.0]]},
    statement="SELECT COUNT(*) AS visitors FROM arrivals WHERE is_first = 1",
    sql="SELECT COUNT(*) AS visitors FROM arrivals",
    claims=_claims(
        ("tables", "agrees", ["arrivals"], ["arrivals"]),
        ("filter_predicates", "differs", ["eq(arrivals.is_first, 1)"], []),
    ),
)
# The person's query counts cancelled orders; the semantic model leaves them out by default.
OMITS_DEFAULT = dict(
    SCALAR_MATCH,
    row=7,
    question="How many orders are there in each channel?",
    status="match_unverified",
    statement="SELECT channel, COUNT(*) AS n FROM orders GROUP BY channel",
    ledger={
        "rows": [
            _part("runs", "confirmed"),
            _part("question_fit", "confirmed"),
            _part(
                "default_filter:orders:orders.status != 'cancelled'",
                "model_gap",
                {"table": "orders", "expr": "orders.status != 'cancelled'"},
                kind="filter",
            ),
        ],
        "verdict": "model_gap",
        "counts": {},
    },
    ledger_verdict="model_gap",
)
# A metric the semantic model does not define is a gap to fill, not a choice between two meanings.
NO_METRIC = dict(
    SCALAR_MATCH,
    row=8,
    question="How many units shipped?",
    status="match_unverified",
    statement="SELECT SUM(quantity) AS units FROM order_items",
    ledger={
        "rows": [
            _part("runs", "confirmed"),
            _part("question_fit", "confirmed"),
            _part(
                "metric:units",
                "model_gap",
                note="the output matches no metric the semantic model defines",
            ),
        ],
        "verdict": "model_gap",
        "counts": {},
    },
    ledger_verdict="model_gap",
)


def _receipt(run: Path, row: int, *tables: str) -> None:
    d = run / "rows" / str(row)
    d.mkdir(parents=True, exist_ok=True)
    (d / "statement-receipt.json").write_text(
        json.dumps({"tables": {"items": [{"ref": t} for t in tables]}})
    )


def _model(tmp_path: Path) -> Path:
    root = tmp_path / "acme"
    for area, table in (("sales", "orders"), ("sales", "order_items"), ("traffic", "arrivals")):
        (root / "subject_areas" / area / "tables").mkdir(parents=True, exist_ok=True)
        (root / "subject_areas" / area / "tables" / f"{table}.yaml").write_text(f"name: {table}\n")
    return root


def _plan_run(tmp_path: Path) -> Path:
    run = _run(
        tmp_path,
        [
            SCALAR_MATCH,
            DEFECT,
            dict(ERROR, row=4),
            dict(UNGRADED_ROW, row=5),
            DEFINITION,
            OMITS_DEFAULT,
            NO_METRIC,
        ],
    )
    for row, table in (
        (1, "orders"),
        (3, "orders"),
        (6, "arrivals"),
        (7, "orders"),
        (8, "order_items"),
    ):
        _receipt(run, row, table)
    return run


def test_each_row_lands_in_exactly_one_list(tmp_path):
    plan = reconcile.own_examples(
        _plan_run(tmp_path), root=_model(tmp_path), now="2026-11-12T00:00:00Z"
    )
    rows = {k: [e["row"] for e in plan[k]] for k in ("add", "ask", "fix", "reword", "not_checked")}
    assert rows == {"add": [1, 8], "ask": [6, 7], "fix": [3], "reword": [], "not_checked": [4, 5]}
    assert plan["counts"] == {
        "add": 2,
        "ask": 2,
        "fix": 1,
        "reword": 0,
        "not_checked": 2,
        "collisions": 0,
        "duplicates": 0,
        "needs_area": 0,
        "written": 2,
    }


def test_a_query_the_data_proved_wrong_is_never_written_as_an_example(tmp_path):
    # A live run wrote such a row: the fix branch fell through to the write.
    run = _plan_run(tmp_path)
    plan = reconcile.own_examples(run, root=_model(tmp_path))
    written = [
        e["question"]
        for f in plan["examples_files"].values()
        for e in json.loads(Path(f).read_text())
    ]
    assert DEFECT["question"] not in written
    assert sorted(written) == sorted([SCALAR_MATCH["question"], NO_METRIC["question"]])


def test_examples_are_filed_by_area_signed_later_and_marked_as_the_persons_own(tmp_path):
    run = _plan_run(tmp_path)
    plan = reconcile.own_examples(run, root=_model(tmp_path), now="2026-11-12T00:00:00Z")
    assert set(plan["examples_files"]) == {"sales"} and plan["needs_area"] == []
    (first, second) = json.loads(Path(plan["examples_files"]["sales"]).read_text())
    assert first == {
        "question": SCALAR_MATCH["question"],
        "sql": SCALAR_MATCH["statement"],
        "tables": ["orders"],
        "source": "own",
        "status": "confirmed",
        "created_at": "2026-11-12T00:00:00Z",
    }
    assert second["tables"] == ["order_items"]


def test_a_table_no_area_claims_is_written_apart_for_the_skill_to_ask(tmp_path):
    run = _plan_run(tmp_path)
    plan = reconcile.own_examples(run, root=None)
    # nothing is written until the Area card is answered; then the same run, with --area
    assert plan["examples_files"] == {} and not list(run.glob("own-examples.add.*.json"))
    assert [r["row"] for r in plan["needs_area"]] == [1, 6, 7, 8]


def test_an_open_question_has_the_shape_of_a_declared_ambiguity(tmp_path):
    plan = reconcile.own_examples(_plan_run(tmp_path), root=_model(tmp_path))
    asks = {a["row"]: a for a in plan["ask"]}
    definition = asks[6]["candidate"]
    assert (
        definition["when"] == "How many visitors arrived in May?"
        and definition["behaviour"] == "ask"
    )
    assert definition["name"] == "how_many_visitors_arrived_in_may"
    assert definition["options"] == [
        {"label": "as your query has it", "how": DEFINITION["statement"]},
        {"label": "as agami answered", "how": DEFINITION["sql"]},
    ]
    assert definition["applies_to"] == ["arrivals"] and asks[6]["differs_in"] == ["filters"]
    # what the card names: the checks where the two sides part, both sides given
    checks = {d["check"]: d for d in asks[6]["differences"]}
    assert "filters" in checks and checks["filters"]["yours"] != checks["filters"]["agami"]
    default = asks[7]["candidate"]["options"]
    assert default[1] == {
        "label": "as the semantic model declares",
        "how": "apply the declared filter orders.status != 'cancelled'",
    }
    assert all(a["status"] == "open" and a["settled_as"] is None for a in plan["ask"])
    # the plan is on disk beside the run, for a later step to read
    assert (
        json.loads((tmp_path / "20260913-101500" / "own-examples.json").read_text())["ask"]
        == plan["ask"]
    )


def test_card_answers_settle_questions_and_leave_rows_out(tmp_path):
    run = _plan_run(tmp_path)
    decisions = [
        {"row": 1, "decision": "nothing"},
        {"row": 6, "decision": "change", "words": "first arrival is the default"},
        {"row": 7, "decision": "example"},
    ]
    plan = reconcile.own_examples(run, root=_model(tmp_path), decisions=decisions)
    asks = {a["row"]: (a["status"], a["settled_as"]) for a in plan["ask"]}
    assert asks == {6: ("settled", "yours"), 7: ("settled", "this_question")}
    assert [e["row"] for e in plan["add"]] == [8]
    assert {
        "row": 1,
        "question": SCALAR_MATCH["question"],
        "source": "the finance dashboard, tiles.csv:2, a number with the SQL behind it",
        "why": "left out on the card",
    } in plan["not_checked"]
    written = [e["question"] for e in json.loads(Path(plan["examples_files"]["sales"]).read_text())]
    # "keep mine for this question" adds it; "make mine the default" goes to save-correction instead
    assert written == [OMITS_DEFAULT["question"], NO_METRIC["question"]]


def test_a_rerun_leaves_no_stale_examples_file(tmp_path):
    run = _plan_run(tmp_path)
    reconcile.own_examples(run, root=_model(tmp_path))
    assert (run / "own-examples.add.sales.json").exists()
    reconcile.own_examples(
        run,
        root=_model(tmp_path),
        decisions=[{"row": 1, "decision": "nothing"}, {"row": 8, "decision": "nothing"}],
    )
    assert not (run / "own-examples.add.sales.json").exists()


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
    good.write_text(
        json.dumps(
            {"ok": True, "data": {"run": run.name, "decisions": [{"row": 6, "decision": "fix"}]}}
        )
    )
    assert (
        reconcile.main(
            [
                "own-examples",
                "--run-dir",
                str(run),
                "--root",
                str(_model(tmp_path)),
                "--decisions",
                str(good),
            ]
        )
        == 0
    )
    out = json.loads(capsys.readouterr().out)
    assert {a["row"]: a["settled_as"] for a in out["ask"]}[6] == "agami"


def test_the_cli_refuses_a_run_that_has_not_finished(tmp_path, capsys):
    assert reconcile.main(["own-examples", "--run-dir", str(tmp_path)]) == 2
    assert "finish the run first" in capsys.readouterr().err


# --- review fixes (#645) ------------------------------------------------------------------------


def test_a_choice_names_at_most_two_readings(tmp_path):
    # The person's query skips a declared filter AND agami answered differently on a definition: the
    # card's "keep the other one" must mean one reading, the semantic model's declaration.
    both = dict(
        OMITS_DEFAULT,
        row=9,
        status="mismatch",
        match=False,
        actual=1.0,
        delta_pct=-0.5,
        sql="SELECT channel, COUNT(*) AS n FROM orders WHERE status != 'cancelled' GROUP BY channel",
        recorded={"columns": ["n"], "rows": [[1.0]]},
        claims=_claims(
            ("tables", "agrees", ["orders"], ["orders"]),
            ("filter_predicates", "differs", ["ne(orders.status, 'cancelled')"], []),
        ),
    )
    run = _run(tmp_path, [both])
    _receipt(run, 9, "orders")
    (ask,) = reconcile.own_examples(run, root=_model(tmp_path))["ask"]
    assert [o["label"] for o in ask["candidate"]["options"]] == [
        "as your query has it",
        "as the semantic model declares",
    ]


def test_a_match_held_open_only_by_a_value_pair_counts_it_as_unchecked(tmp_path):
    pair = dict(
        SCALAR_MATCH,
        status="match_unverified",
        ledger={
            "rows": [
                _part("runs", "confirmed"),
                _part("question_fit", "confirmed"),
                _part("value_pair:revenue~total", "unresolved", note="one value fills most rows"),
            ],
            "verdict": "unresolved",
            "counts": {},
        },
        ledger_verdict="unresolved",
    )
    run = _run(tmp_path, [pair])
    _receipt(run, 1, "orders")
    (add,) = reconcile.own_examples(run, root=_model(tmp_path))["add"]
    assert add["unchecked"] == 1


def test_written_counts_every_example_including_one_kept_for_its_own_question(tmp_path):
    run = _plan_run(tmp_path)
    plan = reconcile.own_examples(
        run, root=_model(tmp_path), decisions=[{"row": 7, "decision": "example"}]
    )
    assert plan["counts"]["add"] == 2 and plan["counts"]["written"] == 3
    assert [w["row"] for w in plan["written"]] == [1, 7, 8]


def test_an_existing_example_under_the_same_question_is_never_overwritten_silently(tmp_path):
    root = _model(tmp_path)
    (root / "prompt_examples" / "sales").mkdir(parents=True)
    (root / "prompt_examples" / "sales" / "examples.yaml").write_text(
        "- question: What was total revenue in Q3 2025?\n"
        "  sql: SELECT SUM(net_amount) FROM orders\n"
        "  source: correction\n  status: confirmed\n"
        "- question: How many units shipped?\n"
        "  sql: SELECT SUM(quantity) AS units FROM order_items\n"  # the same SQL: a re-save, no clash
        "  source: seed\n"
    )
    run = _plan_run(tmp_path)
    plan = reconcile.own_examples(run, root=root)
    assert plan["collisions_checked"] is True
    (clash,) = plan["collisions"]
    assert (clash["row"], clash["existing_source"], clash["replace"]) == (1, "correction", False)
    written = [e["question"] for e in json.loads(Path(plan["examples_files"]["sales"]).read_text())]
    assert written == [NO_METRIC["question"]]
    # "Replace it" on the collision card is --replace for that row, and nothing else
    plan = reconcile.own_examples(run, root=root, decisions=[{"row": 1, "decision": "example"}])
    assert plan["collisions"][0]["replace"] is False and plan["counts"]["written"] == 1
    plan = reconcile.own_examples(run, root=root, replace={1})
    assert plan["collisions"][0]["replace"] is True
    written = [e["question"] for e in json.loads(Path(plan["examples_files"]["sales"]).read_text())]
    assert written == [SCALAR_MATCH["question"], NO_METRIC["question"]]


# --- Copilot review on #649 ----------------------------------------------------------------------


def test_nothing_is_written_when_the_question_fit_is_unconfirmed(tmp_path):
    # A model gap returns the "semantic model" fix before fit is looked at; fit must still gate.
    unfit = dict(
        OMITS_DEFAULT,
        row=2,
        ledger={
            "rows": [
                _part("runs", "confirmed"),
                _part("question_fit", "unresolved", {"fit": "doubtful"}),
                _part(
                    "default_filter:orders:orders.status != 'cancelled'",
                    "model_gap",
                    {"table": "orders", "expr": "orders.status != 'cancelled'"},
                    kind="filter",
                ),
            ],
            "verdict": "model_gap",
            "counts": {},
        },
    )
    no_fit = dict(
        SCALAR_MATCH,
        row=3,
        ledger={"rows": [_part("runs", "confirmed")], "verdict": "confirmed", "counts": {}},
    )
    run = _run(tmp_path, [unfit, no_fit])
    _receipt(run, 2, "orders")
    _receipt(run, 3, "orders")
    plan = reconcile.own_examples(
        run, root=_model(tmp_path), decisions=[{"row": 2, "decision": "example"}]
    )
    assert [r["row"] for r in plan["reword"]] == [2, 3]
    assert plan["written"] == [] and plan["ask"] == [] and plan["add"] == []


def test_a_disputed_row_shows_its_clash_before_the_card_and_mine_only_is_not_replace(tmp_path):
    root = _model(tmp_path)
    (root / "prompt_examples" / "sales").mkdir(parents=True)
    (root / "prompt_examples" / "sales" / "examples.yaml").write_text(
        f"- question: {OMITS_DEFAULT['question']}\n  sql: SELECT channel, COUNT(*) FROM orders"
        " WHERE status != 'cancelled' GROUP BY channel\n  source: correction\n"
    )
    run = _plan_run(tmp_path)
    plan = reconcile.own_examples(run, root=root)  # before any card
    assert [(c["kind"], c["row"]) for c in plan["collisions"]] == [("kept", 7)]
    plan = reconcile.own_examples(run, root=root, decisions=[{"row": 7, "decision": "example"}])
    assert 7 not in [w["row"] for w in plan["written"]]
    plan = reconcile.own_examples(
        run, root=root, decisions=[{"row": 7, "decision": "example"}], replace={7}
    )
    assert 7 in [w["row"] for w in plan["written"]]


def test_two_incoming_rows_under_one_question_are_never_both_written(tmp_path):
    same_a = dict(SCALAR_MATCH, row=1)
    same_b = dict(SCALAR_MATCH, row=2)  # the same SQL: a duplicate, written once
    other = dict(SCALAR_MATCH, row=3, statement="SELECT SUM(net_amount) AS revenue FROM orders")
    run = _run(tmp_path, [same_a, same_b, other])
    for n in (1, 2, 3):
        _receipt(run, n, "orders")
    plan = reconcile.own_examples(run, root=_model(tmp_path))
    assert plan["duplicates"] == [{"row": 2, "question": SCALAR_MATCH["question"], "same_as": 1}]
    (clash,) = plan["collisions"]
    assert clash["kind"] == "incoming" and [r["row"] for r in clash["rows"]] == [1, 3]
    assert clash["chosen"] is None and plan["written"] == []
    plan = reconcile.own_examples(run, root=_model(tmp_path), replace={3})
    assert [w["row"] for w in plan["written"]] == [3]
    (only,) = json.loads(Path(plan["examples_files"]["sales"]).read_text())
    assert only["sql"] == other["statement"]


def test_a_query_over_two_areas_goes_to_the_area_card_then_its_clash_is_checked(tmp_path, capsys):
    root = _model(tmp_path)
    (root / "prompt_examples" / "traffic").mkdir(parents=True)
    (root / "prompt_examples" / "traffic" / "examples.yaml").write_text(
        f"- question: {SCALAR_MATCH['question']}\n  sql: SELECT 1\n  source: correction\n"
    )
    run = _run(tmp_path, [SCALAR_MATCH])
    _receipt(run, 1, "orders", "arrivals")  # sales and traffic
    plan = reconcile.own_examples(run, root=root)
    assert [r["row"] for r in plan["needs_area"]] == [1] and plan["written"] == []
    # the Area card's answer, then the clash in that area holds it until "Replace it"
    plan = reconcile.own_examples(run, root=root, area_for={1: "traffic"})
    assert plan["needs_area"] == [] and [c["area"] for c in plan["collisions"]] == ["traffic"]
    assert plan["written"] == []
    assert (
        reconcile.main(
            [
                "own-examples",
                "--run-dir",
                str(run),
                "--root",
                str(root),
                "--area",
                "1=traffic",
                "--replace",
                "1",
            ]
        )
        == 0
    )
    assert [w["area"] for w in json.loads(capsys.readouterr().out)["written"]] == ["traffic"]
    assert (
        reconcile.main(
            ["own-examples", "--run-dir", str(run), "--root", str(root), "--area", "1=nowhere"]
        )
        == 2
    )
