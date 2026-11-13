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

HEADER = ("Subject area", "Tables", "Described", "Metrics", "Joins", "Examples")


def _cell(text: str) -> str:
    """A name made safe for one table cell or the bold first line: a `|` would split the row into
    an extra column, and a line break would end the row (or the bold) early."""
    return " ".join(text.split()).replace("|", "\\|")


def _count(n: int, noun: str) -> str:
    return f"{n} {noun}" if n == 1 else f"{n} {noun}s"


def _row(name: str, numbers: dict[str, Any]) -> str:
    cells = [name, str(numbers["tables"]), f"{numbers['described_pct']}%", str(numbers["metrics"]),
             str(numbers["joins"]), str(numbers["examples"])]
    return "| " + " | ".join(cells) + " |"


def _signoff_line(count: int) -> str:
    if count == 1:
        return "1 definition is waiting for sign-off."
    return f"{count} definitions are waiting for sign-off."


def onboarding_summary(root: Path) -> dict[str, Any]:
    """`{"markdown": <the summary>, "totals": <sm overview's totals>}` for the model at `root`."""
    root = Path(root).expanduser()
    org = L.load_datasource(root, include_rejected=True)
    view = curate.overview(org, root)
    totals = view["totals"]
    counts = ", ".join(_count(totals[k], noun) for k, noun in
                       (("tables", "table"), ("joins", "join"), ("metrics", "metric"),
                        ("examples", "example")))
    blocks = [f"**{_cell(org.datasource)}: {counts}.**"]
    table = ["| " + " | ".join(HEADER) + " |", "|" + "---|" * len(HEADER)]
    table += [_row(_cell(a["plain_name"]), a) for a in view["areas"]]
    if len(view["areas"]) > 1:
        table.append(_row("**Total**", totals))
    blocks.append("\n".join(table))
    if totals["needs_signoff"] > 0:
        blocks.append(_signoff_line(totals["needs_signoff"]))
    took = timing.onboarding_timing(root)["text"]
    if took:
        blocks.append(took)
    return {"markdown": "\n\n".join(blocks) + "\n", "totals": totals}
