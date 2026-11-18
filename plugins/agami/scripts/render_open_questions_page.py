#!/usr/bin/env python3
"""Render the open questions page from `sm open-questions --all` (#676).

    render_open_questions_page.py --items-file <open-questions.json> --profile <profile> \\
        --artifacts-dir <artifacts_dir> --save-to-helper --out <page.html>
    -> {"page", "rows", "questions", "code_lists", "columns", "signer"}

One row per item, in the engine's order (questions by how far apart their readings are, then
look-alike tables, then code lists and unclear columns of the largest tables first), each under
its stable `id`:

* a question: the question in business words, the evidence numbers, and a dropdown of its readings
  (the one agami answers with, offering the other), **Ask each time** and **Don't know**;
* look-alike tables: which one holds the current data, **Both are in use**, **Don't know**;
* a code list: each code with the build's guess pre-filled, and **The meanings shown are right** /
  **Fix them** / **Don't know**;
* an unclear column: a few sample values and the build's guess, and **That's right** / **Fix it** /
  **Hide this column** / **Don't know**.

Every dropdown starts blank, and blank is undecided: nothing is chosen for the person. The page is
served by `serve_page.py --decisions`, the way every saving page is (#651), and posts
`{"page": "open-questions", "profile", "decisions": [{"id", "choice", ...}]}` once;
`parse_open_questions_decisions.py` checks that file against the same items file and writes the
`sm curate` batch. Answers are recorded for the sign-off identity saved in `local/.config`, which the
page names and cannot change; with none saved, nothing is rendered.

Every value is data: it is written into a JSON block with `<`, `>` and `&` escaped, so no value can
close the block, and the page's script shows it as text only. Stdlib only.
"""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _scratch_out  # noqa: E402
from _read_only_view import saved_signoff  # noqa: E402

_SHARED = Path(__file__).resolve().parent.parent / "shared"
TEMPLATE = _SHARED / "open-questions-template.html"
LOGO = _SHARED / "agami-logo-dark.svg"
DONT_KNOW = {"value": "dont_know", "label": "Don't know"}


def _title(q: dict) -> str:
    """The card's question without its "Type … in Other" instruction, which is the cards' only."""
    return re.split(r"\s+Type\b", str(q.get("question") or ""), maxsplit=1)[0]


def _row(q: dict) -> dict | None:
    kind = q.get("kind")
    base = {"id": q.get("id"), "title": _title(q), "evidence": q.get("evidence") or ""}
    if kind == "ambiguity" and q.get("pattern") == "unit":
        # A unit is a fact with one answer (ACE-182): its units, and no way of behaving.
        units = [
            {
                "value": f"reading:{o['label']}",
                "label": f"{o['label']} ({o.get('shown', '')}): the one people report in",
            }
            for o in q.get("options") or []
        ]
        return {**base, "kind": "question", "choices": [*units, DONT_KNOW]}
    if kind == "ambiguity":
        options = q.get("options") or []
        other = "the others" if len(options) > 2 else "the other"
        readings = [
            {
                "value": f"reading:{o['label']}",
                "label": f"{o['label']} ({o.get('shown', '')}): answer with it, offer {other}",
            }
            for o in options
        ]
        return {
            **base,
            "kind": "question",
            "choices": [*readings, {"value": "ask", "label": "Ask each time"}, DONT_KNOW],
        }
    if kind == "look_alike":
        return {
            **base,
            "kind": "same",
            "choices": [
                *(
                    {
                        "value": f"table:{t['table']}",
                        "label": f"{t['label']} holds the current data",
                    }
                    for t in q.get("tables") or []
                ),
                {"value": "both", "label": "Both are in use"},
                DONT_KNOW,
            ],
        }
    if kind == "coded_values":
        labels = q.get("labels") or {v: "" for v in q.get("values") or []}
        guess = q.get("guess") or {}
        # Confirming signs every code's meaning, so it is offered only when every code has a guess.
        whole = bool(labels) and all(str(guess.get(k) or "").strip() for k in labels)
        confirm = [{"value": "confirm", "label": "The meanings shown are right"}] if whole else []
        return {
            **base,
            "kind": "codes",
            "evidence": "",
            "codes": [{"code": k, "guess": guess.get(k, "")} for k in labels],
            "choices": [*confirm, {"value": "fix", "label": "Fix them"}, DONT_KNOW],
        }
    if kind == "unknown_column":
        guess = q.get("guess") or ""
        confirm = [{"value": "confirm", "label": "That's right"}] if guess else []
        return {
            **base,
            "kind": "column",
            "guess": guess,
            "samples": q.get("samples"),
            "choices": [
                *confirm,
                {"value": "fix", "label": "Fix it: type what it holds"},
                {"value": "hide", "label": "Hide this column"},
                DONT_KNOW,
            ],
        }
    return None


