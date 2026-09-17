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
    assert pi.main(["--block-file", str(block), "--rows-file", str(rows), "--run", "r"]) == 1  # refused: exit 1, so a chain stops
    assert json.loads(capsys.readouterr().out)["ok"] is False and rows.read_text() == before


def test_the_template_files_comment_lines_are_guidance_not_rows(tmp_path):
    f = tmp_path / "reconcile.example.csv"
    f.write_text("# One line per number to check. Keep the header; these # lines are skipped.\n"
                 "# label: the tile's name.  value: the number as shown.\n"
                 "label,value,sql,question\n"
                 "Q3 Revenue,$4.2M,,\n")
    result = reconcile.intake([f], source=None)
    assert [r["label"] for r in result["rows"]] == ["Q3 Revenue"]
    assert [(e["line"], e["reason"]) for e in result["skipped"]] == [(1, "guidance line (starts with #)"), (2, "guidance line (starts with #)")]
    f.write_text("# only guidance\nlabel,value,sql,question\n")
    assert reconcile.intake([f], source=None)["rows"] == []
    # Below the header a leading # is a label, not a comment: a tile can be named "# of orders".
    f.write_text("# guidance\nlabel,value,sql,question\n# of orders,12450,,\n")
    assert [r["label"] for r in reconcile.intake([f], source=None)["rows"]] == ["# of orders"]


def test_the_intake_page_shows_the_number_when_only_a_parsed_value_exists():
    items = ri.items_from_intake({"rows": [{"row": 1, "label": "Orders", "question": "How many orders?", "expected": 12450.0, "raw_value": None,
                                            "provenance": {"shape": "c", "file": "tiles.json", "line": None}}]})
    assert items[0]["expected"] == "12,450"


def test_the_intake_renderer_cli_takes_exactly_one_input_and_refuses_bad_items(tmp_path, capsys):
    intake = tmp_path / "intake.json"
    intake.write_text(json.dumps(INTAKE))
    out = tmp_path / "intake.html"
    assert ri.main(["--title", "t", "--profile", "demo", "--run", "r", "--intake-file", str(intake), "--out", str(out)]) == 0
    assert out.exists() and "3 rows" in capsys.readouterr().out
    # Exactly one of --items-file and --intake-file.
    assert ri.main(["--title", "t", "--profile", "demo", "--run", "r", "--out", str(out)]) == 1
    assert ri.main(["--title", "t", "--profile", "demo", "--run", "r", "--intake-file", str(intake), "--items-file", str(intake), "--out", str(out)]) == 1
    # An items file that is not a list, and one that is unreadable, exit 1 with a message.
    bad = tmp_path / "bad.json"; bad.write_text(json.dumps({"row": 1}))
    assert ri.main(["--title", "t", "--profile", "demo", "--run", "r", "--items-file", str(bad), "--out", str(out)]) == 1
    assert ri.main(["--title", "t", "--profile", "demo", "--run", "r", "--items-file", str(tmp_path / "missing.json"), "--out", str(out)]) == 1
    err = capsys.readouterr().err
    assert "exactly one of" in err and "must be a JSON array" in err


def test_every_item_field_is_validated():
    ok = {"row": 1, "shape": "a", "question": "q"}
    for bad, match in (({"row": "1", "shape": "a"}, "'row' \\(integer\\)"), ({"row": 1, "shape": "z"}, "'shape' must be one of"),
                       ({"row": 1, "shape": "a", "rows": [[1]]}, "whole statement"), ({"row": 1, "shape": "a", "label": 3}, "'label' must be text"),
                       ({"row": 1, "shape": "a", "statement_preview": "x" * 81}, "at most 80 characters"), ("not a dict", "must be an object")):
        with pytest.raises(ValueError, match=match):
            ri.render(title="t", profile="p", run="r", items=[ok, bad])
    html = ri.render(title="t <b>", profile='p"', run="r", items=[ok])
    assert "<title>t &lt;b&gt;" in html and 'profile: "p\\""' in html


def test_the_intake_parser_reports_every_anomaly_and_refuses_a_bad_argument(tmp_path, capsys):
    rows = tmp_path / "rows.json"
    rows.write_text(json.dumps(INTAKE))
    text = ("profile: demo\nprofile: again\nreconcile-run: r\nintake:\n"
            + json.dumps(["not a dict", {"row": 1, "keep": True}, {"row": 1, "keep": False}]) + "\ndone\n")
    data, anomalies, needs = pi.parse(text, {1, 2, 3}, run="r")
    kinds = [a["kind"] for a in anomalies]
    assert "key_repeated" in kinds and "decision_missing_row" in kinds and "row_decided_twice" in kinds
    assert needs["kind"] == "decisions_dropped" and data["decisions"] == [{"row": 1, "keep": True}]
    # --out keeps the rows file untouched and writes the applied rows elsewhere.
    block = tmp_path / "block.txt"
    block.write_text("profile: demo\nreconcile-run: r\nintake:\n" + json.dumps([{"row": 2, "keep": False}]) + "\ndone\n")
    out = tmp_path / "applied.json"
    assert pi.main(["--block-file", str(block), "--rows-file", str(rows), "--run", "r", "--out", str(out)]) == 0
    assert len(json.loads(rows.read_text())["rows"]) == 3 and [r["row"] for r in json.loads(out.read_text())["rows"]] == [1, 3]
    capsys.readouterr()
    assert pi.main(["--block-file", str(block), "--rows-file", str(tmp_path / "missing.json")]) == 2
    printed = json.loads(capsys.readouterr().out)
    assert printed["ok"] is False and printed["needs_judgment"]["kind"] == "bad_argument"


def test_a_guidance_line_above_a_headerless_file_is_recorded_as_skipped(tmp_path):
    f = tmp_path / "tiles.csv"
    f.write_text("# Orders,12450\nQ3 Revenue,$4.2M\n")
    result = reconcile.intake([f], source=None)
    assert [r["label"] for r in result["rows"]] == ["Q3 Revenue"]
    assert result["skipped"] == [{"file": "tiles.csv", "line": 1, "reason": "guidance line (starts with #)"}]


def test_the_intake_page_escapes_the_line_refuses_duplicates_and_a_parse_list():
    with pytest.raises(ValueError, match="'line' must be a whole number"):
        ri.render(title="t", profile="p", run="r", items=[{"row": 1, "shape": "a", "line": "<img src=x onerror=alert(1)>"}])
    with pytest.raises(ValueError, match="share a row number"):
        ri.render(title="t", profile="p", run="r", items=[{"row": 1, "shape": "a"}, {"row": 1, "shape": "c"}])
    html = ri.render(title="t", profile="p", run="r", items=[{"row": 1, "shape": "a", "line": 7}])
    assert "':' + esc(item.line)" in html
    with pytest.raises(ValueError, match="not the output of"):
        ri.items_from_intake([{"label": "x", "expected_value": 1}])
    with pytest.raises(ValueError, match="not an object"):
        ri.items_from_intake({"rows": ["q1"]})

