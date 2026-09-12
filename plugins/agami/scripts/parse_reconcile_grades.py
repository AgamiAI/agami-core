#!/usr/bin/env python3
"""Parse the reconcile grading page's back-channel block into grades, deterministically.

The page lists every question the AI answered when there was nothing to compare against, and a
person marks each right, wrong or unsure. This script does the parse so the skill reads structured
output and applies each grade the way Phase 2.5 says: a "right" makes the answer the expected value,
a "wrong" with SQL becomes a statement graded like any other, a "wrong" with words becomes a finding
carrying them, and "unsure" does nothing.

Input (stdin or --block-file):

    profile: <name>
    reconcile-run: <ts>
    grades:
    [{"row": 3, "grade": "right"}, {"row": 4, "grade": "wrong", "sql": "SELECT ..."},
     {"row": 5, "grade": "wrong", "words": "revenue should exclude refunds"}, {"row": 6, "grade": "unsure"}]
    done

Output (the standard contract):

    {"ok": true,
     "data": {"profile": "<name>"|null, "run": "<ts>"|null, "grades": [{row, grade, sql?, words?}]},
     "anomalies": [...], "needs_judgment": {...}|null}

A grade is projected onto the four keys the page emits and nothing else reaches the skill. A "right"
that also carries SQL is an anomaly and the SQL is dropped: a right answer needs no correction, and a
statement riding in beside one is the hand edit this parser exists to catch.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

_KEYS = {"profile", "reconcile-run", "grades"}
_GRADES = frozenset({"right", "wrong", "unsure"})
_FIELDS = ("row", "grade", "sql", "words")
_STATEMENT_RE = re.compile(r"^\s*(with|select)\b", re.IGNORECASE)
_DROPPED_KINDS = frozenset({"unknown_grade", "grade_missing_row", "row_graded_twice",
                            "grade_not_an_object", "sql_ignored_on_right"})


def _key_of(line: str):
    low = line.strip().lower()
    for k in _KEYS:
        if low.startswith(k + ":") or low == k + ":":
            return k
    return None


def _sections(text: str) -> tuple[dict, list[str]]:
    """Split the block into {key: raw value text}, value spanning until the next key or `done`. A
    key appearing twice keeps the last one and says so."""
    out: dict = {}
    repeated: list[str] = []
    cur_key = None
    cur_val: list[str] = []
    for raw in text.splitlines():
        if raw.strip().lower() == "done":
            break
        k = _key_of(raw)
        if k:
            if cur_key:
                out[cur_key] = "\n".join(cur_val).strip()
            if k in out:
                repeated.append(k)
            cur_key = k
            cur_val = [raw.split(":", 1)[1]]
        elif cur_key:
            cur_val.append(raw)
    if cur_key:
        out[cur_key] = "\n".join(cur_val).strip()
    return out, repeated


def parse(text: str) -> tuple[dict, list, dict | None]:
    sec, repeated = _sections(text)
    anomalies: list = [{"kind": "key_repeated", "detail": key} for key in repeated]
    needs: dict | None = None
    grades: list = []
    data: dict = {"profile": None, "run": None, "grades": grades}

    if "profile" in sec:
        data["profile"] = sec["profile"].strip() or None
    if "reconcile-run" in sec:
        data["run"] = sec["reconcile-run"].strip() or None
    if "grades" not in sec:
        return data, anomalies, needs

    try:
        parsed = json.loads(sec["grades"])
    except Exception as e:
        anomalies.append({"kind": "bad_json", "where": "grades", "detail": str(e)})
        needs = {"kind": "unparseable_json", "section": "grades",
                 "ask": "the `grades:` block isn't valid JSON; re-copy it from the page"}
        return data, anomalies, needs
    if not isinstance(parsed, list):
        anomalies.append({"kind": "grades_not_list", "detail": "expected a JSON array"})
        return data, anomalies, needs

    seen: set[int] = set()
    for entry in parsed:
        if not isinstance(entry, dict):
            anomalies.append({"kind": "grade_not_an_object", "detail": type(entry).__name__})
            continue
        row, grade = entry.get("row"), entry.get("grade")
        if not isinstance(row, int) or isinstance(row, bool):
            anomalies.append({"kind": "grade_missing_row", "detail": str(row)})
            continue
        if grade not in _GRADES:
            anomalies.append({"kind": "unknown_grade", "detail": str(grade), "row": row})
            continue
        if row in seen:
            anomalies.append({"kind": "row_graded_twice", "row": row})
            continue
        seen.add(row)
        out = {"row": row, "grade": grade}
        for field in ("sql", "words"):
            value = entry.get(field)
            if value is None:
                continue
            if not isinstance(value, str):
                anomalies.append({"kind": f"{field}_not_text", "row": row})
                continue
            if grade != "wrong":
                # A right or unsure answer carries no correction; text riding beside it is dropped.
                anomalies.append({"kind": f"{field}_ignored_on_{grade}", "row": row})
                continue
            if field == "sql" and not _STATEMENT_RE.match(value):
                anomalies.append({"kind": "sql_not_a_statement", "row": row})
                continue
            if value.strip():
                out[field] = value.strip()
        grades.append({key: out[key] for key in _FIELDS if key in out})
    # A grade that was dropped is a person's decision that would go missing in silence: a misspelt
    # grade, a row number that is not one, a row graded twice, or SQL beside a `right` (the hand
    # edit this parser exists to catch). Any of them sends the whole block back rather than
    # applying the rest, the way the sibling parsers escalate a malformed entry.
    dropped = sorted({a.get("row") for a in anomalies
                      if a["kind"] in _DROPPED_KINDS and a.get("row") is not None})
    if any(a["kind"] in _DROPPED_KINDS for a in anomalies):
        needs = {"kind": "grades_dropped", "section": "grades", "rows": dropped,
                 "ask": "some grades could not be applied as written (see anomalies); fix them on the "
                        "page and paste the block again"}
    return data, anomalies, needs


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Parse the reconcile grading page's back-channel block.")
    ap.add_argument("--block-file", help="path to the pasted block (else stdin)")
    args = ap.parse_args(argv)
    text = Path(args.block_file).read_text(encoding="utf-8") if args.block_file else sys.stdin.read()
    data, anomalies, needs = parse(text)
    print(json.dumps({"ok": needs is None, "data": data, "anomalies": anomalies, "needs_judgment": needs},
                     indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
