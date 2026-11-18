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
