#!/usr/bin/env python3
"""Check what the open questions page saved, and write it as one `sm curate` batch (#676).

    parse_open_questions_decisions.py --decisions-file <agami-page-<ts>.json> \\
        --items-file <open-questions.json> --expect-profile <profile> \\
        --artifacts-dir <artifacts_dir> --ops-out <ops.json>

The page (`render_open_questions_page.py`, served by `serve_page.py --decisions`) posts
`{"page": "open-questions", "profile", "decisions": [{"id", "choice", "label"?, "table"?, "labels"?,
"text"?}]}`. Nothing in it is trusted: the page and profile must be the ones the skill rendered, each
decision must name an item of the items file the page was rendered from, with a choice that item
offers. Every op is the one that item carries (the same op its card answer completes); the post only
picks which, and supplies the words of a fix:

    reading <label>   the ambiguity's op with that `default` (agami answers with it, offers the other)
    ask               the ambiguity's op with `"behaviour": "ask"` (no default)
    table <table>     the look-alike entry's ops for that table
    confirm           a code list: its labels as they are, signed; a column: its description marked
                      `ai_validated`, the build's guess confirmed by a person
    fix               a code list: the labels typed, over the ones it has; a column: the words typed,
                      stamped `source: human`
    hide              a column: an `exclude` op, so no answer reads it
    both, dont_know   nothing

A row not in the post is undecided and writes nothing: blank is never a default. Every op is signed
by the identity saved in `local/.config` (the page names it); with none saved, nothing is written.

Stdout: {"ok": true, "data": {"ops_file", "signer": {email, role}, "counts": {...}},
"refused": [{"id", "reason"}]}, or {"ok": false, "error"} with exit code 2 when the file as a whole
can't be used. Stdlib only.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _scratch_out  # noqa: E402
from _read_only_view import saved_signoff  # noqa: E402

_TOP_KEYS = {"page", "profile", "decisions"}
_KEYS = {"id", "choice", "label", "table", "labels", "text"}
# The choices each kind of item offers, and the field each choice carries (none for the others).
_CHOICES = {
    "ambiguity": {"reading": "label", "ask": None, "dont_know": None},
    "look_alike": {"table": "table", "both": None, "dont_know": None},
    "coded_values": {"confirm": None, "fix": "labels", "dont_know": None},
    "unknown_column": {"confirm": None, "fix": "text", "hide": None, "dont_know": None},
}
_UNSAFE = re.compile(r"[\x00-\x1f\x7f  ]")
_MAX_LABEL = 200
_MAX_TEXT = 500


def _line(value: object, limit: int) -> str | None:
    """`value` stripped, when it is one short line of text; else None."""
    if not isinstance(value, str):
        return None
    v = value.strip()
    return v if v and not _UNSAFE.search(v) and len(v) <= limit else None


def _column_op(source: dict, action: str = "edit", **rest: object) -> dict:
    """An op on the same column as `source`: its table, area and schema, nothing it set."""
    keep = {k: source[k] for k in ("kind", "area", "name", "schema", "column") if k in source}
    return {"op": action, **keep, **rest}


def _ops(d: dict, item: dict) -> tuple[list[dict] | None, str | None]:
    """The ops one decision writes, or why it can't be applied."""
    kind, choice = item.get("kind"), d.get("choice")
    allowed = _CHOICES.get(kind)
    if allowed is None:
        return None, "the page can't answer this kind of item"
    if choice not in allowed:
        return None, f"{choice!r} is not a choice for this item"
    field = allowed[choice]
    for other in ("label", "table", "labels", "text"):
        if other in d and other != field:
            return None, f"a {choice} carries no {other}"
    op = item.get("op") or {}
    if choice in ("dont_know", "both"):
        return [], None
    unit = kind == "ambiguity" and item.get("pattern") == "unit"
    if choice == "reading":
        picked = [o for o in item.get("options") or [] if o.get("label") == d.get("label")]
        if not picked:
            return None, f"{d.get('label')!r} is not one of its readings"
        if unit:
            # a unit is recorded as facts, as its card answer is (ACE-182): no ambiguity is declared
            return list(picked[0].get("ops") or []), None
        return [{**op, "default": d["label"]}], None
    if choice == "ask" and unit:
        return None, "a unit is a fact: pick the one people report in"
    if choice == "ask":
        return [{**op, "behaviour": "ask"}], None
    if choice == "table":
        match = [t for t in item.get("tables") or [] if t.get("table") == d.get("table")]
        if not match:
            return None, f"{d.get('table')!r} is not one of its tables"
        return list(match[0].get("ops") or []), None
    if choice == "hide":
        return [_column_op(op, "exclude")], None
    if kind == "coded_values":
        current = dict(item.get("labels") or op.get("value") or {})
        if choice == "confirm":
            if not item.get("guess"):
                return None, "there is no guess to confirm"
            # A confirm signs every code's meaning: one still blank would be signed as no meaning.
            blank = [k for k, v in current.items() if not str(v or "").strip()]
            if blank:
                return (
                    None,
                    f"{', '.join(map(str, blank))} has no meaning yet: fix it or leave the list blank",
                )
            return [{**op, "value": current}], None
        typed = d.get("labels")
        if not isinstance(typed, dict):
            return None, "a fix needs its labels"
        unknown = [k for k in typed if k not in current]
        if unknown:
            return None, f"{', '.join(map(str, unknown))} is not one of its codes"
        fixed = {}
        for k, v in typed.items():
            if isinstance(v, str) and not v.strip():
                continue  # left blank on the page: that code keeps the label it has
            line = _line(v, _MAX_LABEL)
            if line is None:
                return None, f"the meaning of {k} must be one short line"
            fixed[k] = line
        if not fixed:
            return None, "a fix needs its words"
        return [{**op, "value": {**current, **fixed}}], None
    # an unclear column
    if choice == "confirm":
        if not item.get("guess"):
            return None, "there is no guess to confirm"
        return [_column_op(op, field="description_source", value="ai_validated")], None
    text = d.get("text")
    if not isinstance(text, str) or not text.strip():
        return None, "a fix needs its words"
    line = _line(text, _MAX_TEXT)
    if line is None:
        return None, "what it holds must be one short line"
    return [{**op, "value": line}], None


