#!/usr/bin/env python3
"""Render the table picker page from a discover inventory (#654).

    render_table_picker.py --inventory <inventory.json> --save-to-helper --out <page.html>
    -> {"page": "<page.html>", "tables": n, "flagged": k, "unticked": u}

The page lists every table of the inventory, grouped by schema, with its row estimate and column
count, a search box, select all / none per schema, and the likely staging and backup copies that
`inventory_summary.py` flags (unticked when every one of them copies another table). It is served by
`serve_page.py --decisions`, and its Save button posts
`{"page": "tables", "profile": <the inventory's profile>, "keep": ["schema.table", ...]}` to that
helper; `inventory_summary.py --keep page:<file>` checks that file against the inventory and writes
the kept-tables file, the same one every other answer to the tables card writes.

Every table and schema name is data: it is written into a JSON block with `<`, `>` and `&` escaped,
so no name can close the block, and the page's script shows it as text only. Stdlib only.
"""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import inventory_summary  # noqa: E402

_SHARED = Path(__file__).resolve().parent.parent / "shared"
TEMPLATE = _SHARED / "table-picker-template.html"
LOGO = _SHARED / "agami-logo-dark.svg"


def page_data(inventory: dict) -> dict:
    rows = inventory_summary.picker_rows(inventory)
    flagged = [r for r in rows if r["why"]]
    return {
        "profile": inventory.get("profile"),
        "noise_all_copies": bool(flagged) and all(r["copy_of"] for r in flagged),
        "tables": rows,
    }


def script_json(value: object) -> str:
    """JSON that is safe inside a <script> block: nothing in it can end the block or open a comment."""
    text = json.dumps(value, ensure_ascii=True)
    return text.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")


def render(inventory: dict) -> str:
    data = page_data(inventory)
    values = {
        "PROFILE": html.escape(str(inventory.get("profile") or "this database")),
        "LOGO": LOGO.read_text(encoding="utf-8"),
        "DATA": script_json(data),
    }
    # One pass, so a value that happens to contain a placeholder is never filled in again.
    return re.sub(
        r"\{\{(PROFILE|LOGO|DATA)\}\}",
        lambda m: values[m.group(1)],
        TEMPLATE.read_text(encoding="utf-8"),
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Render the table picker page from an inventory.")
    ap.add_argument("--inventory", required=True, help="inventory.json written by `sm discover`")
    ap.add_argument("--out", required=True, help="where to write the page")
    # Required, and the only mode: the picker exists to save a pick, so it has no view-only form.
    # Spelled like the other saving renderers so a skill's call says which kind of page it is.
    ap.add_argument(
        "--save-to-helper",
        action="store_true",
        required=True,
        help="the page posts its pick to `serve_page.py --decisions` (the only mode)",
    )
    args = ap.parse_args(argv)
    inventory = json.loads(Path(args.inventory).expanduser().read_text(encoding="utf-8"))
    if not inventory.get("tables"):
        print(json.dumps({"error": "the inventory lists no tables"}))
        return 1
    out = Path(args.out).expanduser()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render(inventory), encoding="utf-8")
    data = page_data(inventory)
    flagged = sum(1 for r in data["tables"] if r["why"])
    print(
        json.dumps(
            {
                "page": str(out),
                "tables": len(data["tables"]),
                "flagged": flagged,
                "unticked": flagged if data["noise_all_copies"] else 0,
            }
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
