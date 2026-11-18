"""`AGAMI_SCRATCH_ONLY=1`: every file a call names is one of agami's scratch files.

agami-connect pre-approves `curate`, `add`, `add-example`, `seed-examples`, `set-terminology`,
`introspect` and `discover` only with this prefix. Without it, a call steered by text in the data could
read `~/.aws/credentials` as an ops file (its parse error, or a validator message, then carries the
contents into the chat). With it, each named input is a scratch file (or the build's own
`join_candidates.json`) and each named output a scratch file or a model's datasource.md.
"""

from __future__ import annotations

import contextlib
import io
import json
import shutil
import sys
from pathlib import Path

import pytest

pytest.importorskip("pydantic")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "packages" / "agami-core" / "src"))

from semantic_model import cli  # noqa: E402

from skill_docs import PLUGIN  # noqa: E402

SAMPLE = PLUGIN / "samples" / "store" / "model"


@pytest.fixture
def art(tmp_path, monkeypatch) -> Path:
    art = tmp_path / "art"
    (art / "local" / "tmp").mkdir(parents=True)
    shutil.copytree(SAMPLE, art / "acme")
    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(art))
    monkeypatch.setenv("AGAMI_SCRATCH_ONLY", "1")
    return art


def _run(argv: list[str]) -> tuple[int, dict]:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        try:
            rc = cli.main(argv)
        except SystemExit as exc:
            rc = exc.code
    text = buf.getvalue().strip()
    return rc, (json.loads(text) if text.startswith("{") else {"raw": text})


def test_an_ops_file_outside_scratch_is_refused_and_not_read(art, tmp_path):
    secret = tmp_path / "credentials.json"
    secret.write_text('{"password": "not-a-real-one"}')
    rc, out = _run(["curate", str(art / "acme"), "--ops-file", str(secret)])
    assert rc == 2
    assert "not-a-real-one" not in json.dumps(out)
    assert "scratch" in out["error"]


def test_an_ops_file_in_scratch_is_read(art):
    ops = art / "local" / "tmp" / "agami-ops.json"
    ops.write_text("[]")
    rc, out = _run(["curate", str(art / "acme"), "--ops-file", str(ops)])
    assert "scratch" not in json.dumps(out)


def test_the_builds_own_join_candidates_are_accepted(art):
    cands = art / "acme" / ".introspect" / "join_candidates.json"
    cands.parent.mkdir(parents=True, exist_ok=True)
    cands.write_text("[]")
    rc, out = _run(["curate", str(art / "acme"), "--ops-file", str(cands), "--candidate", "x"])
    assert "not one of agami's scratch files" not in json.dumps(out)


def test_another_file_under_introspect_is_refused(art):
    other = art / "acme" / ".introspect" / "other.json"
    other.parent.mkdir(parents=True, exist_ok=True)
    other.write_text("[]")
    rc, out = _run(["curate", str(art / "acme"), "--ops-file", str(other)])
    assert rc == 2 and "scratch" in out["error"]


@pytest.mark.parametrize(
    "argv",
    [
        ["add", "ROOT", "--kind", "metric", "--area", "agami-example", "--file", "OUTSIDE"],
        ["seed-examples", "ROOT", "--profile", "acme", "--file", "OUTSIDE"],
        ["set-terminology", "ROOT", "--file", "OUTSIDE"],
        [
            "introspect",
            "--profile",
            "acme",
            "--db-type",
            "sqlite",
            "--artifacts",
            "ART",
            "--tables-file",
            "OUTSIDE",
        ],
        [
            "introspect",
            "--profile",
            "acme",
            "--db-type",
            "sqlite",
            "--artifacts",
            "ART",
            "--progress",
            "OUTSIDE",
        ],
        [
            "discover",
            "--profile",
            "acme",
            "--db-type",
            "sqlite",
            "--artifacts",
            "ART",
            "--out",
            "OUTSIDE",
        ],
    ],
)
def test_every_named_file_is_held_to_scratch(art, tmp_path, argv):
    outside = tmp_path / "outside.json"
    outside.write_text("[]")
    filled = [
        {"ROOT": str(art / "acme"), "ART": str(art), "OUTSIDE": str(outside)}.get(a, a)
        for a in argv
    ]
    rc, out = _run(filled)
    assert rc == 2
    assert "scratch" in out["error"] or "datasource.md" in out["error"]


def test_without_the_guard_a_person_can_name_any_file(art, tmp_path, monkeypatch):
    monkeypatch.delenv("AGAMI_SCRATCH_ONLY")
    ops = tmp_path / "my-ops.json"
    ops.write_text("[]")
    rc, out = _run(["curate", str(art / "acme"), "--ops-file", str(ops)])
    assert "scratch" not in json.dumps(out)
