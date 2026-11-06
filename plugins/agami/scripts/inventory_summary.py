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
        "likely_noise": [{"table": "schema.table", "why": "...", "copy_of": "schema.table" | null}],
        "noise_all_copies": bool,     # every flagged table has a same-named counterpart
        "groups": {"noise_by_pattern": {"<word>": count},
                   "schema_groups": [{"label", "schemas", "schema_count", "tables"}]}}   # [] unless > 4 schemas

`likely_noise` matches whole words at the start or end of the table name (split on `_`, `-`, `.`
and case or digit boundaries): a leading stg, staging or tmp; a trailing stg, staging, tmp, temp,
bak, backup, old, copy or archive; a trailing date or year; a leading test or temp only when the
rest of the name is another table's (`test_orders` beside `orders`). `testimonials`, `test_scores`
and `temp_readings` are not flagged. `copy_of` names the table a flagged one copies, if any.

Keep (writes the file, one `schema.table` per line, in inventory order):

    inventory_summary.py --inventory <inventory.json> --keep <choice> --tables-out <file>
    <choice> = all | drop_noise | schemas:<a,b> | names:<file of table names, one per line>
    -> {"tables_file": "<file>", "kept": n}

Show (a list for the chat, when the person asks what is in a schema or a table):

    inventory_summary.py --inventory <inventory.json> --show <schema | schema.table | table>
    -> {"schema": s, "tables": [{"table", "columns"}], "more": n}           # a schema: its tables
    -> {"table": "schema.table", "columns": [{"name", "type"}], "more": n}   # a table: its columns
    (at most 15 rows; `more` counts the rest; a name that matches nothing, or several tables, is
    reported as {"unmatched"} or {"ambiguous"} with exit 1)

Check columns (names typed in "Other" to leave out, one `schema.table.column` per line):

    inventory_summary.py --inventory <inventory.json> --check-columns <file>
    -> {"exclude_columns": ["schema.table.column", ...]}   # spelled as the inventory spells them
    (any name that is not a column of the inventory: {"unmatched": [...]}, exit 1)

A choice that keeps no tables prints `{"error": "no tables kept"}` (exit 1, nothing written).
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

# Words that mark a table as a copy, scratch or staging only at the start or the end of its name
# (`stg_orders`, `orders_bak`). A word in the middle, or `temp` / `test` leading a real fact table
# (`temp_readings`, `test_scores`), is not noise.
LEADING_NOISE = {"stg", "staging", "tmp"}
TRAILING_NOISE = {"stg", "staging", "tmp", "temp", "bak", "backup", "old", "copy", "archive"}
# Leading `test` / `temp` marks a copy only when the rest of the name is another table's
# (`test_orders` beside `orders`).
LEADING_IF_COPY = {"test", "temp"}
# A table named only one of these words is scratch.
ALONE_NOISE = {"stg", "staging", "tmp", "temp", "bak", "backup", "scratch"}
# A schema with one of these names is a whole area of scratch or copies, so all its tables are noise.
NOISE_SCHEMA_WORDS = {"staging", "stg", "tmp", "temp", "scratch", "backup", "archive", "test"}
MAX_SCHEMA_ROWS = 15
MAX_SCHEMA_GROUPS = 4
_SPLIT = re.compile(
    r"[_\-.\s]+|(?<=[a-z])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])|(?<=[A-Za-z])(?=\d)|(?<=\d)(?=[A-Za-z])"
)
# yyyy, yyyymm, yyyymmdd: a year from 1900 to 2099, then an optional month and day.
_DATE = re.compile(r"^(?:19|20)\d{2}(?:(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])?)?$")


def _words(name: str) -> list[str]:
    return [w for w in _SPLIT.split(name) if w]


def _norm(words: list[str]) -> str:
    return "_".join(w.lower() for w in words)


