"""The reconcile grading page: one row per question, the AI's answer, the receipt's signals, and a
grade. Two rules it keeps: no result rows, and no control decides anything on its own."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agami" / "scripts"))

import render_reconcile_grades as rg  # noqa: E402

ITEMS = [
    {"row": 1, "question": "How many orders were placed in August?", "answer": "4,000",
     "signals": ["1 unreviewed join: orders → customers"], "report_path": "local/charts/p/1.html"},
    {"row": 2, "question": "What was revenue by region?", "answer": "a table of 3 rows, columns region, revenue",
     "signals": []},
]


def test_renders_one_card_per_question_and_the_block_grammar(tmp_path):
    html = rg.render(title="Grade agami's answers · demo", profile="demo", run="20260911-161500", items=ITEMS)
    assert "How many orders were placed in August?" in html
    assert "1 unreviewed join" in html and "a table of 3 rows" in html
    # The block the page builds is the grammar the parser reads.
    for token in ("'profile: '", "'reconcile-run: '", "'grades:'", "'done'"):
        assert token in html, token
    assert 'profile: "demo"' in html and 'run: "20260911-161500"' in html
    # The three grades and the two text fields exist, and the fields hide until "wrong" is chosen.
    # The controls are drawn by the page's script, so the assertion is on the script's own list.
    assert "['right', 'wrong', 'unsure']" in html and 'type="radio"' in html
    assert 'data-field="sql"' in html and 'data-field="words"' in html
    assert "hidden = e.target.value !== 'wrong'" in html


def test_result_rows_are_refused_not_rendered():
    with pytest.raises(ValueError, match="never rendered"):
        rg.render(title="t", profile="p", run="r", items=[{"row": 1, "question": "q", "rows": [[1]]}])
    with pytest.raises(ValueError, match="never rendered"):
        rg.render(title="t", profile="p", run="r",
                  items=[{"row": 1, "question": "q", "recorded": {"columns": ["n"], "rows": [[1]]}}])


def test_an_item_needs_a_row_number_and_a_question():
    with pytest.raises(ValueError, match="'row'"):
        rg.render(title="t", profile="p", run="r", items=[{"question": "q"}])
    with pytest.raises(ValueError, match="'question'"):
        rg.render(title="t", profile="p", run="r", items=[{"row": 1, "question": ""}])


def test_only_the_declared_fields_reach_the_page():
    html = rg.render(title="t", profile="p", run="r",
                     items=[{"row": 1, "question": "q", "answer": "1", "secret": "leak-me"}])
    assert "leak-me" not in html


def test_a_closing_script_tag_in_a_question_cannot_end_the_script():
    html = rg.render(title="t", profile="p", run="r",
                     items=[{"row": 1, "question": "why </script><script>alert(1)"}])
    assert "</script><script>alert" not in html


def test_the_cli_writes_the_file_and_reports_the_count(tmp_path, capsys):
    items = tmp_path / "items.json"
    items.write_text(json.dumps(ITEMS))
    out = tmp_path / "grade.html"
    assert rg.main(["--title", "t", "--profile", "demo", "--run", "r", "--items-file", str(items),
                    "--out", str(out)]) == 0
    assert out.exists() and "2 questions" in capsys.readouterr().out


def test_the_cli_refuses_bad_items_with_a_sentence_not_a_traceback(tmp_path, capsys):
    items = tmp_path / "items.json"
    items.write_text(json.dumps([{"row": "one", "question": "q"}]))
    assert rg.main(["--title", "t", "--profile", "demo", "--run", "r", "--items-file", str(items),
                    "--out", str(tmp_path / "grade.html")]) == 1
    assert "'row'" in capsys.readouterr().err
