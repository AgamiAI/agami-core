"""`AGAMI_SCRATCH_ONLY=1` on the plugin scripts agami-connect pre-approves.

Those scripts take an output path (`--out`, `--tables-out`, `--ops-out`, the page `serve_page.py`
serves, the log `progress_page.py` reads, the sample builder's `--out`, which it deletes first). With
no prompt in front of them, a call steered by text in the data could otherwise overwrite any file the
person owns. Under the guard each path must be one of agami's scratch files, or the one model file
that script writes, and anything else is refused before a byte is written.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

PLUGIN = Path(__file__).resolve().parent.parent / "plugins" / "agami"
SCRIPTS = PLUGIN / "scripts"
sys.path.insert(0, str(SCRIPTS))

import _scratch_out  # noqa: E402

VICTIM_TEXT = "the person's own file\n"


@pytest.fixture
def art(tmp_path, monkeypatch) -> Path:
    art = tmp_path / "art"
    (art / "local" / "tmp").mkdir(parents=True)
    (art / "acme" / ".introspect").mkdir(parents=True)
    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(art))
    monkeypatch.setenv("AGAMI_SCRATCH_ONLY", "1")
    return art


@pytest.fixture
def victim(tmp_path) -> Path:
    v = tmp_path / "home" / "notes.json"
    v.parent.mkdir()
    v.write_text(VICTIM_TEXT)
    return v


def _run(script: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(script), *args],
        capture_output=True,
        text=True,
        timeout=60,
        env={**os.environ},
    )


def _inv(art: Path) -> str:
    inv = art / "local" / "tmp" / "agami-inventory.json"
    inv.write_text(
        json.dumps(
            {
                "profile": "acme",
                "schemas": ["s"],
                "tables": [
                    {"schema": "s", "table": "t", "columns": [{"name": "id", "type": "int"}]}
                ],
            }
        )
    )
    return str(inv)


# Each pre-approved writer, with its output argument filled from a target path.
def _calls(art: Path):
    s = str(art / "local" / "tmp" / "agami-in.json")
    return {
        "render_table_picker": lambda out: (
            SCRIPTS / "render_table_picker.py",
            ["--inventory", _inv(art), "--save-to-helper", "--out", out],
        ),
        "render_open_questions_page": lambda out: (
            SCRIPTS / "render_open_questions_page.py",
            [
                "--items-file",
                s,
                "--profile",
                "acme",
                "--artifacts-dir",
                str(art),
                "--save-to-helper",
                "--out",
                out,
            ],
        ),
        "render_examples_validation": lambda out: (
            SCRIPTS / "render_examples_validation.py",
            ["--title", "t", "--profile", "acme", "--items-file", s, "--out", out],
        ),
        "render_examples_validation items": lambda out: (
            SCRIPTS / "render_examples_validation.py",
            [
                "--title",
                "t",
                "--profile",
                "acme",
                "--items-file",
                out,
                "--out",
                str(art / "local" / "tmp" / "agami-p.html"),
            ],
        ),
        "render_model_explorer": lambda out: (
            SCRIPTS / "render_model_explorer.py",
            ["--profile", "acme", "--artifacts-dir", str(art), "--out", out],
        ),
        "render_model_explorer manifest": lambda out: (
            SCRIPTS / "render_model_explorer.py",
            [
                "--profile",
                "acme",
                "--artifacts-dir",
                str(art),
                "--out",
                str(art / "local" / "tmp" / "agami-x.html"),
                "--manifest-out",
                out,
            ],
        ),
        "parse_open_questions_decisions": lambda out: (
            SCRIPTS / "parse_open_questions_decisions.py",
            [
                "--decisions-file",
                s,
                "--items-file",
                s,
                "--expect-profile",
                "acme",
                "--artifacts-dir",
                str(art),
                "--ops-out",
                out,
            ],
        ),
        "parse_examples_decisions": lambda out: (
            SCRIPTS / "parse_examples_decisions.py",
            [
                "--decisions-file",
                s,
                "--items-file",
                s,
                "--expect-profile",
                "acme",
                "--out-dir",
                str(Path(out).parent),
            ],
        ),
        "inventory_summary": lambda out: (
            SCRIPTS / "inventory_summary.py",
            ["--inventory", _inv(art), "--keep", "all", "--tables-out", out],
        ),
        "model_spec_workbook": lambda out: (
            SCRIPTS / "model_spec_workbook.py",
            ["parse", "--file", s, "--out", out],
        ),
        "model_spec_workbook tables": lambda out: (
            SCRIPTS / "model_spec_workbook.py",
            [
                "parse",
                "--file",
                s,
                "--out",
                str(art / "local" / "tmp" / "agami-spec.json"),
                "--tables-out",
                out,
            ],
        ),
        "serve_page": lambda out: (SCRIPTS / "serve_page.py", [out, "--minutes", "0"]),
        "serve_page decisions": lambda out: (
            SCRIPTS / "serve_page.py",
            [str(art / "local" / "tmp" / "agami-p.html"), "--decisions", out, "--minutes", "0"],
        ),
        "progress_page": lambda out: (SCRIPTS / "progress_page.py", [out, "--minutes", "0"]),
        "build_sample": lambda out: (
            PLUGIN / "samples" / "store" / "build_sample.py",
            ["--out", out],
        ),
    }


NAMES = list(_calls(Path("/x")))


def _guard_said_no(proc: subprocess.CompletedProcess) -> bool:
    said = proc.stdout + proc.stderr
    return any(
        w in said
        for w in ("scratch", "where this script writes", "is a link", "without ~, . or ..")
    )


# The calls whose target is a page, so a link named like one is otherwise a valid scratch name.
PAGES = {
    "render_table_picker",
    "render_open_questions_page",
    "render_examples_validation",
    "render_model_explorer",
    "serve_page",
}


@pytest.mark.parametrize("name", NAMES)
def test_a_path_outside_agamis_folders_is_refused_and_left_alone(name, art, victim):
    script, args = _calls(art)[name](str(victim))
    proc = _run(script, *args)
    assert proc.returncode == 2 and _guard_said_no(proc), (proc.stdout, proc.stderr)
    assert victim.read_text() == VICTIM_TEXT
    assert list(victim.parent.iterdir()) == [victim]  # no temp file, no rename beside it


@pytest.mark.parametrize("name", NAMES)
def test_a_dotdot_path_out_of_scratch_is_refused(name, art, victim):
    sneaky = f"{art}/local/tmp/../../../home/{victim.name}"
    script, args = _calls(art)[name](sneaky)
    proc = _run(script, *args)
    assert proc.returncode == 2 and _guard_said_no(proc), (proc.stdout, proc.stderr)
    assert victim.read_text() == VICTIM_TEXT


@pytest.mark.parametrize("name", NAMES)
def test_a_link_in_scratch_to_a_users_file_is_refused(name, art, victim):
    ext = ".html" if name in PAGES else ".json"
    link = art / "local" / "tmp" / f"agami-planted{ext}"
    link.symlink_to(victim)
    script, args = _calls(art)[name](str(link))
    proc = _run(script, *args)
    assert proc.returncode == 2 and _guard_said_no(proc), (proc.stdout, proc.stderr)
    assert victim.read_text() == VICTIM_TEXT


def test_the_scratch_folder_itself_a_link_is_refused(tmp_path, monkeypatch, victim):
    art = tmp_path / "art2"
    (art / "local").mkdir(parents=True)
    (art / "local" / "tmp").symlink_to(victim.parent)
    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(art))
    monkeypatch.setenv("AGAMI_SCRATCH_ONLY", "1")
    with pytest.raises(_scratch_out.Refused, match="is a link"):
        _scratch_out.scratch_file(str(art / "local" / "tmp" / "agami-x.json"))


def test_a_hard_link_in_scratch_is_refused(art, victim):
    hard = art / "local" / "tmp" / "agami-hard.json"
    os.link(victim, hard)
    with pytest.raises(_scratch_out.Refused, match="hard link"):
        _scratch_out.scratch_file(str(hard))


def test_a_scratch_page_is_written(art):
    out = art / "local" / "tmp" / "agami-tables-1.html"
    proc = _run(
        SCRIPTS / "render_table_picker.py",
        "--inventory",
        _inv(art),
        "--save-to-helper",
        "--out",
        str(out),
    )
    assert proc.returncode == 0, proc.stderr
    assert "<html" in out.read_text().lower()


def test_the_sample_and_spec_files_are_the_only_model_files_allowed(art):
    assert _scratch_out.model_file(
        str(art / "local" / "samples" / "store.db"), "local", "samples", "store.db"
    )
    path = str(art / "local" / "model-spec" / "acme" / "spec.json")
    assert _scratch_out.model_file(
        path, "local", "model-spec", "<profile>", "spec.json", profile_at=2
    )
    with pytest.raises(_scratch_out.Refused):
        _scratch_out.model_file(
            str(art / "local" / "samples" / "other.db"), "local", "samples", "store.db"
        )
    with pytest.raises(_scratch_out.Refused):
        _scratch_out.model_file(
            str(art / "local" / "model-spec" / "local" / "spec.json"),
            "local",
            "model-spec",
            "<profile>",
            "spec.json",
            profile_at=2,
        )
    log = str(art / "acme" / ".introspect" / "progress.log")
    assert _scratch_out.model_file(log, "<profile>", ".introspect", "progress.log", profile_at=0)


def test_a_link_above_a_missing_folder_is_refused(art, victim):
    # `model-spec` links outside and `acme/` under it does not exist yet: the walk must not stop at
    # the missing folder, or save() would create it, and write, outside the artifacts folder.
    outside = victim.parent / "outside"
    outside.mkdir()
    (art / "local" / "model-spec").symlink_to(outside)
    path = str(art / "local" / "model-spec" / "acme" / "spec.json")
    with pytest.raises(_scratch_out.Refused):
        _scratch_out.model_file(path, "local", "model-spec", "<profile>", "spec.json", profile_at=2)
    assert not (outside / "acme").exists()


def test_examples_go_flat_into_scratch_so_add_example_can_read_them(art):
    sys.path.insert(0, str(SCRIPTS))
    import parse_examples_decisions as PED

    files = PED._write(
        art / "local" / "tmp",
        {"keep": {"sales": [{"question": "q", "sql": "s"}]}, "fix": {}},
        flat=True,
    )
    path = Path(files["keep"]["sales"])
    assert path.parent == art / "local" / "tmp"
    assert _scratch_out.scratch_file(str(path)) == path


def test_without_the_guard_nothing_changes(tmp_path, monkeypatch):
    monkeypatch.delenv("AGAMI_SCRATCH_ONLY", raising=False)
    assert not _scratch_out.guarded()


# --- #699: links are inspected outside in, before anything follows them --------------------------------


def _follows_into(monkeypatch, probe: str) -> list:
    """The calls that follow links, made to note any whose argument leads to `probe` once followed
    (judged with the real `realpath`; on macOS and Linux `//uncprobe` is just a local name)."""
    real_realpath = os.path.realpath
    touched: list = []

    def leads_there(p) -> bool:
        try:
            return probe in str(p) or probe in real_realpath(p)
        except (OSError, ValueError, TypeError):
            return False

    for mod, name in ((os.path, "realpath"), (os.path, "exists"), (os.path, "isdir"), (os, "stat")):
        real = getattr(mod, name)

        def spy(p, *a, _real=real, **k):
            if leads_there(p):
                touched.append(p)
            return _real(p, *a, **k)

        monkeypatch.setattr(mod, name, spy)
    return touched


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks")
def test_a_scratch_folder_linked_to_a_unc_share_is_refused_before_it_is_followed(art, monkeypatch):
    (art / "local" / "tmp").rmdir()
    (art / "local" / "tmp").symlink_to("//uncprobe/share")
    touched = _follows_into(monkeypatch, "uncprobe")
    with pytest.raises(_scratch_out.Refused, match="is a link"):
        _scratch_out.scratch_file(str(art / "local" / "tmp" / "agami-x.json"))
    assert touched == []


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks")
def test_a_model_spec_folder_linked_to_a_unc_share_is_refused_before_it_is_followed(art, monkeypatch):
    (art / "local" / "model-spec").symlink_to("//uncprobe/share")
    touched = _follows_into(monkeypatch, "uncprobe")
    path = str(art / "local" / "model-spec" / "acme" / "spec.json")
    with pytest.raises(_scratch_out.Refused, match="is a link"):
        _scratch_out.model_file(path, "local", "model-spec", "<profile>", "spec.json", profile_at=2)
    assert touched == []


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks")
def test_the_core_scratch_folder_linked_to_a_unc_share_is_refused_before_it_is_followed(art, monkeypatch):
    sys.path.insert(0, str(PLUGIN.parent.parent / "packages" / "agami-core" / "src"))
    from semantic_model import scratch

    (art / "local" / "tmp").rmdir()
    (art / "local" / "tmp").symlink_to("//uncprobe/share")
    touched = _follows_into(monkeypatch, "uncprobe")
    with pytest.raises(scratch.NotScratch, match="is a link"):
        scratch.in_scratch(str(art / "local" / "tmp" / "agami-x.json"))
    assert touched == []


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks")
def test_a_dangling_link_at_samples_is_refused_and_nothing_is_made_where_it_points(art, tmp_path):
    # `exists()` is false for a link whose target is missing, so the old walk took it for a missing
    # folder; save() would then have made the target and written store.db outside the folder.
    target = tmp_path / "elsewhere" / "samples"
    (art / "local" / "samples").symlink_to(target)
    with pytest.raises(_scratch_out.Refused, match="is a link"):
        _scratch_out.model_file(
            str(art / "local" / "samples" / "store.db"), "local", "samples", "store.db"
        )
    assert not target.exists() and not target.parent.exists()


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks")
def test_a_dangling_link_at_the_samples_folder_stops_the_sample_build(art, tmp_path):
    target = tmp_path / "elsewhere" / "samples"
    (art / "local" / "samples").symlink_to(target)
    proc = _run(
        PLUGIN / "samples" / "store" / "build_sample.py",
        "--out",
        str(art / "local" / "samples" / "store.db"),
    )
    assert proc.returncode == 2 and _guard_said_no(proc), (proc.stdout, proc.stderr)
    assert not target.exists()


# --- #699: what a guarded renderer reads ------------------------------------------------------------------

MARKER = "MARKER-NEVER-PRINTED"


def _main(module: str, argv: list[str], monkeypatch) -> int:
    import importlib

    mod = importlib.import_module(module)
    monkeypatch.setattr(sys, "argv", [module, *argv])
    try:
        rc = mod.main()
    except SystemExit as e:  # argparse
        rc = e.code
    return rc


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks")
def test_an_items_file_swapped_for_a_link_after_its_check_is_not_read(art, tmp_path, monkeypatch, capsys):
    """The examples dashboard checks each items file's name, then reads it. A link swapped in between
    must not be followed: the read itself refuses a link."""
    outside = tmp_path / "home" / "outside.json"
    outside.parent.mkdir()
    outside.write_text(json.dumps([{"n": 1, "question": MARKER, "sql": "SELECT 1"}]))
    items = art / "local" / "tmp" / "agami-items.json"
    items.write_text("[]")
    out = art / "local" / "tmp" / "agami-page.html"
    real_check = _scratch_out.scratch_file
    swapped = []

    def check_then_swap(path, exts=_scratch_out.TEXT):
        p = real_check(path, exts)
        if p == items and not swapped:
            items.unlink()
            items.symlink_to(outside)
            swapped.append(p)
        return p

    monkeypatch.setattr(_scratch_out, "scratch_file", check_then_swap)
    rc = _main(
        "render_examples_validation",
        ["--title", "t", "--profile", "acme", "--items-file", str(items), "--out", str(out)],
        monkeypatch,
    )
    said = capsys.readouterr()
    assert swapped and rc != 0
    assert MARKER not in said.out + said.err
    assert not out.exists() or MARKER not in out.read_text()


def _no_resolve_of(monkeypatch, probe: str) -> list:
    touched: list = []
    real_resolve, real_realpath = Path.resolve, os.path.realpath

    def resolve(self, *a, **k):
        if probe in str(self):
            touched.append(str(self))
        return real_resolve(self, *a, **k)

    def realpath(p, *a, **k):
        if probe in str(p):
            touched.append(str(p))
        return real_realpath(p, *a, **k)

    monkeypatch.setattr(Path, "resolve", resolve)
    monkeypatch.setattr(os.path, "realpath", realpath)
    return touched


def test_a_guarded_examples_page_never_resolves_an_items_file_before_reading_it(art, monkeypatch):
    """Copilot on #703: the dedupe key used `realpath`, which would follow a file swapped for a
    network-share link before the no-follow reader refused it. Under the guard it is lexical."""
    items = art / "local" / "tmp" / "agami-itemsprobe.json"
    items.write_text("[]")
    out = art / "local" / "tmp" / "agami-page.html"
    touched = _no_resolve_of(monkeypatch, "itemsprobe")
    _main(
        "render_examples_validation",
        ["--title", "t", "--profile", "acme", "--items-file", str(items), "--out", str(out)],
        monkeypatch,
    )
    assert touched == []


def test_a_guarded_examples_page_refuses_an_items_file_given_twice_in_another_case(
    art, monkeypatch, capsys
):
    """Copilot on #703: the lexical key must still fold case where the filesystem does, or one
    file named twice in two cases gets two sets of numbers. Windows' `normcase`, simulated."""
    tmp = art / "local" / "tmp"
    (tmp / "agami-items.json").write_text("[]")
    out = tmp / "agami-page.html"
    monkeypatch.setattr(os.path, "normcase", lambda p: os.fspath(p).lower())
    rc = _main(
        "render_examples_validation",
        ["--title", "t", "--profile", "acme", "--items-file", str(tmp / "agami-items.json"),
         "--items-file", str(tmp / "agami-ITEMS.json"), "--out", str(out)],
        monkeypatch,
    )
    assert rc != 0 and "given twice" in capsys.readouterr().err


