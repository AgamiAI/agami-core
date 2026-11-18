"""`sm init-local` and `sm set-config`: agami writes its own first-run files.

A live onboarding run still stopped for permission on `mkdir -m 700 -p <artifacts_dir>/local` and on
Writes of `.gitignore`, `local/credentials.example` and `local/.config` (outside the working folder).
These two verbs do that work so the calls can be pre-approved; that is safe only because they write
nothing the call supplies as content and act only on agami's own artifacts folder.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import stat
import sys
from pathlib import Path

import pytest

pytest.importorskip("pydantic")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "packages" / "agami-core" / "src"))

from semantic_model import cli  # noqa: E402


@pytest.fixture
def art(tmp_path, monkeypatch) -> Path:
    art = tmp_path / "art"
    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(art))
    return art.resolve() if art.exists() else Path(os.path.realpath(tmp_path)) / "art"


def _run(argv: list[str]) -> tuple[int, dict]:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = cli.main(argv)
    return rc, json.loads(buf.getvalue())


def _mode(p: Path) -> int:
    return stat.S_IMODE(p.stat().st_mode)


@pytest.mark.skipif(os.name == "nt", reason="POSIX modes")
def test_init_local_makes_a_private_local_and_ignores_it(art):
    old = os.umask(0o022)
    try:
        rc, out = _run(["init-local", str(art)])
    finally:
        os.umask(old)
    assert rc == 0, out
    assert _mode(art / "local") == 0o700 and _mode(art / "local" / "tmp") == 0o700
    assert "local/" in (art / ".gitignore").read_text().split()
    assert out["gitignore"] == "added_local"
    rc, out = _run(["init-local", str(art)])  # idempotent
    assert rc == 0 and out["gitignore"] == "had_local"
    assert (art / ".gitignore").read_text().split().count("local/") == 1


def test_an_existing_gitignore_keeps_its_lines(art):
    art.mkdir(parents=True)
    (art / ".gitignore").write_text("*.bak\n")
    _run(["init-local", str(art)])
    lines = (art / ".gitignore").read_text().split()
    assert lines[0] == "*.bak" and "local/" in lines


def test_sample_writes_the_sample_credential_once(art):
    rc, out = _run(["init-local", str(art), "--sample"])
    assert rc == 0 and out["credentials_example"] == "written"
    example = art / "local" / "credentials.example"
    assert example.read_text() == (
        f"[agami-example]\ntype = sqlite\npath = {art / 'local' / 'samples' / 'store.db'}\n"
    )
    example.write_text("[acme]\ntype = postgres\n")  # the person's own template
    rc, out = _run(["init-local", str(art), "--sample"])
    assert out["credentials_example"] == "exists"
    assert example.read_text() == "[acme]\ntype = postgres\n"


@pytest.mark.parametrize("where", ["elsewhere", "relative"])
def test_only_agamis_own_artifacts_folder(art, tmp_path, where):
    target = str(tmp_path / "elsewhere") if where == "elsewhere" else "art"
    for verb in ("init-local", "set-config"):
        rc, out = _run([verb, target])
        assert rc == 2 and "error" in out
    assert not (tmp_path / "elsewhere").exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX modes")
def test_set_config_writes_checked_fields_0600_and_keeps_the_rest(art):
    _run(["init-local", str(art)])
    cfg = art / "local" / ".config"
    cfg.write_text(json.dumps({"personal_data": "enforced_by_warehouse", "tier": "python"}))
    os.chmod(cfg, 0o644)
    rc, out = _run(
        [
            "set-config",
            str(art),
            "--tier",
            "cli",
            "--active-profile",
            "acme",
            "--python3",
            sys.executable
            if Path(sys.executable).name.startswith("python")
            else "/usr/bin/python3",
        ]
    )
    assert rc == 0, out
    doc = json.loads(cfg.read_text())
    assert doc["tier"] == "cli" and doc["active_profile"] == "acme"
    assert doc["personal_data"] == "enforced_by_warehouse"
    assert doc["artifacts_dir"] == str(art) and doc["schema_version"] == 1
    assert _mode(cfg) == 0o600


@pytest.mark.parametrize(
    "argv,why",
    [
        (["--tier", "shell"], "tier"),
        (["--active-profile", "../x"], "profile"),
        (["--active-profile", "local"], "profile"),
        (["--python3", "/bin/sh"], "Python"),
        (["--python3", "python3"], "absolute"),
        (["--python3", "/nonexistent/python3"], "executable"),
        (["--tool", "curl=/usr/bin/curl"], "--tool"),
        (["--tool", "psql=/bin/sh"], "psql"),
    ],
)
def test_set_config_refuses_what_it_cannot_check(art, argv, why):
    _run(["init-local", str(art)])
    rc, out = _run(["set-config", str(art), *argv])
    assert rc == 2 and why in out["error"]
    assert not (art / "local" / ".config").exists()


def test_the_identity_comes_from_a_scratch_file(art):
    _run(["init-local", str(art)])
    ident = art / "local" / "tmp" / "agami-identity.json"
    ident.write_text(json.dumps({"email": "you@example.com", "role": "Data lead"}))
    rc, out = _run(["set-config", str(art), "--identity-file", str(ident)])
    assert rc == 0, out
    doc = json.loads((art / "local" / ".config").read_text())
    assert (doc["reviewer_email"], doc["reviewer_role"]) == ("you@example.com", "Data lead")
    assert "you@example.com" not in json.dumps(out)  # not echoed back


@pytest.mark.parametrize(
    "doc",
    [
        {"email": "not an email", "role": "x"},
        {"email": "you@example.com", "role": "a\nb"},
        {"email": "you@example.com", "role": "x" * 81},
        ["you@example.com"],
    ],
)
def test_a_bad_identity_is_refused(art, doc):
    _run(["init-local", str(art)])
    ident = art / "local" / "tmp" / "agami-identity.json"
    ident.write_text(json.dumps(doc))
    rc, out = _run(["set-config", str(art), "--identity-file", str(ident)])
    assert rc == 2
    assert not (art / "local" / ".config").exists()


def test_an_identity_file_outside_scratch_is_refused(art, tmp_path):
    _run(["init-local", str(art)])
    ident = tmp_path / "agami-identity.json"
    ident.write_text(json.dumps({"email": "you@example.com", "role": "x"}))
    rc, out = _run(["set-config", str(art), "--identity-file", str(ident)])
    assert rc == 2 and "scratch" in out["error"]
