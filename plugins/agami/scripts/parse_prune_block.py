#!/usr/bin/env python3
"""Parse the `AGAMI PRUNE` block the user pastes back from the prune page — and
write a shell-safe `--tables-file` for `sm introspect`.

The skill used to parse this format in prose and "write them to a file" by hand —
fragile, and the source of a real bug: passing the kept list as an unquoted shell
variable, which under zsh does NOT word-split, so all N names arrive as one giant
argument and the engine builds one garbage table. This script does the parse +
the file write deterministically.

Input format (stdin or --block-file):

    AGAMI PRUNE  (anything)
    keep tables: <k> of <n>
    schema.table
    schema.table
    ...
    exclude columns: <c>
    schema.table.column
    ...
    done

Output: writes the kept tables (one `schema.table` per line) to --tables-out, and
prints the contract JSON:

    {"ok": true,
     "data": {"tables_kept": N, "tables_file": "<path>", "excluded_columns": [...],
              "kept_everything": bool},
     "anomalies": [...], "needs_judgment": null}

`kept_everything` is true when k == n with no excluded columns (the skill then runs
introspect WITHOUT --tables-file). Malformed lines are reported as anomalies, not
silently dropped.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

_KEEP_RE = re.compile(r"^\s*keep\s+tables\s*:\s*(?:(\d+)\s+of\s+(\d+))?", re.I)
_EXCL_RE = re.compile(r"^\s*exclude\s+columns\s*:", re.I)
_DONE_RE = re.compile(r"^\s*done\s*$", re.I)
_TABLE_RE = re.compile(r"^[A-Za-z0-9_]+\.[A-Za-z0-9_]+$")             # schema.table
_COLUMN_RE = re.compile(r"^[A-Za-z0-9_]+\.[A-Za-z0-9_]+\.[A-Za-z0-9_]+$")  # schema.table.column


def parse(text: str) -> tuple[list[str], list[str], dict, list]:
    """→ (tables, excluded_columns, meta, anomalies)."""
    lines = text.splitlines()
    tables: list[str] = []
    excluded: list[str] = []
    anomalies: list = []
    meta = {"declared_keep": None, "declared_total": None}

    section = None  # "keep" | "exclude" | None
    for raw in lines:
        line = raw.strip().rstrip(",")
        if not line:
            section = None
            continue
        m = _KEEP_RE.match(line)
        if m:
            section = "keep"
            if m.group(1):
                meta["declared_keep"] = int(m.group(1))
                meta["declared_total"] = int(m.group(2))
            continue
        if _EXCL_RE.match(line):
            section = "exclude"
            continue
        if _DONE_RE.match(line):
            break
        if section == "keep":
            if _TABLE_RE.match(line):
                tables.append(line)
            else:
                anomalies.append({"kind": "bad_table_line", "detail": f"not schema.table: {line!r}"})
        elif section == "exclude":
            if _COLUMN_RE.match(line):
                excluded.append(line)
            else:
                anomalies.append({"kind": "bad_column_line", "detail": f"not schema.table.column: {line!r}"})
        # lines outside any section (headers/prose) are ignored

    # de-dup, preserve order
    tables = list(dict.fromkeys(tables))
    excluded = list(dict.fromkeys(excluded))
    return tables, excluded, meta, anomalies


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Parse an AGAMI PRUNE block; write a shell-safe tables file.")
    ap.add_argument("--block-file", help="path to the pasted block (else read stdin)")
    ap.add_argument("--tables-out", required=True, help="write the kept schema.table list here (one per line)")
    args = ap.parse_args(argv)

    text = Path(args.block_file).read_text(encoding="utf-8") if args.block_file else sys.stdin.read()
    if "AGAMI PRUNE" not in text.upper() and "keep tables" not in text.lower():
        print(json.dumps({"ok": False, "error": "not an AGAMI PRUNE block (no 'keep tables:' marker)"}))
        return 1

    tables, excluded, meta, anomalies = parse(text)
    if meta["declared_keep"] is not None and meta["declared_keep"] != len(tables):
        anomalies.append({"kind": "keep_count_mismatch",
                          "detail": f"header said keep {meta['declared_keep']} but parsed {len(tables)}"})

    out = Path(args.tables_out).expanduser()
    out.write_text("".join(t + "\n" for t in tables), encoding="utf-8")

    kept_everything = (meta["declared_total"] is not None
                       and len(tables) == meta["declared_total"] and not excluded)
    print(json.dumps({
        "ok": True,
        "data": {"tables_kept": len(tables), "tables_file": str(out),
                 "excluded_columns": excluded, "kept_everything": kept_everything},
        "anomalies": anomalies, "needs_judgment": None}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
