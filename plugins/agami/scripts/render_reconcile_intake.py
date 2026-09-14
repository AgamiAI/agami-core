#!/usr/bin/env python3
"""
Reconcile intake page renderer.

What we read from what the person gave us, before anything runs: one row per thing to check, the
question read from a label (editable), the number parsed, whether SQL came with it. The person fixes
a question or drops a row and pastes one block back, the way the connect skill's prune page works.
Stdlib only; the same theme and shared page CSS as the report and grading pages.

Usage:

    python3 render_reconcile_intake.py --title "What we read · default" --profile default \
        --run 20260912-101500 --items-file /tmp/agami-reconcile-intake-items.json \
        --out <artifacts_dir>/local/reconcile/20260912-101500/intake.html
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
TEMPLATE_PATH = SHARED_DIR / "reconcile-intake-template.html"
LOGO_DARK_PATH = SHARED_DIR / "agami-logo-dark.svg"
LOGO_LIGHT_PATH = SHARED_DIR / "agami-logo-light.svg"
THEME_PATH = SHARED_DIR / "theme.css"
PAGE_CSS_PATH = SHARED_DIR / "reconcile-pages.css"

# What one row may carry. `statement_preview` is at most eighty characters of the person's own SQL,
# shown to its author; never a result row, and never agami's SQL.
_FIELDS = ("row", "file", "line", "shape", "label", "question", "expected", "has_statement", "statement_preview", "source")
_SHAPES = {"a", "b", "c", "d"}


def items_from_intake(intake: dict) -> list[dict]:
    """The page's items from `reconcile.py intake`'s output, so the skill writes no items file by hand."""
    if not isinstance(intake, dict) or not isinstance(intake.get("rows"), list):
        raise ValueError("the intake file is not the output of `reconcile.py intake` (an object with a `rows` list)")
    out = []
    for n, row in enumerate(intake["rows"], 1):
        if not isinstance(row, dict):
            raise ValueError(f"intake row {n} is not an object; pass `reconcile.py intake` output, not `parse` output")
        prov = row.get("provenance") or {}
        statement = row.get("statement")
        out.append({
            "row": row.get("row", n), "file": prov.get("file"), "line": prov.get("line"), "shape": prov.get("shape"),
            "label": row.get("label"), "question": row.get("question"),
            "expected": row.get("raw_value") if row.get("raw_value") is not None else _number_text(row.get("expected")),
            "has_statement": bool(statement),
            "statement_preview": (re.sub(r"\s+", " ", statement)[:80] if isinstance(statement, str) else None),
            "source": prov.get("source"),
        })
    return out


def _number_text(value) -> str | None:
    """The report page's number format, from reconcile.py, so the two pages spell a value alike."""
    if value is None or isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        from reconcile import _fmt
        return _fmt(value)
    except ImportError:  # run from somewhere reconcile.py is not importable
        return f"{value:,.2f}".rstrip("0").rstrip(".") if isinstance(value, float) and not value.is_integer() else f"{int(value):,}"


def _validate_item(item: dict, idx: int) -> None:
    if not isinstance(item, dict):
        raise ValueError(f"item {idx}: must be an object")
    if not isinstance(item.get("row"), int) or isinstance(item.get("row"), bool):
        raise ValueError(f"item {idx}: 'row' (integer) is required")
    if item.get("shape") not in _SHAPES:
        raise ValueError(f"item {idx}: 'shape' must be one of {sorted(_SHAPES)}")
    if "rows" in item or "recorded" in item or "statement" in item:
        raise ValueError(f"item {idx}: neither result rows nor a whole statement is rendered; pass 'statement_preview'")
    for key in ("file", "label", "question", "expected", "statement_preview", "source"):
        if item.get(key) is not None and not isinstance(item[key], str):
            raise ValueError(f"item {idx}: '{key}' must be text")
    if item.get("line") is not None and (not isinstance(item["line"], int) or isinstance(item["line"], bool)):
        raise ValueError(f"item {idx}: 'line' must be a whole number")
    if item.get("statement_preview") and len(item["statement_preview"]) > 80:
        raise ValueError(f"item {idx}: 'statement_preview' is at most 80 characters")


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8") if path.exists() else ""


def _script_json(payload) -> str:
    return json.dumps(payload).replace("<", "\\u003c")


def render(*, title: str, profile: str, run: str, items: list[dict]) -> str:
    for i, item in enumerate(items):
        _validate_item(item, i)
    if len({item["row"] for item in items}) != len(items):
        raise ValueError("two items share a row number; edits are keyed by row, so each must be unique")
    projected = [{k: item.get(k) for k in _FIELDS if k in item} for item in items]
    for item in projected:
        for key in _FIELDS:
            item.setdefault(key, None)
    template = TEMPLATE_PATH.read_text(encoding="utf-8")
    values = {
        "REPORT_TITLE": html.escape(title),
        "GENERATED_AT": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        "PROFILE": html.escape(profile or ""), "RUN": html.escape(run or ""),
        "PROFILE_JSON": _script_json(profile or ""), "RUN_JSON": _script_json(run or ""),
        "ITEMS_JSON": _script_json(projected),
        "AGAMI_LOGO_DARK_TEXT": _read(LOGO_DARK_PATH), "AGAMI_LOGO_LIGHT_TEXT": _read(LOGO_LIGHT_PATH),
        "THEME_CSS": _read(THEME_PATH), "PAGE_CSS": _read(PAGE_CSS_PATH),
    }
    return re.sub(r"\{\{([A-Z_]+)\}\}", lambda m: values.get(m.group(1), m.group(0)), template)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Render the reconcile intake page.")
    p.add_argument("--title", required=True)
    p.add_argument("--profile", required=True)
    p.add_argument("--run", required=True)
    p.add_argument("--items-file", help="JSON array of page items")
    p.add_argument("--intake-file", help="the output of `reconcile.py intake`; the items are derived from it")
    p.add_argument("--out", required=True)
    args = p.parse_args(argv)
    if bool(args.items_file) == bool(args.intake_file):
        sys.stderr.write("pass exactly one of --items-file or --intake-file\n")
        return 1
    try:
        with open(os.path.expanduser(args.intake_file or args.items_file), encoding="utf-8") as f:
            loaded = json.load(f)
        items = items_from_intake(loaded) if args.intake_file else loaded
        if not isinstance(items, list):
            raise ValueError("the items must be a JSON array")
        page = render(title=args.title, profile=args.profile, run=args.run, items=items)
    except (OSError, ValueError) as exc:
        sys.stderr.write(f"render_reconcile_intake: {exc}\n")
        return 1
    out_path = Path(os.path.expanduser(args.out))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(page, encoding="utf-8")
    print(f"Wrote {out_path} ({len(items)} row{'s' if len(items) != 1 else ''})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
