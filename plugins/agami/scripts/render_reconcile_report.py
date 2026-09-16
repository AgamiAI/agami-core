#!/usr/bin/env python3
"""
Reconcile report page renderer.

One page per run, one card per row, told in the four beats Phase 3 tells them in: what you gave us
and how we checked it; what agami did with the question and what it answered; how it got there;
what to change or keep. Built from the same theme, logos and paste-back grammar as the intake page
and the model explorer, so a run is shown the way the semantic model is shown. Stdlib only.

Two rules: a result row appears only in the Data section's sample, and no control decides
anything on its own. A "keep" is the person's yes to Phase 3e's offer for that row and is offered
only where the run scored the row `match`; every other decision goes back through the door that
already exists (save-correction for a definition, the person for a fix or a reword).

Usage:

    python3 render_reconcile_report.py \\
        --title "Reconcile · default" --profile default --run 20260912-101500 \\
        --items-file /tmp/agami-reconcile-report-items.json \\
        --out <artifacts_dir>/local/reconcile/20260912-101500/report.html
"""

from __future__ import annotations

import argparse
import datetime
import html
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

SHARED_DIR = Path(__file__).resolve().parent.parent / "shared"
TEMPLATE_PATH = SHARED_DIR / "reconcile-report-template.html"
LOGO_DARK_PATH = SHARED_DIR / "agami-logo-dark.svg"
LOGO_LIGHT_PATH = SHARED_DIR / "agami-logo-light.svg"
THEME_PATH = SHARED_DIR / "theme.css"
PAGE_CSS_PATH = SHARED_DIR / "reconcile-pages.css"

# What one card may carry, beat by beat. Every text field is DISPLAY text the skill already wrote in
# plain language; the lists are one sentence per line. A `rows` or `recorded` key is refused.
_FIELDS = ("row", "label", "question", "source", "status", "status_words", "expected", "answer", "delta_pct", "single_cell",
           "owner", "read", "how", "words", "disagreement", "change", "todo", "report_path", "diff", "sentence", "sql_yours", "sql_agami", "sql_agami_steps", "keep_allowed", "result", "fix", "fix_words", "prefill", "summaries", "sample", "one_query")
_LISTS = ("read", "how", "words", "change", "todo")
_DIFF_KEYS = ("key", "state", "section", "family", "rolled", "yours", "agami", "note", "yours_hi", "agami_hi", "renamed")
_STATUSES = {"match", "match_unverified", "mismatch", "expected_doubtful", "error", "ungraded"}
# Who acts in beat 4, which colors the fourth column: the person's query, the semantic model, the
# question, agami's answer (a worked example), keep, or nothing.
_OWNERS = {"you", "model", "question", "agami", "keep", "nothing"}
_CHECK_STATES = {"held", "defect", "open", "gap", "noted", "differs"}
_DATA_RESULTS = {"matches", "partly", "differs", "could_not_compare", "not_graded"}
_QUERY_RESULTS = {"same", "different", "not_comparable"}
_FIXES = {"query", "semantic_model", "examples", "question", "ask_again", "none", "ungraded"}


_SAMPLE_ROWS = 5


def _validate_sample(sample: Any, idx: int) -> None:
    """The one place this page may carry warehouse values, and the cap is enforced here.

    Everything else on the card is display text built by the items verb. A sample is different: it is
    rows of real data, so the bound is checked at the surface that publishes them rather than trusted
    from whatever produced them. A page is a file that travels; five rows is a sample and five hundred
    is an export.
    """
    if sample is None:
        return
    if not isinstance(sample, dict):
        raise ValueError(f"item {idx}: 'sample' must be an object")
    rows = sample.get("rows")
    if not isinstance(rows, list) or len(rows) > _SAMPLE_ROWS:
        raise ValueError(f"item {idx}: 'sample.rows' is at most {_SAMPLE_ROWS} rows; got "
                         f"{len(rows) if isinstance(rows, list) else type(rows).__name__}")
    pairs = sample.get("pairs")
    if not isinstance(pairs, list) or not all(isinstance(p, list) and len(p) == 2 for p in pairs):
        raise ValueError(f"item {idx}: 'sample.pairs' must be a list of two-name pairs")
    other = sample.get("agami_rows")
    if other is not None and (not isinstance(other, list) or len(other) > _SAMPLE_ROWS):
        raise ValueError(f"item {idx}: 'sample.agami_rows' is at most {_SAMPLE_ROWS} rows")
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("yours"), list):
            raise ValueError(f"item {idx}: every 'sample.rows' entry needs a 'yours' list")
        if row.get("agami") is not None and not isinstance(row["agami"], list):
            raise ValueError(f"item {idx}: 'sample.rows[].agami' is a list or null")


