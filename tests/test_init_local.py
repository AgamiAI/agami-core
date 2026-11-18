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


@pytest.mark.skipif(os.name == "nt", reason="symlinks")
def test_a_link_planted_at_the_config_is_never_written_through(art, tmp_path):
    _run(["init-local", str(art)])
    victim = tmp_path / "victim.txt"
    victim.write_text("the person's own file\n")
    (art / "local" / ".config").symlink_to(victim)
    rc, out = _run(["set-config", str(art), "--tier", "cli"])
    assert rc == 2 and "link" in out["error"]
    assert victim.read_text() == "the person's own file\n"


@pytest.mark.skipif(os.name == "nt", reason="symlinks")
def test_a_linked_local_folder_is_refused(art, tmp_path):
    art.mkdir(parents=True)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (art / "local").symlink_to(elsewhere)
    rc, out = _run(["set-config", str(art), "--tier", "cli"])
    assert rc == 2 and "link" in out["error"]
    assert not (elsewhere / ".config").exists()


PLUGIN = Path(__file__).resolve().parent.parent / "plugins" / "agami"

# A Python whose agami-core is missing and whose pip would install: every pip call leaves a mark.
_SHIM_MISSING_WITH_PIP = """#!/bin/sh
case "$1 $2" in
  "-m pip") [ "$3" = "--version" ] && exit 0; echo "$*" >> "$PIP_MARK"; exit 0 ;;
  "-c "*) exit 1 ;;
esac
exit 0
"""


@pytest.mark.skipif(os.name == "nt", reason="POSIX shell shim")
@pytest.mark.parametrize(
    "argv", [["choose-folder"], ["init-local"], ["init-local", "--sample"], ["set-config"]]
)
def test_setting_up_the_folder_never_installs(art, tmp_path, argv):
    """Onboarding sets the folder up before the person has agreed to install agami's library, so
    these verbs must stop with the install step (exit 12) instead of running pip themselves."""
    import shutil
    import subprocess

    cache = tmp_path / "agami-core" / "0.0.1"  # a versioned cache folder: no dev checkout beside it
    shutil.copytree(PLUGIN / "scripts", cache / "scripts")
    shim = tmp_path / "pyshim"
    shim.write_text(_SHIM_MISSING_WITH_PIP)
    shim.chmod(0o755)
    mark = tmp_path / "pip-calls"
    env = {**os.environ, "AGAMI_PYTHON": str(shim), "PIP_MARK": str(mark)}
    verb, *rest = argv
    r = subprocess.run(
        ["bash", str(cache / "scripts" / "sm"), verb, str(art), *rest],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert r.returncode == 12, r.stderr
    assert not mark.exists(), mark.read_text()
    assert 'sm" install' in r.stderr


@pytest.fixture
def first_run(tmp_path, monkeypatch) -> Path:
    """A brand-new install: no AGAMI_ARTIFACTS_DIR, no saved pointer, nothing at the default."""
    import agami_paths

    monkeypatch.delenv("AGAMI_ARTIFACTS_DIR", raising=False)
    home = Path(os.path.realpath(tmp_path))
    monkeypatch.setattr(agami_paths, "POINTER_PATH", home / ".config" / "agami" / "path")
    monkeypatch.setattr(agami_paths, "DEFAULT_ARTIFACTS_DIR", home / "agami-artifacts")
    return home


def test_the_first_run_with_a_folder_of_its_own_goes_through_the_verbs(first_run):
    """agami-connect's first run, in its order: 0a.6 picks a folder other than the default and
    `choose-folder` saves it, then 0a.1's `init-local` and 0a.7's `set-config` take that folder."""
    import agami_paths

    chosen = first_run / "code" / "acme-data" / "agami"
    rc, out = _run(["choose-folder", str(chosen)])
    assert rc == 0, out
    assert out["artifacts_dir"] == str(chosen)
    assert agami_paths.POINTER_PATH.read_text().strip() == str(chosen)
    rc, out = _run(["init-local", str(chosen)])
    assert rc == 0, out
    assert (chosen / "local" / "tmp").is_dir()
    rc, out = _run(["set-config", str(chosen), "--tier", "python", "--active-profile", "acme"])
    assert rc == 0, out
    doc = json.loads((chosen / "local" / ".config").read_text())
    assert doc["artifacts_dir"] == str(chosen) and doc["active_profile"] == "acme"
    assert not (first_run / "agami-artifacts").exists()  # no stray default folder


@pytest.mark.parametrize("where", ["relative", "a file", "inside local"])
def test_choose_folder_refuses_a_folder_it_cannot_use(first_run, where):
    import agami_paths

    (first_run / "agami-artifacts" / "local").mkdir(parents=True)
    (first_run / "notes.txt").write_text("x")
    target = {
        "relative": "agami",
        "a file": str(first_run / "notes.txt"),
        "inside local": str(first_run / "agami-artifacts" / "local" / "x"),
    }[where]
    rc, out = _run(["choose-folder", target])
    assert rc == 2 and "error" in out
    assert not agami_paths.POINTER_PATH.exists()


def test_a_new_folder_under_the_env_override_says_how_to_switch(first_run, monkeypatch):
    """Connecting a different company picks a new folder, but AGAMI_ARTIFACTS_DIR outranks the
    saved pointer, so saving it would change nothing: refuse before writing, and say what to change."""
    import agami_paths

    current = first_run / "acme"
    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(current))
    new = first_run / "demo"
    rc, out = _run(["choose-folder", str(new)])
    assert rc == 2
    assert "AGAMI_ARTIFACTS_DIR" in out["error"] and str(new) in out["error"]
    assert "restart" in out["error"]
    assert not agami_paths.POINTER_PATH.exists()
    rc, out = _run(["init-local", str(new)])
    assert rc == 2 and "AGAMI_ARTIFACTS_DIR" in out["error"] and "restart" in out["error"]
    assert not new.exists()
    rc, out = _run(["choose-folder", str(current)])  # the folder it already names is fine
    assert rc == 0, out


