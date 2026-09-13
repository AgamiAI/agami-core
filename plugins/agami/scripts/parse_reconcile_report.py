#!/usr/bin/env python3
"""Parse the reconcile report page's back-channel block into decisions, deterministically.

The page tells every row in four beats and the person decides, per row, what happens next: `keep`
(their yes to Phase 3e's offer for that row), `change` (a definition, through save-correction), `fix`
(their own query), `reword` (the question), or `nothing`. This script does the parse so the skill
reads structured output and applies each decision through the door that already exists.

Input (stdin or --block-file):

    profile: <name>
    reconcile-run: <ts>
    decisions:
    [{"row": 1, "decision": "keep"}, {"row": 3, "decision": "change", "words": "revenue nets refunds"},
     {"row": 4, "decision": "fix"}, {"row": 5, "decision": "reword"}, {"row": 6, "decision": "nothing"}]
    done

Output (the standard contract):

    {"ok": true|false,
     "data": {"profile": ..., "run": ..., "decisions": [{row, decision, words?}]},
     "anomalies": [...], "needs_judgment": {...}|null}

A `keep` is applied only to a row the RUN says may be kept, and the run says so through its own
files: pass `--run-dir <artifacts_dir>/local/reconcile/<ts>`, and the parser reads `rows.jsonl` for
the rows whose status is `match` with a single recorded cell, and each row's `ledger.json` for the
`question_fit` part a row with a question and a statement must carry. Nothing about who may be kept
is typed by hand. A block whose `reconcile-run:` is not the run directory's name is refused whole. A
`keep` on any other row, `words` beside a `keep` or a `nothing`, a misspelt decision, a row decided
twice, a missing or non-list `decisions:` section: each sends the block back as `needs_judgment`.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_KEYS = {"profile", "reconcile-run", "decisions"}
_DECISIONS = frozenset({"keep", "change", "fix", "reword", "example", "nothing"})
_WITH_WORDS = frozenset({"change", "fix", "reword", "example"})
_FIELDS = ("row", "decision", "words")
_DROPPED_KINDS = frozenset({"unknown_decision", "decision_missing_row", "row_decided_twice",
                            "decision_not_an_object", "keep_not_offered", "words_ignored_on_keep",
                            "words_ignored_on_nothing", "words_not_text"})


def keepable_rows(run_dir: Path) -> set[int]:
    """The rows Phase 3e may offer, read from the run's own files: status `match`, one recorded cell,
    and, for a row that carries both a question and a statement, a `question_fit` part in its ledger.
    The predicate is the ledger's; this function only reads it."""
    rows_path = Path(run_dir) / "rows.jsonl"
    if not rows_path.exists():
        return set()
    keep: set[int] = set()
    for n, line in enumerate(rows_path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(record, dict) or record.get("status") != "match":
            continue
        recorded = record.get("recorded") or {}
        rows = recorded.get("rows") if isinstance(recorded, dict) else None
        if not (isinstance(rows, list) and len(rows) == 1 and isinstance(rows[0], list) and len(rows[0]) == 1):
            continue
        row = record.get("row", n)
        if record.get("question") and record.get("statement"):
            ledger_path = Path(run_dir) / "rows" / str(row) / "ledger.json"
            try:
                parts = json.loads(ledger_path.read_text(encoding="utf-8")).get("rows", [])
            except (OSError, ValueError, AttributeError):
                continue
            if not any(isinstance(p, dict) and p.get("part") == "question_fit" and p.get("verdict") == "confirmed"
                       for p in parts):
                continue
        keep.add(int(row))
    return keep


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


def parse(text: str, keepable: set[int] | None = None, run: str | None = None) -> tuple[dict, list, dict | None]:
    sec, repeated = _sections(text)
    anomalies: list = [{"kind": "key_repeated", "detail": key} for key in repeated]
    needs: dict | None = None
    decisions: list = []
    data: dict = {"profile": None, "run": None, "decisions": decisions}
    if "profile" in sec:
        data["profile"] = sec["profile"].strip() or None
    if "reconcile-run" in sec:
        data["run"] = sec["reconcile-run"].strip() or None
    if run is not None and data["run"] != run:
        # A block from another run's page must never be applied to this run.
        anomalies.append({"kind": "run_mismatch", "detail": data["run"], "expected": run})
        return data, anomalies, {"kind": "run_mismatch", "section": "reconcile-run",
                                 "ask": f"the block names run {data['run']!r}, this is run {run!r}; paste the block from this run's page"}
    if "decisions" not in sec:
        anomalies.append({"kind": "section_missing", "detail": "decisions"})
        return data, anomalies, {"kind": "section_missing", "section": "decisions",
                                 "ask": "the block has no `decisions:` section; re-copy it whole from the page"}
    try:
        parsed = json.loads(sec["decisions"])
    except Exception as e:
        anomalies.append({"kind": "bad_json", "where": "decisions", "detail": str(e)})
        return data, anomalies, {"kind": "unparseable_json", "section": "decisions",
                                 "ask": "the `decisions:` block isn't valid JSON; re-copy it from the page"}
    if not isinstance(parsed, list):
        anomalies.append({"kind": "decisions_not_list", "detail": "expected a JSON array"})
        return data, anomalies, {"kind": "decisions_not_list", "section": "decisions",
                                 "ask": "the `decisions:` value is not a JSON array; re-copy it from the page"}
    seen: set[int] = set()
    for entry in parsed:
        if not isinstance(entry, dict):
            anomalies.append({"kind": "decision_not_an_object", "detail": type(entry).__name__})
            continue
        row, decision = entry.get("row"), entry.get("decision")
        if not isinstance(row, int) or isinstance(row, bool):
            anomalies.append({"kind": "decision_missing_row", "detail": str(row)})
            continue
        if decision not in _DECISIONS:
            anomalies.append({"kind": "unknown_decision", "detail": str(decision), "row": row})
            continue
        if row in seen:
            anomalies.append({"kind": "row_decided_twice", "row": row})
            continue
        if decision == "keep" and row not in (keepable or set()):
            # The offer's predicate belongs to the ledger: a keep the run's own files do not
            # allow is not the person's to grant from a page.
            anomalies.append({"kind": "keep_not_offered", "row": row})
            continue
        seen.add(row)
        out = {"row": row, "decision": decision}
        words = entry.get("words")
        if words is not None:
            if not isinstance(words, str):
                anomalies.append({"kind": "words_not_text", "row": row})
                continue  # dropped, like every other decision that cannot be applied as written
            elif decision not in _WITH_WORDS and words.strip():
                # A keep or a nothing carries no instruction; words riding beside one are the hand
                # edit this parser exists to catch, as the grading page's parser treats SQL on a right.
                anomalies.append({"kind": f"words_ignored_on_{decision}", "row": row})
            elif words.strip():
                out["words"] = words.strip()
        decisions.append({key: out[key] for key in _FIELDS if key in out})
    dropped = sorted({a.get("row") for a in anomalies if a["kind"] in _DROPPED_KINDS and a.get("row") is not None})
    if any(a["kind"] in _DROPPED_KINDS for a in anomalies):
        needs = {"kind": "decisions_dropped", "section": "decisions", "rows": dropped,
                 "ask": "some decisions could not be applied as written (see anomalies); fix them on the "
                        "page and paste the block again"}
    return data, anomalies, needs


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Parse the reconcile report page's back-channel block.")
    ap.add_argument("--block-file", help="path to the pasted block (else stdin)")
    ap.add_argument("--run-dir", required=True,
                    help="the run directory, <artifacts_dir>/local/reconcile/<ts>; which rows may be kept is read from its files")
    args = ap.parse_args(argv)
    run_dir = Path(args.run_dir)
    if not run_dir.is_dir():
        print(json.dumps({"ok": False, "data": None, "anomalies": [{"kind": "bad_argument", "detail": f"{run_dir} is not a directory"}],
                          "needs_judgment": {"kind": "bad_argument", "ask": "pass the run directory the page was rendered from"}}, indent=2))
        return 2
    try:
        text = Path(args.block_file).read_text(encoding="utf-8") if args.block_file else sys.stdin.read()
    except OSError as exc:
        print(json.dumps({"ok": False, "data": None, "anomalies": [{"kind": "bad_argument", "detail": str(exc)}],
                          "needs_judgment": {"kind": "bad_argument", "ask": "the block file could not be read"}}, indent=2))
        return 2
    data, anomalies, needs = parse(text, keepable_rows(run_dir), run=run_dir.name)
    print(json.dumps({"ok": needs is None, "data": data, "anomalies": anomalies, "needs_judgment": needs}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
