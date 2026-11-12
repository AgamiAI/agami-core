"""The onboarding step list, with each step's status and count read from disk.

agami-connect shows its steps as a task list. When the model re-typed them from prose, the list
drifted between turns and was lost when a turn ended; `sm steps` prints it from the engine instead,
so every surface draws the same list. Status comes from the marks a skill appends to
`<root>/.introspect/timeline.jsonl` and from what is on disk (a model that loads, a build's progress
log); every count comes from the engine's own numbers, never an estimate.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from . import loader as L

STEP_IDS = ("connect", "context", "tables", "build", "describe", "signoff", "examples", "reports",
            "first_question")

# The skill's existing wording (agami-connect "Progress tracking"), so the list reads the same.
LABELS = {
    "connect": "Connect your database",
    "context": "Tell agami about your data",
    "tables": "Pick the tables",
    "build": "Read the tables and joins",
    "describe": "Describe columns and add metrics",
    "signoff": "Sign off definitions and hide personal data",
    "examples": "Write and check example questions",
    "reports": "Check your reports",
    "first_question": "Summary and first question",
}

MARK_STATUSES = ("in_progress", "done", "skipped")

# Each progress-log line, in the person's words: (pattern, phrase, unit). The engine's own wording
# (`columns+grain 30/52`) is for the engine; a person reads "Reading columns … 30/52 tables".
PROGRESS_PHRASES = (
    (re.compile(r"^discovered (\d+) tables"), "Found {0} tables", None),
    (re.compile(r"^columns\+grain (\d+)/(\d+)"), "Reading columns", "tables"),
    (re.compile(r"^relationships: declared FK (\d+)/(\d+)"), "Checking joins", "keys"),
    (re.compile(r"^building relationships"), "Working out the joins", None),
    (re.compile(r"^join candidates"), "Testing possible joins", None),
    (re.compile(r"^done: (\d+) tables, (\d+) relationships"), "Done: {0} tables, {1} joins", None),
)


def parse_progress(raw: str) -> Optional[tuple[re.Match, str, Optional[str]]]:
    """The (match, phrase, unit) of the first progress phrase a log line matches, else None."""
    for pattern, phrase, unit in PROGRESS_PHRASES:
        m = pattern.match(raw)
        if m:
            return m, phrase, unit
    return None


def last_progress_line(log: Path) -> str:
    """The build log's latest non-blank line, or "" when there is none (yet)."""
    try:
        lines = [ln for ln in log.read_text(encoding="utf-8").splitlines() if ln.strip()]
    except OSError:
        return ""
    return lines[-1] if lines else ""


def _timeline(root: Path) -> Path:
    return root / ".introspect" / "timeline.jsonl"


def parse_mark(spec: str) -> tuple[str, str]:
    """`<id>=<status>` as (id, status); ValueError naming what is wrong."""
    step_id, sep, status = spec.partition("=")
    if not sep:
        raise ValueError(f"--mark wants <id>=<status>, got {spec!r}")
    _check(step_id, status)
    return step_id, status


def _check(step_id: str, status: str) -> None:
    if step_id not in STEP_IDS:
        raise ValueError(f"unknown step {step_id!r}; one of {', '.join(STEP_IDS)}")
    if status not in MARK_STATUSES:
        raise ValueError(f"unknown status {status!r}; one of {', '.join(MARK_STATUSES)}")


def mark(root: Path, step_id: str, status: str, now: Optional[datetime] = None) -> None:
    """Append one mark to `<root>/.introspect/timeline.jsonl`. Appended, never rewritten, so two
    surfaces marking at once can't lose each other's line; the last mark per step wins on read."""
    _check(step_id, status)
    at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).replace(microsecond=0)
    path = _timeline(Path(root).expanduser())
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"id": step_id, "status": status, "at": at.isoformat()}) + "\n")


def _last_marks(root: Path) -> dict[str, str]:
    """The last mark per step. A line that doesn't parse (a write cut short) is skipped, not fatal:
    the list is a view, and one bad line shouldn't hide the rest."""
    try:
        text = _timeline(root).read_text(encoding="utf-8")
    except OSError:
        return {}
    marks: dict[str, str] = {}
    for line in text.splitlines():
        try:
            doc = json.loads(line)
        except ValueError:
            continue
        if isinstance(doc, dict) and doc.get("id") in STEP_IDS and doc.get("status") in MARK_STATUSES:
            marks[doc["id"]] = doc["status"]
    return marks


def _totals(root: Path) -> Optional[dict[str, Any]]:
    """The overview totals when the model loads, else None. Any failure to load counts as not
    built: mid-build the tree is half-written, and that must read as "building", not as an error."""
    from . import curate
    try:
        org = L.load_datasource(root, include_rejected=True)
    except Exception:  # noqa: BLE001 — see docstring
        return None
    return curate.overview(org, root)["totals"]


def _build_count(log: Path) -> Optional[str]:
    parsed = parse_progress(last_progress_line(log))
    if parsed and parsed[2] == "tables":
        return f"{parsed[0].group(1)} of {parsed[0].group(2)} tables"
    return None


def _later_counts(totals: dict[str, Any]) -> dict[str, Optional[str]]:
    metrics = totals["metrics"]
    return {
        "build": f"{totals['tables']} tables, {totals['joins']} joins",
        "describe": f"{totals['described_pct']}% described, {metrics} metrics",
        "signoff": f"{metrics - totals['needs_signoff']} of {metrics} signed" if metrics else None,
        "examples": f"{totals['examples']} examples" if totals["examples"] else None,
    }


def onboarding_steps(root: Path) -> dict[str, Any]:
    """The nine steps with status, count and the label text to show, plus the current step."""
    root = Path(root).expanduser()
    status = {i: "pending" for i in STEP_IDS}
    status.update(_last_marks(root))
    counts: dict[str, Optional[str]] = {}

    totals = _totals(root)
    log = root / ".introspect" / "progress.log"
    build_at = STEP_IDS.index("build")
    if totals is not None:
        status["build"] = "done"
        counts = _later_counts(totals)
    elif log.exists():
        status["build"] = "in_progress"
        counts["build"] = _build_count(log)
    elif status["build"] == "done":
        # A mark can't finish the build: only a model that loads does.
        status["build"] = "in_progress"
    if status["build"] != "done":
        for i in STEP_IDS[build_at + 1:]:
            if status[i] in ("in_progress", "done"):
                status[i] = "pending"

    reached = [n for n, i in enumerate(STEP_IDS) if status[i] in ("in_progress", "done")]
    if reached:
        for i in STEP_IDS[:reached[-1]]:
            if status[i] == "pending":
                # A report check is optional: passed over, it was skipped, not done.
                status[i] = "skipped" if i == "reports" else "done"
    running = [i for i in STEP_IDS if status[i] == "in_progress"]
    for i in running[:-1]:
        status[i] = "done"

    result = []
    for i in STEP_IDS:
        count = counts.get(i)
        text = LABELS[i] if count is None else f"{LABELS[i]} — {count}"
        result.append({"id": i, "label": LABELS[i], "status": status[i], "count": count,
                       "text": text})
    current = next((i for i in STEP_IDS if status[i] == "in_progress"),
                   next((i for i in STEP_IDS if status[i] == "pending"), None))
    return {"steps": result, "current": current, "timeline": str(_timeline(root))}
