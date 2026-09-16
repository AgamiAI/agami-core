#!/usr/bin/env python3
"""Parse the reconcile intake page's block and apply it to the rows file, deterministically.

The page showed what we read from what the person gave us; the block says which rows run and, where
the person rewrote one, the question in their words. This applies it to the rows file that
`reconcile.py intake` wrote, so the skill never hand-edits rows: a dropped row is removed, an edited
question replaces the one read from a label, and `provenance.question_from` says so.

Input (stdin or --block-file):

    profile: <name>
    reconcile-run: <ts>
    intake:
    [{"row": 1, "keep": true}, {"row": 2, "keep": false}, {"row": 3, "keep": true, "question": "How many orders were placed?"}]
    done

Output: the standard contract, `{"ok", "data": {"profile", "run", "kept", "dropped", "edited"}, "anomalies",
"needs_judgment"}`. `--rows-file` is the intake output to apply to; `--out` is where the applied rows
go (default: in place). A block naming another run, a row the file does not have, a row decided
twice, a question that is not text, or a missing section sends the block back and writes nothing.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_KEYS = {"profile", "reconcile-run", "intake"}


def _key_of(line: str):
    low = line.strip().lower()
    for k in _KEYS:
        if low.startswith(k + ":") or low == k + ":":
            return k
    return None


def _sections(text: str) -> tuple[dict, list[str]]:
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


def parse(text: str, known_rows: set[int], run: str | None = None) -> tuple[dict, list, dict | None]:
    sec, repeated = _sections(text)
    anomalies: list = [{"kind": "key_repeated", "detail": key} for key in repeated]
    data: dict = {"profile": sec.get("profile", "").strip() or None, "run": sec.get("reconcile-run", "").strip() or None,
                  "decisions": []}
    if run is not None and data["run"] != run:
        anomalies.append({"kind": "run_mismatch", "detail": data["run"], "expected": run})
        return data, anomalies, {"kind": "run_mismatch", "section": "reconcile-run",
                                 "ask": f"the block names run {data['run']!r}, this is run {run!r}; paste the block from this run's page"}
    if "intake" not in sec:
        anomalies.append({"kind": "section_missing", "detail": "intake"})
        return data, anomalies, {"kind": "section_missing", "section": "intake", "ask": "the block has no `intake:` section; re-copy it whole from the page"}
    try:
        parsed = json.loads(sec["intake"])
    except Exception as e:
        anomalies.append({"kind": "bad_json", "where": "intake", "detail": str(e)})
        return data, anomalies, {"kind": "unparseable_json", "section": "intake", "ask": "the `intake:` block isn't valid JSON; re-copy it from the page"}
    if not isinstance(parsed, list):
        anomalies.append({"kind": "intake_not_list"})
        return data, anomalies, {"kind": "intake_not_list", "section": "intake", "ask": "the `intake:` value is not a JSON array; re-copy it from the page"}
    seen: set[int] = set()
    dropped_kinds = set()
    for entry in parsed:
        if not isinstance(entry, dict) or not isinstance(entry.get("row"), int) or isinstance(entry.get("row"), bool):
            anomalies.append({"kind": "decision_missing_row", "detail": str(entry)[:80]}); dropped_kinds.add("decision_missing_row"); continue
        row = entry["row"]
        if row not in known_rows:
            anomalies.append({"kind": "unknown_row", "row": row}); dropped_kinds.add("unknown_row"); continue
        if row in seen:
            anomalies.append({"kind": "row_decided_twice", "row": row}); dropped_kinds.add("row_decided_twice"); continue
        seen.add(row)
        keep = entry.get("keep", True)
        if not isinstance(keep, bool):
            anomalies.append({"kind": "keep_not_boolean", "row": row}); dropped_kinds.add("keep_not_boolean"); continue
        out = {"row": row, "keep": keep}
        question = entry.get("question")
        if question is not None:
            if not isinstance(question, str) or not question.strip():
                anomalies.append({"kind": "question_not_text", "row": row}); dropped_kinds.add("question_not_text"); continue
            out["question"] = question.strip()
        data["decisions"].append(out)
    needs = None
    if dropped_kinds:
        needs = {"kind": "decisions_dropped", "section": "intake",
                 "rows": sorted({a.get("row") for a in anomalies if a.get("row") is not None}),
                 "ask": "some rows could not be applied as written (see anomalies); fix them on the page and paste the block again"}
    return data, anomalies, needs


def apply(intake: dict, decisions: list[dict]) -> tuple[dict, dict]:
    """The rows file with the block applied: dropped rows removed, edited questions in the person's words."""
    by_row = {d["row"]: d for d in decisions}
    kept: list[dict] = []
    counts = {"kept": 0, "dropped": 0, "edited": 0}
    for n, row in enumerate(intake.get("rows", []), 1):
        rid = row.get("row", n)
        d = by_row.get(rid, {"keep": True})
        if not d.get("keep", True):
            counts["dropped"] += 1
            continue
        if d.get("question"):
            row = dict(row, question=d["question"])
            row["provenance"] = dict(row.get("provenance") or {}, question_from="the person, on the intake page")
            counts["edited"] += 1
        kept.append(row)
        counts["kept"] += 1
    return dict(intake, rows=kept), counts


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Parse the reconcile intake page's block and apply it to the rows file.")
    ap.add_argument("--block-file", help="path to the pasted block (else stdin)")
    ap.add_argument("--rows-file", required=True, help="the output of `reconcile.py intake`")
    ap.add_argument("--run", help="the run's folder name; a block naming another run is refused")
    ap.add_argument("--out", help="where the applied rows go (default: in place)")
    args = ap.parse_args(argv)
    try:
        intake = json.loads(Path(args.rows_file).read_text(encoding="utf-8"))
        text = Path(args.block_file).read_text(encoding="utf-8") if args.block_file else sys.stdin.read()
    except (OSError, ValueError) as exc:
        print(json.dumps({"ok": False, "data": None, "anomalies": [{"kind": "bad_argument", "detail": str(exc)}],
                          "needs_judgment": {"kind": "bad_argument", "ask": "pass the rows file `reconcile.py intake` wrote and the pasted block"}}, indent=2))
        return 2
    known = {r.get("row", n) for n, r in enumerate(intake.get("rows", []), 1)}
    data, anomalies, needs = parse(text, known, run=args.run)
    counts = None
    if needs is None:
        applied, counts = apply(intake, data["decisions"])
        Path(args.out or args.rows_file).write_text(json.dumps(applied, indent=2), encoding="utf-8")
    print(json.dumps({"ok": needs is None, "data": {**data, **(counts or {})}, "anomalies": anomalies, "needs_judgment": needs}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
