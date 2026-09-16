"""The 2d row record is a function of the row directory: `reconcile.py record` builds it from the
files the run wrote and appends it to the checkpoint, so nothing in it is typed by the session."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agami" / "scripts"))

import reconcile  # noqa: E402

LEDGER_OK = {"rows": [{"part": "runs", "verdict": "confirmed", "kind": None, "depends_on": [], "evidence": {}, "note": ""}],
             "verdict": "confirmed", "counts": {"confirmed": 1}}
CLAIMS = {"claims": [{"name": "tables", "status": "agrees", "generated": ["orders"], "golden": ["orders"]}]}


def _run(tmp_path: Path) -> Path:
    run = tmp_path / "20260913-101500"
    (run / "rows").mkdir(parents=True)
    (run / "intake.json").write_text(json.dumps({"rows": [
        {"row": 1, "label": "Orders", "question": "How many orders?", "statement": "SELECT COUNT(*) AS n FROM orders",
         "expected": None, "provenance": {"shape": "b", "source": "the sheet", "file": "plan.csv", "line": 2}},
        {"row": 2, "label": None, "question": "Which region sells most?", "statement": None, "expected": None,
         "provenance": {"shape": "a", "source": None, "file": "questions.txt", "line": 1}},
        {"row": 3, "label": "Revenue", "question": "What is revenue?", "statement": None, "expected": 10.0,
         "provenance": {"shape": "c", "source": "the dashboard", "file": "tiles.csv", "line": 3}},
        {"row": 4, "label": "By region", "question": "Orders by region?", "statement": "SELECT region, COUNT(*) FROM orders GROUP BY region",
         "expected": None, "provenance": {"shape": "b", "source": None, "file": "plan.csv", "line": 5}},
    ]}))
    return run


def _files(run: Path, row: int, **files: str) -> Path:
    row_dir = run / "rows" / str(row)
    row_dir.mkdir(parents=True, exist_ok=True)
    for name, text in files.items():
        (row_dir / name.replace("__", ".").replace("_", "-")).write_text(text)
    return row_dir


def _records(run: Path) -> dict[int, dict]:
    return {json.loads(line)["row"]: json.loads(line) for line in (run / "rows.jsonl").read_text().splitlines() if line.strip()}


def test_a_statement_row_that_matches_is_built_from_its_files(tmp_path, capsys):
    run = _run(tmp_path)
    _files(run, 1, agami_answer__json=json.dumps({"row": 1, "question": "How many orders?", "sql": "SELECT COUNT(id) AS n FROM orders",
                                                   "statements": ["SELECT id FROM orders LIMIT 3", "SELECT COUNT(id) AS n FROM orders"], "error": None}),
           actual__csv="n\n42\n", statement__csv="n\n42\n", ledger__json=json.dumps(LEDGER_OK), claims__json=json.dumps(CLAIMS))
    (run / "rows" / "1" / "statement-receipt.json").write_text("{}")
    assert reconcile.main(["record", "--run-dir", str(run), "--row", "1"]) == 0
    printed = json.loads(capsys.readouterr().out)
    rec = _records(run)[1]
    assert rec == printed
    # The pinned 2d keys, every one of them present.
    for key in ("row", "label", "question", "expected", "actual", "delta_pct", "match", "status", "report_path", "sql", "recorded", "error",
                "provenance", "statement", "statement_recorded", "statement_receipt_path", "receipt_path", "ledger", "ledger_verdict",
                "comparison", "claims", "finding_keys", "agami_statements"):
        assert key in rec, key
    assert rec["status"] == "match" and rec["match"] is True
    assert rec["expected"] == 42.0 and rec["actual"] == 42.0  # 1.5f: the statement's one cell is the expected value
    assert rec["recorded"] == {"columns": ["n"], "rows": [[42.0]]} and rec["statement_recorded"] == {"columns": ["n"], "rows": [[42.0]]}
    assert rec["comparison"]["scalar"]["match"] is True and rec["delta_pct"] == 0.0
    assert rec["sql"] == "SELECT COUNT(id) AS n FROM orders" and rec["agami_statements"] == ["SELECT id FROM orders LIMIT 3", "SELECT COUNT(id) AS n FROM orders"]
    assert rec["ledger_verdict"] == "confirmed" and rec["claims"] == CLAIMS and rec["statement_receipt_path"].endswith("rows/1/statement-receipt.json")
    assert rec["provenance"]["shape"] == "b" and rec["receipt_path"] is None and rec["finding_keys"] == []
    assert "words" not in rec   # its only writer went with the grading page (ACE-150)


def test_a_number_row_that_differs_reads_the_diff_it_was_given_or_computes_one(tmp_path):
    run = _run(tmp_path)
    _files(run, 3, agami_answer__json=json.dumps({"sql": "SELECT SUM(amount) FROM orders", "statements": ["SELECT SUM(amount) FROM orders"], "error": None}),
           actual__csv="revenue\n9\n")
    rec = reconcile.record(run, 3)
    assert rec["status"] == "mismatch" and rec["match"] is False and rec["expected"] == 10.0 and rec["actual"] == 9.0
    assert rec["delta_pct"] == pytest.approx(-0.1) and rec["comparison"]["scalar"]["reason"] == "mismatch"
    assert rec["agami_statements"] == []  # one statement carries no list
    # A diff.json the skill wrote (Phase 2c) wins over the computed one.
    _files(run, 3, diff__json=json.dumps({"match": True, "delta": 0.0, "delta_pct": 0.0, "reason": "match"}))
    rec = reconcile.record(run, 3)
    assert rec["status"] == "match" and rec["comparison"]["scalar"]["reason"] == "match"


def test_a_table_row_reads_the_comparison_and_keeps_the_shape_not_the_rows(tmp_path):
    run = _run(tmp_path)
    score = {"status": "scored", "accuracy": 1.0, "reason": "", "unmatched_golden_columns": [], "column_pairs": [["region", "region"], ["n", "count"]],
             "golden_row_count": 3, "generated_row_count": 3, "order_sensitive": False, "column_agreement": [1.0, 1.0], "paired_row_share": 1.0}
    _files(run, 4, agami_answer__json=json.dumps({"sql": "SELECT region, COUNT(*) AS count FROM orders GROUP BY region", "error": None}),
           actual__csv="region,count\nEU,2\nUS,5\nAPAC,1\n", statement__csv="region,n\nEU,2\nUS,5\nAPAC,1\n",
           comparison__json=json.dumps(score), ledger__json=json.dumps(LEDGER_OK))
    rec = reconcile.record(run, 4)
    assert rec["status"] == "match" and rec["comparison"] == {"result_set": score}
    assert rec["recorded"] == {"columns": ["region", "count"], "row_count": 3} and rec["statement_recorded"] == {"columns": ["region", "n"], "row_count": 3}
    assert rec["expected"] is None and rec["actual"] is None  # no single cell on either side


def test_an_agami_failure_is_an_error_row_with_nothing_mistakable_for_an_answer(tmp_path):
    run = _run(tmp_path)
    _files(run, 1, agami_answer__json=json.dumps({"sql": None, "statements": [], "error": "the generator exited without answering"}),
           statement__csv="n\n42\n", ledger__json=json.dumps(LEDGER_OK))
    rec = reconcile.record(run, 1)
    assert rec["status"] == "error" and rec["sql"] is None and rec["recorded"] is None and rec["actual"] is None
    assert rec["error"] == "the generator exited without answering" and rec["expected"] == 42.0


def test_a_statement_agami_wrote_but_did_not_run_names_the_run_file(tmp_path):
    run = _run(tmp_path)
    _files(run, 1, agami_answer__json=json.dumps({"sql": "SELECT 1", "error": None}), statement__csv="n\n42\n",
           agami_run__json=json.dumps({"status": "failed", "kind": "network", "detail": "The database was unreachable."}))
    rec = reconcile.record(run, 1)
    assert rec["status"] == "error" and rec["error"] == "agami's statement did not run: The database was unreachable."


def test_a_missing_answer_file_is_refused_by_name(tmp_path, capsys):
    run = _run(tmp_path)
    assert reconcile.main(["record", "--run-dir", str(run), "--row", "1"]) == 2
    assert "rows/1/agami-answer.json is missing" in capsys.readouterr().err


def test_a_question_only_row_records_ungraded_and_takes_no_expected_value_from_the_run(tmp_path):
    """agami answered and nothing in the run can say whether the answer is right, so the row waits
    for a person rather than reading as an error. Its `expected` stays empty on purpose: agami's own
    result becoming the expected value would compare agami against agami and always match."""
    run = _run(tmp_path)
    _files(run, 2, agami_answer__json=json.dumps({"sql": "SELECT region FROM orders", "error": None}), actual__csv="region\nEU\n")
    rec = reconcile.record(run, 2)
    assert rec["status"] == reconcile.UNGRADED
    assert rec["expected"] is None and rec["statement"] is None and rec["error"] is None
    assert rec["sql"] == "SELECT region FROM orders"


def test_a_row_run_again_replaces_its_record_and_leaves_the_others(tmp_path):
    run = _run(tmp_path)
    _files(run, 3, agami_answer__json=json.dumps({"sql": "SELECT 1", "error": None}), actual__csv="revenue\n9\n")
    _files(run, 1, agami_answer__json=json.dumps({"sql": "SELECT 1", "error": None}), actual__csv="n\n42\n", statement__csv="n\n42\n")
    reconcile.record(run, 3)
    reconcile.record(run, 1)
    _files(run, 3, actual__csv="revenue\n10\n")
    reconcile.record(run, 3)
    lines = [json.loads(line) for line in (run / "rows.jsonl").read_text().splitlines()]
    assert [line["row"] for line in lines] == [1, 3] and lines[1]["status"] == "match"
    assert reconcile.next_chunk(run)["finished"] == 2


def test_a_comparison_file_cannot_turn_a_question_only_row_into_a_match(tmp_path):
    """ACE-150, the never-ground-truth rule at its sharpest. Phase 2.5 writes agami's own query as
    `statement.sql` so the ledger can grade it, which means `statement.csv` holds agami's own result.
    Any comparison built from it scores agami against agami at accuracy 1.0. The test for "nothing of
    the person's to compare against" therefore runs BEFORE the comparison files are read, not after:
    with it fourth in the chain this row recorded `match`, and the card said "the two answers match
    row for row" about one answer.
    """
    run = _run(tmp_path)   # row 2 is the question-only row: no statement of the person's, no number
    row = _files(run, 2, agami_answer__json=json.dumps({"sql": "SELECT region FROM orders", "error": None}),
                 actual__csv="region\nEU\nUS\n", statement__csv="region\nEU\nUS\n",
                 comparison__json=json.dumps({"status": "scored", "accuracy": 1.0,
                                              "golden_row_count": 2, "generated_row_count": 2}))
    rec = reconcile.record(run, 2)
    assert rec["status"] == reconcile.UNGRADED
    assert rec["match"] is None and rec["comparison"] is None and rec["expected"] is None
    # A scalar diff on disk is refused on the same grounds.
    (row / "comparison.json").unlink()
    (row / "diff.json").write_text(json.dumps({"match": True, "delta": 0, "delta_pct": 0.0}))
    assert reconcile.record(run, 2)["status"] == reconcile.UNGRADED