def _split_date(words: list[str]) -> tuple[list[str], bool]:
    """→ (the words before a trailing date or year, whether there was one). A version or quarter
    token after the date (v2, q1) goes with it."""
    rest = (
        words[:-2]
        if len(words) > 2 and words[-2].lower() in ("v", "q") and words[-1].isdigit()
        else words
    )
    tail: list[str] = []
    for word in reversed(rest):
        if not word.isdigit():
            break
        tail.insert(0, word)
    joined = "".join(tail)
    if tail and len(tail) < len(rest) and _DATE.match(joined) and len(joined) in (4, 6, 8):
        return rest[: len(rest) - len(tail)], True
    return words, False


def _base(table: str) -> list[str] | None:
    """The name with its copy markers taken off (`stg_orders_2023` -> orders); None when it has none."""
    words, dated = _split_date(_words(table))
    marked = dated
    if len(words) > 1 and words[-1].lower() in TRAILING_NOISE:
        words, marked = words[:-1], True
    if len(words) > 1 and words[0].lower() in LEADING_NOISE | LEADING_IF_COPY:
        words, marked = words[1:], True
    return words if marked else None


def noise_reason(table: str, schema: str = "", names: set[str] | None = None) -> str | None:
    """Why a table looks like staging, a backup or a dated copy; None if it does not.

    `names` is every table name in the inventory, normalised (`_norm` of its words): with it, a
    leading `test` / `temp` is noise when the rest of the name is another table's."""
    if schema.lower() in NOISE_SCHEMA_WORDS:
        return f"schema is named '{schema.lower()}'"
    all_words = _words(table)
    if len(all_words) == 1 and all_words[0].lower() in ALONE_NOISE:
        return f"name has the word '{all_words[0].lower()}'"
    words, dated = _split_date(all_words)
    if dated:
        return "name ends in a date or year"
    if len(words) > 1 and words[-1].lower() in TRAILING_NOISE:
        return f"name has the word '{words[-1].lower()}'"
    if len(words) > 1 and words[0].lower() in LEADING_NOISE:
        return f"name has the word '{words[0].lower()}'"
    if (
        len(words) > 1
        and words[0].lower() in LEADING_IF_COPY
        and names
        and _norm(words[1:]) in names
    ):
        return f"name has the word '{words[0].lower()}' before another table's name"
    return None


def copy_of(t: dict, tables: list[dict]) -> str | None:
    """The table a flagged table copies (`orders_bak` -> `sales.orders`), same schema first; None
    when no other table has its name without the markers. A table in a noise-named schema copies
    the same-named table in another schema."""
    schema = t.get("schema") or ""
    base = _base(t["table"])
    if base is None:
        if schema.lower() not in NOISE_SCHEMA_WORDS:
            return None
        base = _words(t["table"])
    key = _norm(base)
    hits = [o for o in tables if o is not t and _norm(_words(o["table"])) == key]
    hits.sort(key=lambda o: (o.get("schema") or "") != schema)
    return _qualified(hits[0]) if hits else None


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
                "schema_count": len(chunk),
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
    names = {_norm(_words(t["table"])) for t in tables}
    noise, by_pattern = [], {}
    for t in tables:
        why = noise_reason(t["table"], t.get("schema") or "", names)
        if why:
            noise.append({"table": _qualified(t), "why": why, "copy_of": copy_of(t, tables)})
            key = why.split("'")[1] if "'" in why else "date or year"
            by_pattern[key] = by_pattern.get(key, 0) + 1
    return {
        "tables": len(tables),
        "schemas": [{"name": n, "tables": c} for n, c in shown],
        "schemas_more": {"schemas": len(rest), "tables": sum(c for _, c in rest)} if rest else None,
        "likely_noise": noise,
        # Every flagged table has a same-named counterpart, so dropping them loses no data.
        "noise_all_copies": bool(noise) and all(n["copy_of"] for n in noise),
        "groups": {"noise_by_pattern": by_pattern, "schema_groups": _schema_groups(counts)},
    }


def _read_names(path: str) -> list[str]:
    lines = Path(path).expanduser().read_text(encoding="utf-8").splitlines()
    return [ln.strip().strip(",").strip("\"'`").strip() for ln in lines if ln.strip()]