@pytest.mark.parametrize("which", ["--out", "--manifest-out"])
def test_a_guarded_explorer_refuses_a_unc_output_before_resolving_it(art, monkeypatch, capsys, which):
    unc = "//uncprobe/share/agami-x." + ("html" if which == "--out" else "json")
    argv = ["--profile", "acme", "--artifacts-dir", str(art), "--out",
            str(art / "local" / "tmp" / "agami-x.html")]
    if which == "--out":
        argv[-1] = unc
    else:
        argv += ["--manifest-out", unc]
    touched = _no_resolve_of(monkeypatch, "uncprobe")
    rc = _main("render_model_explorer", argv, monkeypatch)
    assert rc == 2 and "refused" in capsys.readouterr().err
    assert touched == []


def _signed(art: Path) -> None:
    (art / "local" / ".config").write_text(
        json.dumps({"reviewer_email": "you@example.com", "reviewer_role": "analyst"})
    )


def test_a_guarded_open_questions_page_reads_its_items_only_from_scratch(art, victim, capsys, monkeypatch):
    _signed(art)
    victim.write_text(json.dumps({"questions": [{"id": "q1", "question": MARKER}]}))
    out = art / "local" / "tmp" / "agami-oq.html"
    rc = _main(
        "render_open_questions_page",
        ["--items-file", str(victim), "--profile", "acme", "--artifacts-dir", str(art),
         "--save-to-helper", "--out", str(out)],
        monkeypatch,
    )
    said = capsys.readouterr()
    assert rc == 2 and "refused" in said.err, said
    assert MARKER not in said.out + said.err and not out.exists()


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks")
def test_a_guarded_open_questions_page_does_not_follow_a_linked_items_file(art, victim, capsys, monkeypatch):
    _signed(art)
    victim.write_text(json.dumps({"questions": [{"id": "q1", "question": MARKER}]}))
    link = art / "local" / "tmp" / "agami-oq.json"
    link.symlink_to(victim)
    out = art / "local" / "tmp" / "agami-oq.html"
    rc = _main(
        "render_open_questions_page",
        ["--items-file", str(link), "--profile", "acme", "--artifacts-dir", str(art),
         "--save-to-helper", "--out", str(out)],
        monkeypatch,
    )
    said = capsys.readouterr()
    assert rc == 2 and MARKER not in said.out + said.err and not out.exists()


