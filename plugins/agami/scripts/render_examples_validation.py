#!/usr/bin/env python3
"""
Examples-validation dashboard renderer.

Reads plugins/agami/shared/examples-validation-template.html, substitutes
placeholders, and writes a self-contained HTML file. Stdlib only.

Used by agami-connect Phase 6 to render every seed example as a card so the
user can validate / reject / edit each one before they're trusted as
few-shot anchors. Validated examples are also candidates for tests.yaml.

Usage:

    python3 render_examples_validation.py \\
        --title "Seed examples · default" \\
        --profile default \\
        --items-file /tmp/agami-examples-items.json \\
        --out <artifacts_dir>/local/examples-validation/20260510-141500.html
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import sys
from pathlib import Path


SHARED_DIR = Path(__file__).resolve().parent.parent / "shared"
TEMPLATE_PATH = SHARED_DIR / "examples-validation-template.html"
LOGO_DARK_PATH = SHARED_DIR / "agami-logo-dark.svg"
LOGO_LIGHT_PATH = SHARED_DIR / "agami-logo-light.svg"


VALID_STATES = {"unreviewed", "validated", "rejected"}


def _validate_item(item: dict, idx: int) -> None:
    if not isinstance(item, dict):
        raise ValueError(f"item {idx}: must be an object")
    if "n" not in item or not isinstance(item["n"], int) or item["n"] < 1:
        raise ValueError(f"item {idx}: 'n' must be a positive integer")
    if "question" not in item or not isinstance(item["question"], str):
        raise ValueError(f"item {idx}: 'question' (string) is required")
    state = item.get("state", "unreviewed")
    if state not in VALID_STATES:
        raise ValueError(
            f"item {idx}: state must be one of {sorted(VALID_STATES)}, got {state!r}"
        )
    rp = item.get("row_preview")
    if rp is not None and not isinstance(rp, list):
        raise ValueError(f"item {idx}: row_preview must be a list of lists")
    if isinstance(rp, list):
        for j, row in enumerate(rp):
            if not isinstance(row, list):
                raise ValueError(f"item {idx}.row_preview[{j}]: must be a list")
    rh = item.get("row_headers")
    if rh is not None and not isinstance(rh, list):
        raise ValueError(f"item {idx}: row_headers must be a list")


def render(
    *,
    title: str,
    profile: str,
    items: list,
) -> str:
    if not isinstance(items, list):
        raise ValueError("items must be a list")
    for i, it in enumerate(items):
        _validate_item(it, i)

    template = TEMPLATE_PATH.read_text()
    logo_dark_svg = LOGO_DARK_PATH.read_text()
    logo_light_svg = LOGO_LIGHT_PATH.read_text()
    theme_css = (SHARED_DIR / "theme.css").read_text()

    total_rows = sum(int(it.get("row_count") or 0) for it in items)

    # Escape `</` so a `</script>` in an example's question/SQL can't terminate the
    # <script> block holding the items JSON (JS unescapes `<\/` → `</`).
    items_json = json.dumps(items).replace("</", "<\\/")

    out = (
        template
        .replace("{{REPORT_TITLE}}", title)
        .replace("{{GENERATED_AT}}",
                 datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"))
        .replace("{{ITEMS_JSON}}", items_json)
        .replace("{{PROFILE}}", profile or "")
        .replace("{{TOTAL_ROW_COUNT}}", str(total_rows))
        .replace("{{AGAMI_LOGO_DARK_TEXT}}", logo_dark_svg)
        .replace("{{AGAMI_LOGO_LIGHT_TEXT}}", logo_light_svg)
        .replace("{{THEME_CSS}}", theme_css)
    )
    return out


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--title", required=True,
                   help="Dashboard title (e.g., 'Seed examples · default')")
    p.add_argument("--profile", required=True, help="Active profile name")
    p.add_argument("--items-file", required=True,
                   help="Path to a JSON file containing the items[] array")
    p.add_argument("--out", required=True)
    args = p.parse_args()

    with open(os.path.expanduser(args.items_file)) as f:
        items = json.load(f)
    if not isinstance(items, list):
        sys.stderr.write(f"--items-file must contain a JSON array, got {type(items).__name__}\n")
        return 1

    out_path = Path(os.path.expanduser(args.out))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(render(
        title=args.title,
        profile=args.profile,
        items=items,
    ))
    print(f"Wrote {out_path} ({len(items)} example{'s' if len(items) != 1 else ''})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
