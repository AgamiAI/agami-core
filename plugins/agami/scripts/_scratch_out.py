"""Where a pre-approved plugin script may write, or serve from, under `AGAMI_SCRATCH_ONLY=1`.

agami-connect pre-approves its page renderers, page parsers, `serve_page.py`, `progress_page.py`,
`inventory_summary.py`, `model_spec_workbook.py` and the sample builder, and onboarding reads rows
and documents nobody vetted. Each of those scripts takes an output path (`--out`, `--tables-out`,
`--ops-out`, ...), and one steered by such text could otherwise overwrite any file the person owns
with no prompt. So the skill runs them with `AGAMI_SCRATCH_ONLY=1` in front (its rules name that
prefix), and under it every path a script writes or serves is one of:

  * a scratch file: directly inside `<artifacts_dir>/local/tmp/` (no link on the way), named
    `agami-<...>.<ext>`, and, when it already exists, a regular file that is neither a link nor a
    hard link;
  * the one model file that script legitimately writes (named by the script, e.g. the sample's
    `<artifacts_dir>/local/samples/store.db`).

Anything else is refused with a plain message and nothing is written. Writes go to a fresh temp file
in the same folder and are renamed over the name, so a link planted at the name is replaced, never
written through. Without the guard nothing changes: a call without the prefix is not pre-approved,
so the person is asked. This mirrors `semantic_model/scratch.py`, kept stdlib-only and in the plugin
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


def _real_dir(folder: Path) -> bool:
    return folder.is_dir() and os.path.realpath(folder) == str(folder)


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
    tmp = artifacts_dir() / "local" / "tmp"
    if not _real_dir(tmp):
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
    folder = want.parent
    while folder != art.parent and folder.exists():
        if os.path.realpath(folder) != str(folder):
            raise Refused(f"{folder} is a link; agami writes only inside its own folders")
        folder = folder.parent
    _not_a_link(p)
    return p


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