def _validate_item(item: dict, idx: int) -> None:
    if not isinstance(item, dict):
        raise ValueError(f"item {idx}: must be an object")
    if not isinstance(item.get("row"), int) or isinstance(item.get("row"), bool):
        raise ValueError(f"item {idx}: 'row' (integer) is required")
    if not isinstance(item.get("question"), str) or not item["question"].strip():
        raise ValueError(f"item {idx}: 'question' (string) is required")
    if "rows" in item or "recorded" in item:
        raise ValueError(f"item {idx}: result rows are never rendered; pass 'answer' as display text")
    _validate_sample(item.get("sample"), idx)
    if item.get("status") not in _STATUSES:
        raise ValueError(f"item {idx}: 'status' must be one of {sorted(_STATUSES)}")
    for key in ("label", "source", "expected", "answer", "disagreement", "report_path"):
        if key in item and item[key] is not None and not isinstance(item[key], str):
            raise ValueError(f"item {idx}: '{key}' must be display text")
    for key in _LISTS:
        value = item.get(key, [])
        if not isinstance(value, list) or not all(isinstance(s, str) for s in value):
            raise ValueError(f"item {idx}: '{key}' must be a list of sentences")
    steps = item.get("sql_agami_steps", [])
    if steps is not None and (not isinstance(steps, list) or not all(isinstance(s, str) for s in steps)):
        raise ValueError(f"item {idx}: 'sql_agami_steps' must be a list of statements")
    if item.get("owner") is not None and item["owner"] not in _OWNERS:
        raise ValueError(f"item {idx}: 'owner' must be one of {sorted(_OWNERS)}")
    if item.get("delta_pct") is not None and not isinstance(item["delta_pct"], (int, float)):
        raise ValueError(f"item {idx}: 'delta_pct' must be a number")
    for row in item.get("diff", []) or []:
        if (not isinstance(row, dict) or not isinstance(row.get("key"), str) or row.get("state") not in _CHECK_STATES):
            raise ValueError(f"item {idx}: each diff row needs a 'key' and a 'state' in {sorted(_CHECK_STATES)}")
        for side in ("yours", "agami"):
            v = row.get(side)
            if v is not None and not isinstance(v, str) and not (isinstance(v, list) and all(isinstance(x, str) for x in v)):
                raise ValueError(f"item {idx}: diff '{side}' must be text or a list of tokens")
        for hi in ("yours_hi", "agami_hi"):
            v = row.get(hi)
            if v is not None and not (isinstance(v, list) and all(isinstance(x, str) for x in v)):
                raise ValueError(f"item {idx}: diff '{hi}' must be a list of the tokens to highlight")
        if row.get("note") is not None and not isinstance(row["note"], str):
            raise ValueError(f"item {idx}: diff 'note' must be text")
        renamed = row.get("renamed")
        if renamed is not None and not (isinstance(renamed, list) and all(isinstance(p, list) and len(p) == 2 and all(isinstance(x, str) for x in p) for p in renamed)):
            raise ValueError(f"item {idx}: diff 'renamed' must be a list of [yours, agami] name pairs")
        if "rows" in row or "recorded" in row:
            raise ValueError(f"item {idx}: result rows are never rendered, not even inside a diff row")
    if item.get("keep_allowed") is not None and not isinstance(item["keep_allowed"], bool):
        raise ValueError(f"item {idx}: 'keep_allowed' must be true or false")
    if item.get("one_query") is not None and not isinstance(item["one_query"], bool):
        raise ValueError(f"item {idx}: 'one_query' must be true or false")
    result = item.get("result")
    if result is not None:
        if (not isinstance(result, dict) or result.get("data") not in _DATA_RESULTS or result.get("query") not in _QUERY_RESULTS
                or not isinstance(result.get("label"), str) or not isinstance(result.get("unchecked", 0), int)):
            raise ValueError(f"item {idx}: 'result' needs data in {sorted(_DATA_RESULTS)}, query in {sorted(_QUERY_RESULTS)}, a label and an unchecked count")
    if item.get("fix") is not None and item["fix"] not in _FIXES:
        raise ValueError(f"item {idx}: 'fix' must be one of {sorted(_FIXES)}")
    if item.get("fix_words") is not None and not isinstance(item["fix_words"], str):
        raise ValueError(f"item {idx}: 'fix_words' must be text")
    prefill = item.get("prefill")
    if prefill is not None and (not isinstance(prefill, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in prefill.items())):
        raise ValueError(f"item {idx}: 'prefill' must map a decision to the words its box starts with")
    if item.get("sentence") is not None and not isinstance(item["sentence"], str):
        raise ValueError(f"item {idx}: 'sentence' must be text")
    for key in ("sql_yours", "sql_agami"):
        if item.get(key) is not None and not isinstance(item[key], str):
            raise ValueError(f"item {idx}: '{key}' must be the statement's text")


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8") if path.exists() else ""


