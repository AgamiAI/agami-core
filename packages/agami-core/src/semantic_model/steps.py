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

# An unfinished build log this long untouched is taken for a build that died without a word (a
# killed process writes no `failed:` line), so the list doesn't read "building" forever.
STALE_BUILD_SECONDS = 1800

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
        text = log.read_text(encoding="utf-8", errors="replace")
        lines = [ln for ln in text.splitlines() if ln.strip()]
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


def _replay_marks(root: Path) -> tuple[dict[str, str], bool]:
    """The marks that still stand, replayed in order, and whether the build was reopened.

    Marking a step `in_progress` restarts the run from there: every later step's earlier
    `in_progress`/`done` mark is dropped (a `skipped` one is kept), so a reintrospect after a
    finished onboarding doesn't show a finished list. The build is reopened when the latest restart
    is before it and it hasn't been marked since: a model on disk from the earlier run then no
    longer finishes it. A line that doesn't parse (a write cut short) is skipped, not fatal: the
    list is a view, and one bad line shouldn't hide the rest."""
    try:
        text = _timeline(root).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}, False
    marks: dict[str, str] = {}
    build_at = STEP_IDS.index("build")
    restart: Optional[int] = None
    since_restart: set[str] = set()
    for line in text.splitlines():
        try:
            doc = json.loads(line)
        except ValueError:
            continue
        if not (isinstance(doc, dict) and doc.get("id") in STEP_IDS
                and doc.get("status") in MARK_STATUSES):
            continue
        step_id, status = doc["id"], doc["status"]
        if status == "in_progress":
            restart = STEP_IDS.index(step_id)
            since_restart = set()
            for later in STEP_IDS[restart + 1:]:
                if marks.get(later) in ("in_progress", "done"):
                    del marks[later]
        marks[step_id] = status
        since_restart.add(step_id)
    reopened = restart is not None and restart < build_at and "build" not in since_restart
    return marks, reopened


def _totals(root: Path) -> tuple[Optional[dict[str, Any]], Optional[str]]:
    """(overview totals, None) when the model loads, else (None, a one-line problem). The catch is
    broad on purpose: a built model that stops loading (a hand edit, a missing area, a loader bug)
    must still read as built, with the reason shown, rather than crash the list or send it back to
    "building"."""
    from . import curate
    try:
        org = L.load_datasource(root, include_rejected=True)
        return curate.overview(org, root)["totals"], None
    except Exception as exc:  # noqa: BLE001 — see docstring
        first = (str(exc).strip().splitlines() or [type(exc).__name__])[0]
        return None, f"The model in {root} couldn't be read: {first}"


def _build_state(log: Path, model: Path, now: float) -> tuple[Optional[str], Optional[str]]:
    """(state, reason) of the build log that speaks for this model: "running", "failed" (with the
    engine's reason), "stale" or "done"; (None, None) when no log is newer than the model. A file
    that vanishes between the two stats (being replaced) reads as no log, never as running."""
    try:
        log_at = log.stat().st_mtime
        model_at = model.stat().st_mtime if model.exists() else None
    except OSError:
        return None, None
    if model_at is not None and log_at <= model_at:
        return None, None
    last = last_progress_line(log)
    stale = now - log_at > STALE_BUILD_SECONDS
    if last.startswith("done:"):
        # The engine logs `done:` before it writes the tree; a run killed in between (or a dry
        # run) never writes it, so a finished log with no model goes stale like any other.
        return ("stale" if model_at is None and stale else "done"), None
    if last.startswith("failed:"):
        return "failed", last[len("failed:"):].strip()
    if stale:
        return "stale", None
    return "running", None


def _unfinished(state: Optional[str], reason: Optional[str], rebuild: bool) -> Optional[str]:
    """The problem line for a build that ended without finishing, else None."""
    if state == "failed":
        return f"The {'last rebuild' if rebuild else 'build'} didn't finish: {reason}"
    if state == "stale":
        return f"The {'last rebuild' if rebuild else 'build'} stopped before it finished."
    return None


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


def onboarding_steps(root: Path, now: Optional[datetime] = None) -> dict[str, Any]:
    """The nine steps with status, count and the label text to show, the current step, and a
    one-line `problem` when the model on disk or the last build needs the person's attention."""
    root = Path(root).expanduser()
    status = {i: "pending" for i in STEP_IDS}
    marks, reopened = _replay_marks(root)
    status.update(marks)
    counts: dict[str, Optional[str]] = {}
    problem: Optional[str] = None

    log = root / ".introspect" / "progress.log"
    build_at = STEP_IDS.index("build")
    model = root / "datasource.yaml"
    state, reason = _build_state(log, model, (now or datetime.now(timezone.utc)).timestamp())
    if state == "running":
        # The live count, not the numbers of a model this build will replace; and a half-written
        # tree is not a problem to report.
        status["build"] = "in_progress"
        counts["build"] = _build_count(log)
    elif model.exists() and not reopened:
        # A model on disk is built even when it fails to load: a hand edit or a missing area later
        # must show as a problem on a finished list, not send the list back to "building".
        status["build"] = "done"
        totals, problem = _totals(root)
        if totals is not None:
            counts = _later_counts(totals)
        # The model's own failure to load says more than a rebuild that didn't land.
        problem = problem or _unfinished(state, reason, rebuild=True)
    elif state in ("failed", "stale"):
        # The build was reached, so the steps before it are done; the list stops at the build.
        status["build"] = "pending"
        problem = _unfinished(state, reason, rebuild=model.exists())
        for i in STEP_IDS[:build_at]:
            if status[i] in ("pending", "in_progress"):
                status[i] = "done"
    elif reopened and model.exists():
        # The earlier run's model: this run hasn't reached the build yet, and its numbers aren't
        # this run's.
        status["build"] = "pending"
    elif log.exists():
        # The engine logs `done:` before the tree is written, so the build runs on until it lands.
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
            elif status[i] == "in_progress":
                # Only the furthest step reached can still be running.
                status[i] = "done"

    result = []
    for i in STEP_IDS:
        count = counts.get(i)
        text = LABELS[i] if count is None else f"{LABELS[i]} — {count}"
        result.append({"id": i, "label": LABELS[i], "status": status[i], "count": count,
                       "text": text})
    current = next((i for i in STEP_IDS if status[i] == "in_progress"),
                   next((i for i in STEP_IDS if status[i] == "pending"), None))
    return {"steps": result, "current": current, "timeline": str(_timeline(root)),
            "problem": problem}
