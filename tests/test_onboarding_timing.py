"""`sm timing`: how long onboarding took, from the step marks `sm steps --mark` writes, so the
summary's "Set up in …" line is measured, never estimated. Only the current run counts, the time
between steps drops out, and a run with a long break in it shows no line."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

pytest.importorskip("pydantic")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "packages" / "agami-core" / "src"))

from semantic_model import cli, steps, timing  # noqa: E402


def _write(root: Path, marks: list[tuple], day: int = 12) -> None:
    """Marks as (id, status, "HH:MM:SS") or (id, status, "HH:MM:SS", day), in UTC."""
    path = root / ".introspect" / "timeline.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        for i, s, t, *d in marks:
            fh.write(json.dumps({"id": i, "status": s,
                                 "at": f"2026-11-{d[0] if d else day:02d}T{t}+00:00"}) + "\n")


def _seconds(result: dict) -> dict:
    return {s["id"]: s["seconds"] for s in result["steps"]}


FIXED = [
    ("connect", "in_progress", "10:00:00"),
    ("connect", "done", "10:00:30"),
    ("build", "in_progress", "10:01:00"),
    ("build", "done", "10:03:10"),
    ("first_question", "done", "10:06:40"),
]

FULL_RUN = [(i, s, f"10:{n:02d}:{30 if s == 'done' else 0:02d}")
            for n, i in enumerate(steps.STEP_IDS) for s in ("in_progress", "done")]


def test_fixed_timeline(tmp_path):
    _write(tmp_path, FIXED)
    result = timing.onboarding_timing(tmp_path)
    secs = _seconds(result)
    assert secs["connect"] == 30
    assert secs["build"] == 130
    assert secs["first_question"] == 0  # no in_progress mark
    assert secs["describe"] == 0
    assert [s["id"] for s in result["steps"]] == list(steps.STEP_IDS)
    assert [s["label"] for s in result["steps"]] == [steps.LABELS[i] for i in steps.STEP_IDS]
    # The sum of the steps: the gaps between them (10:00:30-10:01:00, and after the build) drop out.
    assert result["total_seconds"] == 160
    assert result["text"] == "Set up in 2 min 40 s"
    assert result["paused"] is False
    assert result["started_at"] == "2026-11-12T10:00:00+00:00"
    assert result["finished_at"] == "2026-11-12T10:06:40+00:00"


@pytest.mark.parametrize("seconds, text", [
    (45, "Set up in 45 s"),
    (400, "Set up in 6 min 40 s"),
    (180, "Set up in 3 min"),
    (3900, "Set up in 1 h 5 min"),
    (3930, "Set up in 1 h 5 min"),
    (7200, "Set up in 2 h"),
    (0, ""),
])
def test_duration_text(seconds, text):
    assert timing.duration_text(seconds) == text


def test_under_a_minute(tmp_path):
    _write(tmp_path, [("connect", "in_progress", "10:00:00"), ("connect", "done", "10:00:45")])
    assert timing.onboarding_timing(tmp_path)["text"] == "Set up in 45 s"


def test_a_70_minute_run(tmp_path):
    _write(tmp_path, [("connect", "in_progress", "10:00:00"), ("connect", "done", "10:30:00"),
                      ("build", "in_progress", "10:30:00"), ("build", "done", "11:10:00")])
    result = timing.onboarding_timing(tmp_path)
    assert result["total_seconds"] == 4200
    assert result["paused"] is False
    assert result["text"] == "Set up in 1 h 10 min"


def test_restart_counts_the_last_attempt(tmp_path):
    _write(tmp_path, [
        ("connect", "in_progress", "10:00:00"),
        ("connect", "done", "10:00:30"),
        ("build", "in_progress", "10:01:00"),
        ("build", "in_progress", "10:01:30"),  # re-marked mid-attempt: the first still counts
        ("connect", "in_progress", "10:10:00"),  # restart: a new run from here
        ("connect", "done", "10:10:20"),
        ("build", "in_progress", "10:11:00"),
        ("build", "done", "10:13:00"),
    ])
    result = timing.onboarding_timing(tmp_path)
    secs = _seconds(result)
    assert secs["build"] == 120
    assert secs["connect"] == 20
    assert result["total_seconds"] == 140
    assert result["text"] == "Set up in 2 min 20 s"


def test_a_finished_step_started_again_is_a_new_attempt(tmp_path):
    _write(tmp_path, [("build", "in_progress", "10:00:00"), ("build", "done", "10:01:00"),
                      ("build", "in_progress", "10:05:00"), ("build", "done", "10:05:30")])
    assert _seconds(timing.onboarding_timing(tmp_path))["build"] == 30


@pytest.mark.parametrize("restart_at", ["context", "build"])
def test_a_reload_two_days_later_counts_only_the_reload(tmp_path, restart_at):
    _write(tmp_path, FULL_RUN, day=1)
    later = steps.STEP_IDS[steps.STEP_IDS.index(restart_at):]
    _write(tmp_path, [(i, s, f"09:{n:02d}:{20 if s == 'done' else 0:02d}")
                      for n, i in enumerate(later) for s in ("in_progress", "done")], day=3)
    result = timing.onboarding_timing(tmp_path)
    assert [s["id"] for s in result["steps"]] == list(later)  # steps before the reload not listed
    assert set(_seconds(result).values()) == {20}
    assert result["total_seconds"] == 20 * len(later)
    assert result["paused"] is False
    assert result["started_at"] == "2026-11-03T09:00:00+00:00"
    assert result["text"] == timing.duration_text(20 * len(later))


def test_a_run_resumed_next_morning_is_paused(tmp_path):
    _write(tmp_path, [("connect", "in_progress", "17:00:00", 12), ("connect", "done", "17:01:00", 12),
                      ("build", "in_progress", "09:00:00", 13), ("build", "done", "09:02:00", 13)])
    result = timing.onboarding_timing(tmp_path)
    assert result["paused"] is True
    assert result["text"] == ""
    assert result["total_seconds"] == 180
    assert _seconds(result)["build"] == 120


def test_a_gap_just_under_two_hours_is_not_a_pause(tmp_path):
    _write(tmp_path, [("connect", "in_progress", "10:00:00"), ("connect", "done", "10:01:00"),
                      ("build", "in_progress", "12:01:00"), ("build", "done", "12:02:00")])
    result = timing.onboarding_timing(tmp_path)
    assert result["paused"] is False
    assert result["text"] == "Set up in 2 min"


@pytest.mark.parametrize("done_at", ["10:00:00", "09:59:00"])
def test_identical_or_backward_timestamps_show_nothing(tmp_path, done_at):
    _write(tmp_path, [("connect", "in_progress", "10:00:00"), ("connect", "done", done_at)])
    result = timing.onboarding_timing(tmp_path)
    assert result["total_seconds"] == 0
    assert result["text"] == ""


def test_no_timeline(tmp_path):
    result = timing.onboarding_timing(tmp_path / "missing")
    assert result["total_seconds"] == 0
    assert set(_seconds(result).values()) == {0}
    assert result["text"] == ""
    assert result["paused"] is False
    assert result["started_at"] is None and result["finished_at"] is None


def test_started_but_nothing_done_is_untimed(tmp_path):
    _write(tmp_path, [("connect", "in_progress", "10:00:00")])
    result = timing.onboarding_timing(tmp_path)
    assert result["total_seconds"] == 0
    assert result["text"] == ""


def test_garbage_lines_ignored(tmp_path):
    _write(tmp_path, FIXED)
    path = tmp_path / ".introspect" / "timeline.jsonl"
    path.write_text(path.read_text() + "not json\n[1, 2]\n"
                    + json.dumps({"id": "nope", "status": "done", "at": "2026-11-12T11:00:00+00:00"})
                    + "\n" + json.dumps({"id": "build", "status": "done", "at": "garbage"}) + "\n"
                    + '{"id": "reports", "status": "done"\n', encoding="utf-8")
    result = timing.onboarding_timing(tmp_path)
    assert result["total_seconds"] == 160
    assert _seconds(result)["build"] == 130


def test_cli_prints_the_same_dict(tmp_path, capsys):
    _write(tmp_path, FIXED)
    assert cli.main(["timing", str(tmp_path)]) == 0
    assert json.loads(capsys.readouterr().out) == timing.onboarding_timing(tmp_path)


def test_cli_with_no_timeline(tmp_path, capsys):
    assert cli.main(["timing", str(tmp_path)]) == 0
    assert json.loads(capsys.readouterr().out)["text"] == ""
