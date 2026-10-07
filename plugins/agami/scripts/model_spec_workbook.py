"""Turn a filled-in model-spec workbook into the JSON `sm apply-spec` applies, plus the table allowlist
`sm introspect --tables-file` takes. Standard library only.

A person describes their model in a spreadsheet — subject areas, which area owns each table, the joins,
metrics, sensitive columns and rules (see `plugins/agami/shared/model-spec-format.md`). This reads the
sheets by name and their columns by header, and prints the counts it read, so the numbers a skill
reports back are counted from the file, never estimated from reading it.

    python model_spec_workbook.py parse --file spec.xlsx --out spec.json --tables-out tables.txt

Exit 0 with a JSON summary on stdout; exit 1 with `{"errors": [...]}` when a required sheet or
column is missing.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _xlsx  # noqa: E402

# sheet -> {field: accepted headers}. Headers match case- and punctuation-insensitively; any column
# not listed (a "Note", a "Source", a "% matched") is for people and is ignored.
SHEETS: dict[str, dict[str, tuple[str, ...]]] = {
    "subject areas": {
        "name": ("subject area", "name", "area"),
        "description": ("description", "what it answers"),
    },
    "tables": {
        "table": ("table",),
        "schema": ("schema",),
        "owner": ("owned by", "owner"),
        "also_in": ("also listed in", "also in", "listed in"),
        "grain": ("grain", "unique key", "key"),
    },
    "joins": {
        "area": ("subject area", "area"),
        "from_table": ("from table",),
        "from_column": ("from column",),
        "to_table": ("to table",),
        "to_column": ("to column",),
        "on": ("condition", "on", "join condition", "extra condition on"),
        "role": ("role",),
        "cardinality": ("cardinality",),
        "join_type": ("join type",),
        "approved": ("approved",),
    },
    "metrics": {
        "name": ("metric", "name"),
        "area": ("subject area", "area"),
        "definition": ("definition",),
        "calculation": ("calculation", "sql"),
        "source_tables": ("source tables", "table", "tables"),
        "approved": ("approved",),
    },
    "columns": {
        "table": ("table",),
        "column": ("column",),
        "name": ("name", "business name", "label"),
        "description": ("description", "definition"),
    },
    "sensitive columns": {"table": ("table",), "column": ("column",)},
    "rules": {"rule": ("rule",), "detail": ("detail", "rule detail", "description")},
    "settings": {"setting": ("setting",), "value": ("value",)},
}
REQUIRED = {
    "subject areas": ("name",),
    "tables": ("table", "owner"),
    "joins": ("from_table", "to_table"),
}
LISTS = {"also_in", "source_tables", "grain"}


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def _find_sheet(names: list[str], want: str) -> Optional[str]:
    return next((n for n in names if _norm(n) == want), None)


def _read(
    path: str, sheet: str, fields: dict[str, tuple[str, ...]]
) -> tuple[list[dict], list[str]]:
    _, rows = _xlsx.read_sheet(path, sheet)
    rows = [r for r in rows if any((c or "").strip() for c in r)]
    if not rows:
        return [], []
    header = [_norm(h) for h in rows[0]]
    col: dict[str, int] = {}
    for f, aliases in fields.items():
        for a in aliases:
            if _norm(a) in header:
                col[f] = header.index(_norm(a))
                break
    out = []
    for r in rows[1:]:
        item = {}
        for f, i in col.items():
            v = (r[i] if i < len(r) else "").strip()
            item[f] = [x.strip() for x in re.split(r"[,\n]", v) if x.strip()] if f in LISTS else v
        out.append(item)
    return out, sorted(col)


def parse(path: str) -> tuple[dict, list[str], list[str]]:
    names = _xlsx.sheet_names(path)
    spec: dict = {}
    errors: list[str] = []
    for want, fields in SHEETS.items():
        sheet = _find_sheet(names, want)
        if sheet is None:
            if want in REQUIRED:
                errors.append(
                    f"the workbook has no {want!r} sheet (sheets found: {', '.join(names)})"
                )
            continue
        items, found = _read(path, sheet, fields)
        for f in REQUIRED.get(want, ()):
            if f not in found:
                errors.append(
                    f"sheet {sheet!r} has no column for {f!r} "
                    f"(accepted headers: {', '.join(fields[f])})"
                )
        key = want.replace(" ", "_")
        req = REQUIRED.get(want, ())
        spec[key] = [i for i in items if all(i.get(f) for f in req) or not req]
    settings = {_norm(s.get("setting", "")): s.get("value", "") for s in spec.pop("settings", [])}
    if settings.get("fiscal year start month"):
        spec["fiscal_year_start_month"] = int(float(settings["fiscal year start month"]))
    default_schema = settings.get("schema", "")
    allow = []
    for t in spec.get("tables", []):
        schema = t.get("schema") or default_schema
        allow.append(f"{schema}.{t['table']}" if schema else t["table"])
    return spec, allow, errors


def summary(spec: dict) -> dict:
    joins = spec.get("joins", [])
    yes = ("true", "yes", "y", "1", "approved", "x")
    return {
        "subject_areas": [a["name"] for a in spec.get("subject_areas", [])],
        "tables": len(spec.get("tables", [])),
        "joins": len(joins),
        "joins_approved": sum((j.get("approved") or "").lower() in yes for j in joins),
        "joins_with_condition": sum(bool(j.get("on")) for j in joins),
        "metrics": len(spec.get("metrics", [])),
        "sensitive_columns": len(spec.get("sensitive_columns", [])),
        "columns_described": len(spec.get("columns", [])),
        "rules": len(spec.get("rules", [])),
    }


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("parse")
    p.add_argument("--file", required=True)
    p.add_argument("--out", required=True, help="where to write the spec JSON")
    p.add_argument("--tables-out", default=None, help="where to write the schema.table allowlist")
    a = ap.parse_args(argv)
    try:
        spec, allow, errors = parse(a.file)
    except (OSError, ValueError) as exc:
        print(json.dumps({"errors": [str(exc)]}))
        return 1
    if errors:
        print(json.dumps({"errors": errors}, indent=2))
        return 1
    Path(a.out).write_text(json.dumps(spec, indent=2), encoding="utf-8")
    if a.tables_out:
        Path(a.tables_out).write_text("\n".join(allow) + "\n", encoding="utf-8")
    print(json.dumps({"spec": a.out, "tables_file": a.tables_out, **summary(spec)}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
