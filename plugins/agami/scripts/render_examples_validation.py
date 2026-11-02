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
import re
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
    # `n` is assigned by render() (a stable global 1..N), not required from the caller.
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
    """Render the examples-validation dashboard HTML.

    NOTE: mutates each item's ``n`` in place — assigning a stable global 1..N in render order
    (see the comment below). A caller that still needs the original per-area numbers should copy
    the items first.
    """
    if not isinstance(items, list):
        raise ValueError("items must be a list")
    # `n` is the example's identity end-to-end: the interaction/DOM key, the `#N` label, the
    # reference emitted in the "Generate feedback" block (`edit N` / `note N`), AND the apply
    # lookup (the skill maps `N` -> that example's question). So it MUST be unique + stable per
    # dashboard. `sm seed-validate` numbers seeds 1..k PER AREA, so a dashboard assembled from
    # several areas collides (two `#1`s) — clicking Edit on one card then opened EVERY card that
    # shared that `n`, and the feedback block was ambiguous. The renderer is the assembly
    # chokepoint: renumber to a single global 1..N in render order so every consumer agrees.
    for i, it in enumerate(items, 1):
        if isinstance(it, dict):
            it["n"] = i
    for i, it in enumerate(items):
        _validate_item(it, i)

    template = TEMPLATE_PATH.read_text(encoding="utf-8")
    logo_dark_svg = LOGO_DARK_PATH.read_text(encoding="utf-8")
    logo_light_svg = LOGO_LIGHT_PATH.read_text(encoding="utf-8")
    theme_css = (SHARED_DIR / "theme.css").read_text(encoding="utf-8")

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


def _read_items(path: str) -> tuple[object, list]:
    """A file's parsed JSON and its items: a bare array (one area's `sm seed-validate`) or the
    combined `{areas, items, totals}` object it prints for several areas."""
    with open(os.path.expanduser(path), encoding="utf-8") as f:
        doc = json.load(f)
    items = doc.get("items") if isinstance(doc, dict) else doc
    if not isinstance(items, list):
        raise ValueError(f"{path}: must hold a JSON array of items or an object with an "
                         f"'items' array, got {type(doc).__name__}")
    return doc, items


def _split_label(arg: str) -> tuple[str | None, str]:
    """`AREA=PATH` labels a single-area file with its area; a plain path (or one that exists with
    an `=` in its name) has no label."""
    head, sep, rest = arg.partition("=")
    if sep and rest and re.fullmatch(r"[\w.-]+", head) and not os.path.exists(os.path.expanduser(arg)):
        return head, rest
    return None, arg


def _read_files(args: list[str]) -> list[tuple[str, object, list]]:
    """Each `--items-file` as (path, parsed JSON, items), with a file's label copied into its
    items. Refuses a file given twice, and, with several files, an item no area is known for."""
    files, seen = [], {}
    for arg in args:
        area, path = _split_label(arg)
        real = os.path.realpath(os.path.expanduser(path))
        if real in seen:
            # Its examples would get two numbers, and write-back keeps only the last.
            raise ValueError(f"{path}: given twice (also as {seen[real]})")
        seen[real] = path
        doc, items = _read_items(path)
        if area:
            for it in items:
                if isinstance(it, dict):
                    it.setdefault("area", area)
        files.append((path, doc, items))
    if len(files) > 1:
        unlabelled = [p for p, _, items in files
                      if any(isinstance(it, dict) and not it.get("area") for it in items)]
        if unlabelled:
            # Two areas can share a question; without the area `reject N` can't say which.
            raise ValueError(f"{', '.join(unlabelled)}: items carry no area; label a single area's "
                             "file as AREA=PATH, or run `sm seed-validate` once for all the areas")
    return files


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--title", required=True,
                   help="Dashboard title (e.g., 'Seed examples · default')")
    p.add_argument("--profile", required=True, help="Active profile name")
    p.add_argument("--items-file", required=True, action="append",
                   help="JSON items from `sm seed-validate`: one area's array or the combined "
                        "object for several areas (repeatable; files render in the order given). "
                        "With several files, label a single area's array as AREA=PATH")
    p.add_argument("--out", required=True)
    args = p.parse_args()

    try:
        files = _read_files(args.items_file)
    except ValueError as e:
        sys.stderr.write(f"--items-file {e}\n")
        return 1
    items = [it for _, _, file_items in files for it in file_items]

    out_path = Path(os.path.expanduser(args.out))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(render(
        title=args.title,
        profile=args.profile,
        items=items,
    ), encoding="utf-8")
    # render() normalized `n` to a stable global 1..N (in place). Persist it back so a later
    # apply pass ("edit N" -> the N-th example) resolves against the SAME numbering the dashboard
    # showed — regardless of the per-area numbers the caller wrote. Atomic (temp + os.replace) so a
    # failed write never leaves the items file truncated; non-fatal (the HTML already embeds the
    # normalized numbering), so a write failure warns rather than aborting the render. Each file
    # keeps its own shape; several files share the one numbering.
    for path, doc, _ in files:
        items_path = os.path.expanduser(path)
        try:
            tmp_path = items_path + ".tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(doc, f)
            os.replace(tmp_path, items_path)
        except OSError as e:
            sys.stderr.write(f"warning: could not normalize {items_path}: {e}\n")
    print(f"Wrote {out_path} ({len(items)} example{'s' if len(items) != 1 else ''})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
