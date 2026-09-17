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
        "diff": [{"key": "rows", "state": "held", "yours": "21 rows", "agami": "21 rows", "note": None, "section": "checks"},
                 {"key": "columns", "state": "defect", "yours": ["number", "channel"], "agami": ["number"], "yours_hi": ["channel"], "note": "scores on columns", "section": "checks"},
                 {"key": "date window", "state": "open", "yours": "date_trunc('year', current_date)", "agami": "placed_at ≥ 2025-06-01", "note": "not folded", "section": "checks"}],
        "sentence": "The two answers differ; the parts that differ: columns.", "words": ['orders: "shipped_at is the anchor."'],
        "change": ["Name the columns you want back."], "todo": ["The question: reword it."], "report_path": "rows/2/receipt.html"}


def test_the_grid_has_one_row_per_check_with_the_category_first_and_the_extra_token_highlighted():
    html = rr.render(title="t", profile="p", run="r", items=[ITEM, dict(ITEM, row=3)])
    assert "function diffGrid(item, section)" in html and "'<div class=\"dg' + (one ? ' one-query' : '')" in html
    # The header names the four columns in this order: mark, what was checked, yours, agami.
    assert '<div class="h"></div><div class="h">checked</div><div class="h">yours</div><div class="h">agami</div>' in html
    # And three when one query was written: a "yours" column on a row where the person wrote no SQL
    # files agami's own facts under their name and leaves the other column empty on every row.
    assert '<div class="h"></div><div class="h">checked</div><div class="h">agami’s query</div>' in html
    # One grid, four tracks: the alignment is structural, not two stacks side by side.
    css = html[html.index("<style>"):html.index("</style>")]
    # The key column is wide enough for its own labels: at 170px "value list for
    # customers.signup_month" wrapped mid-token, which is a column too narrow rather than a word
    # that needed breaking.
    assert re.search(r"\.dg \{[^}]*grid-template-columns: 22px minmax\(140px, 230px\) minmax\(0, 1fr\) minmax\(0, 1fr\)", css)
    assert re.search(r"\.dg\.one-query \{ grid-template-columns: 22px minmax\(140px, 230px\) minmax\(0, 1fr\)", css)
    assert "(side === 'yours' ? 'del' : 'add')" in html and "class=\"tok renamed\"" in html
    assert "MARK = { held: '✓', defect: '✗', open: '○', gap: '▲', noted: '·', differs: '≠' }" in html
    assert "shown.map(diffCard)" in html
    # The sentence and the semantic model's words ride along; the note shows only off a held row.
    assert "r.note && (r.state !== 'held' || r.key === 'columns')" in html and '<div class="quote"><b>What the semantic model says</b>' in html


def test_a_diff_row_is_validated_and_never_carries_result_rows():
    with pytest.raises(ValueError, match="diff row needs a 'key'"):
        rr.render(title="t", profile="p", run="r", items=[dict(ITEM, diff=[{"state": "held"}])])
    with pytest.raises(ValueError, match="text or a list of tokens"):
        rr.render(title="t", profile="p", run="r", items=[dict(ITEM, diff=[{"key": "k", "state": "held", "yours": 3, "section": "checks"}])])
    with pytest.raises(ValueError, match="never rendered"):
        rr.render(title="t", profile="p", run="r", items=[dict(ITEM, diff=[{"key": "k", "state": "held", "rows": [[1]], "section": "checks"}])])
    with pytest.raises(ValueError, match="'sentence' must be text"):
        rr.render(title="t", profile="p", run="r", items=[dict(ITEM, sentence=["no"])])


def test_each_decision_says_what_it_is_and_what_happens_and_the_box_asks_for_the_right_thing():
    html = rr.render(title="t", profile="p", run="r", items=[ITEM, dict(ITEM, row=3)])
    for token in ("change the semantic model", "fix your query", "reword the question",
                  "What should the semantic model say?", "What you will fix, in a few words (optional)", "The question, in the words you mean",
                  "SUGGEST = { keep: 'keep', model: 'change', you: 'fix', question: 'reword'", 'class="tag suggested"', 'class="opt'):
        assert token in html, token
    # The block's contract is untouched: the same five radio values, the same words field.
    for token in ("value=\"' + v + '\"", 'data-field="words"', "['change', 'fix', 'reword', 'example'].includes(d.decision)"):
        assert token in html, token


