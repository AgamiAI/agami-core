"""`sm wait-files` blocks on background subagents' result files without ending the turn.

The skill runs enrichers in the background while the person answers cards, then waits on their
result files with this pre-approved call: a turn that ends loses the skill's pre-approvals.
"""

from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path

import pytest

pytest.importorskip("pydantic")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "packages" / "agami-core" / "src"))

from semantic_model import cli  # noqa: E402


def _wait(capsys, *argv: str) -> dict:
    assert cli.main(["wait-files", *argv]) == 0  # exit 0 whether or not everything is ready
    return json.loads(capsys.readouterr().out)


def test_all_present_returns_at_once(tmp_path, capsys):
    a, b = tmp_path / "a.json", tmp_path / "b.json"
    a.write_text("{}")
    b.write_text("{}")
    started = time.monotonic()
    out = _wait(capsys, "--file", str(a), "--file", str(b), "--wait", "30")
    assert time.monotonic() - started < 2
    assert out == {"ready": [str(a), str(b)], "waiting": [], "all_ready": True}


def test_wait_zero_checks_once(tmp_path, capsys):
    a, b = tmp_path / "a.json", tmp_path / "b.json"
    a.write_text("{}")
    out = _wait(capsys, "--file", str(a), "--file", str(b))
    assert out == {"ready": [str(a)], "waiting": [str(b)], "all_ready": False}


def test_an_empty_file_is_still_waiting(tmp_path, capsys):
    # a subagent's Write can land in two steps; an empty file isn't a result yet
    a = tmp_path / "a.json"
    a.write_text("")
    out = _wait(capsys, "--file", str(a), "--wait", "0")
    assert out == {"ready": [], "waiting": [str(a)], "all_ready": False}


def test_returns_as_soon_as_a_late_file_lands(tmp_path, capsys):
    a = tmp_path / "late.json"
    threading.Timer(0.5, lambda: a.write_text('{"area": "demo"}')).start()
    started = time.monotonic()
    out = _wait(capsys, "--file", str(a), "--wait", "5")
    assert time.monotonic() - started < 3
    assert out["all_ready"] is True and out["ready"] == [str(a)]


def test_paths_are_expanded(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "r.json").write_text("{}")
    out = _wait(capsys, "--file", "~/r.json")
    assert out["ready"] == [str(tmp_path / "r.json")]


def test_wait_is_clamped_to_ten_minutes():
    args = cli._parse_args(["wait-files", "--file", "x", "--wait", "9999"])
    assert cli.wait_files_seconds(args.wait) == cli.WAIT_FILES_MAX == 600
    assert cli.wait_files_seconds(-3) == 0
    assert cli._parse_args(["wait-files", "--file", "x"]).wait == 0
