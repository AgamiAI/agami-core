"""Where a plugin script may write, or serve from, under `AGAMI_SCRATCH_ONLY=1`.

agami-connect runs its page renderers, page parsers, `serve_page.py`, `progress_page.py`,
`inventory_summary.py`, `model_spec_workbook.py` and the sample builder, and onboarding reads rows
and documents nobody vetted. Each of those scripts takes an output path (`--out`, `--tables-out`,
`--ops-out`, ...), and one steered by such text could otherwise overwrite any file the person owns
once approved. So the skill runs them with `AGAMI_SCRATCH_ONLY=1` in front (defence in depth: no
skill pre-approves them today), and under it every path a script writes or serves is one of:

  * a scratch file: directly inside `<artifacts_dir>/local/tmp/` (no link on the way), named
    `agami-<...>.<ext>`, and, when it already exists, a regular file that is neither a link nor a
    hard link;
  * the one model file that script legitimately writes (named by the script, e.g. the sample's
    `<artifacts_dir>/local/samples/store.db`).

Anything else is refused with a plain message and nothing is written. Writes go to a fresh temp file
in the same folder and are renamed over the name, so a link planted at the name is replaced, never
written through. Without the guard nothing changes. This mirrors `semantic_model/scratch.py`, kept stdlib-only and in the plugin
so a stale installed agami-core can't weaken it. Stdlib only.
"""

from __future__ import annotations

import os
import re
import stat
import sys
import tempfile
from pathlib import Path

GUARD_ENV = "AGAMI_SCRATCH_ONLY"
PROFILE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*")
TEXT = ("json", "txt", "tsv", "md")
PAGE = ("html",)


class Refused(ValueError):
    """The path is not one this script may write or serve under the guard."""


def guarded() -> bool:
    return os.environ.get(GUARD_ENV, "") == "1"


def artifacts_dir() -> Path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import _agami_lib

    _agami_lib.ensure_importable()
    from agami_paths import artifacts_dir as resolve

    return resolve()


def _plain(path: str) -> Path:
    """`path` as an absolute path, refusing `~`, `.` and `..` segments (as the scratch guard does)."""
    if path.startswith("~") or any(p in (".", "..") for p in re.split(r"[\\/]", path)):
        raise Refused(f"{path} must be written out in full, without ~, . or ..")
    return Path(os.path.abspath(path))


def _is_link(st: os.stat_result) -> bool:
    return stat.S_ISLNK(st.st_mode) or bool(getattr(st, "st_file_attributes", 0) & _REPARSE_POINT)


def _first_link(art: Path, folder: Path) -> Path | None:
    """The first component of `folder` below `art` that is a link, looked at outside in with `lstat`,
    which never follows one; None when there is none. `exists()` and `realpath()` follow links, so on
    Windows a link to a UNC share would be opened (sending the person's credentials) before it was
    refused, and a dangling link read as a missing folder. Nothing below a missing folder can exist,
    so the walk stops there. `art` itself comes resolved from `agami_paths`."""
    here = art
    for part in folder.relative_to(art).parts:
        here = here / part
        try:
            st = os.lstat(here)
        except FileNotFoundError:
            return None
        if _is_link(st):
            return here
    return None


def _not_a_link(p: Path) -> None:
    """An existing file at `p` must be a plain regular file with one name."""
    try:
        st = os.lstat(p)
    except FileNotFoundError:
        return
    if stat.S_ISLNK(st.st_mode):
        raise Refused(f"{p} is a link; agami writes only its own files")
    if not stat.S_ISREG(st.st_mode):
        raise Refused(f"{p} is not a regular file")
    if st.st_nlink != 1:
        raise Refused(
            f"{p} has another name elsewhere (a hard link); agami writes only its own files"
        )


def scratch_dir() -> Path:
    art = artifacts_dir()
    tmp = art / "local" / "tmp"
    if _first_link(art, tmp) is not None or not tmp.is_dir():
        raise Refused(f"agami's scratch folder {tmp} is missing or is a link")
    return tmp


