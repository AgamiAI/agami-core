"""The onboarding step list comes from the engine: `sm steps` prints the nine steps with a status
and a count read from disk, so every surface draws the same list and a turn that ends loses nothing.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

import pytest
import yaml

from skill_docs import PLUGIN, SKILLS_DIR

pytest.importorskip("pydantic")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "packages" / "agami-core" / "src"))

from semantic_model import cli, curate, steps  # noqa: E402
from semantic_model import loader as L  # noqa: E402

SAMPLE = PLUGIN / "samples" / "store" / "model"
LABELS = (
    "Connect your database",
    "Tell agami about your data",
    "Pick the tables",
    "Read the tables and joins",
    "Describe columns and add metrics",
    "Sign off definitions and hide personal data",
    "Write and check example questions",
    "Check your reports",
    "Summary and first question",
)


@pytest.fixture
def model(tmp_path: Path) -> Path:
    root = tmp_path / "acme"
    shutil.copytree(SAMPLE, root)
    return root


def _by_id(result: dict) -> dict:
    return {s["id"]: s for s in result["steps"]}


def _totals(root: Path) -> dict:
    return curate.overview(L.load_datasource(root, include_rejected=True), root)["totals"]


def _building(tmp_path: Path) -> Path:
    root = tmp_path / "demo"
    log = root / ".introspect" / "progress.log"
    log.parent.mkdir(parents=True)
    log.write_text("discovered 34 tables — reading columns, grain, keys\ncolumns+grain 33/34\n")
    return root


def test_fresh_profile_is_all_pending(tmp_path):
    root = tmp_path / "acme"
    result = steps.onboarding_steps(root)
    assert [s["id"] for s in result["steps"]] == list(steps.STEP_IDS)
    assert len(result["steps"]) == 9
    assert {s["status"] for s in result["steps"]} == {"pending"}
    assert result["current"] == "connect"
    assert not root.exists()


def test_labels_are_the_skills_wording(tmp_path):
    result = steps.onboarding_steps(tmp_path / "acme")
    assert tuple(s["label"] for s in result["steps"]) == LABELS
    assert all(s["text"] == s["label"] for s in result["steps"])
    skill = (SKILLS_DIR / "agami-connect" / "SKILL.md").read_text(encoding="utf-8")
    for label in LABELS:
        assert label in skill, label


def test_built_model_marks_build_done_with_counts(model):
    s = _by_id(steps.onboarding_steps(model))
    totals = _totals(model)
    assert s["build"]["status"] == "done"
    assert s["build"]["count"] == f"{totals['tables']} tables, {totals['joins']} joins"
    assert s["build"]["count"] == "11 tables, 12 joins"  # the sample, pinned
    assert s["build"]["text"] == f"Read the tables and joins — {s['build']['count']}"
    assert [s[i]["status"] for i in ("connect", "context", "tables")] == ["done"] * 3
    assert s["describe"]["status"] == "pending"


def test_describe_count_matches_overview(model):
    s = _by_id(steps.onboarding_steps(model))
    totals = _totals(model)
    assert s["describe"]["count"] == f"{totals['described_pct']}% described, {totals['metrics']} metrics"
    signed = totals["metrics"] - totals["needs_signoff"]
    assert s["signoff"]["count"] == f"{signed} of {totals['metrics']} signed"
    assert s["examples"]["count"] == (f"{totals['examples']} examples" if totals["examples"] else None)
    # pinned to the sample, so a count that drifts from overview's totals can't pass by rebuilding it
    assert [s[i]["count"] for i in ("describe", "signoff", "examples")] == [
        "40% described, 8 metrics", "8 of 8 signed", "14 examples"]


def test_signoff_counts_only_signed_metrics(model):
    metric = next((model / "subject_areas").glob("*/metrics/*.yaml"))
    doc = yaml.safe_load(metric.read_text())
    doc["review_state"] = "unreviewed"
    metric.write_text(yaml.safe_dump(doc))
    assert _by_id(steps.onboarding_steps(model))["signoff"]["count"] == "7 of 8 signed"


def test_a_model_that_stops_loading_does_not_regress_the_list(model):
    ds = model / "datasource.yaml"
    doc = yaml.safe_load(ds.read_text())
    doc["subject_areas"].append("subject_areas/missing")
    ds.write_text(yaml.safe_dump(doc))
    log = model / ".introspect" / "progress.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("columns+grain 3/34\n")
    _age(log, ds)  # a stale log from an earlier build, not one running now
    steps.mark(model, "signoff", "in_progress")
    result = steps.onboarding_steps(model)
    s = _by_id(result)
    assert s["build"]["status"] == "done" and s["build"]["count"] is None
    assert s["describe"] == {"id": "describe", "label": "Describe columns and add metrics",
                             "status": "done", "count": None,
                             "text": "Describe columns and add metrics"}
    assert s["signoff"]["status"] == "in_progress" and s["signoff"]["count"] is None
    assert result["current"] == "signoff"
    assert result["problem"].startswith(f"The model in {model} couldn't be read: ")
    assert "\n" not in result["problem"]


def _age(older: Path, newer: Path) -> None:
    t = newer.stat().st_mtime
    os.utime(older, (t - 60, t - 60))


def _rebuild_log(model: Path, last: str, *, newer: bool = True) -> Path:
    log = model / ".introspect" / "progress.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(f"discovered 11 tables — reading columns, grain, keys\n{last}\n")
    ds = model / "datasource.yaml"
    if newer:
        _age(ds, log)
    else:
        _age(log, ds)
    return log


def test_a_rebuild_shows_live_progress_not_the_old_model(model):
    _rebuild_log(model, "columns+grain 5/11")
    steps.mark(model, "describe", "done")
    result = steps.onboarding_steps(model)
    s = _by_id(result)
    assert s["build"]["status"] == "in_progress"
    assert s["build"]["count"] == "5 of 11 tables"
    assert s["describe"]["status"] == "pending" and s["describe"]["count"] is None
    assert result["current"] == "build"
    assert result["problem"] is None


def test_a_skipped_mark_survives_a_running_build(model):
    _rebuild_log(model, "columns+grain 5/11")
    steps.mark(model, "reports", "skipped")
    steps.mark(model, "examples", "done")
    s = _by_id(steps.onboarding_steps(model))
    assert s["reports"]["status"] == "skipped"
    assert s["examples"]["status"] == "pending"


def test_a_running_build_over_an_unloadable_model_is_no_problem(model):
    ds = model / "datasource.yaml"
    doc = yaml.safe_load(ds.read_text())
    doc["subject_areas"].append("subject_areas/missing")
    ds.write_text(yaml.safe_dump(doc))
    _rebuild_log(model, "columns+grain 5/11")
    result = steps.onboarding_steps(model)
    assert _by_id(result)["build"]["status"] == "in_progress"
    assert result["problem"] is None


def test_a_failed_rebuild_keeps_the_built_model_and_says_why(model):
    _rebuild_log(model, "failed: could not connect to server")
    steps.mark(model, "describe", "in_progress")
    result = steps.onboarding_steps(model)
    s = _by_id(result)
    assert s["build"]["status"] == "done" and s["build"]["count"] == "11 tables, 12 joins"
    assert s["describe"]["status"] == "in_progress"
    assert result["problem"] == "The last rebuild didn't finish: could not connect to server"


def test_a_failed_first_build_is_pending_with_the_reason(tmp_path):
    root = _building(tmp_path)
    (root / ".introspect" / "progress.log").write_text(
        "discovered 34 tables — reading columns, grain, keys\nfailed: permission denied for schema demo\n")
    steps.mark(root, "build", "in_progress")
    steps.mark(root, "describe", "done")
    result = steps.onboarding_steps(root)
    s = _by_id(result)
    assert s["build"]["status"] == "pending" and s["build"]["count"] is None
    assert s["describe"]["status"] == "pending"
    assert [s[i]["status"] for i in ("connect", "context", "tables")] == ["pending"] * 3
    assert result["current"] == "connect"
    assert result["problem"] == "The build didn't finish: permission denied for schema demo"


def test_a_build_silent_for_30_minutes_is_not_running(model, tmp_path):
    from datetime import datetime, timedelta, timezone
    log = _rebuild_log(model, "columns+grain 5/11")
    later = datetime.fromtimestamp(log.stat().st_mtime, timezone.utc) + timedelta(
        seconds=steps.STALE_BUILD_SECONDS + 1)
    soon = later - timedelta(seconds=2)
    assert _by_id(steps.onboarding_steps(model, now=soon))["build"]["status"] == "in_progress"
    result = steps.onboarding_steps(model, now=later)
    assert _by_id(result)["build"]["status"] == "done"
    assert result["problem"] == "The last rebuild stopped before it finished."

    root = _building(tmp_path)
    first = steps.onboarding_steps(root, now=datetime.fromtimestamp(
        (root / ".introspect" / "progress.log").stat().st_mtime, timezone.utc) + timedelta(hours=1))
    assert _by_id(first)["build"]["status"] == "pending"
    assert first["problem"] == "The build stopped before it finished."


def test_a_bad_byte_in_the_build_log_does_not_crash(tmp_path, capsys):
    root = _building(tmp_path)
    (root / ".introspect" / "progress.log").write_bytes(b"\xff\xfe\ncolumns+grain 2/9\n")
    assert cli.main(["steps", str(root)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert _by_id(out)["build"]["count"] == "2 of 9 tables"


def test_a_finished_log_does_not_hold_the_build_open(model):
    _rebuild_log(model, "done: 11 tables, 12 relationships")
    s = _by_id(steps.onboarding_steps(model))
    assert s["build"]["status"] == "done"
    assert s["build"]["count"] == "11 tables, 12 joins"


def test_an_old_log_does_not_hold_the_build_open(model):
    _rebuild_log(model, "columns+grain 5/11", newer=False)
    s = _by_id(steps.onboarding_steps(model))
    assert s["build"]["status"] == "done"
    assert s["build"]["count"] == "11 tables, 12 joins"


def test_problem_is_none_when_the_model_loads(model, tmp_path):
    assert steps.onboarding_steps(model)["problem"] is None
    assert steps.onboarding_steps(tmp_path / "fresh")["problem"] is None


def test_build_in_progress_is_never_done_early(tmp_path):
    root = _building(tmp_path)
    s = _by_id(steps.onboarding_steps(root))
    assert s["build"]["status"] == "in_progress"
    assert s["build"]["count"] == "33 of 34 tables"
    assert s["build"]["text"] == "Read the tables and joins — 33 of 34 tables"
    steps.mark(root, "build", "done")
    steps.mark(root, "describe", "in_progress")
    result = steps.onboarding_steps(root)
    s = _by_id(result)
    assert s["build"]["status"] == "in_progress"
    assert s["describe"]["status"] == "pending"
    assert s["describe"]["count"] is None
    assert result["current"] == "build"


def test_build_log_without_counts_has_no_count(tmp_path):
    root = _building(tmp_path)
    (root / ".introspect" / "progress.log").write_text("building relationships (34 tables)…\n")
    assert _by_id(steps.onboarding_steps(root))["build"] == {
        "id": "build", "label": "Read the tables and joins", "status": "in_progress",
        "count": None, "text": "Read the tables and joins"}


def test_build_done_mark_without_a_model_or_log_is_in_progress(tmp_path):
    root = tmp_path / "acme"
    steps.mark(root, "build", "done")
    s = _by_id(steps.onboarding_steps(root))
    assert s["build"]["status"] == "in_progress"
    assert s["build"]["count"] is None


def test_progress_parsing_is_shared_with_sm_progress():
    # one table of phrases: an unknown line has no count here and is passed through by sm progress
    assert steps.parse_progress("something the engine never writes") is None
    assert cli._progress_line("something the engine never writes") == {
        "line": "something the engine never writes", "done": False}
    m, phrase, unit = steps.parse_progress("columns+grain 33/34")
    assert (m.groups(), unit) == (("33", "34"), "tables")


def test_a_finished_log_before_the_model_lands_is_still_building(tmp_path):
    # the engine logs `done:` before it writes the tree, so the step stays open until the model lands
    root = _building(tmp_path)
    (root / ".introspect" / "progress.log").write_text("done: 34 tables, 7 relationships\n")
    s = _by_id(steps.onboarding_steps(root))
    assert s["build"]["status"] == "in_progress" and s["build"]["count"] is None


def test_mark_appends_and_keeps_one_in_progress(model):
    steps.mark(model, "describe", "in_progress")
    steps.mark(model, "describe", "in_progress")
    timeline = model / ".introspect" / "timeline.jsonl"
    lines = timeline.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert set(json.loads(lines[0])) == {"id", "status", "at"}
    result = steps.onboarding_steps(model)
    assert result["timeline"] == str(timeline)
    assert [s["id"] for s in result["steps"] if s["status"] == "in_progress"] == ["describe"]
    assert result["current"] == "describe"
    steps.mark(model, "signoff", "in_progress")
    s = _by_id(steps.onboarding_steps(model))
    assert s["describe"]["status"] == "done"
    assert [i for i, v in s.items() if v["status"] == "in_progress"] == ["signoff"]


def test_mark_stamps_utc_seconds(tmp_path):
    from datetime import datetime, timedelta, timezone
    root = tmp_path / "acme"
    steps.mark(root, "connect", "done", now=datetime(2026, 1, 2, 4, 5, 6, 789,
                                                     tzinfo=timezone(timedelta(hours=1))))
    line = json.loads((root / ".introspect" / "timeline.jsonl").read_text(encoding="utf-8"))
    assert line == {"id": "connect", "status": "done", "at": "2026-01-02T03:05:06+00:00"}
    with pytest.raises(ValueError):
        steps.mark(root, "nope", "done")
    with pytest.raises(ValueError):
        steps.mark(root, "build", "pending")


def test_a_running_step_behind_a_finished_one_is_done(model):
    steps.mark(model, "describe", "in_progress")
    steps.mark(model, "signoff", "done")
    result = steps.onboarding_steps(model)
    s = _by_id(result)
    assert s["describe"]["status"] == "done"
    assert not [i for i, v in s.items() if v["status"] == "in_progress"]
    assert result["current"] == "examples"


def test_reports_is_skipped_when_passed_over(model):
    steps.mark(model, "first_question", "in_progress")
    result = steps.onboarding_steps(model)
    s = _by_id(result)
    assert s["reports"]["status"] == "skipped"
    assert [s[i]["status"] for i in ("describe", "signoff", "examples")] == ["done"] * 3
    assert result["current"] == "first_question"


def test_all_done_has_no_current(model):
    steps.mark(model, "first_question", "done")
    result = steps.onboarding_steps(model)
    assert result["current"] is None


def test_bad_mark_exits_2_and_writes_nothing(model, capsys):
    for bad in ("nope=done", "build=finished", "build"):
        assert cli.main(["steps", str(model), "--mark", "connect=done", "--mark", bad]) == 2
        err = capsys.readouterr().err
        assert err.strip() and "\n" not in err.strip()
    assert not (model / ".introspect" / "timeline.jsonl").exists()


def test_cli_prints_the_same_dict(model, capsys):
    assert cli.main(["steps", str(model)]) == 0
    assert json.loads(capsys.readouterr().out) == steps.onboarding_steps(model)
    assert cli.main(["steps", str(model), "--mark", "describe=in_progress",
                     "--mark", "describe=done"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out == steps.onboarding_steps(model)
    assert _by_id(out)["describe"]["status"] == "done"


def test_unreadable_timeline_lines_are_ignored(model):
    timeline = model / ".introspect" / "timeline.jsonl"
    timeline.parent.mkdir(parents=True, exist_ok=True)
    timeline.write_text('not json at all\n["a list"]\n{"id": "nope", "status": "done"}\n'
                        '{"id": "signoff", "status": "in_progress", "at": "2026-01-01T00:00:00+00:00"}\n',
                        encoding="utf-8")
    result = steps.onboarding_steps(model)
    assert result["current"] == "signoff"
    assert _by_id(result)["signoff"]["status"] == "in_progress"


def test_a_timeline_line_that_is_not_utf8_is_ignored(model):
    timeline = model / ".introspect" / "timeline.jsonl"
    timeline.parent.mkdir(parents=True, exist_ok=True)
    timeline.write_bytes(b'\xff\xfe{"id": "nope"\n'
                         b'{"id": "examples", "status": "in_progress", "at": "2026-01-01T00:00:00+00:00"}\n')
    assert steps.onboarding_steps(model)["current"] == "examples"