@pytest.mark.skipif(os.name == "nt", reason="symlinks")
@pytest.mark.parametrize("linked", ["local", "local/tmp"])
@pytest.mark.parametrize("sample", [False, True])
def test_init_local_writes_nothing_through_a_linked_folder(art, tmp_path, linked, sample):
    """A linked `local/` (or `local/tmp`) would put the scratch folder and the credential template
    wherever it points, outside the artifacts folder and outside its 0700 `local/`."""
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (art / "local").parent.mkdir(parents=True, exist_ok=True)
    if linked == "local":
        (art / "local").symlink_to(elsewhere)
    else:
        (art / "local").mkdir(mode=0o700)
        (art / "local" / "tmp").symlink_to(elsewhere)
    rc, out = _run(["init-local", str(art), *(["--sample"] if sample else [])])
    assert rc == 2 and "link" in out["error"]
    assert list(elsewhere.iterdir()) == []
    assert not (art / ".gitignore").exists()  # refused before any write


@pytest.mark.skipif(os.name == "nt", reason="symlinks")
def test_the_launchers_scratch_folder_is_never_made_through_a_linked_local(art, tmp_path):
    """`_python.sh` makes `local/tmp` before every `sm` and `py` call; through a linked `local` that
    would create a folder wherever the link points."""
    import subprocess

    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    art.mkdir(parents=True, exist_ok=True)
    (art / "local").symlink_to(elsewhere)
    script = f'. "{PLUGIN / "scripts" / "_python.sh"}"; agami_scratch_dir'
    env = {**os.environ, "AGAMI_ARTIFACTS_DIR": str(art)}
    subprocess.run(["bash", "-c", script], env=env, check=True, capture_output=True, timeout=30)
    assert list(elsewhere.iterdir()) == []