def test_the_two_statements_sit_collapsed_under_the_grid():
    item = dict(ITEM, sql_yours="SELECT number FROM requests", sql_agami="SELECT request FROM request_items")
    html = rr.render(title="t", profile="p", run="r", items=[item, dict(item, row=3)])
    assert "function sqlBlock(item)" in html and 'function section(item, key, title)' in html
    assert "SELECT number FROM requests" in html and "SELECT request FROM request_items" in html
    with pytest.raises(ValueError, match="statement's text"):
        rr.render(title="t", profile="p", run="r", items=[dict(item, sql_yours=["no"])])


def test_the_second_reviews_findings_on_the_renderer():
    # highlights and notes are typed; checks never carry rows; rows are unique; keep_allowed is the verb's word
    with pytest.raises(ValueError, match="tokens to highlight"):
        rr.render(title="t", profile="p", run="r", items=[dict(ITEM, diff=[{"key": "k", "state": "held", "yours_hi": "channel", "section": "checks"}])])
    with pytest.raises(ValueError, match="'note' must be text"):
        rr.render(title="t", profile="p", run="r", items=[dict(ITEM, diff=[{"key": "k", "state": "held", "note": 3, "section": "checks"}])])
    with pytest.raises(ValueError, match="inside a diff row"):
        rr.render(title="t", profile="p", run="r", items=[dict(ITEM, diff=[{"key": "k", "state": "held", "rows": [[1]], "section": "checks"}])])
    with pytest.raises(ValueError, match="share a row number"):
        rr.render(title="t", profile="p", run="r", items=[ITEM, ITEM])
    with pytest.raises(ValueError, match="true or false"):
        rr.render(title="t", profile="p", run="r", items=[dict(ITEM, keep_allowed="yes")])
    html = rr.render(title="t", profile="p", run="r", items=[dict(ITEM, diff=[{"key": "k", "state": "held", "secret": "leak-me", "section": "checks"}]), dict(ITEM, row=3)])
    assert "leak-me" not in html
    # the verb's keep gate wins over the renderer's two facts
    gated = rr.render(title="t", profile="p", run="r", items=[dict(ITEM, status="match", single_cell=True, keep_allowed=False), dict(ITEM, row=3)])
    assert '"keep_allowed": false' in gated
    # the search box reads the grid, the sentence and the SQL; the key column wraps
    assert "(item.diff || []).flatMap(r => [r.key, r.note].concat(r.yours || [], r.agami || []))" in html and "item.sentence, item.sql_yours, item.sql_agami" in html
    # `break-word`, not `anywhere`: the latter splits at any character, so a label that fits on two
    # lines still came apart mid-token.
    assert re.search(r"\.dg \.k \{[^}]*overflow-wrap: break-word", html)
    # every CSS variable the page uses is defined by the theme or the shared sheet
    css = html[html.index("<style>"):html.index("</style>")]
    assert set(re.findall(r"var\((--[a-z0-9-]+)", css)) - set(re.findall(r"(--[a-z0-9-]+)\s*:", css)) == set()
    # a suggested keep on a row that cannot be kept falls back to nothing
    assert "(SUGGEST[item.owner] === 'keep' && !item.keep_allowed) ? 'nothing'" in html


def test_the_result_pill_and_the_fix_pill_come_from_the_verbs_two_fields():
    item = dict(ITEM, result={"data": "matches", "query": "different", "label": "same answer, different query", "unchecked": 2, "differs_in": ["filters"]},
                fix="examples", fix_words="add an example", keep_allowed=True)
    html = rr.render(title="t", profile="p", run="r", items=[item, dict(item, row=3)])
    for token in ("function verdict(item)", "'same answer, different query'".replace("'", '"')[1:-1], "const FIX_WORDS = {};",
                  "function suggestionFor(item)", "item.fix === 'examples' && item.keep_allowed ? 'keep'", 'id="result-chips"', 'class="vact"', '"fix": "examples"', '"label": "same answer, different query"'):
        assert token in html, token
    with pytest.raises(ValueError, match="'result' needs data in"):
        rr.render(title="t", profile="p", run="r", items=[dict(item, result={"data": "maybe", "query": "same", "label": "x"})])
    with pytest.raises(ValueError, match="'fix' must be one of"):
        rr.render(title="t", profile="p", run="r", items=[dict(item, fix="rewrite")])
    # an older items file without the two fields still renders: the verdict falls back to the
    # status in words, and the bar to the owner.
    old = dict(ITEM)
    html = rr.render(title="t", profile="p", run="r", items=[old, dict(old, row=3)])
    assert "r.label || item.status_words" in html and "FIX_CLASS[fix] || 'noted'" in html


