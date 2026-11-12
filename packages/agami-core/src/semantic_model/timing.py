"""How long onboarding took, measured from the step marks in `<root>/.introspect/timeline.jsonl`.

A wait is easier to bear when you can see what it cost. The onboarding summary shows one line,
*"Set up in 6 min 40 s"*, from the marks `sm steps --mark` already writes, so the number is the
engine's own and never an estimate. Restarts are handled by the same replay `sm steps` uses.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from . import steps


def _iso(at: Optional[float]) -> Optional[str]:
    return None if at is None else datetime.fromtimestamp(at, timezone.utc).isoformat()


def duration_text(seconds: int) -> str:
    """`Set up in 6 min 40 s`; `Set up in 45 s` under a minute; `Set up in 3 min` on the minute."""
    minutes, rest = divmod(max(0, seconds), 60)
    if not minutes:
        return f"Set up in {rest} s"
    if not rest:
        return f"Set up in {minutes} min"
    return f"Set up in {minutes} min {rest} s"


def onboarding_timing(root: Path) -> dict[str, Any]:
    """Per-step seconds (first `in_progress` to last `done` of the attempt that stands, 0 when
    either is missing), the total from the earliest counted start to the latest `done`, and the
    line to show (`""` when nothing is timed)."""
    spans = steps.step_spans(Path(root).expanduser())
    result = []
    for step_id in steps.STEP_IDS:
        start, end = spans.get(step_id, (None, None))
        seconds = int(max(0.0, end - start)) if start is not None and end is not None else 0
        result.append({"id": step_id, "label": steps.LABELS[step_id], "seconds": seconds})
    starts = [s for s, _ in spans.values() if s is not None]
    ends = [e for _, e in spans.values() if e is not None]
    started_at = min(starts) if starts else None
    finished_at = max(ends) if ends else None
    timed = started_at is not None and finished_at is not None
    total = int(max(0.0, finished_at - started_at)) if timed else 0
    return {"total_seconds": total, "steps": result, "started_at": _iso(started_at),
            "finished_at": _iso(finished_at), "text": duration_text(total) if timed else ""}
