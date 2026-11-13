"""The onboarding step list, with each step's status and count read from disk.

agami-connect shows its steps as a task list. When the model re-typed them from prose, the list
drifted between turns and was lost when a turn ended; `sm steps` prints it from the engine instead,
so every surface draws the same list. Status comes from the marks a skill appends to
`<root>/.introspect/timeline.jsonl` and from what is on disk (a model that loads, a build's progress
log); every count comes from the engine's own numbers, never an estimate.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from . import loader as L
from .say_text import quoted

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

# The longest failure reason a problem line or progress line carries. Longer than the 80 of other
# model text: the reason can't be told apart from the engine's own hints ("no tables discovered. If
# the catalog is locked down, …"), and a hint cut at 80 loses the advice it exists for.
FAILURE_CAP = 200

# Each progress-log line, in the person's words: (pattern, phrase, unit). The engine's own wording
# (`columns+grain 30/52`) is for the engine; a person reads "Reading columns … 30/52 tables".
PROGRESS_PHRASES = (
    (re.compile(r"^discovered (\d+) tables"), "Found {0} tables", None),
    (re.compile(r"^columns\+grain (\d+)/(\d+)"), "Reading columns", "tables"),
    (re.compile(r"^relationships: declared FK (\d+)/(\d+)"), "Checking joins", "keys"),
    (re.compile(r"^building relationships"), "Working out the joins", None),
    # The scan's count has no total to show against: which pairs are tested hangs on what earlier ones found.
    (re.compile(r"^join candidates: (\d+) probed"), "Testing possible joins: {0} probed", None),
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
    """The build log's latest non-blank line, or "" when there is none (yet), or when the log is a
    link out of its model folder (`<root>/.introspect/progress.log`): `sm progress` prints the line."""
    if not L.inside_model(Path(log).parent.parent, log):
        return ""
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
    root = Path(root).expanduser()
    path = _timeline(root)
    if not L.inside_model(root, path):
        raise ValueError(f"{path} leads out of the model folder {root}; no mark was written")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"id": step_id, "status": status, "at": at.isoformat()}) + "\n")


@dataclass
class Run:
    """The current run: the marks from the last restart onward (from the first mark when there was
    none). `first_step` is the step it restarted at; `spans` holds each step's standing attempt as
    (first `in_progress`, last `done`) epoch seconds, either None when missing; `times` is every
    timed mark of the run in file order."""
    first_step: str
    spans: dict[str, tuple[Optional[float], Optional[float]]] = field(default_factory=dict)
    times: list[float] = field(default_factory=list)


@dataclass
class _Replay:
    """What the timeline says once replayed: the marks that still stand; whether a restart is in
    effect (a step before the build was marked `in_progress` after the last build mark, or with no
    build mark at all); when the standing `build=in_progress` mark was written; and the current
    run's marks, for `sm timing`."""
    marks: dict[str, str]
    restarted: bool
    build_started_at: Optional[float]
    run: Run = field(default_factory=lambda: Run(STEP_IDS[0]))


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
    is skipped, not fatal: the list is a view, and one bad line shouldn't hide the rest.

    A mark that drops a later step's mark also starts a new run for `sm timing`: the times before
    it, earlier steps' included, belong to an earlier run (a reload days later must not count the
    days between). A finished step marked `in_progress` again starts a new attempt of that step."""
    try:
        if not L.inside_model(root, _timeline(root)):
            raise OSError("timeline leads out of the model folder")
        text = _timeline(root).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return _Replay({}, False, None)
    marks: dict[str, str] = {}
    run = Run(STEP_IDS[0])
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
        when = _mark_time(doc)
        if status == "in_progress":
            reopened = [later for later in STEP_IDS[at + 1:]
                        if marks.get(later) in ("in_progress", "done")]
            for later in reopened:
                del marks[later]
            if reopened:
                run = Run(step_id)
            if at < build_at:
                restarted = True
            if marks.get(step_id) == "done":
                run.spans.pop(step_id, None)
        _time_mark(run, step_id, status, when)
        if step_id == "build" and status == "in_progress":
            # Read only while this mark still stands (see onboarding_steps), so no reset is needed.
            build_started_at = when
        marks[step_id] = status
    return _Replay(marks, restarted, build_started_at, run)


def _time_mark(run: Run, step_id: str, status: str, when: Optional[float]) -> None:
    """Record one mark's time on the run: a step's first `in_progress`, its latest `done`."""
    if when is None:
        return
    run.times.append(when)
    start, end = run.spans.get(step_id, (None, None))
    if status == "in_progress" and start is None:
        run.spans[step_id] = (when, end)
    elif status == "done":
        run.spans[step_id] = (start, when)


def current_run(root: Path) -> Run:
    """The marks of the current run, replayed as `sm steps` replays them."""
    return _replay_marks(Path(root).expanduser()).run


def _totals(root: Path) -> tuple[Optional[dict[str, Any]], Optional[str]]:
    """(overview totals, None) when the model loads, else (None, a one-line problem). The catch is
    broad on purpose: a built model that stops loading (a hand edit, a missing area, a loader bug)
    must still read as built, with the reason shown, rather than crash the list or send it back to
    "building"."""
    from . import curate
    try:
        org = L.load_datasource(root, include_rejected=True)
        return {**curate.overview(org, root)["totals"],
                "signoff": curate.model_signoff_counts(root)}, None
    except Exception as exc:  # noqa: BLE001 — see docstring
        first = _load_error(exc)
        # Posted as a `say`: the engine's words first, the folder by name (no absolute path), and
        # the loader's message, which can quote the model's own files, capped and in quotes.
        return None, f"The model in {quoted(root.name)} couldn't be read: {quoted(first)}"


def _load_error(exc: Exception) -> str:
    """The first line of a load error. An OSError's own text starts with the absolute path of the
    file it couldn't open (the home or temp folder included), so it is said as its reason and the
    file's name alone."""
    if isinstance(exc, OSError):
        reason = exc.strerror or type(exc).__name__
        return f"{reason}: '{Path(str(exc.filename)).name}'" if exc.filename else reason
    return (str(exc).strip().splitlines() or [type(exc).__name__])[0]


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


