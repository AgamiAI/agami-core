"""On a resume, `sm steps` tells whether the build already wrote this run's model, so the skill no
longer compares file times by hand before deciding to build again.

The signal is given only by plain `sm steps "<root>"`, only while the build step is in progress by
its mark, no build log is running, and `datasource.yaml` was written at or after the surviving
`build=in_progress` mark. Every other case prints what it printed before.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from skill_docs import PLUGIN

pytest.importorskip("pydantic")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "packages" / "agami-core" / "src"))

from semantic_model import cli, steps  # noqa: E402

SAMPLE = PLUGIN / "samples" / "store" / "model"
WRITTEN = "The model is already written: go to the join cards without building again."
UPDATE = "Update the task list from `list` now, before anything else."
UP_TO_DATE = "The task list is up to date."


def _run(argv: list[str], capsys) -> dict:
    assert cli.main(argv) == 0
    return json.loads(capsys.readouterr().out)


def _started(root: Path, at: datetime) -> None:
    for step in ("connect", "context", "tables"):
        steps.mark(root, step, "done", now=at - timedelta(minutes=1))
    steps.mark(root, "build", "in_progress", now=at)


def _model_written(root: Path, at: datetime) -> None:
    shutil.copytree(SAMPLE, root, dirs_exist_ok=True)
    os.utime(root / "datasource.yaml", (at.timestamp(), at.timestamp()))


def _build(out: dict) -> str:
    return next(line for line in out["list"] if "Read the tables and joins" in line)


NOW = datetime.now(timezone.utc).replace(microsecond=0)


def test_a_model_written_after_the_mark_says_go_to_the_join_cards(tmp_path, capsys):
    root = tmp_path / "acme"
    _model_written(root, NOW - timedelta(minutes=2))
    _started(root, NOW - timedelta(minutes=5))
    out = _run(["steps", str(root)], capsys)
    assert out["then"] == WRITTEN + " Update the task list from `list`."
    assert _build(out) == "▶ Read the tables and joins — 11 tables, 12 joins"
    assert out["problem"] is None
    again = _run(["steps", str(root)], capsys)
    assert again["then"] == WRITTEN  # the list didn't change


def test_a_model_written_in_the_marks_own_second_counts(tmp_path, capsys):
    root = tmp_path / "acme"
    _model_written(root, NOW - timedelta(minutes=5) + timedelta(milliseconds=400))
    _started(root, NOW - timedelta(minutes=5))
    assert _run(["steps", str(root)], capsys)["then"].startswith(WRITTEN)


def test_a_build_long_finished_is_not_read_as_stopped(tmp_path, capsys):
    # Marked two hours ago, written an hour ago, no log: it finished, it did not die.
    root = tmp_path / "acme"
    _model_written(root, NOW - timedelta(hours=1))
    _started(root, NOW - timedelta(hours=2))
    out = _run(["steps", str(root)], capsys)
    assert out["then"].startswith(WRITTEN) and out["problem"] is None


def test_a_stale_mark_over_an_older_model_does_not_say_it(tmp_path, capsys):
    root = tmp_path / "acme"
    _model_written(root, NOW - timedelta(hours=3))
    _started(root, NOW - timedelta(minutes=5))
    out = _run(["steps", str(root)], capsys)
    assert out["then"] == UPDATE
    assert _build(out) == "▶ Read the tables and joins"


def test_a_fresh_first_build_does_not_say_it(tmp_path, capsys):
    root = tmp_path / "acme"
    _started(root, NOW - timedelta(minutes=1))
    out = _run(["steps", str(root)], capsys)
    assert out["then"] == UPDATE and _build(out) == "▶ Read the tables and joins"


def test_a_reload_does_not_say_it(tmp_path, capsys):
    # A finished onboarding reloaded: the earlier steps restart, then the build is marked again,
    # after the old model was written.
    root = tmp_path / "acme"
    _model_written(root, NOW - timedelta(days=2))
    for step in steps.STEP_IDS:
        steps.mark(root, step, "done", now=NOW - timedelta(days=2))
    steps.mark(root, "context", "in_progress", now=NOW - timedelta(minutes=3))
    steps.mark(root, "tables", "done", now=NOW - timedelta(minutes=2))
    steps.mark(root, "build", "in_progress", now=NOW - timedelta(minutes=1))
    out = _run(["steps", str(root)], capsys)
    assert not out["then"].startswith(WRITTEN)
    assert _build(out) == "▶ Read the tables and joins"


def test_a_running_build_does_not_say_it(tmp_path, capsys):
    root = tmp_path / "acme"
    _model_written(root, NOW - timedelta(minutes=2))
    _started(root, NOW - timedelta(minutes=5))
    log = root / ".introspect" / "progress.log"
    log.write_text("discovered 11 tables — reading columns, grain, keys\ncolumns+grain 5/11\n")
    out = _run(["steps", str(root)], capsys)
    assert not out["then"].startswith(WRITTEN)
    assert _build(out) == "▶ Read the tables and joins — 5 of 11 tables"


def test_only_plain_steps_says_it(tmp_path, capsys):
    root = tmp_path / "acme"
    _model_written(root, NOW - timedelta(minutes=2))
    _started(root, NOW - timedelta(minutes=5))
    out = _run(["steps", str(root), "--mark", "reports=skipped"], capsys)
    assert not out["then"].startswith(WRITTEN)


def test_a_model_that_does_not_load_does_not_say_it(tmp_path, capsys):
    root = tmp_path / "acme"
    _model_written(root, NOW - timedelta(minutes=2))
    shutil.rmtree(root / "subject_areas")
    os.utime(root / "datasource.yaml", ((NOW - timedelta(minutes=2)).timestamp(),) * 2)
    _started(root, NOW - timedelta(minutes=5))
    out = _run(["steps", str(root)], capsys)
    assert not out["then"].startswith(WRITTEN)
