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

A `keep` on a row the run did not score `match` is refused with an anomaly and dropped: the
keep-offer's predicate is the ledger's, never the page's. Pass the rows the run scored `match` with
`--match-rows 1,4,7`; without it, every `keep` is refused, because a keep nobody could check is a
keep nobody should apply. Any dropped decision sends the whole block back as `needs_judgment`.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

_KEYS = {"profile", "reconcile-run", "decisions"}
_DECISIONS = frozenset({"keep", "change", "fix", "reword", "nothing"})
_FIELDS = ("row", "decision", "words")
_DROPPED_KINDS = frozenset({"unknown_decision", "decision_missing_row", "row_decided_twice",
                            "decision_not_an_object", "keep_not_offered"})


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


def parse(text: str, match_rows: set[int] | None = None) -> tuple[dict, list, dict | None]:
    sec, repeated = _sections(text)
    anomalies: list = [{"kind": "key_repeated", "detail": key} for key in repeated]
    needs: dict | None = None
    decisions: list = []
    data: dict = {"profile": None, "run": None, "decisions": decisions}
    if "profile" in sec:
        data["profile"] = sec["profile"].strip() or None
    if "reconcile-run" in sec:
        data["run"] = sec["reconcile-run"].strip() or None
    if "decisions" not in sec:
        return data, anomalies, needs
    try:
        parsed = json.loads(sec["decisions"])
    except Exception as e:
        anomalies.append({"kind": "bad_json", "where": "decisions", "detail": str(e)})
        return data, anomalies, {"kind": "unparseable_json", "section": "decisions",
                                 "ask": "the `decisions:` block isn't valid JSON; re-copy it from the page"}
    if not isinstance(parsed, list):
        anomalies.append({"kind": "decisions_not_list", "detail": "expected a JSON array"})
        return data, anomalies, needs
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
        if decision == "keep" and row not in (match_rows or set()):
            # The offer's predicate belongs to the ledger: a keep the run did not score `match`
            # is not the person's to grant from a page.
            anomalies.append({"kind": "keep_not_offered", "row": row})
            continue
        seen.add(row)
        out = {"row": row, "decision": decision}
        words = entry.get("words")
        if words is not None:
            if not isinstance(words, str):
                anomalies.append({"kind": "words_not_text", "row": row})
            elif words.strip():
                out["words"] = words.strip()
        decisions.append({key: out[key] for key in _FIELDS if key in out})
    dropped = sorted({a.get("row") for a in anomalies if a["kind"] in _DROPPED_KINDS and a.get("row") is not None})
    if any(a["kind"] in _DROPPED_KINDS for a in anomalies):
        needs = {"kind": "decisions_dropped", "section": "decisions", "rows": dropped,
                 "ask": "some decisions could not be applied as written (see anomalies); fix them on the "
                        "page and paste the block again"}
    return data, anomalies, needs


def _match_rows(spec: str | None) -> set[int]:
    if not spec:
        return set()
    return {int(part) for part in re.split(r"[,\s]+", spec.strip()) if part}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Parse the reconcile report page's back-channel block.")
    ap.add_argument("--block-file", help="path to the pasted block (else stdin)")
    ap.add_argument("--match-rows", help="comma-separated row numbers the run scored `match`; a keep on any other row is refused")
    args = ap.parse_args(argv)
    text = Path(args.block_file).read_text(encoding="utf-8") if args.block_file else sys.stdin.read()
    data, anomalies, needs = parse(text, _match_rows(args.match_rows))
    print(json.dumps({"ok": needs is None, "data": data, "anomalies": anomalies, "needs_judgment": needs}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
