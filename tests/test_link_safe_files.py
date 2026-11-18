"""The files agami reads and writes by name are never reached through a link or a second name.

A scratch file the guard lets a call read, an output it lets a call write, and the model's own logs
an approved `sm` call reads or appends to: each is opened so that a symlink planted at the name, or
a hard link to a file elsewhere, is refused rather than followed. Where the OS has no `O_NOFOLLOW`
(Windows), the name is checked with `lstat` first and the opened file must be the one it saw.
"""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "packages" / "agami-core" / "src"))

from semantic_model import scratch  # noqa: E402

MARKER = "MARKER-NEVER-PRINTED"


@pytest.fixture
def art(tmp_path, monkeypatch) -> Path:
    art = tmp_path / "art"
    (art / "local" / "tmp").mkdir(parents=True)
    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(art))
    return art


@pytest.fixture
def victim(tmp_path) -> Path:
    v = tmp_path / "private.txt"
    v.write_text(MARKER + "\n")
    return v


@pytest.fixture
def no_nofollow(monkeypatch):
    """The OS as Windows has it: no `O_NOFOLLOW`."""
    monkeypatch.delattr(os, "O_NOFOLLOW", raising=False)


# --- reading a scratch file where no-follow opens are unavailable (4614773376) ---------------------


def test_without_nofollow_a_plain_scratch_file_is_still_read(art, no_nofollow):
    f = art / "local" / "tmp" / "agami-desc.txt"
    f.write_text("hello")
    assert scratch.read_scratch(str(f), 100) == b"hello"


def test_without_nofollow_a_symlink_in_scratch_is_refused(art, victim, no_nofollow):
    link = art / "local" / "tmp" / "agami-desc.txt"
    link.symlink_to(victim)
    with pytest.raises(scratch.NotScratch) as exc:
        scratch.read_scratch(str(link), 100)
    assert MARKER not in str(exc.value)


def test_without_nofollow_a_link_swapped_in_after_the_check_is_refused(
    art, victim, no_nofollow, monkeypatch
):
    f = art / "local" / "tmp" / "agami-desc.txt"
    f.write_text("plain")
    real_open = os.open

    def swapping_open(path, flags, *a, **kw):
        if os.fspath(path) == str(f) and not os.path.islink(f):
            f.unlink()
            f.symlink_to(victim)
        return real_open(path, flags, *a, **kw)

    monkeypatch.setattr(os, "open", swapping_open)
    with pytest.raises(scratch.NotScratch) as exc:
        scratch.read_scratch(str(f), 100)
    assert MARKER not in str(exc.value)


def test_without_nofollow_a_reparse_point_is_refused(art, no_nofollow, monkeypatch):
    f = art / "local" / "tmp" / "agami-desc.txt"
    f.write_text("plain")
    st = os.lstat(f)
    fake = SimpleNamespace(st_mode=st.st_mode, st_ino=st.st_ino, st_dev=st.st_dev,
                           st_file_attributes=0x400)
    real_lstat = os.lstat
    monkeypatch.setattr(
        os, "lstat", lambda p, *a, **kw: fake if os.fspath(p) == str(f) else real_lstat(p, *a, **kw)
    )
    with pytest.raises(scratch.NotScratch):
        scratch.read_scratch(str(f), 100)


def test_without_nofollow_a_file_it_cannot_identify_is_refused(art, no_nofollow, monkeypatch):
    """No inode to compare and no no-follow open: neither check is possible, so it fails closed."""
    f = art / "local" / "tmp" / "agami-desc.txt"
    f.write_text("plain")
    fake = SimpleNamespace(st_mode=stat.S_IFREG | 0o600, st_ino=0, st_dev=0)
    real_lstat = os.lstat
    monkeypatch.setattr(
        os, "lstat", lambda p, *a, **kw: fake if os.fspath(p) == str(f) else real_lstat(p, *a, **kw)
    )
    with pytest.raises(scratch.NotScratch):
        scratch.read_scratch(str(f), 100)


# --- outputs: a linked file at an output name is refused, and every writer is link-safe (4614773456)


def _link(kind: str, name: Path, target: Path) -> None:
    if kind == "symlink":
        name.symlink_to(target)
    else:
        os.link(target, name)


def _cli(argv: list[str]) -> tuple[int, str]:
    import contextlib
    import io

    from semantic_model import cli

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        try:
            rc = cli.main(argv)
        except SystemExit as exc:
            rc = exc.code
    return rc, buf.getvalue()


@pytest.mark.parametrize("kind", ["symlink", "hardlink"])
def test_check_out_refuses_a_linked_scratch_output(art, victim, kind):
    out = art / "local" / "tmp" / "agami-inventory.json"
    _link(kind, out, victim)
    with pytest.raises(scratch.NotScratch):
        scratch.check_out(str(out))
    assert victim.read_text() == MARKER + "\n"


def test_check_out_still_takes_a_fresh_or_plain_scratch_output(art):
    out = art / "local" / "tmp" / "agami-inventory.json"
    assert scratch.check_out(str(out)) == out
    out.write_text("{}")
    assert scratch.check_out(str(out)) == out


def _discovers_nothing(monkeypatch) -> None:
    """`sm discover` with no database: an empty inventory, so only its write is under test."""
    from semantic_model import introspect as INTRO

    monkeypatch.setattr(INTRO, "make_execute_sql_runner", lambda *a, **kw: None)
    monkeypatch.setattr(INTRO, "discover_inventory", lambda *a, **kw: {
        "table_count": 0, "column_mode": "catalog", "schemas": [], "tables": []})


