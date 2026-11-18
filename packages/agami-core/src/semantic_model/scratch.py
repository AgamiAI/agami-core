"""The files a skill hands the CLI, and the only places the CLI writes a file it is told to.

A skill passes free text, ops and lists through files it wrote with its Write tool into
`<artifacts_dir>/local/tmp/`, named `agami-…`. A call can be approved with no prompt (agami's permission hook),
and the text that steers the model during onboarding includes rows and documents nobody vetted, so
a file option that read any path would let such text copy `~/.aws/credentials` into the org record
or `datasource.yaml`. These are the rules, the same as the permission hook's `scratch_file`:

  * read: a regular file directly inside `<artifacts_dir>/local/tmp/` (no link anywhere on the way),
    named `agami-<…>.json|txt|tsv|md`, not a link, not a hard link, and at most `cap` bytes;
  * write (`--out`): such a scratch file, or a model's own `datasource.md`
    (`<artifacts_dir>/<profile>/datasource.md`).

Under the scratch-only guard (below) a JSON input may also be the one file the build writes for a
skill to pass back, `<artifacts_dir>/<profile>/.introspect/join_candidates.json`.

`<artifacts_dir>` is resolved as agami does everywhere (`agami_paths.artifacts_dir()`).
"""

from __future__ import annotations

import os
import re
import stat
from pathlib import Path

NAME = re.compile(r"agami-[A-Za-z0-9._-]+\.(json|txt|tsv|md)")
PROFILE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*")
TEXT_CAP = 1024 * 1024
INPUT_CAP = 16 * 1024 * 1024
# Set on a call to mean every file it names, in or out, follows the rules above. The skills put it
# in front of the calls that read or write a file they name, as defence in depth: a call steered by
# text nobody vetted can't be pointed at another file; a person typing the command without it can
# still name any file.
GUARD_ENV = "AGAMI_SCRATCH_ONLY"


class NotScratch(ValueError):
    """The file is not one agami's skills could have written: refused, in plain words."""


class LinkedTarget(ValueError):
    """A file agami writes, or its folder, is a link: refused, so nothing is written through it."""


class LinkedFile(OSError):
    """A file agami opens by name is a link, has a second name (a hard link), is not a regular file,
    or was swapped for another while it was opened: refused, so nothing passes through it."""


# How Windows marks a symlink or a junction; `lstat` reports it in `st_file_attributes`.
_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


def _is_link(st) -> bool:
    return stat.S_ISLNK(st.st_mode) or bool(getattr(st, "st_file_attributes", 0) & _REPARSE_POINT)


def open_one_name(path: str | os.PathLike, flags: int, mode: int = 0o666) -> int:
    """An fd for `path`, never through a link at its last part, checked on the open file: a regular
    file with one name (`LinkedFile` otherwise; a failed open raises its own OSError).

    Where the OS has no `O_NOFOLLOW` (Windows) an open follows a link, so the name is `lstat`ed
    first, a link or reparse point refused, and the open file must be the one `lstat` saw (device
    and inode): a link swapped in between is refused. A missing name is created only with `O_CREAT`,
    and then exclusively, so a link planted after the `lstat` fails the open. With no inode to
    compare, neither check is possible and the file is refused."""
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    before = None
    if not nofollow:
        try:
            before = os.lstat(path)
        except FileNotFoundError:
            if not flags & os.O_CREAT:
                raise
            flags |= os.O_EXCL
        else:
            if _is_link(before):
                raise LinkedFile(f"{path} is a link")
            if not before.st_ino:
                raise LinkedFile(f"{path}: can't tell whether it is a link, so it is not opened")
    fd = os.open(path, flags | nofollow, mode)
    try:
        st = os.fstat(fd)
        if before is not None and (st.st_dev, st.st_ino) != (before.st_dev, before.st_ino):
            raise LinkedFile(f"{path} was replaced while it was opened")
        if not stat.S_ISREG(st.st_mode):
            raise LinkedFile(f"{path} is not a regular file")
        if st.st_nlink != 1:
            raise LinkedFile(f"{path} has another name elsewhere (a hard link)")
    except BaseException:
        os.close(fd)
        raise
    return fd


