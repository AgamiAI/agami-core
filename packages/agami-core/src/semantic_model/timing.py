"""How long onboarding took, measured from the step marks in `<root>/.introspect/timeline.jsonl`.

A wait is easier to bear when you can see what it cost. The onboarding summary shows one line,
*"Set up in 6 min 40 s"*, from the marks `sm steps --mark` already writes, so the number is the
engine's own and never an estimate. Only the current run counts (a reload starts a new one), read
through the same replay `sm steps` uses.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from . import steps

# Two marks this far apart mean the person stopped and came back (overnight, after a meeting): the
# step times still stand, but a "Set up in …" line over a run with a break in it would mislead.
PAUSE_GAP_SECONDS = 7200


def _iso(at: Optional[float]) -> Optional[str]:
    return None if at is None else datetime.fromtimestamp(at, timezone.utc).isoformat()


def duration_text(seconds: int) -> str:
    """`Set up in 45 s`, `Set up in 6 min 40 s` (`3 min` on the minute), `Set up in 1 h 5 min`
    (`2 h` on the hour); `""` for 0."""
    if seconds <= 0:
        return ""
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"Set up in {hours} h {minutes} min" if minutes else f"Set up in {hours} h"
    if not minutes:
        return f"Set up in {secs} s"
    return f"Set up in {minutes} min {secs} s" if secs else f"Set up in {minutes} min"


def _paused(times: list[float]) -> bool:
    return any(b - a > PAUSE_GAP_SECONDS for a, b in zip(times, times[1:]))


def onboarding_timing(root: Path) -> dict[str, Any]:
    """The current run's steps (from the step it restarted at) with their seconds, first
    `in_progress` to last `done` of the attempt that stands, 0 when either is missing; the total as
    wall-clock from the run's first mark to its last, since the person waited through the time
    between steps too (a live run's sum of steps read 11 min 55 s against a 13 min wait); and the
    line to show, `""` when the run has nothing timed or was paused."""
    run = steps.current_run(Path(root).expanduser())
    result = []
    for step_id in steps.STEP_IDS[steps.STEP_IDS.index(run.first_step):]:
        start, end = run.spans.get(step_id, (None, None))
        seconds = int(max(0.0, end - start)) if start is not None and end is not None else 0
        result.append({"id": step_id, "label": steps.LABELS[step_id], "seconds": seconds})
    # File order, not min/max: a clock that stepped back reads as nothing timed, never as a span.
    total = int(max(0.0, run.times[-1] - run.times[0])) if run.times else 0
    starts = [s for s, _ in run.spans.values() if s is not None]
    ends = [e for _, e in run.spans.values() if e is not None]
    paused = _paused(run.times)
    return {"total_seconds": total, "steps": result,
            "started_at": _iso(min(starts)) if starts else None,
            "finished_at": _iso(max(ends)) if ends else None,
            "paused": paused, "text": "" if paused else duration_text(total)}
