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


def _started(root: Path) -> None:
    """Mark the build started, before any log already written: that log is this attempt's. Marks
    are stamped to the second, so a mark written after the log could land a second later and make
    the log read as an earlier attempt's."""
    from datetime import datetime, timedelta, timezone
    log = root / ".introspect" / "progress.log"
    at = (datetime.fromtimestamp(log.stat().st_mtime, timezone.utc) - timedelta(seconds=2)
          if log.exists() else None)
    steps.mark(root, "build", "in_progress", now=at)


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
    steps.mark(model, "first_question", "done")  # a pending step shows no count (rule E)
    s = _by_id(steps.onboarding_steps(model))
    totals = _totals(model)
    assert s["describe"]["count"] == f"{totals['described_pct']}% described, {totals['metrics']} metrics"
    signoff = curate.signoff_counts(L.load_datasource(model, include_rejected=True))
    assert s["signoff"]["count"] == (
        f"{signoff['signed']} of {signoff['signed'] + signoff['waiting']} signed")
    assert s["examples"]["count"] == (f"{totals['examples']} examples" if totals["examples"] else None)
    # pinned to the sample, so a count that drifts from overview's totals can't pass by rebuilding it
    assert [s[i]["count"] for i in ("describe", "signoff", "examples")] == [
        "40% described, 8 metrics", "8 of 8 signed", "14 examples"]


def test_signoff_counts_only_signed_metrics(model):
    metric = next((model / "subject_areas").glob("*/metrics/*.yaml"))
    doc = yaml.safe_load(metric.read_text())
    doc["review_state"] = "unreviewed"
    metric.write_text(yaml.safe_dump(doc))
    steps.mark(model, "signoff", "done")
    assert _by_id(steps.onboarding_steps(model))["signoff"]["count"] == "7 of 8 signed"


def _unsign_all(root: Path, folder: str) -> int:
    """Send every file in each area's `folder` back to review, as a fresh build leaves them."""
    files = sorted((root / "subject_areas").glob(f"*/{folder}/*.yaml"))
    for f in files:
        doc = yaml.safe_load(f.read_text())
        doc["review_state"] = "unreviewed"
        for k in ("signed_off_by", "signed_off_at", "signed_off_role"):
            doc.pop(k, None)
        f.write_text(yaml.safe_dump(doc))
    return len(files)


def test_signoff_counts_entities_and_joins_like_the_card(model):
    # The card counts metrics, entities and joins awaiting review; a run that signs nothing still
    # shows a count while the step runs and once it is done.
    metrics, entities = _unsign_all(model, "metrics"), _unsign_all(model, "entities")
    rel = model / "subject_areas" / "agami-example" / "relationships.yaml"
    doc = yaml.safe_load(rel.read_text())
    doc["relationships"][0].update(review_state="unreviewed", signed_off_by=None,
                                   signed_off_at=None, signed_off_role=None)
    rel.write_text(yaml.safe_dump(doc))
    total = metrics + entities + 1
    steps.mark(model, "signoff", "in_progress")
    assert _by_id(steps.onboarding_steps(model))["signoff"]["count"] == f"0 of {total} signed"
    assert total == 17
    steps.mark(model, "signoff", "done")
    assert _by_id(steps.onboarding_steps(model))["signoff"]["count"] == "0 of 17 signed"


def _hide_invoices_with_everything_unsigned(model: Path) -> None:
    _unsign_all(model, "metrics")
    _unsign_all(model, "entities")
    rel = model / "subject_areas" / "agami-example" / "relationships.yaml"
    doc = yaml.safe_load(rel.read_text())
    doc["relationships"][0].update(review_state="unreviewed", signed_off_by=None,
                                   signed_off_at=None, signed_off_role=None)
    rel.write_text(yaml.safe_dump(doc))
    table = model / "subject_areas" / "agami-example" / "tables" / "invoices.yaml"
    t = yaml.safe_load(table.read_text())
    t["review_state"] = "rejected"
    table.write_text(yaml.safe_dump(t))


