#!/usr/bin/env python3
"""
Reconcile report page renderer.

One page per run, one card per row, told in the four beats Phase 3 tells them in: what you gave us
and how we checked it; what agami did with the question and what it answered; how it got there;
what to change or keep. Built from the same theme, logos and paste-back grammar as the grading page
and the model explorer, so a run is shown the way the semantic model is shown. Stdlib only.

Two rules, the grading page's own: never a result row (one cell or a shape), and no control decides
anything on its own. A "keep" is the person's yes to Phase 3e's offer for that row and is offered
only where the run scored the row `match`; every other decision goes back through the door that
already exists (save-correction for a definition, the person for a fix or a reword).

Usage:

    python3 render_reconcile_report.py \\
        --title "Reconcile · default" --profile default --run 20260912-101500 \\
        --items-file /tmp/agami-reconcile-report-items.json \\
        --out <artifacts_dir>/local/reconcile/20260912-101500/report.html
"""

from __future__ import annotations

import argparse
import datetime
import html
import json
import os
import re
import sys
from pathlib import Path

SHARED_DIR = Path(__file__).resolve().parent.parent / "shared"
TEMPLATE_PATH = SHARED_DIR / "reconcile-report-template.html"
LOGO_DARK_PATH = SHARED_DIR / "agami-logo-dark.svg"
LOGO_LIGHT_PATH = SHARED_DIR / "agami-logo-light.svg"
THEME_PATH = SHARED_DIR / "theme.css"
PAGE_CSS_PATH = SHARED_DIR / "reconcile-pages.css"

# What one card may carry, beat by beat. Every text field is DISPLAY text the skill already wrote in
# plain language; the lists are one sentence per line. A `rows` or `recorded` key is refused.
_FIELDS = ("row", "label", "question", "source", "status", "expected", "answer", "delta_pct", "single_cell",
           "owner", "read", "how", "words", "disagreement", "change", "checks", "todo", "report_path", "diff", "sentence", "sql_yours", "sql_agami", "keep_allowed", "result", "fix", "fix_words")
_LISTS = ("read", "how", "words", "change", "todo")
_DIFF_KEYS = ("key", "state", "yours", "agami", "note", "yours_hi", "agami_hi")
_STATUSES = {"match", "match_unverified", "mismatch", "expected_doubtful", "error"}
# Who acts in beat 4, which colors the fourth column: the person's query, the semantic model, the
# question, agami's answer (a worked example), keep, or nothing.
_OWNERS = {"you", "model", "question", "agami", "keep", "nothing"}
_CHECK_STATES = {"held", "defect", "open", "gap", "noted"}
_LAYOUTS = ("auto", "cards", "audit")
_DATA_RESULTS = {"matches", "partly", "differs", "could_not_compare"}
_QUERY_RESULTS = {"same", "different", "not_comparable"}
_FIXES = {"query", "semantic_model", "examples", "question", "ask_again", "none"}


