#!/usr/bin/env python3
"""Summarise a discover inventory for the "pick tables" card, and write the kept-tables file.

`sm discover` writes `<profile>/.introspect/inventory.json`
(`{schemas, tables:[{schema, table, columns:[{name,type}]}]}`). This script turns it into
something short enough to show in the chat, then turns the person's answer into the
`--tables-file` that `sm introspect` takes.

Summary (default):

    inventory_summary.py --inventory <inventory.json>
    -> {"tables": n,
        "schemas": [{"name", "tables"}, ...],          # the 15 largest schemas at most
        "schemas_more": {"schemas": k, "tables": m} | null,   # what the 15 rows leave out
        "likely_noise": [{"table": "schema.table", "why": "..."}],
        "groups": {"noise_by_pattern": {"<word>": count},
                   "schema_groups": [{"label", "schemas", "tables"}]}}   # [] unless > 4 schemas

`likely_noise` matches whole words of the table name (split on `_`, `-`, `.` and case or digit
boundaries): stg, staging, tmp, temp, bak, backup, old, archive, test, or a trailing date or year.
`testimonials` is one word and matches nothing; `customers` is never flagged.

Keep (writes the file, one `schema.table` per line, in inventory order):

    inventory_summary.py --inventory <inventory.json> --keep <choice> --tables-out <file>
    <choice> = all | drop_noise | schemas:<a,b> | names:<file of table names, one per line>
    -> {"tables_file": "<file>", "kept": n}

A schema or table name that matches nothing (or a bare table name that matches several) is
reported as `{"unmatched": [...], "ambiguous": {...}}` with exit code 1, and nothing is written:
the skill asks the same card again rather than guessing.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

NOISE_WORDS = {"stg", "staging", "tmp", "temp", "bak", "backup", "old", "archive", "test"}
MAX_SCHEMA_ROWS = 15
MAX_SCHEMA_GROUPS = 4
_SPLIT = re.compile(
    r"[_\-.\s]+|(?<=[a-z])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])|(?<=[A-Za-z])(?=\d)|(?<=\d)(?=[A-Za-z])"
)
# yyyy, yyyymm, yyyymmdd: a year from 1900 to 2099, then an optional month and day.
_DATE = re.compile(r"^(?:19|20)\d{2}(?:(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])?)?$")


def _words(name: str) -> list[str]:
    return [w for w in _SPLIT.split(name) if w]


def noise_reason(table: str) -> str | None:
    """Why a table name looks like staging, a backup or a dated copy; None if it does not."""
    words = _words(table)
    for word in words:
        if word.lower() in NOISE_WORDS:
            return f"name has the word '{word.lower()}'"
    # A trailing run of digit words (2023, 2023_01, 20230115) that reads as a date.
    tail: list[str] = []
    for word in reversed(words):
        if not word.isdigit():
            break
        tail.insert(0, word)
    if (
        tail
        and len(tail) < len(words)
        and _DATE.match("".join(tail))
        and len("".join(tail)) in (4, 6, 8)
    ):
        return "name ends in a date or year"
    return None


def _qualified(t: dict) -> str:
    return f"{t['schema']}.{t['table']}" if t.get("schema") else t["table"]


def _schema_groups(counts: dict[str, int]) -> list[dict]:
    """Split many schemas into at most four alphabetical groups, as options for one card question."""
    if len(counts) <= 4:
        return []
    names = sorted(counts)
    size = -(-len(names) // MAX_SCHEMA_GROUPS)
    groups = []
    for i in range(0, len(names), size):
        chunk = names[i : i + size]
        groups.append(
            {
                "label": f"{chunk[0]} to {chunk[-1]}",
                "schemas": chunk,
                "tables": sum(counts[s] for s in chunk),
            }
        )
    return groups


def summarise(inventory: dict) -> dict:
    tables = inventory.get("tables") or []
    counts: dict[str, int] = {}
    for t in tables:
        counts[t.get("schema") or ""] = counts.get(t.get("schema") or "", 0) + 1
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    shown, rest = ranked[:MAX_SCHEMA_ROWS], ranked[MAX_SCHEMA_ROWS:]
    noise, by_pattern = [], {}
    for t in tables:
        why = noise_reason(t["table"])
        if why:
            noise.append({"table": _qualified(t), "why": why})
            key = why.split("'")[1] if "'" in why else "date or year"
            by_pattern[key] = by_pattern.get(key, 0) + 1
    return {
        "tables": len(tables),
        "schemas": [{"name": n, "tables": c} for n, c in shown],
        "schemas_more": {"schemas": len(rest), "tables": sum(c for _, c in rest)} if rest else None,
        "likely_noise": noise,
        "groups": {"noise_by_pattern": by_pattern, "schema_groups": _schema_groups(counts)},
    }


def _read_names(path: str) -> list[str]:
    lines = Path(path).expanduser().read_text(encoding="utf-8").splitlines()
    return [ln.strip().strip(",") for ln in lines if ln.strip()]


def choose(inventory: dict, choice: str) -> tuple[list[str], dict | None]:
    """→ (kept `schema.table` names in inventory order, problem dict or None)."""
    tables = inventory.get("tables") or []
    if choice == "all":
        return [_qualified(t) for t in tables], None
    if choice == "drop_noise":
        return [_qualified(t) for t in tables if not noise_reason(t["table"])], None
    if choice.startswith("schemas:"):
        wanted = [s.strip() for s in choice[len("schemas:") :].split(",") if s.strip()]
        known = {t.get("schema") or "" for t in tables}
        missing = [s for s in wanted if s not in known]
        if missing:
            return [], {"unmatched": missing}
        return [_qualified(t) for t in tables if (t.get("schema") or "") in wanted], None
    if choice.startswith("names:"):
        qualified = [_qualified(t) for t in tables]
        by_bare: dict[str, list[str]] = {}
        for t in tables:
            by_bare.setdefault(t["table"], []).append(_qualified(t))
        picked: set[str] = set()
        unmatched, ambiguous = [], {}
        for name in _read_names(choice[len("names:") :]):
            if name in qualified:
                picked.add(name)
            elif len(by_bare.get(name, [])) == 1:
                picked.add(by_bare[name][0])
            elif name in by_bare:
                ambiguous[name] = by_bare[name]
            else:
                unmatched.append(name)
        if unmatched or ambiguous:
            return [], {"unmatched": unmatched, "ambiguous": ambiguous}
        return [q for q in qualified if q in picked], None
    return [], {
        "unmatched": [choice],
        "error": "choice must be all, drop_noise, schemas:<a,b> or names:<file>",
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Summarise a discover inventory; write the kept-tables file."
    )
    ap.add_argument("--inventory", required=True, help="inventory.json written by `sm discover`")
    ap.add_argument("--keep", help="all | drop_noise | schemas:<a,b> | names:<file>")
    ap.add_argument("--tables-out", help="where --keep writes the kept schema.table list")
    args = ap.parse_args(argv)

    inventory = json.loads(Path(args.inventory).expanduser().read_text(encoding="utf-8"))
    if not args.keep:
        print(json.dumps(summarise(inventory), indent=2))
        return 0
    if not args.tables_out:
        print(json.dumps({"error": "--keep needs --tables-out"}))
        return 2
    kept, problem = choose(inventory, args.keep)
    if problem:
        print(json.dumps(problem))
        return 1
    out = Path(args.tables_out).expanduser()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("".join(t + "\n" for t in kept), encoding="utf-8")
    print(json.dumps({"tables_file": str(out), "kept": len(kept)}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
