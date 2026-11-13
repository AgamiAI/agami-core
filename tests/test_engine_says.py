"""The engine hands over what to post (`say`) and what to do next (`then`) at the moment they are
needed: a live run computed the closing summary without posting it, and refreshed its task list
after only about half of the step calls, because the skill text that said so was read long before.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest

pytest.importorskip("pydantic")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "packages" / "agami-core" / "src"))

from semantic_model import cli, steps, summary  # noqa: E402

from skill_docs import PLUGIN  # noqa: E402

SAMPLE = PLUGIN / "samples" / "store" / "model"

UPDATE = "Update the task list from `list` now, before anything else."
UP_TO_DATE = "The task list is up to date."
CLOSING = ("Post `say` exactly as written, then update the task list from `list`, then ask the "
           "closing card in that same message.")


@pytest.fixture
def model(tmp_path: Path) -> Path:
    root = tmp_path / "acme"
    shutil.copytree(SAMPLE, root)
    return root


def _run(argv: list[str], capsys) -> dict:
    assert cli.main(argv) == 0
    return json.loads(capsys.readouterr().out)


# --- A. sm steps ---------------------------------------------------------------------------------


def test_steps_says_update_then_up_to_date(model, capsys):
    first = _run(["steps", str(model)], capsys)
    assert (first["changed"], first["then"]) == (True, UPDATE)
    assert "say" not in first
    again = _run(["steps", str(model)], capsys)
    assert (again["changed"], again["then"]) == (False, UP_TO_DATE)
    assert again["list"] == first["list"]


def test_a_mark_changes_the_list(model, capsys):
    _run(["steps", str(model)], capsys)
    out = _run(["steps", str(model), "--mark", "describe=in_progress"], capsys)
    assert (out["changed"], out["then"]) == (True, UPDATE)
    assert "say" not in out  # only the closing mark carries the summary


def test_the_last_printed_list_is_kept_inside_the_model(model, capsys):
    out = _run(["steps", str(model)], capsys)
    kept = json.loads((model / ".introspect" / "steps.last").read_text(encoding="utf-8"))
    assert kept["list"] == out["list"]
    assert [p.name for p in (model / ".introspect").iterdir() if p.name.startswith(".")] == []


def test_full_carries_the_keys_too(model, capsys):
    _run(["steps", str(model)], capsys)
    out = _run(["steps", str(model), "--full"], capsys)
    assert "steps" in out and (out["changed"], out["then"]) == (False, UP_TO_DATE)


def test_the_closing_mark_says_the_whole_summary(model, capsys):
    steps.mark(model, "examples", "done")
    out = _run(["steps", str(model), "--mark", "first_question=in_progress"], capsys)
    assert out["say"] == summary.onboarding_summary(model)["markdown"]
    assert out["say"].startswith("**agami-example: 11 tables, 12 joins, 8 metrics, 14 examples.**")
    assert out["then"] == CLOSING
    assert out["list"][-1].startswith("▶ Summary and first question")
    # the same list printed again carries no summary
    later = _run(["steps", str(model)], capsys)
    assert "say" not in later and later["then"] == UP_TO_DATE


def test_other_marks_say_nothing(model, capsys):
    for mark in ("first_question=done", "examples=in_progress", "signoff=done"):
        out = _run(["steps", str(model), "--mark", mark], capsys)
        assert "say" not in out, mark


def test_a_problem_is_said_first(model, capsys):
    (model / "datasource.yaml").write_text("datasource: [unclosed\n")
    out = _run(["steps", str(model), "--mark", "first_question=in_progress"], capsys)
    assert out["problem"] and out["say"] == out["problem"]
    assert out["then"] == "Say this to the person before going on."


def test_a_closing_mark_with_no_summary_says_nothing(model, capsys, monkeypatch):
    def broken(root):
        raise ValueError("cannot sum up")
    monkeypatch.setattr(summary, "onboarding_summary", broken)
    out = _run(["steps", str(model), "--mark", "first_question=in_progress"], capsys)
    assert "say" not in out and out["then"] == UPDATE


def test_an_unreadable_last_file_reads_as_changed(model, capsys):
    (model / ".introspect").mkdir(exist_ok=True)
    (model / ".introspect" / "steps.last").write_text("not json")
    assert _run(["steps", str(model)], capsys)["changed"] is True
    (model / ".introspect" / "steps.last").write_text("[1, 2]")
    assert steps._read_last(model) == {}


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks")
def test_a_last_file_linking_out_is_neither_read_nor_written(model, tmp_path, capsys):
    target = tmp_path / "elsewhere.json"
    target.write_text(json.dumps({"list": ["x"]}))
    (model / ".introspect").mkdir(exist_ok=True)
    (model / ".introspect" / "steps.last").symlink_to(target)
    out = _run(["steps", str(model)], capsys)
    assert out["changed"] is True
    assert json.loads(target.read_text()) == {"list": ["x"]}


def test_a_write_that_fails_is_harmless(model, capsys, monkeypatch):
    import os
    monkeypatch.setattr(os, "replace", lambda *a: (_ for _ in ()).throw(OSError("disk")))
    out = _run(["steps", str(model)], capsys)
    assert out["changed"] is True
    assert not [p for p in (model / ".introspect").iterdir() if p.name.endswith(".tmp")]


# --- B. sm progress ------------------------------------------------------------------------------


def _log(root: Path, *lines: str) -> None:
    log = root / ".introspect" / "progress.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("".join(f"{ln}\n" for ln in lines))


def test_progress_posts_line_and_list_then_nothing_new(tmp_path, capsys):
    root = tmp_path / "demo"
    _log(root, "columns+grain 3/34")
    steps.mark(root, "build", "in_progress")
    first = _run(["progress", str(root)], capsys)
    assert first["then"] == "Post `line` and update the task list from `list`."
    assert first["list_changed"] is True and first["list"]
    assert first["changed"] is False  # the line's own flag keeps its meaning
    again = _run(["progress", str(root)], capsys)
    assert again["then"] == "Nothing new: call this again."
    assert "list" not in again and again["list_changed"] is False


def test_progress_posts_only_a_new_line(tmp_path, capsys):
    root = tmp_path / "demo"
    # lines with no table count: the list's build count stays as it was
    _log(root, "discovered 34 tables")
    steps.mark(root, "build", "in_progress")
    _run(["progress", str(root)], capsys)
    _log(root, "discovered 34 tables", "building relationships (34 tables)")
    out = _run(["progress", str(root)], capsys)
    assert out["line"] == "Working out the joins"
    assert out["then"] == "Post `line`." and "list" not in out


def test_steps_and_progress_share_the_last_list(tmp_path, capsys):
    root = tmp_path / "demo"
    _log(root, "columns+grain 3/34")
    steps.mark(root, "build", "in_progress")
    _run(["steps", str(root)], capsys)
    out = _run(["progress", str(root)], capsys)
    assert out["list_changed"] is False and out["then"] == "Post `line`."


def test_progress_list_only(tmp_path, capsys):
    root = tmp_path / "demo"
    _log(root, "columns+grain 3/34")
    _run(["progress", str(root)], capsys)
    steps.mark(root, "reports", "skipped")  # a skipped step leaves the list
    out = _run(["progress", str(root)], capsys)
    assert out["list_changed"] is True and out["then"] == UPDATE


def test_a_finished_build_is_not_polled_for_ever(tmp_path, capsys):
    root = tmp_path / "demo"
    _log(root, "done: 34 tables, 7 relationships")
    _run(["progress", str(root)], capsys)
    out = _run(["progress", str(root)], capsys)
    assert out["then"] == "The build has ended: run `sm steps` and go on."


def test_progress_before_the_build_starts(tmp_path, capsys):
    out = _run(["progress", str(tmp_path / "missing")], capsys)
    assert out["started"] is False and "changed" not in out
    assert out["then"] == UPDATE  # nothing remembered for a folder that doesn't exist