def choose(inventory: dict, choice: str) -> tuple[list[str], dict | None]:
    """→ (kept `schema.table` names in inventory order, problem dict or None)."""
    tables = inventory.get("tables") or []
    if choice == "all":
        return [_qualified(t) for t in tables], None
    if choice == "drop_noise":
        names = {_norm(_words(t["table"])) for t in tables}
        return [
            _qualified(t)
            for t in tables
            if not noise_reason(t["table"], t.get("schema") or "", names)
        ], None
    if choice.startswith("schemas:"):
        wanted = [s.strip() for s in choice[len("schemas:") :].split(",") if s.strip()]
        known = {t.get("schema") or "" for t in tables}
        missing = [s for s in wanted if s not in known]
        if missing:
            return [], {"unmatched": missing}
        return [_qualified(t) for t in tables if (t.get("schema") or "") in wanted], None
    if choice.startswith("names:"):
        qualified = [_qualified(t) for t in tables]
        by_full = {q.lower(): q for q in qualified}
        by_bare: dict[str, list[str]] = {}
        for t in tables:
            by_bare.setdefault(t["table"].lower(), []).append(_qualified(t))
        picked: set[str] = set()
        unmatched, ambiguous = [], {}
        for name in _read_names(choice[len("names:") :]):
            key = name.lower()
            if key in by_full:
                picked.add(by_full[key])
            elif len(by_bare.get(key, [])) == 1:
                picked.add(by_bare[key][0])
            elif key in by_bare:
                ambiguous[name] = by_bare[key]
            else:
                unmatched.append(name)
        if unmatched or ambiguous:
            return [], {"unmatched": unmatched, "ambiguous": ambiguous}
        return [q for q in qualified if q in picked], None
    return [], {
        "unmatched": [choice],
        "error": "choice must be all, drop_noise, schemas:<a,b> or names:<file>",
    }


MAX_SHOW_ROWS = 15


def show(inventory: dict, name: str) -> tuple[dict, bool]:
    """A schema's tables, or a table's columns, capped for the chat. → (result, ok)."""
    tables = inventory.get("tables") or []
    key = name.strip().lower()
    in_schema = [t for t in tables if (t.get("schema") or "").lower() == key]
    if in_schema:
        rows = [{"table": t["table"], "columns": len(t.get("columns") or [])} for t in in_schema]
        return {
            "schema": in_schema[0].get("schema"),
            "tables": rows[:MAX_SHOW_ROWS],
            "more": max(0, len(rows) - MAX_SHOW_ROWS),
        }, True
    hits = [t for t in tables if _qualified(t).lower() == key] or [
        t for t in tables if t["table"].lower() == key
    ]
    if len(hits) > 1:
        return {"ambiguous": {name: [_qualified(t) for t in hits]}}, False
    if not hits:
        return {"unmatched": [name]}, False
    cols = [{"name": c.get("name"), "type": c.get("type")} for c in hits[0].get("columns") or []]
    return {
        "table": _qualified(hits[0]),
        "columns": cols[:MAX_SHOW_ROWS],
        "more": max(0, len(cols) - MAX_SHOW_ROWS),
    }, True


# One part of a dotted name: plain, or quoted with "", `` or [] (a quoted part may hold spaces).
_PART = r'(?:"[^"\n]+"|`[^`\n]+`|\[[^\]\n]+\]|[A-Za-z_][A-Za-z0-9_$]*)'
_DOTTED = re.compile(rf"(?<![\w$\"`\].]){_PART}(?:\.{_PART})+")
_PART_RE = re.compile(_PART)


def _dotted_names(text: str) -> list[list[str]]:
    """Every dotted name in `text`, as its unquoted parts: `db.schema.table`, `"sales"."orders"`,
    `[dbo].[orders]`. A whole name quoted as one part (`` `proj.sales.orders` ``) is split on its dots."""
    names = []
    for m in _DOTTED.finditer(text):
        parts: list[str] = []
        for p in _PART_RE.findall(m.group(0)):
            if p[0] in '"`[':
                parts.extend(p[1:-1].split(".") if p[0] == "`" else [p[1:-1]])
            else:
                parts.append(p)
        names.append(parts)
    for m in re.finditer(r"`([^`\n.]+(?:\.[^`\n.]+)+)`", text):
        if not re.search(r"`\s*\.", text[m.end() : m.end() + 2]):
            names.append(m.group(1).split("."))
    return names