def render(*, title: str, profile: str, run: str, items: list[dict]) -> str:
    for i, item in enumerate(items):
        _validate_item(item, i)
    rows_seen = [item["row"] for item in items]
    if len(set(rows_seen)) != len(rows_seen):
        raise ValueError("two items share a row number; decisions are keyed by row, so each must be unique")
    projected = [{k: item.get(k) for k in _FIELDS if k in item} for item in items]
    for item in projected:
        if item.get("diff"):
            item["diff"] = [{k: row.get(k) for k in _DIFF_KEYS if k in row} for row in item["diff"]]
    for item in projected:
        # ACE-138's guarantee is that every row a person reads has its status in words. report-items
        # fills it; a hand-written items file need not, and the verdict would then be blank.
        if not item.get("status_words"):
            item["status_words"] = _reconcile().status_words(item.get("status"))
        for key in _LISTS:
            item.setdefault(key, [])
        for key in ("label", "source", "expected", "answer", "disagreement", "report_path", "owner", "delta_pct"):
            item.setdefault(key, None)
        # The one rule the page enforces about decisions: keep is offered where the run said match
        # AND the answer is one cell, which is Phase 3e's own predicate; a table row can match and
        # still never be offered.
        # The keep-offer is Phase 3e's: report-items applies the parser's own gate (one cell, a confirmed fit
        # where a question and a statement both exist); an older items file falls back to the two facts.
        if isinstance(item.get("keep_allowed"), bool):
            pass
        else:
            item["keep_allowed"] = item["status"] == "match" and item.get("single_cell") is True
    template = TEMPLATE_PATH.read_text(encoding="utf-8")
    values = {
        "REPORT_TITLE": html.escape(title),
        # A date a person reads, in their own day, not an ISO string in UTC.
        "GENERATED_AT": datetime.datetime.now().strftime("%d %B %Y at %H:%M"),
        "PROFILE": html.escape(profile or ""),
        "RUN": html.escape(run or ""),
        "PROFILE_JSON": _script_json(profile or ""),
        "RUN_JSON": _script_json(run or ""),
        "ITEMS_JSON": _script_json(projected),
        # The status legend (colour family + chip words) comes from the one table in reconcile.py,
        # so the page writes no status vocabulary of its own. Same door the items come through.
        "STATUS_JSON": _script_json(_reconcile().status_legend()),
        "AGAMI_LOGO_DARK_TEXT": _read(LOGO_DARK_PATH),
        "AGAMI_LOGO_LIGHT_TEXT": _read(LOGO_LIGHT_PATH),
        "THEME_CSS": _read(THEME_PATH),
        "PAGE_CSS": _read(PAGE_CSS_PATH),
    }
    # One pass, so a placeholder token inside a person's text is copied and never expanded.
    return re.sub(r"\{\{([A-Z_]+)\}\}", lambda m: values.get(m.group(1), m.group(0)), template)


