#!/usr/bin/env python3
"""
Sample chart renderer.

Reads plugins/agami/shared/chart-template.html, substitutes the placeholders,
and writes a self-contained HTML file. Stdlib only — no Plotly, no charting deps.

The agami skill itself does this via the Read + Write tools (no script needed).
This file exists so users with their own automation can import or copy it.

Usage:
    python sample_render_chart.py \\
        --title "Top customers" \\
        --type bar \\
        --labels '["Carol Chen","Dave Davis","Bob Brown"]' \\
        --datasets '[{"label":"Spend","data":[148.95,93.96,45.0]}]' \\
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

TEMPLATE_PATH = (
    Path(__file__).resolve().parent.parent / "shared" / "chart-template.html"
)


def render(title: str, chart_type: str, labels: list, datasets: list, sql: str = "") -> str:
    if chart_type not in VALID_TYPES:
        raise ValueError(f"chart_type must be one of {sorted(VALID_TYPES)}, got {chart_type!r}")

    template = TEMPLATE_PATH.read_text()
    out = (
        template
        .replace("{{TITLE}}", _escape_html(title))
        .replace("{{CHART_TYPE}}", chart_type)
        .replace("{{LABELS}}", json.dumps(labels))
        .replace("{{DATASETS}}", json.dumps(datasets))
        .replace("{{GENERATED_AT}}", datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"))
        .replace("{{SQL}}", _escape_html(sql or ""))
    )
    return out


def _escape_html(s: str) -> str:
    return (
        s.replace("&", "&amp;")
         .replace("<", "&lt;")
         .replace(">", "&gt;")
    )


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--title", required=True)
    p.add_argument("--type", required=True, choices=sorted(VALID_TYPES))
    p.add_argument("--labels", required=True, help="JSON array of labels")
    p.add_argument("--datasets", required=True, help="JSON array of Chart.js datasets")
    p.add_argument("--sql", default="", help="optional SQL shown in the chart footer")
    p.add_argument("--out", required=True)
    args = p.parse_args()

    labels = json.loads(args.labels)
    datasets = json.loads(args.datasets)
    if not isinstance(labels, list) or not isinstance(datasets, list):
        sys.stderr.write("--labels and --datasets must be JSON arrays\n")
        return 1

    out_path = Path(os.path.expanduser(args.out))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(render(args.title, args.type, labels, datasets, sql=args.sql))
    print(f"Wrote {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
