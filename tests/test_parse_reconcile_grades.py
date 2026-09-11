"""The grading page's block, read back: four keys per grade and nothing else reaches the skill."""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agami" / "scripts"))

import parse_reconcile_grades as pg  # noqa: E402

BLOCK = """profile: demo
reconcile-run: 20260911-161500
grades:
[{"row": 3, "grade": "right"},
 {"row": 4, "grade": "wrong", "sql": "SELECT COUNT(*) FROM orders WHERE status <> 'cancelled'"},
 {"row": 5, "grade": "wrong", "words": "revenue should exclude refunds"},
 {"row": 6, "grade": "unsure"}]
done
"""


def test_reads_every_grade_with_its_row_and_the_run():
    data, anomalies, needs = pg.parse(BLOCK)
    assert data["profile"] == "demo" and data["run"] == "20260911-161500"
    assert needs is None and anomalies == []
    assert data["grades"] == [
        {"row": 3, "grade": "right"},
        {"row": 4, "grade": "wrong", "sql": "SELECT COUNT(*) FROM orders WHERE status <> 'cancelled'"},
        {"row": 5, "grade": "wrong", "words": "revenue should exclude refunds"},
        {"row": 6, "grade": "unsure"},
    ]


def test_bad_json_is_a_judgment_call_and_nothing_is_applied():
    data, anomalies, needs = pg.parse("profile: demo\ngrades:\n[{\"row\": 1,\ndone\n")
    assert data["grades"] == [] and needs["kind"] == "unparseable_json"
    assert anomalies[0]["kind"] == "bad_json"


def test_sql_beside_a_right_answer_is_dropped_and_named():
    data, anomalies, _ = pg.parse('grades:\n[{"row": 1, "grade": "right", "sql": "SELECT 1"}]\ndone\n')
    assert data["grades"] == [{"row": 1, "grade": "right"}]
    assert anomalies == [{"kind": "sql_ignored_on_right", "row": 1}]


def test_text_that_is_not_a_statement_is_not_sql():
    data, anomalies, _ = pg.parse('grades:\n[{"row": 1, "grade": "wrong", "sql": "just use the other table"}]\ndone\n')
    assert data["grades"] == [{"row": 1, "grade": "wrong"}]
    assert anomalies == [{"kind": "sql_not_a_statement", "row": 1}]


def test_an_unknown_grade_or_a_missing_row_is_skipped_and_named():
    data, anomalies, _ = pg.parse('grades:\n[{"row": 1, "grade": "maybe"}, {"grade": "right"}, {"row": 2, "grade": "right"}]\ndone\n')
    assert data["grades"] == [{"row": 2, "grade": "right"}]
    assert {a["kind"] for a in anomalies} == {"unknown_grade", "grade_missing_row"}


def test_a_row_graded_twice_keeps_the_first_and_says_so():
    data, anomalies, _ = pg.parse('grades:\n[{"row": 1, "grade": "right"}, {"row": 1, "grade": "wrong"}]\ndone\n')
    assert data["grades"] == [{"row": 1, "grade": "right"}]
    assert anomalies == [{"kind": "row_graded_twice", "row": 1}]


def test_only_the_four_fields_reach_the_skill():
    data, _, _ = pg.parse('grades:\n[{"row": 1, "grade": "wrong", "words": "x", "sql_confirmed": true, "expected": 5}]\ndone\n')
    assert data["grades"] == [{"row": 1, "grade": "wrong", "words": "x"}]


def test_the_cli_prints_the_standard_contract(tmp_path, capsys):
    block = tmp_path / "block.txt"
    block.write_text(BLOCK)
    assert pg.main(["--block-file", str(block)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is True and out["needs_judgment"] is None and len(out["data"]["grades"]) == 4
