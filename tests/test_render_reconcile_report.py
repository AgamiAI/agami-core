"""The reconcile report page: one card per row in four beats, on the plugin's own theme. Two rules it
keeps: no result rows, and no control decides anything on its own; a keep is offered only where the
run scored the row `match`."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agami" / "scripts"))

import render_reconcile_report as rr  # noqa: E402

ITEMS = [
    {"row": 3, "label": "Q3 Revenue", "question": "What was total revenue in Q3 2025?",
     "source": "from your CSV, tile 3, with the SQL behind it", "status": "mismatch",
     "expected": "$4,200,000", "answer": "$3,890,000",
     "read": ["your query held on every part but one: it leaves out the declared filter on orders"],
     "how": ["agami read orders, applied the declared filter on status, and matched the metric revenue"],
     "words": ["orders, table caveat: cancelled orders are excluded from revenue"],
     "disagreement": None,
     "change": ["your query counts cancelled orders; add the filter and the numbers meet"],
     "report_path": "local/charts/p/3.html"},
    {"row": 1, "label": "Order count", "question": "How many orders were placed in Q3 2025?",
     "status": "match", "expected": "12,450", "answer": "12,450", "single_cell": True,
     "read": ["a number and a question we read from the label, which you confirmed; no query to check"],
     "how": ["agami read orders with the declared filter on status and matched the metric order count"],
     "change": ["keep it: the numbers agree and nothing is unconfirmed"]},
]


def test_renders_one_card_per_row_in_four_beats_and_the_block_grammar():
    html = rr.render(title="Reconcile · demo", profile="demo", run="20260912-101500", items=ITEMS)
    for text in ("What was total revenue in Q3 2025?", "leaves out the declared filter", "matched the metric revenue",
                 "cancelled orders are excluded from revenue", "add the filter and the numbers meet", "How many orders"):
        assert text in html, text
    for part in ("function verdict(item)", "function section(item, key, title)",
                 "section(item, 'data', 'Data')", "section(item, 'sql', 'SQL')", "section(item, 'checks', 'Checks')"):
        assert part in html, part
    # One card shape, and its bar is coloured by who acts rather than by the outcome.
    assert "shown.map(diffCard)" in html and "FIX_CLASS[fix] || 'noted'" in html
    for token in ("'profile: '", "'reconcile-run: '", "'decisions:'", "'done'"):
        assert token in html, token
    assert 'profile: "demo"' in html and 'run: "20260912-101500"' in html
    # An error row with nothing else on it still has a verdict: the status in words, which the
    # renderer fills when the items file did not, so ACE-138's guarantee holds for a typed file too.
    error = rr.render(title="t", profile="p", run="r", items=[{"row": 7, "question": "q", "status": "error"}])
    assert '"status_words": "could not compare"' in error


def _payload(html: str) -> dict:
    import re
    payload = json.loads(re.search(r"const DATA = \{ profile: .*?, run: .*?, items: (\[.*\]) \};", html).group(1)
                         .replace("\\u003c", "<"))
    return {item["row"]: item for item in payload}


def test_keep_is_offered_only_where_the_run_said_match_with_one_cell():
    """Phase 3e's own predicate, both halves: a table can match and is still never offered."""
    by_row = _payload(rr.render(title="t", profile="p", run="r", items=ITEMS))
    assert by_row[1]["keep_allowed"] is True and by_row[3]["keep_allowed"] is False
    table = dict(ITEMS[1], row=9, single_cell=False, answer="a table of 3 rows, columns region, revenue")
    by_row = _payload(rr.render(title="t", profile="p", run="r", items=[table]))
    assert by_row[9]["keep_allowed"] is False
    by_row = _payload(rr.render(title="t", profile="p", run="r", items=[dict(ITEMS[1], row=8, single_cell=None)]))
    assert by_row[8]["keep_allowed"] is False


def test_result_rows_are_refused_and_a_status_is_required():
    with pytest.raises(ValueError, match="never rendered"):
        rr.render(title="t", profile="p", run="r", items=[{"row": 1, "question": "q", "status": "match", "rows": [[1]]}])
    with pytest.raises(ValueError, match="'status'"):
        rr.render(title="t", profile="p", run="r", items=[{"row": 1, "question": "q"}])
    with pytest.raises(ValueError, match="'read'"):
        rr.render(title="t", profile="p", run="r", items=[{"row": 1, "question": "q", "status": "match", "read": "not a list"}])


