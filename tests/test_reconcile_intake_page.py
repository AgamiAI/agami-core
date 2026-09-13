"""The intake page: what was read, before anything runs, in the same design language as the other
pages; and the parser that applies the person's block to the rows file, never by hand."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agami" / "scripts"))

import parse_reconcile_intake as pi  # noqa: E402
import reconcile  # noqa: E402
import render_reconcile_intake as ri  # noqa: E402

INTAKE = {"shape": "d", "skipped": [], "rows": [
    {"row": 1, "label": "Q3 Revenue", "question": "What was total revenue in Q3 2025?", "statement": "SELECT SUM(total_amount) AS revenue FROM orders WHERE status <> 'cancelled'",
     "expected": 4200000.0, "raw_value": "$4.2M", "provenance": {"shape": "d", "source": "the finance dashboard", "file": "tiles.csv", "line": 2}},
    {"row": 2, "label": "Order count", "question": "How many orders were placed?", "statement": None, "expected": 12450.0, "raw_value": "12,450",
     "provenance": {"shape": "c", "source": "the finance dashboard", "file": "tiles.csv", "line": 3}},
    {"row": 3, "label": None, "question": "What is the refund rate?", "statement": None, "expected": None, "raw_value": None,
     "provenance": {"shape": "a", "source": None, "file": "questions.txt", "line": 1}}]}


def test_items_come_from_the_intake_output_and_show_only_a_preview_of_sql():
    items = ri.items_from_intake(INTAKE)
    assert [i["shape"] for i in items] == ["d", "c", "a"]
    assert items[0]["has_statement"] is True and len(items[0]["statement_preview"]) <= 80
    assert items[1]["expected"] == "12,450" and items[2]["expected"] is None
    html = ri.render(title="What we read · demo", profile="demo", run="r", items=items)
    assert "What was total revenue in Q3 2025?" in html and "tiles.csv" in html
    for token in ("'profile: '", "'reconcile-run: '", "'intake:'", "'done'"):
        assert token in html, token
    assert 'data-field="question"' in html and 'data-field="keep"' in html
    # The shared design language is on the page, and the whole statement never is.
    assert ".beat.b4" in html and ".pill.held" in html
    assert "status <> 'cancelled'" in items[0]["statement_preview"] and "SELECT SUM(total_amount)" in html
    with pytest.raises(ValueError, match="whole statement"):
        ri.render(title="t", profile="p", run="r", items=[{"row": 1, "shape": "b", "statement": "SELECT 1"}])


def test_the_block_applies_to_the_rows_file_and_says_what_it_did(tmp_path):
    rows = tmp_path / "rows.json"
    rows.write_text(json.dumps(INTAKE))
    block = tmp_path / "block.txt"
    block.write_text("profile: demo\nreconcile-run: r\nintake:\n" + json.dumps([
        {"row": 1, "keep": True}, {"row": 2, "keep": False}, {"row": 3, "keep": True, "question": "What share of payments were refunded?"}]) + "\ndone\n")
    assert pi.main(["--block-file", str(block), "--rows-file", str(rows), "--run", "r"]) == 0
    applied = json.loads(rows.read_text())
    assert [r["row"] for r in applied["rows"]] == [1, 3]
    assert applied["rows"][1]["question"] == "What share of payments were refunded?"
    assert applied["rows"][1]["provenance"]["question_from"] == "the person, on the intake page"
    assert applied["rows"][0]["question"] == "What was total revenue in Q3 2025?"


def test_a_bad_block_applies_nothing(tmp_path, capsys):
    rows = tmp_path / "rows.json"
    rows.write_text(json.dumps(INTAKE))
    before = rows.read_text()
    known = {1, 2, 3}
    for text, kind in (
        ("profile: demo\nreconcile-run: other\nintake:\n[]\ndone\n", "run_mismatch"),
        ("profile: demo\nreconcile-run: r\ndone\n", "section_missing"),
        ("profile: demo\nreconcile-run: r\nintake:\n[not json\ndone\n", "unparseable_json"),
        ("profile: demo\nreconcile-run: r\nintake:\n" + json.dumps([{"row": 9, "keep": True}, {"row": 1, "keep": "yes"}, {"row": 2, "question": ""}]) + "\ndone\n", "decisions_dropped"),
    ):
        _data, anomalies, needs = pi.parse(text, known, run="r")
        assert needs["kind"] == kind, (text, needs)
    block = tmp_path / "block.txt"
    block.write_text("profile: demo\nreconcile-run: r\nintake:\n" + json.dumps([{"row": 9, "keep": False}]) + "\ndone\n")
    assert pi.main(["--block-file", str(block), "--rows-file", str(rows), "--run", "r"]) == 0
    assert json.loads(capsys.readouterr().out)["ok"] is False and rows.read_text() == before


def test_the_template_files_comment_lines_are_guidance_not_rows(tmp_path):
    f = tmp_path / "reconcile.example.csv"
    f.write_text("# One line per number to check. Keep the header; these # lines are skipped.\n"
                 "# label: the tile's name.  value: the number as shown.\n"
                 "label,value,sql,question\n"
                 "Q3 Revenue,$4.2M,,\n")
    result = reconcile.intake([f], source=None)
    assert [r["label"] for r in result["rows"]] == ["Q3 Revenue"] and result["skipped"] == []
    f.write_text("# only guidance\nlabel,value,sql,question\n")
    assert reconcile.intake([f], source=None)["rows"] == []
