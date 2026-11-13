"""The engine records the two `say`s that must be shown before a card (the closing summary and the
examples table) in `<artifacts_dir>/local/tmp/agami-say-pending.json`, for the plugin's
AskUserQuestion hook. Recording is best effort: when it can't be done, the command prints what it
printed before, less the `say_id`.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

pytest.importorskip("pydantic")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "packages" / "agami-core" / "src"))

from semantic_model import cli, say_pending, steps  # noqa: E402

from skill_docs import PLUGIN  # noqa: E402

SAMPLE = PLUGIN / "samples" / "store" / "model"
CLOSING = ("Post `say` exactly as written, then update the task list from `list`, then ask the "
           "closing card in that same message.")
GATED = " The card is refused until this text has been posted."


@pytest.fixture
def art(tmp_path: Path, monkeypatch) -> Path:
    root = tmp_path / "artifacts"
    shutil.copytree(SAMPLE, root / "acme")
    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(root))
    return root


@pytest.fixture
def fake_db(monkeypatch):
    import subprocess

    def fake_run(cmd, capture_output, text, env, encoding=None):
        return subprocess.CompletedProcess(cmd, 0, "label,total\nalpha,1234\n", "")

    monkeypatch.setattr(subprocess, "run", fake_run)


def _run(argv: list[str], capsys) -> dict:
    assert cli.main(argv) == 0
    return json.loads(capsys.readouterr().out)


def _pending(art: Path) -> list[dict]:
    return json.loads((art / "local" / "tmp" / say_pending.FILE_NAME).read_text(encoding="utf-8"))


def _close(art: Path, capsys) -> dict:
    steps.mark(art / "acme", "examples", "done")
    return _run(["steps", str(art / "acme"), "--mark", "first_question=in_progress"], capsys)


# --- registration by the two commands ----------------------------------------------------------------


def test_the_closing_mark_registers_its_summary(art, capsys):
    out = _close(art, capsys)
    assert out["then"] == CLOSING + GATED
    (entry,) = _pending(art)
    assert entry["id"] == out["say_id"] and len(entry["id"]) == 8
    int(entry["id"], 16)
    assert entry["kind"] == "summary" and entry["say"] == out["say"] and entry["refused"] == 0
    assert entry["needle"] == out["say"].splitlines()[0].strip()
    assert datetime.fromisoformat(entry["created_at"]).tzinfo is not None
    assert list(out).index("say_id") == list(out).index("say") + 1  # beside `say`


def test_a_second_closing_mark_replaces_the_first(art, capsys):
    first = _close(art, capsys)
    second = _run(["steps", str(art / "acme"), "--mark", "first_question=in_progress"], capsys)
    assert second["say_id"] != first["say_id"]
    assert [e["id"] for e in _pending(art)] == [second["say_id"]]


def test_other_steps_calls_register_nothing(art, capsys):
    out = _run(["steps", str(art / "acme"), "--mark", "describe=in_progress"], capsys)
    assert "say_id" not in out
    assert not (art / "local" / "tmp" / say_pending.FILE_NAME).exists()


def test_seed_validate_out_registers_the_examples_table(art, capsys, fake_db):
    target = art / "local" / "tmp" / "agami-examples.json"
    target.parent.mkdir(parents=True)
    out = _run(["seed-validate", str(art / "acme"), "--profile", "acme", "--out", str(target)], capsys)
    assert out["then"] == "Post `say`, then ask the examples card." + GATED
    (entry,) = _pending(art)
    assert (entry["id"], entry["kind"], entry["say"]) == (out["say_id"], "examples", out["say"])
    assert entry["needle"] == "| # | Example | Rows | Result |"


def test_no_card_registers_nothing(art, capsys, fake_db):
    target = art / "local" / "tmp" / "agami-examples.json"
    target.parent.mkdir(parents=True)
    out = _run(["seed-validate", str(art / "acme"), "--profile", "acme", "--out", str(target),
                "--no-card"], capsys)
    assert out["then"] == "Post `say`." and "say_id" not in out
    assert not (art / "local" / "tmp" / say_pending.FILE_NAME).exists()


def test_examples_then_summary_are_both_kept_in_order(art, capsys, fake_db):
    target = art / "local" / "tmp" / "agami-examples.json"
    target.parent.mkdir(parents=True)
    ex = _run(["seed-validate", str(art / "acme"), "--profile", "acme", "--out", str(target)], capsys)
    summ = _close(art, capsys)
    assert [(e["kind"], e["id"]) for e in _pending(art)] == [("examples", ex["say_id"]),
                                                              ("summary", summ["say_id"])]


# --- when it can't be recorded, nothing else changes -----------------------------------------------


def _without_registration(art: Path, capsys, monkeypatch) -> dict:
    monkeypatch.setattr(say_pending, "register", lambda *a, **k: None)
    out = _close(art, capsys)
    monkeypatch.undo()
    return out


def test_no_artifacts_folder_leaves_the_output_as_it_was(tmp_path, capsys, monkeypatch):
    root = tmp_path / "acme"
    shutil.copytree(SAMPLE, root)
    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(tmp_path / "missing"))
    steps.mark(root, "examples", "done")
    out = _run(["steps", str(root), "--mark", "first_question=in_progress"], capsys)
    assert out["then"] == CLOSING and "say_id" not in out
    assert not (tmp_path / "missing").exists()


@pytest.mark.skipif(sys.platform == "win32" or getattr(os, "geteuid", lambda: 1)() == 0,
                    reason="permissions")
def test_an_unwritable_scratch_folder_changes_only_the_registration(art, capsys):
    tmp = art / "local" / "tmp"
    tmp.mkdir(parents=True)
    tmp.chmod(0o500)
    try:
        steps.mark(art / "acme", "examples", "done")
        rc = cli.main(["steps", str(art / "acme"), "--mark", "first_question=in_progress"])
        out = json.loads(capsys.readouterr().out)
    finally:
        tmp.chmod(0o700)
    assert rc == 0 and out["then"] == CLOSING and "say_id" not in out and out["say"]
    assert list(tmp.iterdir()) == []


@pytest.mark.skipif(not hasattr(os, "symlink") or sys.platform == "win32", reason="symlinks")
def test_a_scratch_folder_that_is_a_link_is_not_written_through(art, tmp_path, capsys):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (art / "local").mkdir()
    (art / "local" / "tmp").symlink_to(elsewhere)
    out = _close(art, capsys)
    assert out["then"] == CLOSING and "say_id" not in out
    assert list(elsewhere.iterdir()) == []


@pytest.mark.skipif(not hasattr(os, "symlink") or sys.platform == "win32", reason="symlinks")
def test_a_pending_file_that_is_a_link_is_not_written_through(art, tmp_path, capsys):
    target = tmp_path / "target.json"
    target.write_text("[]")
    (art / "local" / "tmp").mkdir(parents=True)
    (art / "local" / "tmp" / say_pending.FILE_NAME).symlink_to(target)
    out = _close(art, capsys)
    assert "say_id" not in out and target.read_text() == "[]"


def test_a_damaged_pending_file_is_replaced(art, capsys):
    (art / "local" / "tmp").mkdir(parents=True)
    (art / "local" / "tmp" / say_pending.FILE_NAME).write_text("{not json")
    out = _close(art, capsys)
    assert [e["id"] for e in _pending(art)] == [out["say_id"]]


def test_a_register_that_fails_leaves_the_output_as_it_was(art, capsys, monkeypatch):
    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(say_pending, "save", boom)
    out = _close(art, capsys)
    assert out["then"] == CLOSING and "say_id" not in out


# --- the module itself -------------------------------------------------------------------------------


@pytest.mark.parametrize("say, needle", [
    ("**acme: 3 tables.**\n\nmore", "**acme: 3 tables.**"),
    ("\n\n   \n  first  \nsecond", "first"),
    ("x" * 300, "x" * 120),
    ("", ""),
    ("  \n \n", ""),
])
def test_the_needle_is_the_first_non_empty_line(say, needle):
    assert say_pending.needle_of(say) == needle


def test_register_refuses_an_empty_say_or_unknown_kind(tmp_path):
    assert say_pending.register(tmp_path, "summary", "  \n") is None
    assert say_pending.register(tmp_path, "other", "text") is None
    assert not (tmp_path / "local").exists()


def test_register_stamps_the_time_in_utc(tmp_path):
    when = datetime(2026, 11, 13, 9, 30, 15, 999, tzinfo=timezone.utc)
    sid = say_pending.register(tmp_path, "summary", "hello", now=when)
    (entry,) = say_pending.load(say_pending.pending_path(tmp_path))
    assert entry == {"id": sid, "kind": "summary", "needle": "hello", "say": "hello",
                     "created_at": "2026-11-13T09:30:15+00:00", "refused": 0}


@pytest.mark.parametrize("text", ["{}", "[1]", '"x"', "null"])
def test_load_rejects_anything_but_a_list_of_entries(tmp_path, text):
    path = tmp_path / "p.json"
    path.write_text(text)
    with pytest.raises(ValueError):
        say_pending.load(path)


def test_load_of_a_missing_file_is_empty(tmp_path):
    assert say_pending.load(tmp_path / "none.json") == []