def _script_json(payload) -> str:
    """JSON safe inside a `<script>` block: every `<` written as `\\u003c`."""
    return json.dumps(payload).replace("<", "\\u003c")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Render the reconcile report page.")
    p.add_argument("--title", required=True)
    p.add_argument("--profile", required=True, help="Active profile name")
    p.add_argument("--run", default=None, help="The reconcile run's folder name (its timestamp); with --run-dir, the directory's name")
    source = p.add_mutually_exclusive_group(required=True)
    source.add_argument("--run-dir", default=None, dest="run_dir",
                        help="the run directory: the items are built from its files by reconcile.py report-items, never from a typed file")
    p.add_argument("--words-file", default=None, dest="words_file",
                   help='with --run-dir: {"<row>": {"sentence": "...", "change": ["..."]}}, the only two fields the session writes')
    source.add_argument("--items-file", dest="items_file",
                   help="JSON array of {row, label, question, source, status, expected, answer, read, how, words, disagreement, change, report_path}")
    p.add_argument("--out", required=True)
    args = p.parse_args(argv)

    run_dir = Path(os.path.expanduser(args.run_dir)) if args.run_dir else None
    if run_dir is None and args.words_file:
        sys.stderr.write("--words-file goes with --run-dir\n")
        return 1
    run = args.run or (run_dir.name if run_dir else None)
    if not run:
        sys.stderr.write("--run is required with --items-file\n")
        return 1
    if run_dir is not None:
        try:
            items = _items_from_run(run_dir, Path(os.path.expanduser(args.words_file)) if args.words_file else None)
        except ValueError as exc:
            sys.stderr.write(f"render_reconcile_report: {exc}\n")
            return 1
    else:
        with open(os.path.expanduser(args.items_file), encoding="utf-8") as f:
            items = json.load(f)
        if not isinstance(items, list):
            sys.stderr.write(f"--items-file must contain a JSON array, got {type(items).__name__}\n")
            return 1
    try:
        page = render(title=args.title, profile=args.profile, run=run, items=items)
    except ValueError as exc:
        sys.stderr.write(f"render_reconcile_report: {exc}\n")
        return 1
    out_path = Path(os.path.expanduser(args.out))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if run_dir is not None:
        page = _stamped(page, _reconcile().stamp_for(run, items))
        (run_dir / "report-items.json").write_text(json.dumps(items, indent=2), encoding="utf-8")
    out_path.write_text(page, encoding="utf-8")
    if run_dir is not None:
        for line in _three_lines(items, out_path):
            print(line)
    else:
        print(f"Wrote {out_path} ({len(items)} row{'s' if len(items) != 1 else ''})")
    return 0


def _reconcile():
    """The items verb, imported from beside this file: the renderer builds the items itself so
    nothing between the run's files and the page is typed."""
    here = str(Path(__file__).resolve().parent)
    if here not in sys.path:
        sys.path.insert(0, here)
    import reconcile  # noqa: PLC0415
    return reconcile


_WORD_FIELDS = {"sentence": str, "change": list}


def _items_from_run(run_dir: Path, words_file: Path | None) -> list[dict]:
    """The items `reconcile.py report-items` builds from the run directory, with the session's words
    laid over the two fields it may write. Any other field in the words file is refused: it belongs to
    the run's files, and a page that showed a typed check would be a page nobody could trust."""
    if not (run_dir / "rows.jsonl").exists():
        raise ValueError(f"{run_dir} holds no rows.jsonl; nothing to render")
    items = _reconcile().report_items(run_dir)
    if words_file is None:
        return items
    try:
        words = json.loads(words_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"words file: {exc}") from exc
    if not isinstance(words, dict):
        raise ValueError("words file: expected an object keyed by row number")
    by_row = {item["row"]: item for item in items}
    for key, fields in words.items():
        try:
            row = int(key)
        except (TypeError, ValueError):
            raise ValueError(f"words file: {key!r} is not a row number") from None
        if row not in by_row:
            raise ValueError(f"words file: row {row} is not in this run")
        if not isinstance(fields, dict):
            raise ValueError(f"words file: row {row} must carry an object")
        for field, value in fields.items():
            kind = _WORD_FIELDS.get(field)
            if kind is None:
                raise ValueError(f"words file: row {row} carries {field!r}, which only the run's files may write; "
                                 "the session writes sentence and change and nothing else")
            if not isinstance(value, kind) or (kind is list and not all(isinstance(v, str) for v in value)):
                raise ValueError(f"words file: row {row} {field} must be {'a sentence' if kind is str else 'a list of sentences'}")
            by_row[row][field] = value
    return items


def _stamped(page: str, stamp: str) -> str:
    """The page with its render stamp in the head, so `check-run` can tell which items it shows."""
    head = page.find("<head>")
    if head == -1:
        return stamp + "\n" + page
    at = head + len("<head>")
    return page[:at] + "\n  " + stamp + page[at:]


def _three_lines(items: list[dict], out_path: Path) -> list[str]:
    """What the skill says after rendering, counted from the items and never tallied by hand: the
    results and the fixes as words, the page's path, the next step."""
    results: dict[str, int] = {}
    fixes: dict[str, int] = {}
    for item in items:
        label = ((item.get("result") or {}).get("label") if isinstance(item.get("result"), dict) else None) or item.get("status") or "unknown"
        results[label] = results.get(label, 0) + 1
        fix = item.get("fix_words") or item.get("fix") or "unknown"
        fixes[fix] = fixes.get(fix, 0) + 1
    said = lambda counts: "; ".join(f"{label} {n}" for label, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))  # noqa: E731
    n = len(items)
    return [
        f"{n} row{'s' if n != 1 else ''}. Result: {said(results)}. Fix: {said(fixes)}.",
        f"Report: {out_path}",
        "Next: open the report, decide per row, and paste the block back.",
    ]


if __name__ == "__main__":
    sys.exit(main())