def write_replacing(path: Path, text: str, mode: int | None = None) -> None:
    """`text` at `path` through a temp file in the same folder and a rename, so a link planted at the
    name is replaced, never written through. A folder that is a link is refused (`LinkedTarget`), as
    is a name that is one, so a file in another place is never touched. `mode` sets the file's
    permissions (created that way, never readable by others even for a moment); None keeps the
    usual umask."""
    import contextlib
    import tempfile

    if path.parent.is_symlink():
        raise LinkedTarget(f"{path.parent} is a link; agami writes {path.name} only in a real folder")
    if path.is_symlink():
        raise LinkedTarget(f"{path} is a link; agami will not write through it")
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        if mode is None:
            umask = os.umask(0)
            os.umask(umask)
            mode = 0o666 & ~umask
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def guarded() -> bool:
    return os.environ.get(GUARD_ENV, "") == "1"


def _artifacts_dir() -> Path:
    from agami_paths import artifacts_dir

    return artifacts_dir()


def scratch_dir() -> Path:
    """`<artifacts_dir>/local/tmp`, when it and `local` are real folders, not links."""
    tmp = _artifacts_dir() / "local" / "tmp"
    if os.path.realpath(tmp) != str(tmp) or not tmp.is_dir():
        raise NotScratch(f"agami's scratch folder {tmp} is missing or is a link")
    return tmp


def _plain(path: str) -> Path:
    """`path` as an absolute path, refusing `~`, `.` and `..` segments (as the hook does)."""
    if path.startswith("~") or any(p in (".", "..") for p in re.split(r"[\\/]", path)):
        raise NotScratch(f"{path} must be written out in full, without ~, . or ..")
    return Path(os.path.abspath(path))


def in_scratch(path: str) -> Path:
    p = _plain(path)
    if not NAME.fullmatch(p.name):
        raise NotScratch(
            f"{path} is not one of agami's scratch files: write it as "
            f"<artifacts_dir>/local/tmp/agami-<name>.json (or .txt, .tsv, .md) and pass that"
        )
    tmp = scratch_dir()
    if os.path.realpath(p.parent) != str(tmp):
        raise NotScratch(f"{path} is not directly inside agami's scratch folder {tmp}")
    return p


ENGINE_INPUTS = ("join_candidates.json",)


def engine_input(path: str) -> Path:
    """`path` when it is a build's own `<artifacts_dir>/<profile>/.introspect/<ENGINE_INPUTS>`."""
    p = _plain(path)
    real = Path(os.path.realpath(p))
    profile = real.parent.parent
    if (
        p.name in ENGINE_INPUTS
        and real.name == p.name
        and real.parent.name == ".introspect"
        and profile.parent == _artifacts_dir()
        and PROFILE.fullmatch(profile.name)
        and profile.name.lower() != "local"
    ):
        return p
    raise NotScratch(f"{path} is not one of agami's scratch files")


def check_input(path: str) -> Path:
    """A file the guard lets a JSON input name: a scratch file, or a build's own engine file."""
    try:
        return in_scratch(path)
    except NotScratch:
        try:
            return engine_input(path)
        except NotScratch:
            pass
        raise


def read_scratch(path: str, cap: int, engine_ok: bool = False) -> bytes:
    """The bytes of scratch file `path` (or, with `engine_ok`, a build's own engine file), or
    NotScratch. Opened by `open_one_name`, so nothing can be swapped in between the check and the
    read, on Windows too."""
    p = check_input(path) if engine_ok else in_scratch(path)
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = open_one_name(p, flags)
    except FileNotFoundError:
        raise NotScratch(f"file not found: {p}") from None
    except IsADirectoryError:
        raise NotScratch(f"{p} is a folder, not a file") from None
    except LinkedFile as exc:
        raise NotScratch(f"{exc}; write a fresh file") from None
    except OSError as exc:
        raise NotScratch(f"could not open {p} (a link is refused): {exc.strerror}") from None
    with os.fdopen(fd, "rb") as fh:
        data = fh.read(cap + 1)
    if len(data) > cap:
        raise NotScratch(f"{p} is larger than {cap} bytes")
    return data


def check_out(path: str) -> Path:
    """Where `--out` may write: a scratch file, or a model's own `datasource.md`. Returns the path.
    The write itself is a temp file and a rename in that folder, which replaces a link or a hard
    link at the name instead of writing through it, so only the folder needs checking here."""
    p = _plain(path)
    if p.name == "datasource.md":
        art = _artifacts_dir()
        profile = Path(os.path.realpath(p.parent))
        if (
            profile.parent == art
            and PROFILE.fullmatch(profile.name)
            and "local" not in (profile.name.lower(), p.parent.name.lower())
            and profile.is_dir()
        ):
            return p
        raise NotScratch(
            f"{path}: --out writes a datasource.md only in a model folder, "
            f"{art}/<profile>/datasource.md"
        )
    return in_scratch(path)
