"""Every reconcile page's script runs to the end of its load path under Node with a stub document, so a
reference used before it is declared, a missing function or a typo surfaces here rather than as a
blank page. Skipped where Node is not installed."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agami" / "scripts"))

import render_reconcile_intake as ri  # noqa: E402
import render_reconcile_report as rr  # noqa: E402

NODE = shutil.which("node")
if NODE is None and os.environ.get("CI"):
    raise RuntimeError("node is required on CI for the page load tests; add a setup-node step")

# A document that answers every call the pages make at load time with an inert element. Structure is
# not modelled: the point is that the script's own code runs, not that the DOM is right.
STUB = r"""
function el() {
  const e = { innerHTML: '', textContent: '', hidden: false, disabled: false, value: '', className: '', placeholder: '', dataset: {}, style: {},
    classList: { toggle() {}, add() {}, remove() {}, contains() { return false; } },
    querySelectorAll() { return []; }, querySelector() { return el(); }, addEventListener() {}, closest() { return el(); }, select() {}, focus() {} };
  return e;
}
const document = { getElementById() { return el(); }, querySelectorAll() { return []; }, querySelector() { return el(); }, addEventListener() {} };
const navigator = { clipboard: { writeText() { return Promise.resolve(); } } };
const window = { document, navigator };
"""


def _script(html: str) -> str:
    blocks = re.findall(r"<script>(.*?)</script>", html, flags=re.S)
    assert blocks, "no script block"
    return blocks[-1]


def _run(html: str) -> None:
    proc = subprocess.run([NODE, "-e", STUB + _script(html) + "\nconsole.log('loaded')"], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0 and "loaded" in proc.stdout, proc.stderr


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_the_report_page_loads_with_result_and_fix_items():
    items = [{"row": 1, "question": "How many orders?", "status": "match", "owner": "keep", "keep_allowed": True,
              "result": {"data": "matches", "query": "same", "label": "match", "unchecked": 0, "differs_in": []}, "fix": "none", "fix_words": "nothing to fix",
              "diff": [{"key": "answer", "state": "held", "section": "data", "yours": "10", "agami": "10", "note": None}], "sentence": "The numbers match.", "sql_yours": "SELECT 1", "sql_agami": "SELECT 1"},
             {"row": 2, "question": "What was revenue?", "status": "mismatch", "owner": "model",
              "result": {"data": "differs", "query": "different", "label": "different answer", "unchecked": 1, "differs_in": ["filters"]}, "fix": "semantic_model", "fix_words": "fix the semantic model",
              "diff": [{"key": "filters", "state": "defect", "section": "sql", "yours": ["a = 1"], "agami": ["b = 2"], "yours_hi": ["a = 1"], "agami_hi": ["b = 2"], "note": "differ"}], "words": ["orders: \"a caveat\""]}]
    _run(rr.render(title="t", profile="p", run="r", items=items))
    _run(rr.render(title="t", profile="p", run="r", items=items[:1]))  # the audit layout
    # an older items file: beats, no result or fix
    _run(rr.render(title="t", profile="p", run="r", items=[{"row": 1, "question": "q", "status": "match", "read": ["a"], "how": ["b"], "change": []},
                                                            {"row": 2, "question": "q2", "status": "error", "checks": [{"step": "s", "state": "open"}]}]))


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_the_report_page_loads_a_card_listing_every_query_agami_tried():
    """The list only renders on a row where a query did not run, which is exactly the card a person
    opens, so a typo in that code must fail here rather than blank the card."""
    attempts = [
        {"query": 1, "sql": "SELECT units_sold FROM orders", "happened": "blocked", "run_by": "agami",
         "why": "agami's safety check blocked it before it reached the database.", "grade": "no_answer",
         "grade_words": "No answer: it did not run."},
        {"query": 2, "sql": "SELECT COUNT(*) FROM orders", "happened": "ran", "run_by": "reconcile",
         "why": "Reconcile ran it after agami's session ended.", "grade": "right", "grade_words": "Right: the same result as your query."},
        {"query": 3, "sql": "SELECT '</pre><b>x</b>' FROM orders", "happened": "not_run", "run_by": "agami",
         "why": "It never ran.", "grade": "not_graded", "grade_words": "Not graded: it never ran."},
        {"query": None, "sql": "", "happened": "not_run", "run_by": "agami", "why": "3 more queries never ran.", "more": 3,
         "grade": "not_graded", "grade_words": "Not graded: they never ran."},
    ]
    items = [{"row": 1, "question": "How many orders?", "status": "error", "owner": "model",
              "result": {"data": "could_not_compare", "query": "not_comparable", "label": "could not compare", "unchecked": 0, "differs_in": []},
              "fix": "semantic_model", "fix_words": "fix the semantic model", "diff": [], "sentence": "This row could not be compared.",
              "sql_yours": "SELECT COUNT(*) FROM orders", "sql_agami": "SELECT units_sold FROM orders", "attempts": attempts}]
    _run(rr.render(title="t", profile="p", run="r", items=items))

    # What the card's SQL section actually shows, in both layouts: with a query of yours beside agami's,
    # and with agami's alone.
    alone = {**items[0], "row": 2, "sql_yours": None}
    script = STUB + _script(rr.render(title="t", profile="p", run="r", items=[items[0], alone]))
    shown = "console.log(JSON.stringify(DATA.items.map(sqlBlock)))"
    proc = subprocess.run([NODE, "-e", script + "\n" + shown], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    for block in json.loads(proc.stdout.strip().splitlines()[-1]):
        assert "Every query agami tried (6)</summary>" in block  # three numbered, and three a line stands for
        assert block.count(" · run by reconcile afterwards") == 1
        assert "It never ran." in block and "No answer: it did not run." in block
        assert "&lt;/pre&gt;&lt;b&gt;x&lt;/b&gt;" in block and "<b>x</b>" not in block


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_the_intake_page_loads():
    _run(ri.render(title="t", profile="p", run="r", items=[{"row": 1, "shape": "a", "question": "q", "file": "f.txt", "line": 1},
                                                            {"row": 2, "shape": "d", "label": "Revenue", "expected": "$4.2M", "has_statement": True, "statement_preview": "SELECT 1"}]))
