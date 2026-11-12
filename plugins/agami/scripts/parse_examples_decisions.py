#!/usr/bin/env python3
"""Check what the seed examples page saved, against the items it was rendered from (#651).

    parse_examples_decisions.py --decisions-file <agami-page-<ts>.json> \\
        --items-file <agami-examples-items.json> --expect-profile <profile> [--area <area>] [--out-dir <dir>]

The page (rendered `--save-to-helper`, served by `serve_page.py --decisions`) posts
`{"page": "examples", "profile", "decisions": [{"n", "area", "question", "action", "sql"?, "note"?}],
"note_all"}`. Nothing in it is trusted: the page and profile must be the ones the skill rendered, each
decision must be well formed, and its `n`, `area` and `question` must match that item of the items
file. The SQL kept is the items file's, never the post's; only a `fix` carries new SQL.

Stdout: {"ok": true, "data": {"keep": {area: [{question, sql}]}, "fix": {area: [{question, sql}]},
"drop": ["<area>/<question>"], "notes": [{n, area, question, note}], "note_all": str|null,
"files": {"keep": {area: path}, "fix": {area: path}}}, "refused": [{"n", "reason"}]}
or {"ok": false, "error": "..."} with exit code 2 when the file as a whole can't be used. With
`--out-dir`, each area's keeps and fixes are written as `sm add-example --file` inputs. Stdlib only.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

ACTIONS = {"keep", "drop", "fix", "note"}
_TOP_KEYS = {"page", "profile", "decisions", "note_all"}
_KEYS = {"n", "area", "question", "action", "sql", "note"}
# the field each action needs, and that no other action may carry
_NEEDS = {"fix": "sql", "note": "note"}


def _items(path: Path, default_area: str | None) -> dict[int, dict]:
    doc = json.loads(path.read_text(encoding="utf-8"))
    items = doc.get("items") if isinstance(doc, dict) else doc
    if not isinstance(items, list):
        raise ValueError("the items file holds no items")
    out = {}
    for it in items:
        if isinstance(it, dict) and isinstance(it.get("n"), int):
            # `posted_area` is what the page showed (and posts back); `area` where the example lives
            out[it["n"]] = {
                **it,
                "posted_area": it.get("area"),
                "area": it.get("area") or default_area,
            }
    return out


def _problem(d: object, items: dict[int, dict], seen: set[int]) -> str | None:
    """Why one decision can't be applied, or None."""
    if not isinstance(d, dict):
        return "not an object"
    extra = set(d) - _KEYS
    if extra:
        return f"unknown keys: {', '.join(sorted(extra))}"
    n, action = d.get("n"), d.get("action")
    if not isinstance(n, int) or isinstance(n, bool):
        return "n is not a number"
    if n in seen:
        return "decided twice"
    if not isinstance(action, str) or action not in ACTIONS:
        return f"action {action!r} is not one of {', '.join(sorted(ACTIONS))}"
    for act, field in _NEEDS.items():
        value = d.get(field)
        if action == act and not (isinstance(value, str) and value.strip()):
            return f"a {act} needs its {field}"
        if action != act and field in d:
            return f"a {action} carries no {field}"
    item = items.get(n)
    if item is None:
        return "no such example on the page"
    if not item.get("area"):
        return "the items carry no area; pass --area"
    if d.get("question") != item.get("question") or d.get("area") != item["posted_area"]:
        return "the question or area is not that example's"
    if action == "keep" and item.get("error"):
        return "it failed, so it has no result to keep"
    return None


def parse(
    doc: object, items: dict[int, dict], expect_profile: str
) -> tuple[dict | None, list, str | None]:
    if not isinstance(doc, dict):
        return None, [], "the decisions file is not a JSON object"
    if doc.get("page") != "examples":
        return (
            None,
            [],
            f"this decisions file is from the {doc.get('page')!r} page, not the examples page",
        )
    if doc.get("profile") != expect_profile:
        return None, [], f"this decisions file is for another model than {expect_profile}"
    extra = set(doc) - _TOP_KEYS
    if extra:
        return None, [], f"unknown keys: {', '.join(sorted(extra))}"
    decisions, note_all = doc.get("decisions"), doc.get("note_all")
    if not isinstance(decisions, list):
        return None, [], "decisions is not a list"
    if note_all is not None and not isinstance(note_all, str):
        return None, [], "note_all is not text"
    data: dict = {
        "keep": {},
        "fix": {},
        "drop": [],
        "notes": [],
        "note_all": (note_all or "").strip() or None,
    }
    refused, seen = [], set()
    for d in decisions:
        why = _problem(d, items, seen)
        n = d.get("n") if isinstance(d, dict) else None
        if why:
            refused.append({"n": n, "reason": why})
            continue
        seen.add(n)
        item = items[n]
        area, question = item["area"], item["question"]
        if d["action"] == "keep":
            data["keep"].setdefault(area, []).append(
                {"question": question, "sql": item.get("sql", "")}
            )
        elif d["action"] == "fix":
            data["fix"].setdefault(area, []).append({"question": question, "sql": d["sql"].strip()})
        elif d["action"] == "drop":
            data["drop"].append(f"{area}/{question}")
        else:
            data["notes"].append(
                {"n": n, "area": area, "question": question, "note": d["note"].strip()}
            )
    return data, refused, None


def _write(out_dir: Path, data: dict) -> dict:
    """Each area's keeps and fixes as `sm add-example --file` inputs, in a new private folder under
    `out_dir`, so another save checked at the same time can never overwrite the files this one returns."""
    files: dict = {"keep": {}, "fix": {}}
    out_dir.mkdir(parents=True, exist_ok=True)
    out_dir = Path(tempfile.mkdtemp(dir=out_dir, prefix="agami-page-examples-"))
    for kind in ("keep", "fix"):
        for area, examples in data[kind].items():
            path = out_dir / f"agami-page-{kind}.{area}.json"
            path.write_text(json.dumps(examples, indent=2), encoding="utf-8")
            files[kind][area] = str(path)
    return files


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Check what the seed examples page saved.")
    ap.add_argument("--decisions-file", required=True)
    ap.add_argument("--items-file", required=True, help="the items file the page was rendered from")
    ap.add_argument("--expect-profile", required=True, help="the profile the page was rendered for")
    ap.add_argument("--area", help="the area of items that carry none (a single area's items file)")
    ap.add_argument(
        "--out-dir", help="write each area's keeps and fixes as sm add-example inputs here"
    )
    args = ap.parse_args(argv)
    try:
        doc = json.loads(Path(args.decisions_file).read_text(encoding="utf-8"))
        items = _items(Path(args.items_file), args.area)
    except (OSError, ValueError, RecursionError) as e:
        print(json.dumps({"ok": False, "error": f"could not read the files: {e}"}))
        return 2
    data, refused, error = parse(doc, items, args.expect_profile)
    if error:
        print(json.dumps({"ok": False, "error": error}))
        return 2
    data["files"] = _write(Path(args.out_dir), data) if args.out_dir else {"keep": {}, "fix": {}}
    print(json.dumps({"ok": True, "data": data, "refused": refused}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
