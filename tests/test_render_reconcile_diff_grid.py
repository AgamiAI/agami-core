"""The report card is a grid: one row per check, the category first, yours and agami as values with
the differing tokens highlighted, and the sides aligned by construction. Items without a diff keep
the four beats, so an older items file still renders."""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agami" / "scripts"))

import render_reconcile_report as rr  # noqa: E402

ITEM = {"row": 2, "label": "Orders since June", "question": "List all orders placed from June this year.", "source": "your validation sheet, plan.csv:3",
        "status": "mismatch", "expected": "21 rows", "answer": "21 rows", "single_cell": False, "owner": "question",
        "diff": [{"key": "rows", "state": "held", "yours": "21 rows", "agami": "21 rows", "note": None},
                 {"key": "columns", "state": "defect", "yours": ["number", "channel"], "agami": ["number"], "yours_hi": ["channel"], "note": "scores on columns"},
                 {"key": "date window", "state": "open", "yours": "date_trunc('year', current_date)", "agami": "placed_at ≥ 2025-06-01", "note": "not folded"}],
        "sentence": "The two answers differ; the parts that differ: columns.", "words": ['orders: "shipped_at is the anchor."'],
        "change": ["Name the columns you want back."], "todo": ["The question: reword it."], "report_path": "rows/2/receipt.html"}


def test_the_grid_has_one_row_per_check_with_the_category_first_and_the_extra_token_highlighted():
    html = rr.render(title="t", profile="p", run="r", items=[ITEM, dict(ITEM, row=3)])
    assert "function diffGrid(item)" in html and 'class="dg"' in html
    # The header names the four columns in this order: mark, what was checked, yours, agami.
    assert '<div class="h"></div><div class="h">checked</div><div class="h">yours</div><div class="h">agami</div>' in html
    # One grid, four tracks: the alignment is structural, not two stacks side by side.
    css = html[html.index("<style>"):html.index("</style>")]
    assert re.search(r"\.dg \{[^}]*grid-template-columns: 22px minmax\(120px, 170px\) minmax\(0, 1fr\) minmax\(0, 1fr\)", css)
    assert "'<span class=\"tok' + ((hi || []).includes(tok) ? ' hi' : '')" in html
    assert "MARK = { held: '✓', defect: '✗', open: '○', gap: '▲', noted: '·' }" in html
    assert "diffCard(item)" in html and "diffAudit(item)" in html and 'layout: "cards"' in html
    # The sentence and the semantic model's words ride along; the note shows only off a held row.
    assert "r.note && r.state !== 'held'" in html and '<details class="words"><summary>The semantic model says</summary>' in html


def test_one_item_with_a_diff_takes_the_audit_layout_and_keeps_the_rail():
    html = rr.render(title="t", profile="p", run="r", items=[ITEM])
    assert 'layout: "audit"' in html and "function diffAudit(item)" in html and '<div class="rail"><b>What to do</b>' in html


def test_an_item_without_a_diff_still_renders_the_beats():
    old = {"row": 1, "question": "q", "status": "match", "owner": "keep", "read": ["a"], "how": ["b"], "change": []}
    html = rr.render(title="t", profile="p", run="r", items=[old, dict(old, row=2)])
    assert "beat b4 ' + esc(owner)" in html and "item.diff && item.diff.length ?" in html


def test_a_diff_row_is_validated_and_never_carries_result_rows():
    with pytest.raises(ValueError, match="diff row needs a 'key'"):
        rr.render(title="t", profile="p", run="r", items=[dict(ITEM, diff=[{"state": "held"}])])
    with pytest.raises(ValueError, match="text or a list of tokens"):
        rr.render(title="t", profile="p", run="r", items=[dict(ITEM, diff=[{"key": "k", "state": "held", "yours": 3}])])
    with pytest.raises(ValueError, match="never rendered"):
        rr.render(title="t", profile="p", run="r", items=[dict(ITEM, diff=[{"key": "k", "state": "held", "rows": [[1]]}])])
    with pytest.raises(ValueError, match="'sentence' must be text"):
        rr.render(title="t", profile="p", run="r", items=[dict(ITEM, sentence=["no"])])