def test_only_declared_fields_reach_the_page_and_the_payload_is_safe():
    html = rr.render(title='t <b>"q"</b>', profile='p";alert(1);//', run="r", items=[
        {"row": 1, "question": "why <!-- {{THEME_CSS}} </script>", "status": "error", "secret": "leak-me"}])
    assert "leak-me" not in html
    assert "why \\u003c!-- {{THEME_CSS}} \\u003c/script>" in html
    assert html.count("{{") == 1
    assert 'profile: "p\\";alert(1);//"' in html and "<title>t &lt;b&gt;&quot;q&quot;&lt;/b&gt;" in html


def test_the_cli_writes_the_file_and_reports_the_count(tmp_path, capsys):
    items = tmp_path / "items.json"
    items.write_text(json.dumps(ITEMS))
    out = tmp_path / "report.html"
    assert rr.main(["--title", "t", "--profile", "demo", "--run", "r", "--items-file", str(items), "--out", str(out)]) == 0
    assert out.exists() and "2 rows" in capsys.readouterr().out
    items.write_text(json.dumps([{"row": 1, "question": "q", "status": "maybe"}]))
    assert rr.main(["--title", "t", "--profile", "demo", "--run", "r", "--items-file", str(items), "--out", str(out)]) == 1
    assert "'status'" in capsys.readouterr().err


AUDIT = [{"row": 1, "label": "Paid revenue", "question": "What is paid revenue?", "status": "match_unverified",
          "expected": "5,967,671", "answer": "5,950,667", "delta_pct": -0.28, "single_cell": True, "owner": "you",
          "checks": [
              {"step": "It runs, read-only, on the same road agami uses", "state": "held", "detail": "one SELECT; the zero-row check passed"},
              {"step": "Joins are on the declared keys", "state": "defect", "detail": "orders to payments is on o.id = pay.id; the declared key is pay.order_id"},
              {"step": "No join repeats the rows behind the total", "state": "open", "detail": "depends on the join above"},
              {"step": "The output matches a declared metric", "state": "gap", "detail": "no metric sums payments"},
              {"step": "Noticed", "state": "noted", "detail": "200 of 4000 orders have no payment and are dropped"}],
          "todo": ["Your query: join on pay.order_id = o.id, then re-run this row.", "Nothing to keep yet."],
          "words": ["orders, table caveat: cancelled orders are excluded from revenue"]}]


def test_one_row_and_a_batch_render_the_same_card():
    # There is no audit layout any more: diffAudit was an alias of diffCard before this removed it,
    # and the only thing that ever differed was the opening sentence, which counts the rows itself.
    one, many = rr.render(title="t", profile="p", run="r", items=AUDIT), rr.render(title="t", profile="p", run="r", items=ITEMS)
    for html in (one, many):
        assert "shown.map(diffCard)" in html and "DATA.items.length === 1" in html
        assert "layout" not in html.split("const DATA =")[1].split(";")[0]
    assert not hasattr(rr, "choose_layout")


def test_owner_delta_and_check_states_come_from_closed_sets():
    with pytest.raises(ValueError, match="'owner'"):
        rr.render(title="t", profile="p", run="r", items=[{"row": 1, "question": "q", "status": "match", "owner": "them"}])
    with pytest.raises(ValueError, match="'delta_pct'"):
        rr.render(title="t", profile="p", run="r", items=[{"row": 1, "question": "q", "status": "match", "delta_pct": "7%"}])
    with pytest.raises(ValueError, match="each diff row needs"):
        rr.render(title="t", profile="p", run="r",
                  items=[{"row": 1, "question": "q", "status": "match", "diff": [{"key": "x", "state": "maybe"}]}])


# --- ACE-135: several statements in the SQL block -----------------------------------------------

def test_the_sql_block_lists_every_statement_agami_wrote_and_marks_the_compared_one():
    item = {"row": 1, "question": "How many orders?", "status": "match", "sql_yours": "SELECT COUNT(*) FROM orders",
            "sql_agami": "SELECT COUNT(*) AS n FROM orders", "sql_agami_steps": ["SELECT status FROM orders LIMIT 5", "SELECT COUNT(*) AS n FROM orders"]}
    page = rr.render(title="t", profile="demo", run="r", items=[item])
    assert "the last is compared" in page and "SELECT status FROM orders LIMIT 5" in page
    import pytest
    with pytest.raises(ValueError, match="sql_agami_steps"):
        rr._validate_item(dict(item, sql_agami_steps="SELECT 1"), 0)
