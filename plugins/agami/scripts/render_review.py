#!/usr/bin/env python3
"""
Review-dashboard renderer for the agami-review skill.

Reads plugins/agami/shared/review-dashboard-template.html, substitutes
placeholders (including the inline agami logos), and writes a self-contained
HTML file. Stdlib only.

The agami-review SKILL builds a JSON file describing the review queue (items
needing review + summary counts) and pipes it through this renderer instead
of doing template substitution through the LLM. Same rationale as
render_chart.py — Read+Write of the template costs ~30KB of token I/O per
render.

Usage:

    python3 render_review.py \\
        --title "Review queue · default · threshold 0.7" \\
        --threshold 0.7 \\
        --model-version abc123def456 \\
        --items-file /tmp/agami-review-items.json \\
        --summary-file /tmp/agami-review-summary.json \\
        --out ~/.agami/review/20260510-141500.html

The shape of items / summary JSON files is documented in
shared/review-dashboard-template.html (ITEMS_JSON / SUMMARY_JSON schemas).
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import sys
from pathlib import Path


SHARED_DIR = Path(__file__).resolve().parent.parent / "shared"
TEMPLATE_PATH = SHARED_DIR / "review-dashboard-template.html"
LOGO_DARK_PATH = SHARED_DIR / "agami-logo-dark.svg"
LOGO_LIGHT_PATH = SHARED_DIR / "agami-logo-light.svg"


VALID_ENTITY_TYPES = {"join", "metric", "field_description", "named_filter", "dataset", "entity"}
# All four review states are now valid — the dashboard's tabs surface each
# group separately (For Review / Approved Automatically / Manually Approved /
# Rejected). The SKILL classifies each entity into a tab via `item.tab`.
# `not_applicable` is filtered out by the SKILL before this renderer is
# called (empty-description fields aren't surfaced); allowed here as a safety
# net so a stray entry doesn't blow up the render.
VALID_REVIEW_STATES = {"unreviewed", "approved", "rejected", "stale", "not_applicable"}
VALID_TABS = {"review", "auto", "manual", "rejected"}


def _validate_item(item: dict, idx: int) -> None:
    if not isinstance(item, dict):
        raise ValueError(f"item {idx}: must be an object")
    for k in ("n", "entity_type", "title"):
        if k not in item:
            raise ValueError(f"item {idx}: missing required key '{k}'")
    if not isinstance(item["n"], int) or item["n"] < 1:
        raise ValueError(f"item {idx}: 'n' must be a positive integer")
    if item["entity_type"] not in VALID_ENTITY_TYPES:
        raise ValueError(
            f"item {idx}: entity_type must be one of {sorted(VALID_ENTITY_TYPES)}, "
            f"got {item['entity_type']!r}"
        )
    if "review_state" in item and item["review_state"] not in VALID_REVIEW_STATES:
        raise ValueError(
            f"item {idx}: review_state must be one of {sorted(VALID_REVIEW_STATES)}, "
            f"got {item['review_state']!r}"
        )
    if "tab" in item and item["tab"] not in VALID_TABS:
        raise ValueError(
            f"item {idx}: tab must be one of {sorted(VALID_TABS)}, got {item['tab']!r}"
        )
    c = item.get("confidence")
    # confidence may be a 0-1 number (legacy) OR a categorical label from the
    # semantic model ("confirmed" | "inferred" | "proposed").
    if c is not None and isinstance(c, str):
        if c not in ("confirmed", "inferred", "proposed"):
            raise ValueError(
                f"item {idx}: categorical confidence must be confirmed/inferred/proposed, got {c!r}")
    elif c is not None:
        if isinstance(c, bool) or not isinstance(c, (int, float)):
            raise ValueError(f"item {idx}: confidence must be a number or categorical label")
        if not (0.0 <= float(c) <= 1.0):
            raise ValueError(f"item {idx}: confidence {c} outside [0, 1]")
    sig = item.get("signals")
    if sig is not None and not isinstance(sig, list):
        raise ValueError(f"item {idx}: signals must be a list")


def render(
    *,
    title: str,
    threshold: float,
    model_version: str,
    items: list,
    summary: dict | None = None,
) -> str:
    if not isinstance(items, list):
        raise ValueError("items must be a list")
    for i, it in enumerate(items):
        _validate_item(it, i)
    if summary is not None and not isinstance(summary, dict):
        raise ValueError("summary must be an object or null")

    template = TEMPLATE_PATH.read_text()
    logo_dark_svg = LOGO_DARK_PATH.read_text()
    logo_light_svg = LOGO_LIGHT_PATH.read_text()

    out = (
        template
        .replace("{{REPORT_TITLE}}", title)
        .replace("{{GENERATED_AT}}",
                 datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"))
        .replace("{{ITEMS_JSON}}", json.dumps(items))
        .replace("{{SUMMARY_JSON}}", json.dumps(summary or {}))
        .replace("{{THRESHOLD}}", f"{threshold:g}")
        .replace("{{MODEL_VERSION}}", model_version or "")
        .replace("{{AGAMI_LOGO_DARK_TEXT}}", logo_dark_svg)
        .replace("{{AGAMI_LOGO_LIGHT_TEXT}}", logo_light_svg)
    )
    return out


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--title", required=True,
                   help="Dashboard title (e.g., 'Review queue · default · threshold 0.7')")
    p.add_argument("--threshold", type=float, required=True,
                   help="Active review threshold (e.g., 0.7)")
    p.add_argument("--model-version", default="",
                   help="Short content hash from index.yaml.introspect_meta.model_version")
    p.add_argument("--items-file", required=True,
                   help="Path to a JSON file containing the items[] array")
    p.add_argument("--summary-file",
                   help="Path to a JSON file containing the summary object (optional)")
    p.add_argument("--out", required=True)
    args = p.parse_args()

    with open(os.path.expanduser(args.items_file)) as f:
        items = json.load(f)
    if not isinstance(items, list):
        sys.stderr.write(f"--items-file must contain a JSON array, got {type(items).__name__}\n")
        return 1

    summary = None
    if args.summary_file:
        with open(os.path.expanduser(args.summary_file)) as f:
            summary = json.load(f)
        if not isinstance(summary, dict):
            sys.stderr.write(f"--summary-file must contain a JSON object, got {type(summary).__name__}\n")
            return 1

    out_path = Path(os.path.expanduser(args.out))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(render(
        title=args.title,
        threshold=args.threshold,
        model_version=args.model_version,
        items=items,
        summary=summary,
    ))
    print(f"Wrote {out_path} ({len(items)} item{'s' if len(items) != 1 else ''})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
