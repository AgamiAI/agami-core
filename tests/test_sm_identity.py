"""`sm identity` says who sign-offs are recorded under, and an email this computer already knows.

A live run asked "Which email should sign-offs be recorded under? Type it in Other" with no text field
and only "Don't sign anything yet" to press. A card can't have a field, so the email git already
holds on this computer (read locally, no network) is offered as the first option instead; the person
still picks it. A saved identity means no card at all.
"""

from __future__ import annotations

import contextlib
import io
import json
import subprocess
from pathlib import Path

import pytest

pytest.importorskip("pydantic")

from semantic_model import cli  # noqa: E402


def _run(art: Path) -> dict:
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        assert cli.main(["identity", str(art)]) == 0
    return json.loads(out.getvalue())


def _git_email(monkeypatch, value: str | None) -> list:
    calls = []

    def fake(cmd, **kw):
        calls.append(cmd)
        if value is None:
            raise FileNotFoundError("git")
        return subprocess.CompletedProcess(cmd, 0, stdout=value + "\n", stderr="")

    monkeypatch.setattr(cli.subprocess, "run", fake)
    return calls


def test_a_saved_identity_is_used_and_git_is_not_read(tmp_path, monkeypatch):
    (tmp_path / "local").mkdir()
    (tmp_path / "local" / ".config").write_text(
        json.dumps({"reviewer_email": "you@example.com", "reviewer_role": "Data Lead", "tier": "cli"}))
    calls = _git_email(monkeypatch, "other@example.com")
    assert _run(tmp_path) == {"reviewer_email": "you@example.com", "reviewer_role": "Data Lead",
                              "suggested_email": None}
    assert calls == []


def test_with_no_saved_email_git_email_is_suggested(tmp_path, monkeypatch):
    calls = _git_email(monkeypatch, "you@example.com")
    assert _run(tmp_path) == {"reviewer_email": None, "reviewer_role": None,
                              "suggested_email": "you@example.com"}
    assert calls and calls[0][:3] == ["git", "config", "--get"]


@pytest.mark.parametrize("value", [None, "", "not an email", "a@b"])
def test_no_usable_git_email_suggests_nothing(tmp_path, monkeypatch, value):
    _git_email(monkeypatch, value)
    assert _run(tmp_path)["suggested_email"] is None


@pytest.mark.parametrize("config", ["not json", "[1]", '{"reviewer_email": ""}'])
def test_a_config_without_an_identity_reads_as_none(tmp_path, monkeypatch, config):
    (tmp_path / "local").mkdir()
    (tmp_path / "local" / ".config").write_text(config)
    _git_email(monkeypatch, None)
    assert _run(tmp_path) == {"reviewer_email": None, "reviewer_role": None, "suggested_email": None}