@pytest.mark.parametrize("kind", ["symlink", "hardlink"])
def test_discover_under_the_guard_refuses_a_linked_inventory_out(art, victim, kind, monkeypatch):
    _discovers_nothing(monkeypatch)
    monkeypatch.setenv("AGAMI_SCRATCH_ONLY", "1")
    out = art / "local" / "tmp" / "agami-inventory.json"
    _link(kind, out, victim)
    rc, _ = _cli(["discover", "--profile", "acme", "--db-type", "duckdb", "--artifacts", str(art),
                  "--inventory-out", str(out)])
    assert rc == 2
    assert victim.read_text() == MARKER + "\n"


@pytest.mark.parametrize("kind", ["symlink", "hardlink"])
def test_discover_never_writes_through_a_linked_inventory_out(art, victim, kind, monkeypatch):
    """Without the guard a person may name any path, but the write still replaces the name."""
    _discovers_nothing(monkeypatch)
    out = art / "local" / "tmp" / "agami-inventory.json"
    _link(kind, out, victim)
    _cli(["discover", "--profile", "acme", "--db-type", "duckdb", "--artifacts", str(art),
          "--inventory-out", str(out)])
    assert victim.read_text() == MARKER + "\n"


@pytest.mark.parametrize("kind", ["symlink", "hardlink"])
def test_the_progress_log_is_never_written_through_a_link(tmp_path, victim, kind, monkeypatch):
    from semantic_model import introspect as INTRO

    monkeypatch.setattr(INTRO, "_introspect", lambda *a, **kw: (None, None))
    log = tmp_path / "acme" / ".introspect" / "progress.log"
    log.parent.mkdir(parents=True)
    _link(kind, log, victim)
    INTRO.introspect("acme", "duckdb", runner=None, artifacts_dir=tmp_path, progress_path=log,
                     batch=(1, 2))
    INTRO._progress(log, "done: appended")
    INTRO.log_failure(log, RuntimeError("appended"))
    assert victim.read_text() == MARKER + "\n"


def test_the_progress_log_is_still_written_when_plain(tmp_path, monkeypatch):
    from semantic_model import introspect as INTRO

    log = tmp_path / "acme" / ".introspect" / "progress.log"
    INTRO.log_failure(log, RuntimeError("it stopped"))
    assert log.read_text().rstrip().endswith("failed: it stopped")
    INTRO._progress(log, "one more")
    assert log.read_text().rstrip().endswith("one more")


def test_the_progress_log_appends_without_nofollow_too(tmp_path, victim, no_nofollow):
    from semantic_model import introspect as INTRO

    log = tmp_path / "progress.log"
    INTRO._progress(log, "first")
    INTRO._progress(log, "second")
    assert log.read_text().splitlines()[-1].endswith("second")
    other = tmp_path / "other.log"
    other.symlink_to(victim)
    INTRO._progress(other, "never")
    assert victim.read_text() == MARKER + "\n"


# --- the model's own files, as the hook-approved verbs read and write them (4614773516) ------------


@pytest.fixture
def model(art) -> Path:
    import shutil

    from skill_docs import PLUGIN

    pytest.importorskip("pydantic")
    root = art / "acme"
    shutil.copytree(PLUGIN / "samples" / "store" / "model", root)
    (root / ".introspect").mkdir(exist_ok=True)
    return root


def test_progress_does_not_print_a_hard_linked_log(model, tmp_path):
    victim = tmp_path / "private.log"
    victim.write_text(f"2026-01-01T00:00:00Z {MARKER}\n")
    os.link(victim, model / ".introspect" / "progress.log")
    rc, out = _cli(["progress", str(model)])
    assert MARKER not in out


def test_a_mark_is_not_appended_to_a_hard_linked_timeline(model, victim):
    os.link(victim, model / ".introspect" / "timeline.jsonl")
    rc, _ = _cli(["steps", str(model), "--mark", "connect=done"])
    assert rc == 2
    assert victim.read_text() == MARKER + "\n"


def test_a_hard_linked_timeline_is_not_replayed(model, tmp_path):
    from semantic_model import steps

    victim = tmp_path / "private.jsonl"
    victim.write_text('{"id": "build", "status": "in_progress", "at": "2026-01-01T00:00:00+00:00"}\n')
    os.link(victim, model / ".introspect" / "timeline.jsonl")
    assert steps.attempt_started_at(model) is None


def test_a_hard_linked_steps_last_is_not_read(model, tmp_path):
    from semantic_model import steps

    victim = tmp_path / "private.json"
    victim.write_text('{"list": "' + MARKER + '"}')
    os.link(victim, model / ".introspect" / "steps.last")
    assert steps.read_printed(model) == {}


def test_a_hard_linked_log_is_not_read_for_batches(model, tmp_path):
    from semantic_model import steps

    victim = tmp_path / "private.log"
    victim.write_text("2026-01-01T00:00:00Z batch 1 of 3\n")
    log = model / ".introspect" / "progress.log"
    os.link(victim, log)
    assert steps._batches_left(log) is False


def test_an_approved_verb_refuses_a_hard_linked_model_file(model, tmp_path):
    ds = model / "datasource.yaml"
    victim = tmp_path / "datasource.yaml"
    victim.write_bytes(ds.read_bytes())
    ds.unlink()
    os.link(victim, ds)
    for verb in ("areas", "summary", "overview", "model-tree"):
        rc, out = _cli([verb, str(model)])
        assert "outside_model" in out, verb


def test_an_approved_verb_refuses_a_hard_linked_curation_log(model, tmp_path):
    from semantic_model import curate
    from semantic_model import loader as L

    victim = tmp_path / "private.jsonl"
    victim.write_text('{"note": "' + MARKER + '"}\n')
    os.link(victim, model / curate.CURATION_LOG)
    with L.contained(model), pytest.raises(L.OutsideModel):
        curate.read_log(model)
