#!/usr/bin/env python3
"""
HTML report renderer for agami-query-database.

Reads plugins/agami/shared/chart-template.html, substitutes every placeholder
(including the inline agami logos from shared/agami-logo-{dark,light}.svg),
and writes a self-contained HTML file containing one or more sections. Each
section has its own chart + table + insight + SQL — but they all live in
the same file. Stdlib only.

The agami-query-database SKILL invokes this script in Phase 4e instead of
doing template substitution through the LLM's Read + Write tools — that
path costs ~30KB of token I/O per query (template + two SVG logos) and is
the dominant slowness in chart rendering. Calling this script keeps the
LLM's job to producing a small JSON sections file, and the cheap shell
substitution lives here.

Usage (single section — backwards compatible with the old chart):

    python render_chart.py \\
        --title "Top customers" \\
        --summary "Carol Chen leads at $148.95, ahead of the next customer by 3x." \\
        --section '{
          "title": "Top customers by spend",
          "insights": "Carol Chen leads at $148.95.",
          "chart_type": "bar",
          "labels": ["Carol Chen","Dave Davis","Bob Brown"],
          "datasets": [{"label":"Spend","data":[148.95,93.96,45.0]}],
          "table_headers": ["Customer","Spend"],
          "table_rows": [["Carol Chen",148.95],["Dave Davis",93.96],["Bob Brown",45.0]],
          "sql": "SELECT c.name, SUM(...) FROM ..."
        }' \\
        --out ~/.agami/charts/single.html

Usage (multi-section narrative):

    python render_chart.py \\
        --title "How is the business doing?" \\
        --summary "Revenue up 12% QoQ; Carol Chen is the top customer; pending orders growing." \\
        --sections-file my-report.json \\
        --out ~/.agami/charts/q1-review.html

The file `my-report.json` is a JSON array of section objects, see the
SECTIONS_JSON schema documented in shared/chart-template.html.
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


def _validate_section(sec: dict, idx: int) -> None:
    if not isinstance(sec, dict):
        raise ValueError(f"section {idx}: must be an object")
    if "title" not in sec or not isinstance(sec["title"], str):
        raise ValueError(f"section {idx}: 'title' (string) is required")
    ct = sec.get("chart_type")
    if ct is not None and ct not in VALID_TYPES:
        raise ValueError(
            f"section {idx}: chart_type must be one of {sorted(VALID_TYPES)} or null, got {ct!r}"
        )
    headers = sec.get("table_headers")
    rows = sec.get("table_rows")
    if headers is not None and not isinstance(headers, list):
        raise ValueError(f"section {idx}: table_headers must be a list")
    if rows is not None and not isinstance(rows, list):
        raise ValueError(f"section {idx}: table_rows must be a list of lists")


def _format_sql(sql: str) -> str:
    """Pretty-print a SQL string for display in the chart's SQL section.

    Tries sqlglot first (best results — proper indentation, keyword case,
    line breaks at clause boundaries). Falls back to a small heuristic
    formatter if sqlglot isn't installed: insert newlines before common
    top-level clause keywords. Either way, returns a multi-line string
    that's readable when wrapped in <pre>.

    The original SQL passes through unchanged if it's already multi-line
    (heuristic: contains a newline) — assume the caller knew what they
    were doing.
    """
    if not isinstance(sql, str) or not sql.strip():
        return sql
    if "\n" in sql:
        return sql

    try:
        import sqlglot
        # pretty=True formats with indentation; dialect=None means generic ANSI
        return sqlglot.transpile(sql, pretty=True)[0]
    except Exception:
        pass

    # Heuristic fallback: break before every top-level clause keyword.
    # Not as pretty as sqlglot but still much better than one line.
    import re
    keywords = [
        "SELECT", "FROM", "WHERE", "GROUP BY", "HAVING", "ORDER BY",
        "LIMIT", "OFFSET", "UNION ALL", "UNION", "INTERSECT", "EXCEPT",
        "LEFT JOIN", "RIGHT JOIN", "INNER JOIN", "OUTER JOIN",
        "FULL JOIN", "CROSS JOIN", "JOIN",
        "WITH", "ON",
    ]
    out = sql
    for kw in keywords:
        out = re.sub(rf"\s+{kw}\s+", f"\n{kw} ", out, flags=re.IGNORECASE)
    return out.strip()


def render(
    *,
    title: str,
    summary: str,
    sections: list,
) -> str:
    if not isinstance(sections, list) or not sections:
        raise ValueError("sections must be a non-empty list")
    for i, sec in enumerate(sections):
        _validate_section(sec, i)

    # Format SQL in every section before serializing.
    sections = [
        {**sec, "sql": _format_sql(sec["sql"])} if isinstance(sec.get("sql"), str) else sec
        for sec in sections
    ]

    template = TEMPLATE_PATH.read_text()
    logo_dark_svg = LOGO_DARK_PATH.read_text()
    logo_light_svg = LOGO_LIGHT_PATH.read_text()

    out = (
        template
        .replace("{{REPORT_TITLE}}", title)
        .replace("{{REPORT_TITLE_JSON}}", json.dumps(title))
        .replace("{{REPORT_SUMMARY_JSON}}", json.dumps(summary or ""))
        .replace("{{GENERATED_AT}}", datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"))
        .replace("{{SECTIONS_JSON}}", json.dumps(sections))
        .replace("{{AGAMI_LOGO_DARK_TEXT}}", logo_dark_svg)
        .replace("{{AGAMI_LOGO_LIGHT_TEXT}}", logo_light_svg)
    )
    return out


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--title", required=True, help="Report title (the user's question)")
    p.add_argument("--summary", default="", help="1-3 sentence executive summary across all sections")

    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--section", action="append", help="JSON object for a single section. Repeat for multiple.")
    src.add_argument("--sections-file", help="Path to a JSON file containing a list of section objects.")

    p.add_argument("--out", required=True)
    args = p.parse_args()

    if args.sections_file:
        with open(os.path.expanduser(args.sections_file)) as f:
            sections = json.load(f)
        if not isinstance(sections, list):
            sys.stderr.write(f"--sections-file must contain a JSON array, got {type(sections).__name__}\n")
            return 1
    else:
        sections = [json.loads(s) for s in args.section]

    out_path = Path(os.path.expanduser(args.out))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(render(title=args.title, summary=args.summary, sections=sections))
    print(f"Wrote {out_path} ({len(sections)} section{'s' if len(sections) != 1 else ''})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