def page_rows(out: dict) -> list[dict]:
    return [r for r in (_row(q) for q in out.get("questions") or []) if r is not None]


def script_json(value: object) -> str:
    """JSON that is safe inside a <script> block: nothing in it can end the block or open a comment."""
    text = json.dumps(value, ensure_ascii=True)
    return text.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")


def _who(signoff: dict) -> str:
    if signoff.get("email") and signoff.get("role"):
        return (
            "Your answers are recorded as yours: <strong>"
            + html.escape(signoff["email"])
            + "</strong> ("
            + html.escape(signoff["role"])
            + "), from your agami settings, with the time."
        )
    return "No sign-off identity is saved yet, so answers can't be saved from this page; set it in the chat."


def render(out: dict, *, profile: str, signoff: dict) -> str:
    signer = signoff if signoff.get("email") and signoff.get("role") else None
    data = {"profile": profile, "signer": signer, "rows": page_rows(out)}
    values = {
        "PROFILE": html.escape(str(profile or "this database")),
        "LOGO": LOGO.read_text(encoding="utf-8"),
        "WHO": _who(signoff),
        "DATA": script_json(data),
    }
    # One pass, so a value that happens to contain a placeholder is never filled in again.
    return re.sub(
        r"\{\{(PROFILE|LOGO|WHO|DATA)\}\}",
        lambda m: values[m.group(1)],
        TEMPLATE.read_text(encoding="utf-8"),
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Render the open questions page.")
    ap.add_argument("--items-file", required=True, help="the output of `sm open-questions --all`")
    ap.add_argument("--profile", required=True, help="the database the questions are about")
    ap.add_argument(
        "--artifacts-dir", required=True, help="where local/.config holds the saved identity"
    )
    ap.add_argument("--out", required=True, help="where to write the page")
    # Required, and the only mode: the page exists to save answers, as the table picker does.
    ap.add_argument(
        "--save-to-helper",
        action="store_true",
        required=True,
        help="the page posts its answers to `serve_page.py --decisions` (the only mode)",
    )
    args = ap.parse_args(argv)
    page = Path(args.out).expanduser()
    if _scratch_out.guarded():
        try:
            page = _scratch_out.scratch_file(args.out, _scratch_out.PAGE)
        except _scratch_out.Refused as e:
            return _scratch_out.refuse("render_open_questions_page.py", e)
    out_doc = json.loads(Path(args.items_file).expanduser().read_text(encoding="utf-8"))
    signoff = saved_signoff(Path(args.artifacts_dir).expanduser())
    if not (signoff.get("email") and signoff.get("role")):
        print(
            json.dumps(
                {
                    "error": "no sign-off identity is saved; ask the identity card first, "
                    "since every answer is recorded as the person's"
                }
            )
        )
        return 1
    rows = page_rows(out_doc)
    _scratch_out.save(page, render(out_doc, profile=args.profile, signoff=signoff))
    kinds = [r["kind"] for r in rows]
    print(
        json.dumps(
            {
                "page": str(page),
                "rows": len(rows),
                "questions": kinds.count("question") + kinds.count("same"),
                "code_lists": kinds.count("codes"),
                "columns": kinds.count("column"),
                "signer": signoff["email"],
            }
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
