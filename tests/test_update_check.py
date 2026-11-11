"""The once-a-day note that a newer agami is published (#633).

`update_check.py` asks PyPI for the newest agami-core through `pip index versions` and prints a note
only when it is newer than the plugin. pip is mocked here, so no test touches the network.
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agami" / "scripts"))

import update_check as UC  # noqa: E402

DAY = date(2026, 11, 6)


@pytest.fixture
def art(tmp_path, monkeypatch):
    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(tmp_path))
    (tmp_path / "local").mkdir()
    monkeypatch.setattr(UC, "plugin_version", lambda: "0.9.22")
    return tmp_path


def _pip(monkeypatch, stdout="", returncode=0, raises=None):
    calls = []

    def run(cmd, **kw):
        calls.append(cmd)
        if raises:
            raise raises
        return subprocess.CompletedProcess(cmd, returncode, stdout=stdout, stderr="")

    monkeypatch.setattr(UC.subprocess, "run", run)
    return calls


def _listing(newest: str) -> str:
    return f"agami-core ({newest})\nAvailable versions: {newest}, 0.9.22, 0.9.21\n"


def test_a_newer_release_prints_the_versions_the_changelog_and_the_update(art, monkeypatch, capsys):
    calls = _pip(monkeypatch, _listing("0.9.25"))
    assert UC.main(DAY) == 0
    out = capsys.readouterr().out
    assert "agami 0.9.25 is available (you have 0.9.22)." in out
    assert "https://github.com/AgamiAI/agami-core/blob/v0.9.25/CHANGELOG.md" in out
    assert "/plugin update agami-core@agami" in out
    assert calls[0][:6] == [sys.executable, "-m", "pip", "index", "versions", "agami-core"]


def test_verbose_pip_output_still_finds_the_version(art, monkeypatch, capsys):
    _pip(monkeypatch, "Created temporary directory: /x\nLooking in indexes: https://pypi.org/simple\n"
         + _listing("0.9.25"))
    UC.main(DAY)
    assert "agami 0.9.25 is available" in capsys.readouterr().out


@pytest.mark.parametrize("newest", ["0.9.22", "0.9.21", "0.9.22rc1"])
def test_the_same_or_an_older_release_prints_nothing(art, monkeypatch, capsys, newest):
    _pip(monkeypatch, _listing(newest))
    UC.main(DAY)
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("pip", [
    {"raises": FileNotFoundError()},
    {"raises": subprocess.TimeoutExpired("pip", 10)},
    {"returncode": 1, "stdout": ""},                       # no pip, or a pip without `index`
    {"stdout": "ERROR: No matching distribution found"},   # offline or a mirror without it
])
def test_pip_failing_prints_nothing_and_still_succeeds(art, monkeypatch, capsys, pip):
    _pip(monkeypatch, **pip)
    assert UC.main(DAY) == 0
    assert capsys.readouterr().out == ""


def test_it_checks_at_most_once_a_day(art, monkeypatch, capsys):
    calls = _pip(monkeypatch, _listing("0.9.25"))
    UC.main(DAY)
    UC.main(DAY)
    assert len(calls) == 1
    assert json.loads((art / "local" / UC.STATE_FILE).read_text()) == {"checked_on": "2026-11-06"}
    UC.main(date(2026, 11, 7))
    assert len(calls) == 2
    assert capsys.readouterr().out.count("is available") == 2


def test_a_failed_check_also_counts_for_the_day(art, monkeypatch):
    calls = _pip(monkeypatch, raises=FileNotFoundError())
    UC.main(DAY)
    UC.main(DAY)
    assert len(calls) == 1


def test_it_never_creates_the_artifacts_folder(tmp_path, monkeypatch, capsys):
    # It runs before first-time setup has chosen where the artifacts folder goes.
    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(tmp_path / "not-chosen"))
    monkeypatch.setattr(UC, "plugin_version", lambda: "0.9.22")
    _pip(monkeypatch, _listing("0.9.25"))
    UC.main(DAY)
    assert not (tmp_path / "not-chosen").exists()
    assert "is available" in capsys.readouterr().out


def test_an_unknown_plugin_version_skips_the_check(art, monkeypatch, capsys):
    monkeypatch.setattr(UC, "plugin_version", lambda: None)
    calls = _pip(monkeypatch, _listing("0.9.25"))
    UC.main(DAY)
    assert calls == [] and capsys.readouterr().out == ""


def test_a_corrupt_state_file_is_treated_as_not_checked(art, monkeypatch):
    (art / "local" / UC.STATE_FILE).write_text("{not json")
    calls = _pip(monkeypatch, _listing("0.9.22"))
    UC.main(DAY)
    assert len(calls) == 1
