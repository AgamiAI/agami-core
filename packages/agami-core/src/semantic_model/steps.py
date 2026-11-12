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
from dataclasses import dataclass
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


@dataclass
class _Replay:
    """What the timeline says once replayed: the marks that still stand; whether a restart is in
    effect (a step before the build was marked `in_progress` after the last build mark, or with no
    build mark at all); and when the standing `build=in_progress` mark was written."""
    marks: dict[str, str]
    restarted: bool
    build_started_at: Optional[float]


def _mark_time(doc: dict) -> Optional[float]:
    try:
        return datetime.fromisoformat(doc["at"]).timestamp()
    except (KeyError, TypeError, ValueError):
        return None


def _replay_marks(root: Path) -> _Replay:
    """Replay the marks in order (rule A).

    Marking a step `in_progress` restarts the run from there: every later step's earlier
    `in_progress`/`done` mark is dropped (a `skipped` one is kept), so a reintrospect after a
    finished onboarding doesn't show a finished list. A line that doesn't parse (a write cut short)
    is skipped, not fatal: the list is a view, and one bad line shouldn't hide the rest."""
    try:
        text = _timeline(root).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return _Replay({}, False, None)
    marks: dict[str, str] = {}
    build_at = STEP_IDS.index("build")
    restarted = False
    build_started_at: Optional[float] = None
    for line in text.splitlines():
        try:
            doc = json.loads(line)
        except ValueError:
            continue
        if not (isinstance(doc, dict) and doc.get("id") in STEP_IDS
                and doc.get("status") in MARK_STATUSES):
            continue
        step_id, status = doc["id"], doc["status"]
        at = STEP_IDS.index(step_id)
        if status == "in_progress":
            for later in STEP_IDS[at + 1:]:
                if marks.get(later) in ("in_progress", "done"):
                    del marks[later]
            if at < build_at:
                restarted = True
        if step_id == "build":
            restarted = False
            build_started_at = _mark_time(doc) if status == "in_progress" else None
        elif status == "in_progress" and at < build_at:
            build_started_at = None  # the build's own mark was just dropped
        marks[step_id] = status
    return _Replay(marks, restarted, build_started_at)


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


def _log_state(log: Path, model: Path, now: float,
               attempt_at: Optional[float] = None) -> tuple[str, Optional[str]]:
    """(state, reason) of the build log that speaks for this model: "running", "failed" (with the
    engine's reason), "stopped", or "none" when no log is newer than datasource.yaml, or the log is
    older than this attempt's `build=in_progress` mark (`attempt_at`): a retry's list must not
    carry the last attempt's failure until the engine truncates the log. Marks are stamped to the
    second, so a log written in the mark's own second is this attempt's. A file that vanishes
    between the two stats (being replaced) reads as no log, never as running."""
    try:
        log_at = log.stat().st_mtime
        model_at = model.stat().st_mtime if model.exists() else None
    except OSError:
        return "none", None
    if model_at is not None and log_at <= model_at:
        return "none", None
    if attempt_at is not None and int(log_at) < attempt_at:
        return "none", None
    last = last_progress_line(log)
    if last.startswith("failed:"):
        return "failed", last[len("failed:"):].strip()
    if last.startswith("done:") and model_at is not None:
        # A rebuild that has logged its end and is writing the tree: the marks decide.
        return "none", None
    # Unfinished, or `done:` with no tree yet (the engine logs it before writing; a run killed in
    # between, or a dry run, never writes it): live until it has been silent too long.
    return ("stopped" if now - log_at > STALE_BUILD_SECONDS else "running"), None


def _unfinished(state: str, reason: Optional[str], noun: str) -> str:
    """The problem line for a build that ended without finishing."""
    if state == "failed":
        return f"The {noun} didn't finish: {reason}"
    return f"The {noun} stopped before it finished."


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


