"""Text the engine handed over that must be shown before the next card: the pending-say file.

Live runs posted one-line `say`s every time, but a multi-line `say` printed right before a card
(the closing summary, the examples table) went into a thinking block the person never saw, whatever
the skill text said. So the command that prints such a `say` also records it here, and the plugin's
`AskUserQuestion` hook (`plugins/agami/scripts/say_gate.py`) shows it to the person as the card is
asked, unless the assistant has posted it.

The file is `<artifacts_dir>/local/tmp/say-pending.json`: a JSON list of
`{"id", "kind", "needle", "say", "created_at"}`. An earlier engine also wrote `refused` and
`resumed`, counts for card refusals the hook no longer makes; the hook still reads such an entry.
This module owns that format. It imports only the stdlib because the hook, which never imports the
engine, keeps a copy of the reader; a test keeps the two in step. Registering is best effort: any
failure is silent and leaves the command's own output as it would have been, less the `say_id`.
"""

from __future__ import annotations

import json
import os
import secrets
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

# Not `agami-…`: the plugin's approve hook lets Write and Read of `agami-*` scratch files through
# without a prompt, and a subagent able to write this file could plant text for the assistant to post.
FILE_NAME = "say-pending.json"
KINDS = ("summary", "examples")
NEEDLE_CHARS = 120
# The longest `say` registered (the gate drops longer entries as forged). The examples table is at
# most about 5,000 characters (15 rows of capped cells and a line naming at most 5 failures); the
# summary is about 1,000 fixed characters plus about 140 per subject area (80-character names), so
# 32,000 covers some 200 areas. A longer summary is still printed, just not gated.
MAX_SAY_CHARS = 32_000
FIELDS = ("id", "kind", "needle", "say", "created_at")


def pending_path(artifacts_dir: Path) -> Path:
    return Path(artifacts_dir) / "local" / "tmp" / FILE_NAME


def needle_of(say: str) -> str:
    """The first non-empty line of `say`, as printed (ends trimmed), cut to NEEDLE_CHARS: what the
    hook looks for in the assistant's visible text."""
    for line in say.splitlines():
        if line.strip():
            return line.strip()[:NEEDLE_CHARS]
    return ""


def load(path: Path) -> list[dict[str, Any]]:
    """The entries in `path`; [] when it is missing. ValueError when it is not a JSON list of
    objects (the caller decides what a bad file means: the engine replaces it, the hook stays out)."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    doc = json.loads(text)
    if not isinstance(doc, list) or not all(isinstance(e, dict) for e in doc):
        raise ValueError("not a list of entries")
    return doc


def save(path: Path, entries: list[dict[str, Any]]) -> None:
    """Write `entries` atomically, so the hook never reads half a file."""
    path = Path(path)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{FILE_NAME}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(entries, fh, ensure_ascii=False)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _scratch(artifacts_dir: Path) -> Optional[Path]:
    """`<artifacts_dir>/local/tmp`, made if missing, when the artifacts folder exists and neither
    `local` nor `tmp` is a link: the hook reads only a real scratch folder, so writing through a
    link would register text no gate ever sees, somewhere else on disk."""
    art = Path(os.path.realpath(artifacts_dir))
    if not art.is_dir():
        return None
    tmp = art / "local" / "tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    if os.path.normcase(os.path.realpath(tmp)) != os.path.normcase(str(tmp)) or not tmp.is_dir():
        return None
    return tmp


def register(artifacts_dir: Path, kind: str, say: str, now: Optional[datetime] = None) -> Optional[str]:
    """Record `say` as waiting to be posted, replacing an older entry of the same `kind`, and return
    its 8-hex id; None when nothing was recorded (no artifacts folder, a link, a write that failed,
    an empty `say`). Never raises: the command printing `say` must not change because of this."""
    try:
        needle = needle_of(say)
        if kind not in KINDS or not needle or len(say) > MAX_SAY_CHARS:
            return None
        tmp = _scratch(artifacts_dir)
        if tmp is None:
            return None
        path = tmp / FILE_NAME
        if path.is_symlink() or (path.exists() and not path.is_file()):
            return None
        try:
            entries = load(path)
        except ValueError:
            entries = []  # a damaged file is replaced: the hook gives no opinion on it meanwhile
        entry_id = secrets.token_hex(4)
        at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).replace(microsecond=0)
        entries = [e for e in entries if e.get("kind") != kind]
        entries.append({"id": entry_id, "kind": kind, "needle": needle, "say": say,
                        "created_at": at.isoformat()})
        save(path, entries)
        return entry_id
    except Exception:  # noqa: BLE001 — best effort, see docstring
        return None