def _validate_item(item: dict, idx: int) -> None:
    if not isinstance(item, dict):
        raise ValueError(f"item {idx}: must be an object")
    if not isinstance(item.get("row"), int) or isinstance(item.get("row"), bool):
        raise ValueError(f"item {idx}: 'row' (integer) is required")
    if not isinstance(item.get("question"), str) or not item["question"].strip():
        raise ValueError(f"item {idx}: 'question' (string) is required")
    if "rows" in item or "recorded" in item:
        raise ValueError(f"item {idx}: result rows are never rendered; pass 'answer' as display text")
    if item.get("status") not in _STATUSES:
        raise ValueError(f"item {idx}: 'status' must be one of {sorted(_STATUSES)}")
    for key in ("label", "source", "expected", "answer", "disagreement", "report_path"):
        if key in item and item[key] is not None and not isinstance(item[key], str):
            raise ValueError(f"item {idx}: '{key}' must be display text")
    for key in _LISTS:
        value = item.get(key, [])
        if not isinstance(value, list) or not all(isinstance(s, str) for s in value):
            raise ValueError(f"item {idx}: '{key}' must be a list of sentences")
    if item.get("owner") is not None and item["owner"] not in _OWNERS:
        raise ValueError(f"item {idx}: 'owner' must be one of {sorted(_OWNERS)}")
    if item.get("delta_pct") is not None and not isinstance(item["delta_pct"], (int, float)):
        raise ValueError(f"item {idx}: 'delta_pct' must be a number")
    for check in item.get("checks", []) or []:
        if (not isinstance(check, dict) or not isinstance(check.get("step"), str)
                or check.get("state") not in _CHECK_STATES):
            raise ValueError(f"item {idx}: each check needs a 'step' and a 'state' in {sorted(_CHECK_STATES)}")
    for check in item.get("checks", []) or []:
        if "rows" in check or "recorded" in check:
            raise ValueError(f"item {idx}: result rows are never rendered, not even inside a check")
    for row in item.get("diff", []) or []:
        if (not isinstance(row, dict) or not isinstance(row.get("key"), str) or row.get("state") not in _CHECK_STATES):
            raise ValueError(f"item {idx}: each diff row needs a 'key' and a 'state' in {sorted(_CHECK_STATES)}")
        for side in ("yours", "agami"):
            v = row.get(side)
            if v is not None and not isinstance(v, str) and not (isinstance(v, list) and all(isinstance(x, str) for x in v)):
                raise ValueError(f"item {idx}: diff '{side}' must be text or a list of tokens")
        for hi in ("yours_hi", "agami_hi"):
            v = row.get(hi)
            if v is not None and not (isinstance(v, list) and all(isinstance(x, str) for x in v)):
                raise ValueError(f"item {idx}: diff '{hi}' must be a list of the tokens to highlight")
        if row.get("note") is not None and not isinstance(row["note"], str):
            raise ValueError(f"item {idx}: diff 'note' must be text")
        if "rows" in row or "recorded" in row:
            raise ValueError(f"item {idx}: result rows are never rendered, not even inside a diff row")
    if item.get("keep_allowed") is not None and not isinstance(item["keep_allowed"], bool):
        raise ValueError(f"item {idx}: 'keep_allowed' must be true or false")
    result = item.get("result")
    if result is not None:
        if (not isinstance(result, dict) or result.get("data") not in _DATA_RESULTS or result.get("query") not in _QUERY_RESULTS
                or not isinstance(result.get("label"), str) or not isinstance(result.get("unchecked", 0), int)):
            raise ValueError(f"item {idx}: 'result' needs data in {sorted(_DATA_RESULTS)}, query in {sorted(_QUERY_RESULTS)}, a label and an unchecked count")
    if item.get("fix") is not None and item["fix"] not in _FIXES:
        raise ValueError(f"item {idx}: 'fix' must be one of {sorted(_FIXES)}")
    if item.get("fix_words") is not None and not isinstance(item["fix_words"], str):
        raise ValueError(f"item {idx}: 'fix_words' must be text")
    if item.get("sentence") is not None and not isinstance(item["sentence"], str):
        raise ValueError(f"item {idx}: 'sentence' must be text")
    for key in ("sql_yours", "sql_agami"):
        if item.get(key) is not None and not isinstance(item[key], str):
            raise ValueError(f"item {idx}: '{key}' must be the statement's text")


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8") if path.exists() else ""


def choose_layout(items: list[dict], layout: str = "auto") -> str:
    """`audit` for one row that carries checks (a single trusted query, read part by part), `cards`
    for anything else. A person can force either."""
    if layout != "auto":
        return layout
    return "audit" if len(items) == 1 and (items[0].get("checks") or items[0].get("diff")) else "cards"


