#!/usr/bin/env python3
"""
Sample report renderer.

Reads plugins/agami/shared/chart-template.html, substitutes every placeholder
(including the inline agami logos from shared/agami-logo-{dark,light}.svg),
and writes a self-contained HTML file containing chart + table + insights + SQL.
Stdlib only.

The agami skill itself does this via the Read + Write tools — no script needed.
This file exists so users with their own automation can import or copy it.

Usage:
    python sample_render_chart.py \\
        --title "Top customers" \\
        --insights "Carol Chen leads at $148.95, ahead of the next customer by 3x." \\
        --type bar \\
        --labels '["Carol Chen","Dave Davis","Bob Brown"]' \\
        --datasets '[{"label":"Spend","data":[148.95,93.96,45.0]}]' \\
        --table-headers '["Customer","Spend"]' \\
        --table-rows '[["Carol Chen",148.95],["Dave Davis",93.96],["Bob Brown",45.0]]' \\
        --sql "SELECT c.name, SUM(...) FROM ..." \\
        --out ~/.agami/charts/top-customers.html
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import sys
from pathlib import Path


VALID_TYPES = {"bar", "line", "pie", "doughnut", "scatter"}

SHARED_DIR = Path(__file__).resolve().parent.parent / "shared"
TEMPLATE_PATH = SHARED_DIR / "chart-template.html"
LOGO_DARK_PATH = SHARED_DIR / "agami-logo-dark.svg"   # dark text — for light backgrounds
LOGO_LIGHT_PATH = SHARED_DIR / "agami-logo-light.svg"  # light text — for dark backgrounds


def _escape_html(s: str) -> str:
    return (
        s.replace("&", "&amp;")
         .replace("<", "&lt;")
         .replace(">", "&gt;")
    )


def render(
    *,
    title: str,
    insights: str,
    chart_type: str,
    labels: list,
    datasets: list,
    table_headers: list,
    table_rows: list,
    sql: str = "",
) -> str:
    if chart_type not in VALID_TYPES:
        raise ValueError(f"chart_type must be one of {sorted(VALID_TYPES)}, got {chart_type!r}")
    if not isinstance(labels, list):
        raise ValueError("labels must be a list")
    if not isinstance(datasets, list):
        raise ValueError("datasets must be a list")
    if not isinstance(table_headers, list):
        raise ValueError("table_headers must be a list")
    if not isinstance(table_rows, list):
        raise ValueError("table_rows must be a list")

    template = TEMPLATE_PATH.read_text()
    logo_dark_svg = LOGO_DARK_PATH.read_text()
    logo_light_svg = LOGO_LIGHT_PATH.read_text()

    out = (
        template
        .replace("{{TITLE}}", _escape_html(title))
        .replace("{{INSIGHTS}}", _escape_html(insights))
        .replace("{{CHART_TYPE}}", chart_type)
        .replace("{{LABELS}}", json.dumps(labels))
        .replace("{{DATASETS}}", json.dumps(datasets))
        .replace("{{TABLE_HEADERS}}", json.dumps(table_headers))
        .replace("{{TABLE_ROWS}}", json.dumps(table_rows))
        .replace("{{GENERATED_AT}}", datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"))
        .replace("{{SQL}}", _escape_html(sql or ""))
        .replace("{{AGAMI_LOGO_DARK_TEXT}}", logo_dark_svg)
        .replace("{{AGAMI_LOGO_LIGHT_TEXT}}", logo_light_svg)
    )
    return out


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--title", required=True)
    p.add_argument("--insights", default="")
    p.add_argument("--type", required=True, choices=sorted(VALID_TYPES))
    p.add_argument("--labels", required=True, help="JSON array of labels")
    p.add_argument("--datasets", required=True, help="JSON array of Chart.js datasets")
    p.add_argument("--table-headers", required=True, help="JSON array of column headers")
    p.add_argument("--table-rows", required=True, help="JSON array of row arrays")
    p.add_argument("--sql", default="", help="SQL shown in the collapsible footer")
    p.add_argument("--out", required=True)
    args = p.parse_args()

    out_path = Path(os.path.expanduser(args.out))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(render(
        title=args.title,
        insights=args.insights,
        chart_type=args.type,
        labels=json.loads(args.labels),
        datasets=json.loads(args.datasets),
        table_headers=json.loads(args.table_headers),
        table_rows=json.loads(args.table_rows),
        sql=args.sql,
    ))
    print(f"Wrote {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