def used_schemas(inventory: dict, text: str) -> list[dict]:
    """The inventory's schemas that shared SQL or questions name as `schema.table`, most used first.
    Two- and three-part names count, quoted with `"`, a backtick or `[]`, matched without case.

    A live run grouped 16 schemas alphabetically on the pick card; the person picked "asset to
    cmdb" and lost the schemas 64 of their 69 example questions read. Reading which schemas their
    own SQL names lets the card lead with those.
    """
    tables = inventory.get("tables") or []
    known = {}
    for tb in tables:
        s = tb.get("schema") or ""
        if s:
            known.setdefault(s.lower(), [s, set()])[1].add(tb["table"].lower())
    counts: dict[str, int] = {}
    for parts in _dotted_names(text):
        # `schema.table`, `db.schema.table` or `schema.table.column`: the first adjacent pair that is
        # a known schema and one of its tables names the schema.
        for first, second in zip(parts, parts[1:]):
            entry = known.get(first.lower())
            if entry and second.lower() in entry[1]:
                counts[entry[0]] = counts.get(entry[0], 0) + 1
                break
    return [
        {"schema": s, "mentions": n}
        for s, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    ]


def check_columns(inventory: dict, names: list[str]) -> tuple[list[str], list[str]]:
    """→ (the names as the inventory spells them, the names that are not a column)."""
    known = {}
    for t in inventory.get("tables") or []:
        for c in t.get("columns") or []:
            full = f"{_qualified(t)}.{c.get('name')}"
            known[full.lower()] = full
    found, unmatched = [], []
    for n in names:
        (found if n.lower() in known else unmatched).append(known.get(n.lower(), n))
    return list(dict.fromkeys(found)), unmatched


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Summarise a discover inventory; write the kept-tables file."
    )
    ap.add_argument("--inventory", required=True, help="inventory.json written by `sm discover`")
    ap.add_argument("--keep", help="all | drop_noise | schemas:<a,b> | names:<file>")
    ap.add_argument("--tables-out", help="where --keep writes the kept schema.table list")
    ap.add_argument("--show", help="a schema (its tables) or a table (its columns), for the chat")
    ap.add_argument("--check-columns", help="file of schema.table.column names to leave out")
    ap.add_argument(
        "--used-by",
        action="append",
        default=[],
        help="a file of shared SQL or questions; adds the schemas it names as `used_schemas`",
    )
    args = ap.parse_args(argv)

    inventory = json.loads(Path(args.inventory).expanduser().read_text(encoding="utf-8"))
    if args.show:
        result, ok = show(inventory, args.show)
        print(json.dumps(result, indent=2))
        return 0 if ok else 1
    if args.check_columns:
        found, unmatched = check_columns(inventory, _read_names(args.check_columns))
        print(json.dumps({"unmatched": unmatched} if unmatched else {"exclude_columns": found}))
        return 1 if unmatched else 0
    if not args.keep:
        summary = summarise(inventory)
        if args.used_by:
            text = "\n".join(
                Path(f).expanduser().read_text(encoding="utf-8", errors="replace")
                for f in args.used_by
            )
            summary["used_schemas"] = used_schemas(inventory, text)
        print(json.dumps(summary, indent=2))
        return 0
    if not args.tables_out:
        print(json.dumps({"error": "--keep needs --tables-out"}))
        return 2
    kept, problem = choose(inventory, args.keep)
    if problem:
        print(json.dumps(problem))
        return 1
    if not kept:
        print(json.dumps({"error": "no tables kept"}))
        return 1
    out = Path(args.tables_out).expanduser()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("".join(t + "\n" for t in kept), encoding="utf-8")
    print(json.dumps({"tables_file": str(out), "kept": len(kept)}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
