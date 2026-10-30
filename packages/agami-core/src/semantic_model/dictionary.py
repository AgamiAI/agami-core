"""Column descriptions from a data-dictionary file (TSV, CSV or JSON), matched onto the model.

`sm describe-file` reads the file here, matches each row to one column of the on-disk model, and
turns the matches into curate edit ops. A row that can't be applied comes back with a reason
instead of being dropped, so a customer's dictionary can be checked against what landed.
"""

from __future__ import annotations

import csv
import io
import json
from pathlib import Path
from typing import Any, Optional

from .loader import _read_yaml
from .models import bare_name, table_key

FORMATS = ("tsv", "csv", "json")


def format_for(path: Optional[str], override: Optional[str]) -> str:
    """The explicit format, else the file extension's; anything else reads as TSV, as it always did."""
    if override:
        return override
    ext = Path(path).suffix.lower().lstrip(".") if path else ""
    return ext if ext in ("csv", "json") else "tsv"


def _row(at: str, table: Any, column: Any, description: Any,
         area: Any = None, schema: Any = None) -> dict:
    def s(v: Any) -> str:
        return "" if v is None else str(v).strip()
    return {"at": at, "area": s(area), "schema": s(schema), "table": s(table),
            "column": s(column), "description": s(description)}


def _from_loc(at: str, loc: str, description: Any) -> dict:
    segs = [p.strip() for p in loc.split(".")]
    if len(segs) == 3:
        return _row(at, segs[1], segs[2], description, area=segs[0])
    if len(segs) == 2:
        return _row(at, segs[0], segs[1], description)
    return {"at": at, "entry": loc, "malformed": "expected table.column or area.table.column"}


def parse_tsv(text: str) -> list[dict]:
    rows = []
    for n, ln in enumerate(text.splitlines(), 1):
        if not ln.strip() or ln.lstrip().startswith("#"):
            continue
        if "\t" not in ln:
            rows.append({"at": f"line {n}", "entry": ln.strip(), "malformed": "no TAB between location and description"})
            continue
        loc, desc = ln.split("\t", 1)
        rows.append(_from_loc(f"line {n}", loc.strip(), desc))
    return rows


def parse_csv(text: str) -> list[dict]:
    # The csv module, not a split on commas, so a quoted description may hold commas and newlines.
    # Strict, so an unterminated quote fails the file instead of swallowing the rows after it.
    reader = csv.DictReader(io.StringIO(text.lstrip("﻿")), strict=True)
    try:
        fields = {(f or "").strip().lower(): f for f in reader.fieldnames or []}
        missing = [k for k in ("table", "column", "description") if k not in fields]
        if missing:
            raise ValueError(f"CSV header needs table, column and description (missing: {', '.join(missing)})")

        def get(rec: dict, key: str) -> Any:
            return rec.get(fields[key]) if key in fields else None

        rows = []
        for r in reader:
            at = f"line {reader.line_num}"
            if None in r:  # DictReader's key for fields past the header
                cells = [v for k, v in r.items() if k is not None] + r[None]
                rows.append({"at": at, "entry": ",".join(str(v) for v in cells)[:80],
                             "malformed": "more fields than the header — an unquoted comma?"})
                continue
            rows.append(_row(at, get(r, "table"), get(r, "column"), get(r, "description"),
                             area=get(r, "area"), schema=get(r, "schema")))
        return rows
    except csv.Error as exc:
        raise ValueError(f"after line {reader.line_num}: {exc}") from exc


def parse_json(text: str) -> list[dict]:
    data = json.loads(text)
    if isinstance(data, dict):
        return [_from_loc(f"key {k!r}", str(k), v) for k, v in data.items()]
    if not isinstance(data, list):
        raise ValueError("JSON must be a list of {table, column, description} objects "
                         "or an object mapping 'table.column' to a description")
    rows = []
    for i, rec in enumerate(data):
        if not isinstance(rec, dict):
            rows.append({"at": f"entry {i}", "entry": json.dumps(rec)[:80], "malformed": "not an object"})
            continue
        low = {str(k).lower(): v for k, v in rec.items()}
        rows.append(_row(f"entry {i}", low.get("table"), low.get("column"), low.get("description"),
                         area=low.get("area"), schema=low.get("schema")))
    return rows


def parse(text: str, fmt: str) -> list[dict]:
    """Rows of the file, or ValueError when the file as a whole can't be read in that format."""
    return {"tsv": parse_tsv, "csv": parse_csv, "json": parse_json}[fmt](text)