def _count(counts: dict, item: dict, choice: str) -> None:
    kind = item.get("kind")
    if choice in ("dont_know", "both"):
        counts["left_as_is"] += 1
    elif kind in ("ambiguity", "look_alike"):
        counts["questions"] += 1
    elif kind == "coded_values":
        counts["code_lists"] += 1
    else:
        key = {"confirm": "columns_confirmed", "fix": "columns_described", "hide": "columns_hidden"}
        counts[key[choice]] += 1


def parse(
    doc: object, items: dict[str, dict], expect_profile: str
) -> tuple[dict | None, list, str | None]:
    if not isinstance(doc, dict):
        return None, [], "the decisions file is not a JSON object"
    if doc.get("page") != "open-questions":
        return (
            None,
            [],
            f"this decisions file is from the {doc.get('page')!r} page, not the open questions page",
        )
    if doc.get("profile") != expect_profile:
        return None, [], f"this decisions file is for another model than {expect_profile}"
    extra = set(doc) - _TOP_KEYS
    if extra:
        return None, [], f"unknown keys: {', '.join(sorted(extra))}"
    decisions = doc.get("decisions")
    if not isinstance(decisions, list):
        return None, [], "decisions is not a list"
    counts = dict.fromkeys(
        (
            "questions",
            "code_lists",
            "columns_confirmed",
            "columns_described",
            "columns_hidden",
            "left_as_is",
        ),
        0,
    )
    ops: list[dict] = []
    refused: list[dict] = []
    seen: set[str] = set()
    for d in decisions:
        if not isinstance(d, dict):
            refused.append({"id": None, "reason": "not an object"})
            continue
        ident = d.get("id")
        extra = set(d) - _KEYS
        if extra:
            refused.append({"id": ident, "reason": f"unknown keys: {', '.join(sorted(extra))}"})
            continue
        if not isinstance(ident, str) or ident not in items:
            refused.append({"id": ident, "reason": "no such item on the page"})
            continue
        if ident in seen:
            refused.append({"id": ident, "reason": "decided twice"})
            continue
        written, why = _ops(d, items[ident])
        if why:
            refused.append({"id": ident, "reason": why})
            continue
        seen.add(ident)
        ops.extend(written)
        _count(counts, items[ident], d["choice"])
    counts["undecided"] = len(items) - len(seen)
    return {"ops": ops, "counts": counts}, refused, None


def _items(path: str) -> dict[str, dict]:
    doc = json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
    questions = doc.get("questions") if isinstance(doc, dict) else None
    if not isinstance(questions, list):
        raise ValueError("the items file holds no questions list")
    return {q["id"]: q for q in questions if isinstance(q, dict) and isinstance(q.get("id"), str)}


def _fail(error: str) -> int:
    print(json.dumps({"ok": False, "error": error}))
    return 2


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Check what the open questions page saved.")
    ap.add_argument("--decisions-file", required=True)
    ap.add_argument("--items-file", required=True, help="the file the page was rendered from")
    ap.add_argument("--expect-profile", required=True, help="the profile the page was rendered for")
    ap.add_argument(
        "--artifacts-dir", required=True, help="where local/.config holds the saved identity"
    )
    ap.add_argument(
        "--ops-out", required=True, help="where to write the `sm curate --ops-file` batch"
    )
    args = ap.parse_args(argv)
    out = Path(args.ops_out).expanduser()
    if _scratch_out.guarded():
        try:
            out = _scratch_out.scratch_file(args.ops_out, ("json",))
        except _scratch_out.Refused as e:
            return _fail(str(e))
    signoff = saved_signoff(Path(args.artifacts_dir).expanduser())
    if not (signoff.get("email") and signoff.get("role")):
        return _fail("no sign-off identity is saved, so no answer can be recorded as the person's")
    try:
        doc = json.loads(Path(args.decisions_file).expanduser().read_text(encoding="utf-8"))
        items = _items(args.items_file)
    except (OSError, ValueError, RecursionError) as e:
        return _fail(f"could not read the files: {e}")
    data, refused, error = parse(doc, items, args.expect_profile)
    if error:
        return _fail(error)
    _scratch_out.write_text(out, json.dumps(data["ops"], indent=2))
    print(
        json.dumps(
            {
                "ok": True,
                "data": {"ops_file": str(out), "signer": signoff, "counts": data["counts"]},
                "refused": refused,
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