def _inventory_doc() -> str:
    return json.dumps(
        {"profile": "acme", "schemas": ["s"],
         "tables": [{"schema": "s", "table": MARKER, "columns": [{"name": "id", "type": "int"}]}]}
    )


def test_a_guarded_table_picker_reads_its_inventory_only_from_scratch_or_the_build(art, victim, capsys, monkeypatch):
    victim.write_text(_inventory_doc())
    out = art / "local" / "tmp" / "agami-picker.html"
    rc = _main(
        "render_table_picker",
        ["--inventory", str(victim), "--save-to-helper", "--out", str(out)],
        monkeypatch,
    )
    said = capsys.readouterr()
    assert rc == 2 and "refused" in said.err, said
    assert MARKER not in said.out + said.err and not out.exists()


def test_a_guarded_table_picker_takes_the_builds_own_inventory(art, capsys, monkeypatch):
    inv = art / "acme" / ".introspect" / "inventory.json"
    inv.write_text(_inventory_doc())
    out = art / "local" / "tmp" / "agami-picker.html"
    rc = _main(
        "render_table_picker",
        ["--inventory", str(inv), "--save-to-helper", "--out", str(out)],
        monkeypatch,
    )
    assert rc == 0, capsys.readouterr()
    assert MARKER in out.read_text()


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks")
def test_a_guarded_table_picker_does_not_follow_a_linked_inventory(art, victim, capsys, monkeypatch):
    victim.write_text(_inventory_doc())
    inv = art / "acme" / ".introspect" / "inventory.json"
    inv.symlink_to(victim)
    out = art / "local" / "tmp" / "agami-picker.html"
    rc = _main(
        "render_table_picker",
        ["--inventory", str(inv), "--save-to-helper", "--out", str(out)],
        monkeypatch,
    )
    said = capsys.readouterr()
    assert rc == 2 and MARKER not in said.out + said.err and not out.exists()
