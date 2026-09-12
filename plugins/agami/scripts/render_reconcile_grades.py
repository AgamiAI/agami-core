#!/usr/bin/env python3
"""
Reconcile grading page renderer.

A list of questions has no answer to compare against, so a person grades what the AI answered.
This reads plugins/agami/shared/reconcile-grades-template.html, substitutes placeholders, and
writes a self-contained HTML file. Stdlib only.

Used by agami-reconcile Phase 2.5. One row per question: the question, the AI's answer as one
recorded cell or the shape of a table, the receipt's signals, and a link to the receipt. Never a
result row: the page is written to the ignored half of the artifacts like every rendered page,
and what a person grades is the answer, not the data.

Usage:

    python3 render_reconcile_grades.py \\
        --title "Grade agami's answers · default" \\
        --profile default --run 20260911-161500 \\
        --items-file /tmp/agami-reconcile-grade-items.json \\
        --out <artifacts_dir>/local/reconcile/20260911-161500/grade.html
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
TEMPLATE_PATH = SHARED_DIR / "reconcile-grades-template.html"
LOGO_DARK_PATH = SHARED_DIR / "agami-logo-dark.svg"
LOGO_LIGHT_PATH = SHARED_DIR / "agami-logo-light.svg"
THEME_PATH = SHARED_DIR / "theme.css"

# What one item may carry. `answer` is DISPLAY text the skill already reduced to one cell or a
# shape; a `rows` key is refused rather than rendered, because the page's rule is no result rows.
_FIELDS = ("row", "question", "answer", "signals", "report_path")


def _validate_item(item: dict, idx: int) -> None:
    if not isinstance(item, dict):
        raise ValueError(f"item {idx}: must be an object")
    if not isinstance(item.get("row"), int):
        raise ValueError(f"item {idx}: 'row' (integer) is required")
    if not isinstance(item.get("question"), str) or not item["question"].strip():
        raise ValueError(f"item {idx}: 'question' (string) is required")
    if "rows" in item or "recorded" in item:
        raise ValueError(f"item {idx}: result rows are never rendered; pass 'answer' as display text")
    if "answer" in item and not isinstance(item["answer"], str):
        raise ValueError(f"item {idx}: 'answer' must be display text")
    signals = item.get("signals", [])
    if not isinstance(signals, list) or not all(isinstance(s, str) for s in signals):
        raise ValueError(f"item {idx}: 'signals' must be a list of strings")


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8") if path.exists() else ""


def render(*, title: str, profile: str, run: str, items: list[dict]) -> str:
    for i, item in enumerate(items):
        _validate_item(item, i)
    projected = [{k: item.get(k) for k in _FIELDS if k in item} for item in items]
    for item in projected:
        item.setdefault("answer", "")
        item.setdefault("signals", [])
        item.setdefault("report_path", None)
    template = TEMPLATE_PATH.read_text(encoding="utf-8")
    values = {
        "REPORT_TITLE": html.escape(title),
        "GENERATED_AT": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        "PROFILE": html.escape(profile or ""),
        "RUN": html.escape(run or ""),
        "PROFILE_JSON": _script_json(profile or ""),
        "RUN_JSON": _script_json(run or ""),
        "ITEMS_JSON": _script_json(projected),
        "AGAMI_LOGO_DARK_TEXT": _read(LOGO_DARK_PATH),
        "AGAMI_LOGO_LIGHT_TEXT": _read(LOGO_LIGHT_PATH),
        "THEME_CSS": _read(THEME_PATH),
    }
    # One pass over the template, so a placeholder token inside a person's question (or the
    # profile name) is copied as text and never expanded by a later substitution.
    return re.sub(r"\{\{([A-Z_]+)\}\}", lambda m: values.get(m.group(1), m.group(0)), template)


def _script_json(payload) -> str:
    """JSON safe inside a `<script>` block: every `<` is written as `\\u003c`, so no `</script>`,
    `<!--` or `<script` in a person's text can end the block or put the parser in a comment state.
    JSON.parse and a JS literal both read the escape back as the character."""
    return json.dumps(payload).replace("<", "\\u003c")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Render the reconcile grading page.")
    p.add_argument("--title", required=True)
    p.add_argument("--profile", required=True, help="Active profile name")
    p.add_argument("--run", required=True, help="The reconcile run's folder name (its timestamp)")
    p.add_argument("--items-file", required=True, help="JSON array of {row, question, answer, signals, report_path}")
    p.add_argument("--out", required=True)
    args = p.parse_args(argv)

    with open(os.path.expanduser(args.items_file), encoding="utf-8") as f:
        items = json.load(f)
    if not isinstance(items, list):
        sys.stderr.write(f"--items-file must contain a JSON array, got {type(items).__name__}\n")
        return 1
    try:
        html = render(title=args.title, profile=args.profile, run=args.run, items=items)
    except ValueError as exc:
        sys.stderr.write(f"render_reconcile_grades: {exc}\n")
        return 1
    out_path = Path(os.path.expanduser(args.out))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html, encoding="utf-8")
    print(f"Wrote {out_path} ({len(items)} question{'s' if len(items) != 1 else ''})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