def _tables(root: Path) -> list[dict]:
    """Every table file of the model: its area, name (the file stem, as curate resolves it), the
    bare table name and schema it declares (`name: public.orders` carries its schema), and columns."""
    out = []
    for p in sorted((root / "subject_areas").glob("*/tables/*.yaml")):
        doc = _read_yaml(p) or {}
        cols = [c for c in doc.get("columns", []) or [] if isinstance(c, dict) and c.get("name")]
        declared = str(doc.get("name") or p.stem)
        out.append({"area": p.parent.parent.name, "name": p.stem, "bare": bare_name(declared),
                    "schema": table_key(declared, doc.get("schema"))[0], "columns": cols})
    return out


def _pick(cands: list, key, want: str) -> list:
    """Exact matches if any, else case-insensitive ones."""
    exact = [c for c in cands if key(c) == want]
    return exact or [c for c in cands if key(c).lower() == want.lower()]


def _entry(r: dict) -> str:
    if "entry" in r:
        return r["entry"]
    return ".".join(x for x in (r["area"], r["table"], r["column"]) if x)


def match(root: str | Path, rows: list[dict], *, source: str) -> dict:
    """Turn parsed rows into curate edit ops on the model's own spelling of each table and column.

    Returns {"ops", "unmatched", "kept_human", "case_corrected", "schema_prefix_stripped"}. With
    source `metadata` a column whose description a person wrote is left alone: a dictionary file
    fills in, it doesn't overrule an edit someone made in the model. A `SCHEMA.TABLE` in the table
    column, with no schema column, is read as that schema's table when the model has the schema."""
    tables = _tables(Path(root))
    schemas = {t["schema"] for t in tables if t["schema"]}
    ops: list[dict] = []
    unmatched: list[dict] = []
    kept_human: list[str] = []
    case_corrected: list[dict] = []
    schema_prefix_stripped: list[dict] = []

    def miss(r: dict, reason: str, detail: str) -> None:
        unmatched.append({"at": r["at"], "entry": _entry(r), "reason": reason, "detail": detail})

    for r in rows:
        if "malformed" in r:
            miss(r, "malformed", r["malformed"])
            continue
        if not r["table"] or not r["column"]:
            miss(r, "malformed", "needs both a table and a column")
            continue
        if not r["description"]:
            miss(r, "empty_description", "no description given")
            continue
        cands = tables
        if r["area"]:
            cands = _pick(cands, lambda t: t["area"], r["area"])
        want, schema, stripped = r["table"], r["schema"], False
        if "." in want and not schema and not _pick(cands, lambda t: t["name"], want):
            prefix, _, want = want.rpartition(".")
            if prefix.lower() not in schemas:
                miss(r, "no_table", f"schema {prefix!r} is not in the model")
                continue
            schema, stripped = prefix, True
        if schema:
            cands = [t for t in cands if t["schema"] == schema.lower()]
        cands = _pick(cands, lambda t: t["name"], want) or _pick(cands, lambda t: t["bare"], want)
        if not cands:
            miss(r, "no_table", f"no table {r['table']!r} in the model")
            continue
        if len(cands) > 1:
            where = ", ".join(f"{t['area']} ({t['schema'] or 'no schema'})" for t in cands)
            miss(r, "ambiguous_table", f"{r['table']!r} is in {where}; add an area or schema to the row")
            continue
        t = cands[0]
        cols = _pick(t["columns"], lambda c: c["name"], r["column"])
        if not cols:
            miss(r, "no_column", f"table {t['name']!r} has no column {r['column']!r}")
            continue
        if len(cols) > 1:
            miss(r, "ambiguous_column", f"{r['column']!r} matches {[c['name'] for c in cols]} only by case")
            continue
        col = cols[0]
        loc = f"{t['area']}.{t['name']}.{col['name']}"
        if (source == "metadata" and col.get("description_source") == "human"
                and (col.get("description") or "").strip()):
            kept_human.append(loc)
            continue
        if stripped:
            schema_prefix_stripped.append({"entry": _entry(r), "applied_to": loc})
        elif t["name"] != r["table"] or col["name"] != r["column"]:
            case_corrected.append({"entry": _entry(r), "applied_to": loc})
        ops.append({"op": "edit", "kind": "table", "area": t["area"], "name": t["name"],
                    "column": col["name"], "field": "description", "value": r["description"],
                    "source": source})
    return {"ops": ops, "unmatched": unmatched, "kept_human": kept_human,
            "case_corrected": case_corrected, "schema_prefix_stripped": schema_prefix_stripped}
