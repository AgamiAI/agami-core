"""`sm timing`: how long onboarding took, from the step marks `sm steps --mark` writes, so the
summary's "Set up in …" line is measured, never estimated."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

pytest.importorskip("pydantic")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "packages" / "agami-core" / "src"))

from semantic_model import cli, steps, timing  # noqa: E402


def _write(root: Path, marks: list[tuple[str, str, str]]) -> None:
    """Marks as (id, status, "HH:MM:SS") on one fixed day, in UTC."""
    path = root / ".introspect" / "timeline.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(
        json.dumps({"id": i, "status": s, "at": f"2026-11-12T{t}+00:00"}) + "\n"
        for i, s, t in marks), encoding="utf-8")


def _seconds(result: dict) -> dict:
    return {s["id"]: s["seconds"] for s in result["steps"]}


FIXED = [
    ("connect", "in_progress", "10:00:00"),
    ("connect", "done", "10:00:30"),
    ("build", "in_progress", "10:01:00"),
    ("build", "done", "10:03:10"),
    ("first_question", "done", "10:06:40"),
]


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
    assert result["total_seconds"] == 400
    assert result["text"] == "Set up in 6 min 40 s"
    assert result["started_at"] == "2026-11-12T10:00:00+00:00"
    assert result["finished_at"] == "2026-11-12T10:06:40+00:00"


def test_under_a_minute(tmp_path):
    _write(tmp_path, [("connect", "in_progress", "10:00:00"), ("connect", "done", "10:00:45")])
    assert timing.onboarding_timing(tmp_path)["text"] == "Set up in 45 s"


def test_exact_minutes(tmp_path):
    _write(tmp_path, [("connect", "in_progress", "10:00:00"), ("build", "done", "10:03:00")])
    result = timing.onboarding_timing(tmp_path)
    assert result["total_seconds"] == 180
    assert result["text"] == "Set up in 3 min"


def test_restart_counts_the_last_attempt(tmp_path):
    _write(tmp_path, [
        ("connect", "in_progress", "10:00:00"),
        ("connect", "done", "10:00:30"),
        ("build", "in_progress", "10:01:00"),
        ("build", "in_progress", "10:01:30"),  # re-marked mid-attempt: the first still counts
        ("connect", "in_progress", "10:10:00"),  # restart: drops the build's attempt
        ("connect", "done", "10:10:20"),
        ("build", "in_progress", "10:11:00"),
        ("build", "done", "10:13:00"),
    ])
    result = timing.onboarding_timing(tmp_path)
    secs = _seconds(result)
    assert secs["build"] == 120
    assert secs["connect"] == 20  # a finished step started again is a new attempt
    assert result["total_seconds"] == 180
    assert result["text"] == "Set up in 3 min"


def test_no_timeline(tmp_path):
    result = timing.onboarding_timing(tmp_path / "missing")
    assert result["total_seconds"] == 0
    assert set(_seconds(result).values()) == {0}
    assert result["text"] == ""
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
    assert result["total_seconds"] == 400
    assert _seconds(result)["build"] == 130


def test_cli_prints_the_same_dict(tmp_path, capsys):
    _write(tmp_path, FIXED)
    assert cli.main(["timing", str(tmp_path)]) == 0
    assert json.loads(capsys.readouterr().out) == timing.onboarding_timing(tmp_path)


def test_cli_with_no_timeline(tmp_path, capsys):
    assert cli.main(["timing", str(tmp_path)]) == 0
    assert json.loads(capsys.readouterr().out)["text"] == ""
