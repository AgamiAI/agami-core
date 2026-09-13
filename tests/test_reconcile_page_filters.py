"""Every reconcile page filters the same way: the count chips are toggles, a search box narrows by a
word, a "showing" line says how many, and the block a page builds covers every decided row whether
or not it is shown. The bar hides itself on a page of one row."""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agami" / "scripts"))

import render_reconcile_grades as rg  # noqa: E402
import render_reconcile_intake as ri  # noqa: E402
import render_reconcile_report as rr  # noqa: E402

CARDS = [{"row": 1, "question": "How many orders were placed?", "status": "match", "owner": "keep"},
         {"row": 2, "question": "What was total revenue?", "status": "mismatch", "owner": "model", "read": ["Your SQL counts cancelled orders."]}]
GRADES = [{"row": 1, "question": "What is the refund rate?", "answer": "3.1%"}, {"row": 2, "question": "How many customers?", "answer": "412"}]
INTAKE = [{"row": 1, "shape": "a", "question": "q1"}, {"row": 2, "shape": "c", "label": "Orders", "expected": "12"}]

COMMON = ('id="filters"', 'id="q"', 'id="showing"', 'id="clear"', "data-filter=", "function visible()", "'Showing ' +")


def _assert_bar(html: str, extra: tuple[str, ...] = ()) -> None:
    for token in COMMON + extra:
        assert token in html, token


def test_the_report_page_filters_by_status_owner_and_word_and_the_block_reads_every_row():
    html = rr.render(title="t", profile="p", run="r", items=CARDS)
    _assert_bar(html, ("data-owner=", "OWNER_SHORT", "function haystack(item)"))
    # The audit layout is one row: the bar hides itself, and so does any page of one row.
    assert "bar.hidden = DATA.layout === 'audit' || DATA.items.length < 2" in html
    # decided() is what generateDecisions reads, and it walks the decisions, not visible().
    decided = html[html.index("function decided()"):html.index("function renderSummary()")]
    assert "visible()" not in decided and "Object.keys(decisions)" in decided
    assert "Your decisions on the hidden rows are kept." in html


def test_the_grading_page_filters_by_grade_state_and_word_and_keeps_its_pinned_tokens():
    html = rg.render(title="t", profile="p", run="r", items=GRADES)
    _assert_bar(html, ("GRADE_WORDS", "ungraded"))
    for token in ("['right', 'wrong', 'unsure']", 'type="radio"', 'data-field="sql"', 'data-field="words"', "hidden = e.target.value !== 'wrong'"):
        assert token in html, token
    graded = html[html.index("function graded()"):html.index("function renderSummary()")]
    assert "visible()" not in graded
    assert "document.getElementById('filters').hidden = DATA.items.length < 2" in html


def test_the_intake_page_filters_by_shape_and_word_and_the_block_reads_every_row():
    html = ri.render(title="t", profile="p", run="r", items=INTAKE)
    _assert_bar(html)
    decided = html[html.index("function decided()"):html.index("function renderSummary()")]
    assert "visible()" not in decided and "DATA.items.map" in decided
    assert "Your edits on the hidden rows are kept." in html
