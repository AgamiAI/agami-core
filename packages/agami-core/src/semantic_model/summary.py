"""The onboarding's closing summary as markdown, built only from engine state, so the skill posts it
as printed.

A live run computed the overview and the "Set up in …" line and then showed neither: the skill went
straight to its next card. Text that is ready to post leaves nothing to assemble, and nothing to skip
by accident. Next steps are not part of it; the card after it offers those.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from . import curate, timing
from . import loader as L
from .say_text import cell, count, plain

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