def render(*, title: str, profile: str, run: str, items: list[dict], layout: str = "auto") -> str:
    if layout not in _LAYOUTS:
        raise ValueError(f"layout must be one of {_LAYOUTS}")
    for i, item in enumerate(items):
        _validate_item(item, i)
    rows_seen = [item["row"] for item in items]
    if len(set(rows_seen)) != len(rows_seen):
        raise ValueError("two items share a row number; decisions are keyed by row, so each must be unique")
    projected = [{k: item.get(k) for k in _FIELDS if k in item} for item in items]
    for item in projected:
        if item.get("diff"):
            item["diff"] = [{k: row.get(k) for k in _DIFF_KEYS if k in row} for row in item["diff"]]
        if item.get("checks"):
            item["checks"] = [{k: c.get(k) for k in ("step", "state", "detail", "note") if k in c} for c in item["checks"]]
    for item in projected:
        for key in _LISTS:
            item.setdefault(key, [])
        item.setdefault("checks", [])
        for key in ("label", "source", "expected", "answer", "disagreement", "report_path", "owner", "delta_pct"):
            item.setdefault(key, None)
        # The one rule the page enforces about decisions: keep is offered where the run said match
        # AND the answer is one cell, which is Phase 3e's own predicate; a table row can match and
        # still never be offered.
        # The keep-offer is Phase 3e's: report-items applies the parser's own gate (one cell, a confirmed fit
        # where a question and a statement both exist); an older items file falls back to the two facts.
        if isinstance(item.get("keep_allowed"), bool):
            pass
        else:
            item["keep_allowed"] = item["status"] == "match" and item.get("single_cell") is True
    template = TEMPLATE_PATH.read_text(encoding="utf-8")
    values = {
        "REPORT_TITLE": html.escape(title),
        "GENERATED_AT": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        "PROFILE": html.escape(profile or ""),
        "RUN": html.escape(run or ""),
        "PROFILE_JSON": _script_json(profile or ""),
        "RUN_JSON": _script_json(run or ""),
        "ITEMS_JSON": _script_json(projected),
        "LAYOUT_JSON": _script_json(choose_layout(projected, layout)),
        "AGAMI_LOGO_DARK_TEXT": _read(LOGO_DARK_PATH),
        "AGAMI_LOGO_LIGHT_TEXT": _read(LOGO_LIGHT_PATH),
        "THEME_CSS": _read(THEME_PATH),
        "PAGE_CSS": _read(PAGE_CSS_PATH),
    }
    # One pass, so a placeholder token inside a person's text is copied and never expanded.
    return re.sub(r"\{\{([A-Z_]+)\}\}", lambda m: values.get(m.group(1), m.group(0)), template)


def _script_json(payload) -> str:
    """JSON safe inside a `<script>` block: every `<` written as `\\u003c`."""
    return json.dumps(payload).replace("<", "\\u003c")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Render the reconcile report page.")
    p.add_argument("--title", required=True)
    p.add_argument("--profile", required=True, help="Active profile name")
    p.add_argument("--run", required=True, help="The reconcile run's folder name (its timestamp)")
    p.add_argument("--items-file", required=True,
                   help="JSON array of {row, label, question, source, status, expected, answer, read, how, words, disagreement, change, report_path}")
    p.add_argument("--layout", choices=list(_LAYOUTS), default="auto",
                   help="cards for a batch, audit for one statement read part by part; auto picks from the items")
    p.add_argument("--out", required=True)
    args = p.parse_args(argv)

    with open(os.path.expanduser(args.items_file), encoding="utf-8") as f:
        items = json.load(f)
    if not isinstance(items, list):
        sys.stderr.write(f"--items-file must contain a JSON array, got {type(items).__name__}\n")
        return 1
    try:
        page = render(title=args.title, profile=args.profile, run=args.run, items=items, layout=args.layout)
    except ValueError as exc:
        sys.stderr.write(f"render_reconcile_report: {exc}\n")
        return 1
    out_path = Path(os.path.expanduser(args.out))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(page, encoding="utf-8")
    print(f"Wrote {out_path} ({len(items)} row{'s' if len(items) != 1 else ''})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
