"""The onboarding's closing summary as markdown, built only from engine state, so the skill posts it
as printed.

A live run computed the overview and the "Set up in …" line and then showed neither: the skill went
straight to its next card. Text that is ready to post leaves nothing to assemble, and nothing to skip
by accident.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from . import curate, timing
from . import loader as L
from .plain_names import display_name
from .say_text import cell, count, plain

HEADER = ("Subject area", "Tables", "Described", "Metrics", "Joins", "Examples")


def _row(name: str, numbers: dict[str, Any]) -> str:
    cells = [
        name,
        str(numbers["tables"]),
        f"{numbers['described_pct']}%",
        str(numbers["metrics"]),
        str(numbers["joins"]),
        str(numbers["examples"]),
    ]
    return "| " + " | ".join(cells) + " |"


def _signoff_line(counts: dict[str, int]) -> Optional[str]:
    """The sign-off card's own count, split by kind ("23 definitions are waiting for sign-off: 11
    metrics, 12 entities."): a live run's summary counted metrics alone and said 11 where the card
    said 23. A kind with nothing waiting is left out, and None when nothing waits."""
    kinds = (
        ("metrics", "metric", "metrics"),
        ("entities", "entity", "entities"),
        ("joins", "join", "joins"),
    )
    parts = [count(counts[key], one, many) for key, one, many in kinds if counts[key]]
    total = sum(counts[key] for key, _, _ in kinds)
    if not total:
        return None
    head = "1 definition is" if total == 1 else f"{total} definitions are"
    return f"{head} waiting for sign-off: {', '.join(parts)}."


def onboarding_summary(root: Path) -> dict[str, Any]:
    """`{"markdown": <the summary>, "totals": <sm overview's totals>}` for the model at `root`."""
    root = Path(root).expanduser()
    org = L.load_datasource(root, include_rejected=True)
    view = curate.overview(org, root)
    totals = view["totals"]
    numbers = ", ".join(
        count(totals[k], k[:-1], k) for k in ("tables", "joins", "metrics", "examples")
    )
    # Posted verbatim: model names stay one capped line, escaped. The sample's key (`agami-example`)
    # is its profile, datasource and area name; a person reads its display name.
    blocks = [f"**{plain(display_name(org.datasource))}: {numbers}.**"]
    table = ["| " + " | ".join(HEADER) + " |", "|" + "---|" * len(HEADER)]
    table += [_row(cell(a["plain_name"]), a) for a in view["areas"]]
    if len(view["areas"]) > 1:
        table.append(_row("**Total**", totals))
    blocks.append("\n".join(table))
    line = _signoff_line(curate.model_signoff_counts(root))
    if line:
        blocks.append(line)
    took = timing.onboarding_timing(root)["text"]
    if took:
        blocks.append(took)
    return {"markdown": "\n\n".join(blocks) + "\n", "totals": totals}