def _imply(status: dict[str, str]) -> None:
    """Rule C: a step before the furthest one reached is done (a passed-over report check is
    skipped), and only the furthest step reached can still be in progress."""
    reached = [n for n, i in enumerate(STEP_IDS) if status[i] in ("in_progress", "done")]
    if not reached:
        return
    for i in STEP_IDS[:reached[-1]]:
        if status[i] == "pending":
            status[i] = "skipped" if i == "reports" else "done"
        elif status[i] == "in_progress":
            status[i] = "done"


def _reopen_from(status: dict[str, str], step_id: str, to: str) -> None:
    """`step_id` set to `to`, every step before it finished, every later one back to pending (a
    skip kept)."""
    at = STEP_IDS.index(step_id)
    status[step_id] = to
    for i in STEP_IDS[:at]:
        if status[i] in ("pending", "in_progress"):
            status[i] = "done"
    for i in STEP_IDS[at + 1:]:
        if status[i] in ("in_progress", "done"):
            status[i] = "pending"


def onboarding_steps(root: Path, now: Optional[datetime] = None) -> dict[str, Any]:
    """The nine steps with status, count and the label text to show, the current step, and a
    one-line `problem` when the model on disk or the last build needs the person's attention.

    Marks decide status; disk supplies counts and a few clamps on the build: replay the marks (A),
    let a loadable model finish an unmarked build (B), imply the steps behind the furthest one
    reached (C), clamp the build to what its log and datasource.yaml say (D), then counts (E)."""
    root = Path(root).expanduser()
    build_at = STEP_IDS.index("build")
    model = root / "datasource.yaml"
    log = root / ".introspect" / "progress.log"

    replay = _replay_marks(root)
    marks = replay.marks
    status = {i: marks.get(i, "pending") for i in STEP_IDS}
    totals, load_problem = _totals(root) if model.exists() else (None, None)
    # B: resuming a profile with no marks, or the sample's copied model. Not while a restart is in
    # effect: a reintrospect hasn't reached its build, and the old model mustn't tick it.
    if "build" not in marks and model.exists() and not replay.restarted:
        status["build"] = "done"
    _imply(status)

    attempt_at = replay.build_started_at if marks.get("build") == "in_progress" else None
    state, reason = _log_state(log, model, (now or datetime.now(timezone.utc)).timestamp(),
                               attempt_at)
    problem: Optional[str] = None
    live = False
    noun = "rebuild" if model.exists() else "build"
    if state == "running":
        # D1: a build running now wins over any mark, and a half-written tree is no problem.
        _reopen_from(status, "build", "in_progress")
        live = True
    elif status["build"] == "in_progress" or (status["build"] == "done" and not model.exists()):
        if state == "none":
            # D2/D4: the build was started (or marked done) but no model of this run has landed:
            # an earlier run's model must not finish it.
            _reopen_from(status, "build", "in_progress")
        else:
            # D3/D4: it stopped. The list waits at the build with the reason.
            _reopen_from(status, "build", "pending")
            problem = _unfinished(state, reason, noun)
    elif status["build"] == "done":
        # D7 first: the model's own failure to load says more than a rebuild that didn't land.
        # D5: a built model stays built when a later rebuild didn't finish.
        problem = load_problem or (_unfinished(state, reason, "last rebuild")
                                   if state in ("failed", "stopped") else None)
    # D6: a pending build (an earlier step reopened) ignores the log. A pending or running build
    # has no later step reached: _reopen_from or the restart already reset them. A `skipped` build
    # is left alone, with the steps after it as marked.

    # E: only a step that is done or under way carries a count.
    counts: dict[str, Optional[str]] = {}
    if live:
        counts["build"] = _build_count(log)
    elif status["build"] == "done" and totals is not None:
        counts = _later_counts(totals)

    result = []
    for i in STEP_IDS:
        count = counts.get(i) if status[i] in ("done", "in_progress") else None
        text = LABELS[i] if count is None else f"{LABELS[i]} — {count}"
        result.append({"id": i, "label": LABELS[i], "status": status[i], "count": count,
                       "text": text})
    current = next((i for i in STEP_IDS if status[i] == "in_progress"),
                   next((i for i in STEP_IDS if status[i] == "pending"), None))
    return {"steps": result, "current": current, "timeline": str(_timeline(root)),
            "problem": problem}
