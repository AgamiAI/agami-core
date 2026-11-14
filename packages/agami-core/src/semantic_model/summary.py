"""The onboarding's closing summary as markdown, built only from engine state, so the skill posts it
as printed.

A live run computed the overview and the "Set up in …" line and then showed neither: the skill went
straight to its next card. Text that is ready to post leaves nothing to assemble, and nothing to skip
by accident.

`closing_message` is the whole message that ends onboarding: the summary, the examples kept in the
model and a few questions to ask first. It ends its turn with no card after it, because a long text
written before a tool call in the same reply reached the person only as a one-sentence summary
(shared/engine-says.md); the last message of a turn is posted as written.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from . import curate, timing
from . import loader as L
from .say_text import cell, count, plain, quoted

HEADER = ("Subject area", "Tables", "Described", "Metrics", "Joins", "Examples")


def _row(name: str, numbers: dict[str, Any]) -> str:
    cells = [name, str(numbers["tables"]), f"{numbers['described_pct']}%", str(numbers["metrics"]),
             str(numbers["joins"]), str(numbers["examples"])]
    return "| " + " | ".join(cells) + " |"


def _signoff_line(counts: dict[str, int]) -> str:
    """The sign-off card's own count, split by kind ("23 definitions are waiting for sign-off: 11
    metrics, 12 entities."): a live run's summary counted metrics alone and said 11 where the card
    said 23. A kind with nothing waiting is left out."""
    total = counts["waiting"]
    head = "1 definition is" if total == 1 else f"{total} definitions are"
    parts = [count(counts[key], one, many) for key, one, many in
             (("metrics", "metric", "metrics"), ("entities", "entity", "entities"),
              ("joins", "join", "joins")) if counts[key]]
    return f"{head} waiting for sign-off: {', '.join(parts)}."


def onboarding_summary(root: Path) -> dict[str, Any]:
    """`{"markdown": <the summary>, "totals": <sm overview's totals>}` for the model at `root`."""
    root = Path(root).expanduser()
    org = L.load_datasource(root, include_rejected=True)
    view = curate.overview(org, root)
    totals = view["totals"]
    numbers = ", ".join(count(totals[k], k[:-1], k) for k in ("tables", "joins", "metrics", "examples"))
    # The summary is posted verbatim as a `say`: model names stay one capped line, escaped.
    blocks = [f"**{plain(org.datasource)}: {numbers}.**"]
    table = ["| " + " | ".join(HEADER) + " |", "|" + "---|" * len(HEADER)]
    table += [_row(cell(a["plain_name"]), a) for a in view["areas"]]
    if len(view["areas"]) > 1:
        table.append(_row("**Total**", totals))
    blocks.append("\n".join(table))
    waiting = curate.model_signoff_counts(root)
    if waiting["waiting"] > 0:
        blocks.append(_signoff_line(waiting))
    took = timing.onboarding_timing(root)["text"]
    if took:
        blocks.append(took)
    return {"markdown": "\n\n".join(blocks) + "\n", "totals": totals}


# --- the closing message: the summary, the examples kept, the questions to ask first --------------

EXAMPLES_SHOWN = 15
PREVIEW_CHARS = 60
FAILED_NAMED = 3
SUGGESTED = 4
DESCRIPTION_CHARS = 200
RUN_FILE = "examples.last.json"


def _run_path(root: Path) -> Path:
    return root / ".introspect" / RUN_FILE


def _first_row(item: dict) -> str:
    """The first result row's values joined, as recorded (escaped only when shown)."""
    rows = item.get("row_preview") or []
    return ", ".join(str(v) for v in rows[0]) if rows and isinstance(rows[0], list) else ""


def remember_examples_run(root: Path, items: list[dict], now: Optional[datetime] = None) -> None:
    """Keep what the example run (`sm seed-validate --out`) found, in the model's `.introspect/`, so
    the closing message can show it at the end of onboarding: shown mid-run, before more calls, the
    table reached the person only as a summary. Written atomically, only inside an existing model
    folder; a record that can't be written leaves the closing table without its results."""
    root = Path(root).expanduser()
    path = _run_path(root)
    when = (now or datetime.now(timezone.utc)).isoformat(timespec="seconds")
    kept = [{"area": it.get("area"), "question": str(it.get("question", "")),
             "rows": it.get("row_count", 0), "result": _first_row(it),
             **({"error": str(it["error"])} if it.get("error") else {})} for it in items]
    try:
        if not root.is_dir():
            return
        path.parent.mkdir(exist_ok=True)
        if not L.inside_model(root, path):
            return
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".examples.last.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump({"at": when, "items": kept}, fh, ensure_ascii=False)
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    except OSError:
        pass