def scratch_file(path: str, exts: tuple[str, ...] = TEXT) -> Path:
    """`path` when it is (or would be) one of agami's scratch files with one of `exts`."""
    p = _plain(path)
    if not re.fullmatch(r"agami-[A-Za-z0-9._-]+\.(" + "|".join(exts) + ")", p.name):
        raise Refused(
            f"{path} is not one of agami's scratch files: name it "
            f"<artifacts_dir>/local/tmp/agami-<name>.{exts[0]}"
        )
    tmp = scratch_dir()
    if p.parent != tmp:
        raise Refused(f"{path} is not directly inside agami's scratch folder {tmp}")
    _not_a_link(p)
    return p


def model_file(path: str, *rel: str, profile_at: int | None = None) -> Path:
    """`path` when it is exactly `<artifacts_dir>/<rel...>`, with each folder real (not a link).
    A `rel` part `"<profile>"` (at index `profile_at`) matches any profile name but `local`."""
    p = _plain(path)
    art = artifacts_dir()
    parts = list(rel)
    if profile_at is not None:
        try:
            name = p.relative_to(art).parts[profile_at]
        except (ValueError, IndexError):
            name = ""
        if not PROFILE.fullmatch(name) or name.lower() == "local":
            raise Refused(f"{path} is not where this script writes ({art}/{'/'.join(rel)})")
        parts[profile_at] = name
    want = art.joinpath(*parts)
    if p != want:
        raise Refused(f"{path} is not where this script writes ({art}/{'/'.join(rel)})")
    # A link anywhere above the file, a dangling one too, would carry the write (and the mkdir in
    # save()) outside the artifacts folder.
    link = _first_link(art, want.parent)
    if link is not None:
        raise Refused(f"{link} is a link; agami writes only inside its own folders")
    _not_a_link(p)
    return p


# How Windows marks a symlink or a junction; `lstat` reports it in `st_file_attributes`.
_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
INPUT_CAP = 16 * 1024 * 1024


def read_file(path: Path, cap: int = INPUT_CAP) -> str:
    """The text of a file a check above passed, opened so that a link swapped in since is not
    followed and checked on the open file: a regular file with one name, at most `cap` bytes.
    Where the OS has no `O_NOFOLLOW` (Windows) the name is `lstat`ed first, a link or reparse point
    refused, and the open file must be the one it saw; with no inode to compare it is refused.
    Mirrors `semantic_model.scratch.open_one_name`."""
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    before = None
    if not nofollow:
        try:
            before = os.lstat(path)
        except OSError as exc:
            raise Refused(f"could not read {path}: {exc.strerror}") from None
        if stat.S_ISLNK(before.st_mode) or getattr(before, "st_file_attributes", 0) & _REPARSE_POINT:
            raise Refused(f"{path} is a link; agami reads only its own files")
        if not before.st_ino:
            raise Refused(f"{path}: can't tell whether it is a link, so it is not read")
    try:
        fd = os.open(path, os.O_RDONLY | nofollow | getattr(os, "O_NONBLOCK", 0))
    except OSError as exc:
        raise Refused(f"could not read {path} (a link is refused): {exc.strerror}") from None
    with os.fdopen(fd, "rb") as fh:
        st = os.fstat(fh.fileno())
        if before is not None and (st.st_dev, st.st_ino) != (before.st_dev, before.st_ino):
            raise Refused(f"{path} was replaced while it was opened")
        if not stat.S_ISREG(st.st_mode):
            raise Refused(f"{path} is not a regular file")
        if st.st_nlink != 1:
            raise Refused(f"{path} has another name elsewhere (a hard link); agami reads only its own files")
        data = fh.read(cap + 1)
    if len(data) > cap:
        raise Refused(f"{path} is larger than {cap} bytes")
    return data.decode("utf-8", errors="replace")


def read_scratch_text(path: str) -> str:
    """The text of `path` when it is one of agami's `agami-<...>.json` scratch files, read link-safe:
    what a parser reads under the guard, so a call steered by text in the data can't point it at (and
    get an error quoting) a file the person owns."""
    return read_file(scratch_file(os.path.expanduser(path), ("json",)))


def save(path: Path, text: str) -> None:
    """Write `text` to `path` in one step: a fresh temp file beside it, then a rename, so a link
    planted at the name is replaced and never written through."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def refuse(script: str, exc: Refused) -> int:
    """Say why in one line, on stderr and as JSON on stdout (the skills read stdout), and exit 2."""
    import json

    msg = f"{script}: refused under {GUARD_ENV}=1: {exc}"
    sys.stderr.write(msg + "\n")
    print(json.dumps({"ok": False, "error": msg}))
    return 2
