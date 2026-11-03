#!/usr/bin/env python3
"""Draw a chart as text, for the chat, from the same SECTIONS file the HTML chart reads.

The chat cannot show an image or a page, but it shows a fenced block exactly as written. So "Show
as a chart" draws horizontal bars in characters, one row per label, from `csv_to_sections.py`'s
output: the bar lengths come from `datasets[].data` (the raw numbers) and the printed values from
`table_rows` (already formatted by the same units formatter as the chat table). Nothing is
transcribed by hand, so the chat chart, the table and the full view can never disagree.

Usage:

    python3 text_chart.py --sections-file <artifacts_dir>/local/tmp/agami-sections-<ts>.json

Prints markdown: per section its title, then a ```text block with the bars. Stdlib only.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

WIDTH = 30  # bar cells at the longest value: fits beside a label in a narrow chat column
MAX_ROWS = 15  # the chat's table limit (question-cards rule 3); the rest is counted, not drawn
LABEL_MAX = 24
FULL, PARTIALS = "█", " ▏▎▍▌▋▊▉"
MISSING = "—"
_BLANK_CELLS = ("", "nan", "none", "null", "inf", "-inf")


def _number(v: object) -> float | None:
    """A drawable value, or None for a missing one (null, NaN, infinity, text)."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    return v if math.isfinite(v) else None


def _bar(value: float | None, top: float) -> str:
    """A bar of eighth-cell precision, so close values still look different. No bar for a missing value."""
    if value is None or top <= 0 or value <= 0:
        return ""
    eighths = round(value / top * WIDTH * 8)
    return FULL * (eighths // 8) + (PARTIALS[eighths % 8] if eighths % 8 else "")


def _label(text: str) -> str:
    text = str(text)
    return text if len(text) <= LABEL_MAX else text[: LABEL_MAX - 1] + "…"


def _shown_values(section: dict, dataset_label: str, n: int) -> list[str]:
    """The formatted cell for each row, from `table_rows`, where the header names the dataset."""
    headers = section.get("table_headers") or []
    rows = section.get("table_rows") or []
    if dataset_label in headers and len(rows) >= n:
        col = headers.index(dataset_label)
        return [str(r[col]) if col < len(r) else "" for r in rows[:n]]
    return [f"{v:,}" if _number(v) is not None else "" for v in section["datasets"][0]["data"][:n]]


def draw_section(section: dict) -> str:
    """One section as markdown: its title, then the bars in a text block (or why there are none)."""
    title = section.get("title") or "Chart"
    labels = section.get("labels") or []
    datasets = section.get("datasets") or []
    if not labels or not datasets:
        return f"**{title}**: nothing to chart (no rows)."
    first = datasets[0]
    data = [_number(v) for v in first.get("data") or []]
    n = min(len(labels), len(data), MAX_ROWS)
    if n == 0 or all(v is None for v in data[:n]):
        return f"**{title}**: nothing to chart (no rows)."
    if any(v is not None and v < 0 for v in data[:n]):
        return f"**{title}**: some values are negative, which a text bar chart can't show; see the table."
    top = max((v for v in data[:n] if v is not None), default=0)
    shown = [MISSING if v is None and s.strip().lower() in _BLANK_CELLS else s
             for v, s in zip(data, _shown_values(section, first.get("label", ""), n))]
    label_w = max(len(_label(lab)) for lab in labels[:n])
    value_w = max(len(s) for s in shown)
    lines = [
        f"{_label(lab).ljust(label_w)}  {_bar(v, top).ljust(WIDTH)}  {s.rjust(value_w)}"
        for lab, v, s in zip(labels, data, shown)
    ]
    notes = []
    if len(labels) > MAX_ROWS:
        notes.append(f"Showing {MAX_ROWS} of {len(labels)}; {len(labels) - MAX_ROWS} more in the table.")
    if len(datasets) > 1:
        notes.append(f"Bars show {first.get('label') or 'the first value'}; the table has the other columns.")
    out = [f"**{title}**", "", "```text", *lines, "```"]
    return "\n".join(out + ([""] + notes if notes else []))


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--sections-file", required=True, help="csv_to_sections.py's --out file")
    args = p.parse_args(argv)
    try:
        sections = json.loads(Path(args.sections_file).read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        sys.stderr.write(f"--sections-file: {e}\n")
        return 2
    if not isinstance(sections, list):
        sys.stderr.write("--sections-file must contain a JSON array of sections\n")
        return 2
    print("\n\n".join(draw_section(s) for s in sections if isinstance(s, dict)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