def test_the_result_chips_filter_and_replace_the_status_chips_when_labels_exist():
    item = dict(ITEM, result={"data": "differs", "query": "different", "label": "different answer", "unchecked": 0, "differs_in": ["columns"]}, fix="query", fix_words="fix your query")
    html = rr.render(title="t", profile="p", run="r", items=[item, dict(item, row=3)])
    for token in ("view.result.has((item.result || {}).label || '')", 'data-result="\' + esc(label) + \'"'.replace("\\", ""), "toggle(view.result, el.dataset.result)",
                  "document.getElementById('result-row').hidden = facets === 0 || DATA.items.length < 2;", "view.result.clear();"):
        assert token in html, token


def test_the_card_after_the_second_read_question_first_folding_checks_example_and_prefill():
    item = dict(ITEM, prefill={"change": "add values declared on x", "fix": "remove channel", "reword": "List all orders placed from June this year.", "example": ""},
                result={"data": "partly", "query": "different", "label": "same rows, different columns", "unchecked": 0, "differs_in": ["columns"]}, fix="query", fix_words="fix your query")
    html = rr.render(title="t", profile="p", run="r", items=[item, dict(item, row=3)])
    for token in ('<span class="title q">\' + esc(item.question) + \'</span>'.replace("\\", ""), "function verdict(item)", '<div class="vl">\' + esc(label) + \'</div>',
                  "[item.label, item.source].filter(Boolean).join(' · ')", "<details class=\"sec\"".replace("\\", ""), "</summary>'", "section(item, 'data', 'Data')",
                  "example: { word: 'add an example'", "function prefillFor(item, decision)", "['change', 'fix', 'reword', 'example'].includes(d.decision)",
                  "function suggestionFor(item)", "item.fix === 'examples' && item.keep_allowed ? 'keep'", '"prefill": {"change": "add values declared on x"'):
        assert token in html, token
    # the pinned contract stays
    for token in ("['change', 'fix', 'reword', 'example'].includes(e.target.value)", 'data-field="words"'):
        assert token in html, token


def test_list_cells_read_like_a_diff():
    item = dict(ITEM, diff=[{"key": "columns", "state": "defect", "yours": ["number", "channel"], "agami": ["o.number", "region"], "yours_hi": ["channel"], "agami_hi": ["region"], "renamed": [["number", "o.number"]], "section": "checks"}])
    html = rr.render(title="t", profile="p", run="r", items=[item, dict(item, row=3)])
    for token in ("(state === 'differs' ? 'diff' : (side === 'yours' ? 'del' : 'add'))", "class=\"tok renamed\" title=\"same values as '", '"renamed": [["number", "o.number"]]'):
        assert token.replace("\\", "") in html, token
    with pytest.raises(ValueError, match="name pairs"):
        rr.render(title="t", profile="p", run="r", items=[dict(item, diff=[{"key": "columns", "state": "held", "renamed": ["number"], "section": "checks"}])])


def test_the_checks_start_closed_and_the_palette_has_one_meaning_per_color():
    html = rr.render(title="t", profile="p", run="r", items=[ITEM, dict(ITEM, row=3)])
    assert "'<details class=\"sec\"><summary>" in html and "(open ? ' open' : '')" not in html
    css = html[html.index("<style>"):html.index("</style>")]
    assert "--agami:" in css and ".pill.agami { background: var(--agami-bg); color: var(--agami); }" in css
    assert ".dg .v .tok.add { background: var(--agami-bg); color: var(--agami);" in css and "background: var(--agami-bg); color: var(--agami); border-radius: 999px" in css
    # The colour still has its one meaning; what is gone is the standalone key for it, since the
    # fix chips carry the same colours with words, counts and filtering.
    assert '<span class="chips result"' in html and '<div class="legend"' not in html

