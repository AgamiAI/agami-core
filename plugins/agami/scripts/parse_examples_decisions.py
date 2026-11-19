#!/usr/bin/env python3
"""Check what the seed examples page saved, against the items it was rendered from (#651).

    parse_examples_decisions.py --decisions-file <agami-page-<ts>.json> \\
        --items-file <agami-examples-items.json> [--items-file AREA=PATH ...] --expect-profile <profile> \\
        [--area <area>] [--out-dir <dir>]

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
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _scratch_out  # noqa: E402

ACTIONS = {"keep", "drop", "fix", "note"}
_TOP_KEYS = {"page", "profile", "decisions", "note_all"}
_KEYS = {"n", "area", "question", "action", "sql", "note"}
# the field each action needs, and that no other action may carry
_NEEDS = {"fix": "sql", "note": "note"}


def _items(args: list[str], default_area: str | None, read=None) -> dict[int, dict]:
    """Every items file the page was rendered from, read and numbered exactly as the renderer does:
    its own reader (repeated `--items-file`, `AREA=PATH` labels, a file given twice or several
    unlabelled ones refused), then one global 1..N in the order given."""
    from render_examples_validation import _read_files

    rendered = [it for _, _, items in _read_files(list(args), read) for it in items]
    out = {}
    for n, it in enumerate(rendered, 1):
        if isinstance(it, dict):
            # `posted_area` is what the page showed (and posts back); `area` where the example lives
            out[n] = {
                **it,
                "n": n,
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


def _write(out_dir: Path, data: dict, flat: bool = False) -> dict:
    """Each area's keeps and fixes as `sm add-example --file` inputs, in a new private folder under
    `out_dir`, so another save checked at the same time can never overwrite the files this one returns.
    `flat` (the scratch-only guard) writes them as fresh files directly in `out_dir` instead, since
    `sm add-example` under the guard reads a file only directly inside agami's scratch folder."""
    files: dict = {"keep": {}, "fix": {}}
    out_dir.mkdir(parents=True, exist_ok=True)
    if not flat:
        out_dir = Path(tempfile.mkdtemp(dir=out_dir, prefix="agami-page-examples-"))
    for kind in ("keep", "fix"):
        for area, examples in data[kind].items():
            text = json.dumps(examples, indent=2)
            if flat:
                fd, name = tempfile.mkstemp(dir=out_dir, prefix=f"agami-page-{kind}.", suffix=".json")
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    fh.write(text)
                path = Path(name)
            else:
                path = out_dir / f"agami-page-{kind}.{area}.json"
                path.write_text(text, encoding="utf-8")
            files[kind][area] = str(path)
    return files


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Check what the seed examples page saved.")
    ap.add_argument("--decisions-file", required=True)
    ap.add_argument(
        "--items-file",
        required=True,
        action="append",
        help="the items file(s) the page was rendered from, given exactly as to "
        "render_examples_validation.py (repeatable; AREA=PATH labels a single area's file)",
    )
    ap.add_argument("--expect-profile", required=True, help="the profile the page was rendered for")
    ap.add_argument("--area", help="the area of items that carry none (a single area's items file)")
    ap.add_argument(
        "--out-dir", help="write each area's keeps and fixes as sm add-example inputs here"
    )
    args = ap.parse_args(argv)
    under_guard = _scratch_out.guarded()
    guarded = under_guard and bool(args.out_dir)
    if guarded:
        try:
            if _scratch_out._plain(args.out_dir) != _scratch_out.scratch_dir():
                raise _scratch_out.Refused(
                    f"--out-dir {args.out_dir} is not agami's scratch folder {_scratch_out.scratch_dir()}"
                )
        except _scratch_out.Refused as e:
            print(json.dumps({"ok": False, "error": str(e)}))
            return 2
    try:
        if under_guard:
            # The inputs are held to scratch too, with or without an --out-dir.
            doc = json.loads(_scratch_out.read_scratch_text(args.decisions_file))
            items = _items(args.items_file, args.area, _scratch_out.read_scratch_text)
        else:
            doc = json.loads(Path(args.decisions_file).read_text(encoding="utf-8"))
            items = _items(args.items_file, args.area)
    except _scratch_out.Refused as e:
        print(json.dumps({"ok": False, "error": str(e)}))
        return 2
    except (OSError, ValueError, RecursionError) as e:
        # Under the guard the files are not the person's to read back, so say nothing of them.
        why = "" if under_guard else f": {e}"
        print(json.dumps({"ok": False, "error": f"could not read the files{why}"}))
        return 2
    data, refused, error = parse(doc, items, args.expect_profile)
    if error:
        print(json.dumps({"ok": False, "error": error}))
        return 2
    data["files"] = (
        _write(Path(args.out_dir), data, flat=guarded) if args.out_dir else {"keep": {}, "fix": {}}
    )
    print(json.dumps({"ok": True, "data": data, "refused": refused}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