def test_signoff_count_is_the_review_queues_with_a_hidden_table(model, capsys):
    # Hiding a table drops what hangs off it from the queue; the step must count the same items.
    _hide_invoices_with_everything_unsigned(model)
    assert cli.main(["review-queue", str(model)]) == 0
    counts = json.loads(capsys.readouterr().out)["counts"]
    queued = counts["rule_1"] + counts["rule_2"]
    assert queued == 14
    steps.mark(model, "signoff", "in_progress")
    assert _by_id(steps.onboarding_steps(model))["signoff"]["count"] == f"0 of {queued} signed"


def test_describe_shows_its_percentage_before_any_metric(model):
    for f in (model / "subject_areas").glob("*/metrics/*.yaml"):
        f.unlink()
    steps.mark(model, "describe", "in_progress")
    s = _by_id(steps.onboarding_steps(model))
    assert s["describe"]["count"] == "40% described, 0 metrics"
    assert s["signoff"]["count"] is None  # pending: no count yet


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
    # the folder by name, never its absolute path; the loader's words quoted
    assert result["problem"].startswith('The model in "acme" couldn\'t be read: "')
    assert str(model) not in result["problem"]
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
    assert result["problem"] == 'The last rebuild didn\'t finish: "could not connect to server"'


def test_a_failed_first_build_is_pending_with_the_reason(tmp_path):
    root = _building(tmp_path)
    (root / ".introspect" / "progress.log").write_text(
        "discovered 34 tables — reading columns, grain, keys\nfailed: permission denied for schema demo\n")
    _started(root)
    steps.mark(root, "describe", "done")
    result = steps.onboarding_steps(root)
    s = _by_id(result)
    assert s["build"]["status"] == "pending" and s["build"]["count"] is None
    assert s["describe"]["status"] == "pending"
    # the build was reached, so the steps before it are done and the list stops at the build
    assert [s[i]["status"] for i in ("connect", "context", "tables")] == ["done"] * 3
    assert result["current"] == "build"
    assert result["problem"] == 'The build didn\'t finish: "permission denied for schema demo"'


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
    _started(root)  # a pending build ignores its log (rule D6)
    first = steps.onboarding_steps(root, now=datetime.fromtimestamp(
        (root / ".introspect" / "progress.log").stat().st_mtime, timezone.utc) + timedelta(hours=1))
    assert _by_id(first)["build"]["status"] == "pending"
    assert first["problem"] == "The build stopped before it finished."
    assert first["current"] == "build"


def test_a_bad_byte_in_the_build_log_does_not_crash(tmp_path, capsys):
    root = _building(tmp_path)
    (root / ".introspect" / "progress.log").write_bytes(b"\xff\xfe\ncolumns+grain 2/9\n")
    assert cli.main(["steps", str(root), "--full"]) == 0
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
    # one table of phrases: an unknown line has no count here and is passed through, quoted, by sm progress
    assert steps.parse_progress("something the engine never writes") is None
    assert cli._progress_line("something the engine never writes") == {
        "line": '"something the engine never writes"', "done": False, "failed": False}
    m, phrase, unit = steps.parse_progress("columns+grain 33/34")
    assert (m.groups(), unit) == (("33", "34"), "tables")


def test_a_finished_log_before_the_model_lands_is_still_building(tmp_path):
    # the engine logs `done:` before it writes the tree, so the step stays open until the model lands
    root = _building(tmp_path)
    (root / ".introspect" / "progress.log").write_text("done: 34 tables, 7 relationships\n")
    s = _by_id(steps.onboarding_steps(root))
    assert s["build"]["status"] == "in_progress" and s["build"]["count"] is None


