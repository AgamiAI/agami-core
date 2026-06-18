#!/usr/bin/env python3
"""Build a chart/report SECTIONS file from result CSVs — deterministically.

The agami-query render path used to have the LLM hand-write the sections JSON
that render_chart.py consumes, TRANSCRIBING result numbers into `table_rows` and
`datasets[].data`. A miscopy shows a wrong number in the chart while the table is
right, and nothing catches it. This script takes the numbers out of the model's
hands: it reads the actual result CSV(s) and emits the sections JSON. The LLM
supplies only the *presentation spec* — title, insight prose, chart type, which
column is the label, and the verbatim SQL — never the numbers.

Reuses `semantic_model.units` (the exact formatter `sm format-table` uses) so the
formatted cells are identical across the chat table, the HTML table, and the CSV.

Input — a SPEC file (JSON list of section objects; one per chart/section):
  {
    "title":        "Top customers by spend",       # LLM
    "insights":     "Avery Adams leads at $121,561…",# LLM prose
    "chart_type":   "bar" | "line" | "pie" | "doughnut" | "scatter" | null,
    "csv_file":     "/tmp/agami-result-1.csv",       # the executed query's result
    "units":        {"total_spend": "USD"},          # from `sm prepare`
    "sql_file":     "/tmp/agami-q-1.sql",            # verbatim SQL (preferred)
    "sql":          "SELECT …",                       # or inline (sql_file wins)
    "label_col":    0,                                # optional, default 0
    "value_cols":   [1],                              # optional, default: non-label cols
    "header_relabels": {"total_amount": "Total Spend"}# optional cosmetic
  }

Output — writes the bare sections array (what render_chart.py reads via
--sections-file) to --out, and prints a status object to stdout:
  {"ok": true, "data": {"section_count": N, "out": "<path>"}, "anomalies": [...]}

Anomalies (non-fatal, surfaced for the LLM): a value column with non-numeric
cells (not chartable), a missing label/value column, an empty result. The script
still produces best-effort sections; the LLM decides whether to mention them.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from semantic_model import units  # noqa: E402  (stdlib-only module)


def _unit_for(unit_map: dict, headers: list[str], i: int):
    """Unit for column i: by output name, then by positional key `#i` (carries the
    unit for DB-auto-named columns) — same lookup as units.format_table."""
    h = headers[i] if i < len(headers) else ""
    return unit_map.get(h) or unit_map.get(f"#{i}")


def _raw_number(cell: str):
    """Parse a raw CSV cell to a float for chart data, or None if not numeric."""
    s = (cell or "").strip().replace(",", "")
    if s in ("", "-", "+"):
        return None
    try:
        f = float(s)
        return int(f) if f == int(f) else f
    except ValueError:
        return None


def _build_section(spec: dict, idx: int, anomalies: list) -> dict:
    csv_path = spec.get("csv_file")
    if not csv_path:
        raise ValueError(f"section {idx}: 'csv_file' is required")
    text = Path(csv_path).expanduser().read_text(encoding="utf-8")
    reader = list(csv.reader(io.StringIO(text)))
    headers = reader[0] if reader else []
    rows = reader[1:] if len(reader) > 1 else []
    if not headers:
        anomalies.append({"kind": "empty_result", "where": f"section {idx}",
                          "detail": f"{csv_path} has no header row"})
    if not rows:
        anomalies.append({"kind": "no_rows", "where": f"section {idx}",
                          "detail": f"{csv_path} returned 0 data rows"})

    unit_map = spec.get("units") or {}
    relabels = spec.get("header_relabels") or {}
    ncols = len(headers)
    label_col = spec.get("label_col", 0)
    if label_col >= ncols and ncols:
        anomalies.append({"kind": "label_col_oob", "where": f"section {idx}",
                          "detail": f"label_col {label_col} >= column count {ncols}; using 0"})
        label_col = 0
    value_cols = spec.get("value_cols")
    if value_cols is None:
        value_cols = [i for i in range(ncols) if i != label_col]

    # table_rows — every cell formatted EXACTLY via units (no LLM, no abbreviation).
    table_headers = [relabels.get(h, h) for h in headers]
    table_rows = [[units.format_cell(c, _unit_for(unit_map, headers, i)) for i, c in enumerate(r)]
                  for r in rows]

    # labels — the label column, formatted (handles date encodings / choice labels).
    label_unit = _unit_for(unit_map, headers, label_col)
    labels = [units.format_value(r[label_col], label_unit) if label_col < len(r) else ""
              for r in rows]

    # datasets — RAW numbers parsed from the CSV (Chart.js needs numbers, not strings).
    datasets = []
    for ci in value_cols:
        if ci >= ncols:
            continue
        data = [_raw_number(r[ci]) if ci < len(r) else None for r in rows]
        if rows and all(v is None for v in data):
            anomalies.append({"kind": "non_numeric_value_col", "where": f"section {idx}",
                              "detail": f"column '{headers[ci]}' has no numeric values; "
                                        "not chartable (left out of datasets)"})
            continue
        datasets.append({"label": relabels.get(headers[ci], headers[ci]), "data": data})

    # section-level unit — the chart's y-axis/tooltip formatting applies to ALL
    # datasets, so set it ONLY when every plotted value column shares one non-null
    # unit. Mixing a currency col with a unitless count → no section unit (else the
    # count bars would be mis-formatted as currency).
    value_units = {_unit_for(unit_map, headers, ci) for ci in value_cols if ci < ncols}
    section_unit = next(iter(value_units)) if (len(value_units) == 1 and None not in value_units) else None

    # verbatim SQL — read from a file (no transcription) if given, else inline.
    sql = None
    if spec.get("sql_file"):
        sql = Path(spec["sql_file"]).expanduser().read_text(encoding="utf-8").strip()
    elif spec.get("sql"):
        sql = spec["sql"]

    section: dict = {
        "title": spec.get("title", ""),
        "insights": spec.get("insights", ""),
        "chart_type": spec.get("chart_type"),
        "labels": labels,
        "datasets": datasets,
        "table_headers": table_headers,
        "table_rows": table_rows,
    }
    if section_unit:
        section["unit"] = section_unit
    if sql:
        section["sql"] = sql
    return section


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Build a render sections file from result CSVs.")
    ap.add_argument("--spec", required=True, help="JSON file: a list of section specs")
    ap.add_argument("--out", required=True, help="write the sections array here (render_chart --sections-file)")
    args = ap.parse_args(argv)

    try:
        spec = json.loads(Path(args.spec).expanduser().read_text(encoding="utf-8"))
    except Exception as e:
        print(json.dumps({"ok": False, "error": f"could not read spec: {e}"}))
        return 1
    if isinstance(spec, dict):
        spec = [spec]  # tolerate a single-section object
    if not isinstance(spec, list) or not spec:
        print(json.dumps({"ok": False, "error": "spec must be a non-empty list of section objects"}))
        return 1

    anomalies: list = []
    try:
        sections = [_build_section(s, i, anomalies) for i, s in enumerate(spec)]
    except Exception as e:
        print(json.dumps({"ok": False, "error": str(e)}))
        return 1

    Path(args.out).expanduser().write_text(json.dumps(sections, indent=2), encoding="utf-8")
    print(json.dumps({"ok": True, "data": {"section_count": len(sections), "out": args.out},
                      "anomalies": anomalies, "needs_judgment": None}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
