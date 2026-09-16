"""Every reconcile page's script runs to the end of its load path under Node with a stub document, so a
reference used before it is declared, a missing function or a typo surfaces here rather than as a
blank page. Skipped where Node is not installed."""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agami" / "scripts"))

import render_reconcile_grades as rg  # noqa: E402
import render_reconcile_intake as ri  # noqa: E402
import render_reconcile_report as rr  # noqa: E402

NODE = shutil.which("node")

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
              "diff": [{"key": "answer", "state": "held", "yours": "10", "agami": "10", "note": None}], "sentence": "The numbers match.", "sql_yours": "SELECT 1", "sql_agami": "SELECT 1"},
             {"row": 2, "question": "What was revenue?", "status": "mismatch", "owner": "model",
              "result": {"data": "differs", "query": "different", "label": "different answer", "unchecked": 1, "differs_in": ["filters"]}, "fix": "semantic_model", "fix_words": "fix the semantic model",
              "diff": [{"key": "filters", "state": "defect", "yours": ["a = 1"], "agami": ["b = 2"], "yours_hi": ["a = 1"], "agami_hi": ["b = 2"], "note": "differ"}], "words": ["orders: \"a caveat\""]}]
    _run(rr.render(title="t", profile="p", run="r", items=items))
    _run(rr.render(title="t", profile="p", run="r", items=items[:1]))  # the audit layout
    # an older items file: beats, no result or fix
    _run(rr.render(title="t", profile="p", run="r", items=[{"row": 1, "question": "q", "status": "match", "read": ["a"], "how": ["b"], "change": []},
                                                            {"row": 2, "question": "q2", "status": "error", "checks": [{"step": "s", "state": "open"}]}]))


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_the_grading_and_intake_pages_load():
    _run(rg.render(title="t", profile="p", run="r", items=[{"row": 1, "question": "q", "answer": "3.1%", "signals": ["joined a to b"]}, {"row": 2, "question": "q2", "answer": "4"}]))
    _run(ri.render(title="t", profile="p", run="r", items=[{"row": 1, "shape": "a", "question": "q", "file": "f.txt", "line": 1},
                                                            {"row": 2, "shape": "d", "label": "Revenue", "expected": "$4.2M", "has_statement": True, "statement_preview": "SELECT 1"}]))