def _examples_run(root: Path) -> list[dict]:
    """The items of this onboarding run's example run, or [] when there was none (a ready-made model
    copied in) or the record is older than the run (an earlier onboarding's)."""
    from . import steps
    path = _run_path(root)
    try:
        if not L.inside_model(root, path):
            return []
        doc = json.loads(path.read_text(encoding="utf-8"))
        at = datetime.fromisoformat(doc["at"]).timestamp()
    except (OSError, ValueError, KeyError, TypeError):
        return []
    times = steps.current_run(root).times
    if times and at < min(times):
        return []
    items = doc.get("items")
    return [it for it in items if isinstance(it, dict)] if isinstance(items, list) else []


def _kept_examples(root: Path, org) -> list[dict]:
    """Every example the model keeps (rejected ones left out), with its area, in the model's order."""
    out = []
    for sa in org.subject_areas:
        for ex in L.list_prompt_examples(root, sa.name):
            if isinstance(ex, dict) and str(ex.get("question") or "").strip():
                out.append({"area": sa.name, "question": str(ex["question"]).strip(),
                            "tables": ex.get("tables") or []})
    return out


def _examples_blocks(kept: list[dict], run: list[dict]) -> list[str]:
    """The examples table, how many failed their run (left out), and how to drop or change one."""
    blocks = []
    ran = {(it.get("area"), it.get("question", "").strip()): it for it in run if not it.get("error")}
    if kept:
        if run:
            table = ["| Example | Rows | Result |", "|---|---|---|"]
            for ex in kept[:EXAMPLES_SHOWN]:
                it = ran.get((ex["area"], ex["question"]))
                rows, result = (str(it.get("rows", 0)), str(it.get("result") or "no rows")) if it else ("", "")
                table.append(f"| {cell(ex['question'])} | {cell(rows)} | {cell(result, PREVIEW_CHARS)} |")
            head = "**The example questions, checked against your data:**"
        else:
            table = ["| Example |", "|---|"] + [f"| {cell(ex['question'])} |" for ex in kept[:EXAMPLES_SHOWN]]
            head = "**The example questions in the model:**"
        blocks += [head, "\n".join(table)]
        if len(kept) > EXAMPLES_SHOWN:
            # its own paragraph: a line straight under the table would render as one more row
            blocks.append(f"Showing {EXAMPLES_SHOWN} of {len(kept)}.")
    failed = [it for it in run if it.get("error")]
    if failed:
        named = "; ".join(f"{quoted(it.get('question', ''))} ({quoted(it['error'], 60)})"
                          for it in failed[:FAILED_NAMED])
        more = f"; and {len(failed) - FAILED_NAMED} more" if len(failed) > FAILED_NAMED else ""
        verb = "failed its run and was" if len(failed) == 1 else "failed their run and were"
        blocks.append(f"{count(len(failed), 'example', 'examples')} {verb} left out: {named}{more}.")
    if kept:
        blocks.append("These examples are kept, and agami answers from them first. To drop or change one, "
                      "say which in your own words, for example \"drop the refunds example\".")
    return blocks


def suggested_questions(kept: list[dict], n: int = SUGGESTED) -> list[str]:
    """Up to `n` example questions to ask first: the ones joining the most tables first (they show
    agami handling joins), one subject area at a time, so a model of several areas is spread over.
    Each is an example the model keeps, so it answers from a checked statement."""
    by_area: dict[str, list[dict]] = {}
    for ex in sorted(kept, key=lambda e: -len(e["tables"])):  # stable: model order breaks ties
        by_area.setdefault(ex["area"], []).append(ex)
    picks: list[str] = []
    while len(picks) < n and any(by_area.values()):
        for queue in by_area.values():
            if queue and len(picks) < n:
                picks.append(queue.pop(0)["question"])
    return picks


def closing_message(root: Path) -> str:
    """The message that ends onboarding, ready to post as printed: the summary, the example questions
    kept in the model (with what each returned, when they were run this onboarding), and up to four
    of them to ask first, as a numbered list the person answers with a number or a question of
    their own. Model text stays in table cells or quotes (say_text)."""
    root = Path(root).expanduser()
    blocks = [onboarding_summary(root)["markdown"].rstrip("\n")]
    org = L.load_datasource(root)
    if (org.description or "").strip():
        # the line that routes a question to this database when an organization has several
        blocks.append(f"Agents pick this database by its description: {quoted(org.description, DESCRIPTION_CHARS)}")
    kept = _kept_examples(root, org)
    blocks += _examples_blocks(kept, _examples_run(root))
    blocks.append('To sign off definitions or hide columns later, say "review the model".')
    picks = suggested_questions(kept)
    if picks:
        blocks.append("**Questions to start with:**")
        blocks.append("\n".join(f"{i}. {quoted(q)}" for i, q in enumerate(picks, 1)))
        blocks.append("Reply with a number, or ask your own question.")
    else:
        blocks.append("Ask any question about your data.")
    return "\n\n".join(blocks) + "\n"