def test_a_finished_log_whose_model_never_lands_goes_stale(tmp_path):
    # a kill between the `done:` line and the tree write (or a dry run) must not read "building"
    # forever
    from datetime import datetime, timedelta, timezone
    root = _building(tmp_path)
    log = root / ".introspect" / "progress.log"
    log.write_text("done: 34 tables, 7 relationships\n")
    _started(root)
    at = datetime.fromtimestamp(log.stat().st_mtime, timezone.utc)
    soon = at + timedelta(seconds=steps.STALE_BUILD_SECONDS - 1)
    assert _by_id(steps.onboarding_steps(root, now=soon))["build"]["status"] == "in_progress"
    result = steps.onboarding_steps(root, now=at + timedelta(seconds=steps.STALE_BUILD_SECONDS + 1))
    assert _by_id(result)["build"]["status"] == "pending"
    assert result["current"] == "build"
    assert result["problem"] == "The build stopped before it finished."


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


def test_cli_full_prints_the_same_dict(model, capsys):
    said = {"changed", "then", "list"}
    assert cli.main(["steps", str(model), "--full"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert {k: v for k, v in out.items() if k not in said} == steps.onboarding_steps(model)
    assert cli.main(["steps", str(model), "--full", "--mark", "describe=in_progress",
                     "--mark", "describe=done"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert {k: v for k, v in out.items() if k not in said} == steps.onboarding_steps(model)
    assert _by_id(out)["describe"]["status"] == "done"


def test_cli_prints_a_compact_list_by_default(model, capsys):
    steps.mark(model, "connect", "done")
    steps.mark(model, "reports", "skipped")
    steps.mark(model, "describe", "in_progress")
    assert cli.main(["steps", str(model)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert set(out) == {"list", "current", "problem", "changed", "then"}
    full = steps.onboarding_steps(model)
    shown = [s for s in full["steps"] if s["status"] != "skipped"]
    assert out["list"] == [{"done": "✓ ", "in_progress": "▶ ", "pending": "· "}[s["status"]] + s["text"]
                           for s in shown]
    assert len(out["list"]) == 8  # reports skipped: not listed
    assert out["list"][0] == "✓ Connect your database"
    assert out["list"][3].startswith("✓ Read the tables and joins — ")
    assert out["list"][4].startswith("▶ Describe columns and add metrics")
    assert out["list"][-1] == "· Summary and first question"
    assert (out["current"], out["problem"]) == ("describe", None)


def test_compact_list_of_a_fresh_folder(tmp_path, capsys):
    assert cli.main(["steps", str(tmp_path / "acme")]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out == {"list": ["· " + label for label in LABELS], "current": "connect",
                   "problem": None, "changed": True,
                   "then": "Update the task list from `list` now, before anything else."}
    assert not (tmp_path / "acme").exists()  # a fresh profile gets no folder made for steps.last


def test_compact_keeps_the_problem(tmp_path):
    full = {"steps": [{"id": "build", "status": "pending", "text": "Read the tables and joins"}],
            "current": "build", "problem": "The build stopped before it finished."}
    assert steps.compact(full) == {"list": ["· Read the tables and joins"], "current": "build",
                                   "problem": "The build stopped before it finished."}


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


def _complete(root: Path) -> None:
    for i in steps.STEP_IDS:
        steps.mark(root, i, "in_progress")
        steps.mark(root, i, "done")


def test_restarting_an_earlier_step_reopens_the_later_ones(model):
    # a reintrospect after a finished onboarding must not show a finished list for the whole run
    _complete(model)
    steps.mark(model, "reports", "skipped")
    assert steps.onboarding_steps(model)["current"] is None
    steps.mark(model, "tables", "in_progress")
    result = steps.onboarding_steps(model)
    s = _by_id(result)
    assert [s[i]["status"] for i in ("connect", "context")] == ["done", "done"]
    assert s["tables"]["status"] == "in_progress"
    assert [s[i]["status"] for i in ("build", "describe", "signoff", "examples", "first_question")] \
        == ["pending"] * 5
    assert s["reports"]["status"] == "skipped"  # a skip survives the restart
    assert s["build"]["count"] is None  # the old model's numbers are not this run's
    assert result["current"] == "tables"


def test_a_rebuild_after_restart_completes_normally(model):
    _complete(model)
    steps.mark(model, "tables", "in_progress")
    steps.mark(model, "tables", "done")
    steps.mark(model, "build", "in_progress")
    _rebuild_log(model, "columns+grain 5/11")
    s = _by_id(steps.onboarding_steps(model))
    assert s["tables"]["status"] == "done"
    assert s["build"]["status"] == "in_progress" and s["build"]["count"] == "5 of 11 tables"
    assert s["describe"]["status"] == "pending"

    _rebuild_log(model, "columns+grain 5/11", newer=False)  # the rebuilt model has landed
    steps.mark(model, "build", "done")
    steps.mark(model, "describe", "in_progress")
    result = steps.onboarding_steps(model)
    s = _by_id(result)
    assert s["build"]["status"] == "done" and s["build"]["count"] == "11 tables, 12 joins"
    assert s["describe"]["status"] == "in_progress"
    assert [s[i]["status"] for i in ("signoff", "examples", "first_question")] == ["pending"] * 3
    assert result["current"] == "describe"
    for i in steps.STEP_IDS[steps.STEP_IDS.index("describe"):]:
        steps.mark(model, i, "in_progress")
        steps.mark(model, i, "done")
    result = steps.onboarding_steps(model)
    assert {s["status"] for s in result["steps"]} == {"done"}
    assert result["current"] is None


def test_steps_refuses_an_empty_root(tmp_path, capsys, monkeypatch):
    # an unset "$ROOT" reaches the command as "": it must not write a timeline into the cwd
    monkeypatch.chdir(tmp_path)
    assert cli.main(["steps", "", "--mark", "connect=done"]) == 2
    err = capsys.readouterr().err.strip()
    assert err and "\n" not in err
    assert not list(tmp_path.iterdir())


# --- status rules v2: marks decide status; disk supplies counts and a few clamps on the build ---

_LETTER = {"d": "done", "i": "in_progress", "p": "pending", "s": "skipped"}
BUILT = "11 tables, 12 joins"


def _sample(tmp_path: Path) -> Path:
    root = tmp_path / "acme"
    shutil.copytree(SAMPLE, root)
    return root


def _stale(log: Path):
    from datetime import datetime, timedelta, timezone
    return datetime.fromtimestamp(log.stat().st_mtime, timezone.utc) + timedelta(
        seconds=steps.STALE_BUILD_SECONDS + 1)


def _no_root(tmp_path):
    return tmp_path / "acme", None


def _no_marks(tmp_path):
    return _sample(tmp_path), None


def _forward_run(tmp_path):
    root = _sample(tmp_path)
    done: set[str] = set()
    for i in steps.STEP_IDS:
        steps.mark(root, i, "in_progress")
        s = _by_id(steps.onboarding_steps(root))
        assert all(s[d]["status"] == "done" for d in done), i  # never loses a done
        assert s[i]["status"] == "in_progress", i
        steps.mark(root, i, "done")
        done.add(i)
    return root, None


def _done_mark_no_yaml(tmp_path):
    root = tmp_path / "acme"
    steps.mark(root, "build", "done")
    return root, None


def _running_first_build(tmp_path):
    root = _building(tmp_path)
    _started(root)
    return root, None


def _restart_build(tmp_path):  # I1
    root = _sample(tmp_path)
    _complete(root)
    _started(root)
    return root, None


def _restart_build_running(tmp_path):
    root, _ = _restart_build(tmp_path)
    _rebuild_log(root, "columns+grain 5/11")
    return root, None


def _failed_first_build(tmp_path):
    root = _building(tmp_path)
    (root / ".introspect" / "progress.log").write_text("columns+grain 3/34\nfailed: boom\n")
    _started(root)
    return root, None


def _failed_rebuild_marked(tmp_path):
    root = _sample(tmp_path)
    _started(root)
    _rebuild_log(root, "failed: boom")
    return root, None


def _failed_rebuild_unmarked(tmp_path):
    root = _sample(tmp_path)
    _rebuild_log(root, "failed: boom")
    return root, None


def _restart_context_over_failed_log(tmp_path):  # I2
    root = _sample(tmp_path)
    _complete(root)
    _rebuild_log(root, "failed: boom")
    steps.mark(root, "context", "in_progress")
    return root, None


def _restart_tables(tmp_path):
    root = _sample(tmp_path)
    _complete(root)
    steps.mark(root, "tables", "in_progress")
    return root, None


def _unloadable(tmp_path):
    root = _sample(tmp_path)
    ds = root / "datasource.yaml"
    doc = yaml.safe_load(ds.read_text())
    doc["subject_areas"].append("subject_areas/missing")
    ds.write_text(yaml.safe_dump(doc))
    steps.mark(root, "signoff", "in_progress")
    return root, None


def _stale_first_build(tmp_path):
    root = _building(tmp_path)
    _started(root)
    return root, _stale(root / ".introspect" / "progress.log")


def _done_log_no_yaml(tmp_path):
    root = _building(tmp_path)
    (root / ".introspect" / "progress.log").write_text("done: 34 tables, 7 relationships\n")
    _started(root)
    return root, None


def _done_log_no_yaml_stale(tmp_path):
    root, _ = _done_log_no_yaml(tmp_path)
    return root, _stale(root / ".introspect" / "progress.log")


def _skips_survive(tmp_path):
    root = _sample(tmp_path)
    _complete(root)
    steps.mark(root, "reports", "skipped")
    steps.mark(root, "tables", "in_progress")
    _rebuild_log(root, "columns+grain 5/11")
    return root, None


def _behind_a_finished_step(tmp_path):
    root = _sample(tmp_path)
    steps.mark(root, "describe", "in_progress")
    steps.mark(root, "signoff", "done")
    return root, None


def _describing(tmp_path):
    root = _sample(tmp_path)
    steps.mark(root, "describe", "in_progress")
    return root, None


def _restart_connect(tmp_path):
    root = _sample(tmp_path)
    _complete(root)
    steps.mark(root, "connect", "in_progress")
    return root, None


def _marks(root: Path, *specs: str) -> None:
    for spec in specs:
        steps.mark(root, *spec.split("="))


def _restart_drops_later(tmp_path):
    root = tmp_path / "acme"
    _marks(root, "connect=in_progress", "connect=done", "context=in_progress", "context=done",
           "tables=in_progress", "context=in_progress")
    return root, None


def _done_does_not_restart(tmp_path):
    root = tmp_path / "acme"
    _marks(root, "tables=in_progress", "tables=done", "connect=done")
    return root, None


def _unloadable_no_marks(tmp_path):
    root, _ = _unloadable(tmp_path)
    (root / ".introspect" / "timeline.jsonl").unlink()  # a profile from before the timeline
    return root, None


def _reintrospect_context_done(tmp_path):
    root = _sample(tmp_path)
    _complete(root)
    _marks(root, "context=in_progress", "context=done")
    return root, None


def _reintrospect_tables_done(tmp_path):
    root, _ = _reintrospect_context_done(tmp_path)
    _marks(root, "tables=in_progress", "tables=done")
    return root, None


def _built_then_failed_rebuild(tmp_path):
    root = _sample(tmp_path)
    steps.mark(root, "build", "done")
    _rebuild_log(root, "failed: boom")
    return root, None


def _unloadable_then_failed_rebuild(tmp_path):
    root = _sample(tmp_path)
    ds = root / "datasource.yaml"
    doc = yaml.safe_load(ds.read_text())
    doc["subject_areas"].append("subject_areas/missing")
    ds.write_text(yaml.safe_dump(doc))
    steps.mark(root, "build", "done")
    _rebuild_log(root, "failed: boom")
    return root, None


def _stale_failed_first_build(tmp_path):
    root, _ = _failed_first_build(tmp_path)
    return root, _stale(root / ".introspect" / "progress.log")


def _log_older_than_model(tmp_path):
    root = _sample(tmp_path)
    _rebuild_log(root, "columns+grain 5/11", newer=False)
    return root, None


def _done_log_with_model(tmp_path):
    root = _sample(tmp_path)
    _rebuild_log(root, "done: 11 tables, 12 relationships")
    return root, None


def _reports_passed_over(tmp_path):
    root = _sample(tmp_path)
    steps.mark(root, "first_question", "in_progress")
    return root, None


def _retry_after_failure(tmp_path):
    root = _building(tmp_path)
    log = root / ".introspect" / "progress.log"
    log.write_text("columns+grain 3/34\nfailed: boom\n")
    earlier = log.stat().st_mtime - 10
    os.utime(log, (earlier, earlier))
    steps.mark(root, "build", "in_progress")  # the retry, after the failed attempt's log
    return root, None


def _build_skipped(tmp_path):
    root = tmp_path / "acme"
    _marks(root, "build=skipped", "describe=in_progress")
    return root, None


def _sample_copied(tmp_path):  # sample.md 6A: the copy, then step 7's mark
    root = _sample(tmp_path)
    _marks(root, "connect=in_progress", "connect=done", "first_question=in_progress")
    return root, None


def _stale_mark(root: Path, *, model_at_mark: bool = False):
    """A `build=in_progress` mark left from a session that ended: no log since, and now long after."""
    from datetime import datetime, timedelta, timezone
    at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    steps.mark(root, "build", "in_progress", now=at)
    return at + timedelta(seconds=steps.STALE_BUILD_SECONDS + 1)


def _stale_mark_no_log(tmp_path):
    root = tmp_path / "acme"
    return root, _stale_mark(root)


def _stale_mark_older_log(tmp_path):  # a rebuild's mark; the only log is the last attempt's
    root = _sample(tmp_path)
    log = _rebuild_log(root, "failed: boom", newer=False)
    os.utime(log, (1_000_000, 1_000_000))
    # the model is the earlier run's, older than the mark (a model written after it is this
    # attempt's, which tests/test_steps_resume.py covers)
    os.utime(root / "datasource.yaml", (2_000_000, 2_000_000))
    return root, _stale_mark(root)


def _context_done_over_model(tmp_path):
    root = _sample(tmp_path)
    _marks(root, "context=done")
    return root, None


def _done_marks_before_build(tmp_path):  # a `done` mark is no restart; only `in_progress` is
    root = _sample(tmp_path)
    _marks(root, "connect=done", "tables=done")
    return root, None


DESCRIBED, SIGNED, EXAMPLES = "40% described, 8 metrics", "8 of 8 signed", "14 examples"
ALL_COUNTS = {"build": BUILT, "describe": DESCRIBED, "signoff": SIGNED, "examples": EXAMPLES}

# (setup, the nine statuses in order, current, counts that must be shown, problem)
STATUS_RULES = {
    "1-no-root": (_no_root, "ppppppppp", "connect", {}, None),
    "2-no-marks-model-loads": (_no_marks, "ddddppppp", "describe", {"build": BUILT}, None),
    "3-forward-first-run": (_forward_run, "ddddddddd", None, ALL_COUNTS, None),
    "4-build-done-mark-no-yaml": (_done_mark_no_yaml, "dddippppp", "build", {}, None),
    "5-running-first-build": (_running_first_build, "dddippppp", "build",
                              {"build": "33 of 34 tables"}, None),
    "6-restart-build-no-new-log": (_restart_build, "dddippppp", "build", {}, None),
    "7-restart-build-running": (_restart_build_running, "dddippppp", "build",
                                {"build": "5 of 11 tables"}, None),
    "8-failed-first-build": (_failed_first_build, "dddpppppp", "build", {},
                             "The build didn't finish: \"boom\""),
    "9-failed-rebuild-marked": (_failed_rebuild_marked, "dddpppppp", "build", {},
                                "The rebuild didn't finish: \"boom\""),
    "10-failed-rebuild-unmarked": (_failed_rebuild_unmarked, "ddddppppp", "describe",
                                   {"build": BUILT}, "The last rebuild didn't finish: \"boom\""),
    "11-restart-context-over-failed-log": (_restart_context_over_failed_log, "dippppppp",
                                           "context", {}, None),
    "12-restart-tables": (_restart_tables, "ddipppppp", "tables", {}, None),
    "13-unloadable-model": (_unloadable, "dddddippp", "signoff", {}, "The model in "),
    "14-stale-first-build": (_stale_first_build, "dddpppppp", "build", {},
                             "The build stopped before it finished."),
    "15a-done-log-no-yaml-fresh": (_done_log_no_yaml, "dddippppp", "build", {}, None),
    "15b-done-log-no-yaml-stale": (_done_log_no_yaml_stale, "dddpppppp", "build", {},
                                   "The build stopped before it finished."),
    "16-skips-survive": (_skips_survive, "dddipppsp", "build", {"build": "5 of 11 tables"}, None),
    "17-running-behind-a-finished-step": (_behind_a_finished_step, "ddddddppp", "examples",
                                          {"build": BUILT, "describe": DESCRIBED,
                                           "signoff": SIGNED}, None),
    "18-pending-has-no-count": (_describing, "ddddipppp", "describe",
                                {"build": BUILT, "describe": DESCRIBED}, None),
    "19-restart-connect": (_restart_connect, "ipppppppp", "connect", {}, None),
    "A-restart-drops-later-marks": (_restart_drops_later, "dippppppp", "context", {}, None),
    "A-done-does-not-restart": (_done_does_not_restart, "dddpppppp", "build", {}, None),
    "B-unloadable-no-marks": (_unloadable_no_marks, "ddddppppp", "describe", {}, "The model in "),
    "B-reintrospect-context-done": (_reintrospect_context_done, "ddppppppp", "tables", {}, None),
    "B-reintrospect-tables-done": (_reintrospect_tables_done, "dddpppppp", "build", {}, None),
    "D5-build-done-mark-failed-rebuild": (_built_then_failed_rebuild, "ddddppppp", "describe",
                                          {"build": BUILT}, "The last rebuild didn't finish: \"boom\""),
    "D7-unloadable-beats-failed-rebuild": (_unloadable_then_failed_rebuild, "ddddppppp",
                                           "describe", {}, "The model in "),
    "D-stale-failed-log-stays-failed": (_stale_failed_first_build, "dddpppppp", "build", {},
                                        "The build didn't finish: \"boom\""),
    "D-log-older-than-model-is-none": (_log_older_than_model, "ddddppppp", "describe",
                                       {"build": BUILT}, None),
    "D-done-log-with-model-is-none": (_done_log_with_model, "ddddppppp", "describe",
                                      {"build": BUILT}, None),
    "C-reports-implied-skipped": (_reports_passed_over, "dddddddsi", "first_question",
                                  ALL_COUNTS, None),
    "3-retry-after-failure": (_retry_after_failure, "dddippppp", "build", {}, None),
    "build-skipped-is-harmless": (_build_skipped, "dddsipppp", "describe", {}, None),
    "sample-copy-path": (_sample_copied, "dddddddsi", "first_question", ALL_COUNTS, None),
    "D-stale-mark-no-log": (_stale_mark_no_log, "dddpppppp", "build", {},
                            "The build stopped before it finished."),
    "D-stale-mark-older-log": (_stale_mark_older_log, "dddpppppp", "build", {},
                               "The rebuild stopped before it finished."),
    "B-model-only-context-done": (_context_done_over_model, "ddddppppp", "describe",
                                  {"build": BUILT}, None),
    "A-done-before-build-is-no-restart": (_done_marks_before_build, "ddddppppp", "describe",
                                          {"build": BUILT}, None),
}


@pytest.mark.parametrize("row", list(STATUS_RULES), ids=list(STATUS_RULES))
def test_status_rules_v2(row, tmp_path):
    setup, want, current, counts, problem = STATUS_RULES[row]
    root, now = setup(tmp_path)
    result = steps.onboarding_steps(root, now=now)
    s = _by_id(result)
    assert [s[i]["status"] for i in steps.STEP_IDS] == [_LETTER[c] for c in want]
    assert result["current"] == current
    for i in steps.STEP_IDS:
        assert s[i]["count"] == counts.get(i), i  # every count shown, and only these
    if problem is None:
        assert result["problem"] is None
    else:
        assert result["problem"] and result["problem"].startswith(problem)