def _own_log(log: Path, attempt_at: float) -> bool:
    """Whether the build log was written during this attempt (same second-stamp rule as _log_state)."""
    try:
        return int(log.stat().st_mtime) >= attempt_at
    except OSError:
        return False


def _unfinished(state: str, reason: Optional[str], noun: str) -> str:
    """The problem line for a build that ended without finishing."""
    if state == "failed":
        # the reason is the build's error text, which can quote the database or the model
        return f"The {noun} didn't finish: {quoted(reason or '', FAILURE_CAP)}"
    return f"The {noun} stopped before it finished."


def _build_count(log: Path) -> Optional[str]:
    parsed = parse_progress(last_progress_line(log))
    if parsed and parsed[2] == "tables":
        return f"{parsed[0].group(1)} of {parsed[0].group(2)} tables"
    return None


def _later_counts(totals: dict[str, Any]) -> dict[str, Optional[str]]:
    # The sign-off card's own total (metrics, entities and joins), so a run that signs nothing
    # still reads "0 of 23 signed", and the step never counts metrics alone while the card says 23.
    signoff = totals["signoff"]
    to_sign = signoff["waiting"] + signoff["signed"]
    return {
        "build": f"{totals['tables']} tables, {totals['joins']} joins",
        "describe": f"{totals['described_pct']}% described, {totals['metrics']} metrics",
        "signoff": f"{signoff['signed']} of {to_sign} signed" if to_sign else None,
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
    now_at = (now or datetime.now(timezone.utc)).timestamp()
    state, reason = _log_state(log, model, now_at, attempt_at)
    if state == "none" and attempt_at is not None and now_at - attempt_at > STALE_BUILD_SECONDS \
            and not _own_log(log, attempt_at):
        # A mark left by a session that ended before the build wrote a line: nothing is building.
        state = "stopped"
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


# The mark in front of each line of the compact list. A `skipped` step has none: it isn't listed.
LIST_MARKS = {"done": "✓ ", "in_progress": "▶ ", "pending": "· "}


def compact(result: dict[str, Any]) -> dict[str, Any]:
    """`onboarding_steps`' result as the list the skill copies into its task list, one string per
    step that isn't skipped, in order (`✓ ` done, `▶ ` in progress, `· ` pending, then the step's
    text), with `current` and `problem` unchanged. Short on purpose: a live run's full output was
    long enough that the skill filtered it with grep and sed, and never had the whole list to copy."""
    return {"list": [LIST_MARKS[s["status"]] + s["text"] for s in result["steps"]
                     if s["status"] in LIST_MARKS],
            "current": result["current"], "problem": result["problem"]}


# --- what the skill does next ------------------------------------------------------------------
# A live run refreshed its task list after only about half of the `sm steps` / `sm progress` calls,
# and computed the closing summary without posting it: an instruction in the skill text is read long
# before the moment it applies. So the engine's own output carries, at that moment, the text to post
# (`say`) and the next action (`then`). Every `then` the engine prints is one of these.

THEN_UPDATE = "Update the task list from `list` now, before anything else."
THEN_UP_TO_DATE = "The task list is up to date."
THEN_CLOSING = ("Post `say` exactly as written, then update the task list from `list`, then ask the "
                "closing card in that same message.")
THEN_PROBLEM = "Say this to the person before going on."
THEN_PROBLEM_AND_UPDATE = ("Say this to the person before going on. Then update the task list from "
                           "`list`.")
THEN_NO_SUMMARY = ("Say this to the person. The summary could not be built, so do not ask the "
                   "closing card; fix the problem and run this command again.")
THEN_LINE_AND_LIST = "Post `line` and update the task list from `list`."
THEN_LINE = "Post `line`."
THEN_NOTHING = "Nothing new: call this again."
THEN_ENDED = "The build has ended: run `sm steps` and go on."
THEN_NOT_BUILT = ("The model is not built yet, so there is no summary: do not ask the closing "
                  "card.")
THEN_AND_UPDATE = " Update the task list from `list`."


def _last_path(root: Path) -> Path:
    return root / ".introspect" / "steps.last"


def read_printed(root: Path) -> dict[str, Any]:
    """What `sm steps` / `sm progress` last printed for this root: `list`, `line` and `problem` (the
    last problem said). {} when nothing was, or the file can't be read: then everything reads as
    new, which only costs one extra refresh."""
    root = Path(root).expanduser()
    path = _last_path(root)
    try:
        if not L.inside_model(root, path):
            return {}
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return doc if isinstance(doc, dict) else {}


def remember_printed(root: Path, before: dict[str, Any], printed: dict[str, Any]) -> None:
    """Record what this call printed (`printed`: any of `list`, `line`, `problem`) over `before`
    (what `read_printed` returned) in
    `<root>/.introspect/steps.last`. Written atomically, so a reader never sees half a file, and
    only when the model folder exists and the file stays inside it: a fresh profile gets no folder
    made for it, and a link out of the folder is never written through."""
    root = Path(root).expanduser()
    path = _last_path(root)
    if not root.is_dir():
        return
    try:
        path.parent.mkdir(exist_ok=True)
        if not L.inside_model(root, path):
            return
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".steps.last.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump({**before, **printed}, fh, ensure_ascii=False)
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    except OSError:
        pass  # a list that can't be remembered reads as changed next time: harmless


def steps_next(changed: bool, problem: Optional[str], said_before: Optional[str],
               closing: bool, summary: Optional[str], built: bool = True) -> dict[str, Any]:
    """`say` and `then` for `sm steps`.

    A problem is said once: the same problem already said (`said_before`) is left out of `say`, but
    still in `problem`. It never hides the refresh or the closing card: with a changed list, `then`
    asks for both; on the closing mark the summary follows it in the same `say`. When the closing
    summary can't be built, the person hears the problem and no closing card is asked; when that is
    because the model isn't built (none on disk, or a build running), `then` says so, since nothing
    else would tell why the summary is missing."""
    fresh = problem if problem and problem != said_before else None
    if closing and summary is not None:
        return {"say": f"{fresh}\n\n{summary}" if fresh else summary, "then": THEN_CLOSING}
    if closing and problem:
        return {"say": problem, "then": THEN_NO_SUMMARY}
    if closing and not built:
        return {"then": THEN_NOT_BUILT + (THEN_AND_UPDATE if changed else "")}
    if fresh:
        return {"say": fresh, "then": THEN_PROBLEM_AND_UPDATE if changed else THEN_PROBLEM}
    return {"then": THEN_UPDATE if changed else THEN_UP_TO_DATE}


def progress_next(line_new: bool, list_changed: bool, ended: bool) -> str:
    """`then` for `sm progress`. A finished or failed log never moves on, so "call this again"
    would loop for ever: the skill is sent to `sm steps` instead."""
    if line_new and list_changed:
        return THEN_LINE_AND_LIST
    if line_new:
        return THEN_LINE
    if list_changed:
        return THEN_UPDATE
    return THEN_ENDED if ended else THEN_NOTHING
