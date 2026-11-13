"""PreToolUse hook on AskUserQuestion: refuse a card, at most twice, while text the engine handed over
for the person has not been shown.

Live onboarding runs never posted the closing summary, and lately not the examples table, although
the engine printed both as `say` with a `then` saying to post it before the card: the model wrote its
wrap-up into a thinking block the person never sees, then asked the card. Instructions did not fix
it. So the engine records those two `say`s in `<artifacts_dir>/local/tmp/say-pending.json`
(format owned by `semantic_model/say_pending.py`; the reader below is a copy, kept in step by
tests/test_say_gate.py), and this hook, before a card, checks the session's transcript:

  1. Not AskUserQuestion, no pending file, or nothing in it: no opinion.
  2. Entries older than MAX_AGE, and entries not shaped as the engine writes them (the needle not
     the say's first line, a control character, longer than the engine registers, an unknown
     kind: forged, since the file's text is posted verbatim), are dropped (the file is rewritten).
  3. An entry belongs to this session only if its id is in a tool result in the transcript's tail
     (the engine printed it as `say_id`). Others are left alone.
  4. One that belongs is satisfied once its needle (the first line of its `say`) is in visible
     assistant text after that tool result: text blocks only, never thinking. Satisfied ones go.
  5. If one that belongs is unsatisfied, the oldest is demanded: refused once more and saved, then
     a deny whose reason is REASON and the `say`. One refused MAX_REFUSALS times already is dropped
     instead, silently: a card is never blocked for good.

It denies nothing but AskUserQuestion, never allows, reads only the pending file and the transcript
path it is handed, and writes only the pending file (through a temp file beside it, for an atomic
replace). Every doubt prints nothing, so the card goes ahead: input it can't parse, a file it can't
read or trust, a count it can't save, an exception. Like approve_own_calls.py it imports nothing of
the plugin or the agami package, and hooks.json runs it with `python3 -I` from PATH.

Contract (https://code.claude.com/docs/en/hooks): stdin has `transcript_path`, `hook_event_name`,
`tool_name` and `tool_input` ("Common input fields", "PreToolUse input"); a
`hookSpecificOutput.permissionDecision` of "deny" prevents the call and its
`permissionDecisionReason` is "shown to Claude" ("PreToolUse decision control"); exit 0 with no
output is no decision. The transcript "is written asynchronously and may lag the in-memory
conversation": in a live session the text posted right before a retried card was not on disk yet
when the hook ran, and the card was refused a second time. Lines are appended in order, so the gate
waits (at most CARD_WAIT seconds) until the card's own tool_use line (`tool_use_id`) is in the
transcript, which puts everything before it there too; if it never appears, no opinion.
"""

from __future__ import annotations

import json
import os
import re
import stat
import sys
import tempfile
import time
import unicodedata
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

# Not `agami-…`: approve_own_calls.py lets Write and Read of `agami-*` scratch files through.
FILE_NAME = "say-pending.json"
KINDS = ("summary", "examples")
FIELDS = ("id", "kind", "needle", "say", "created_at", "refused")
NEEDLE_CHARS = 120

REASON = ("agami has text for the person that has not been shown yet. Post the text below as your "
          "message, exactly as written, then ask this card again.")
MAX_REFUSALS = 2
MAX_AGE = timedelta(minutes=30)
# A clock that moved back a little must not hide a fresh entry; one far in the future is not ours.
MAX_SKEW = timedelta(minutes=5)

# A card's event is small; a pending file holds two `say`s of a few kilobytes each. Anything far
# larger has been tampered with or is not ours: no opinion.
MAX_INPUT_BYTES = 1024 * 1024
MAX_PENDING_BYTES = 256 * 1024
# The longest `say` the engine registers (say_pending.MAX_SAY_CHARS, where the number is derived;
# a test keeps the two equal). An entry longer than that was not written by the engine.
MAX_SAY_CHARS = 32_000
TAIL_LINES = 400
TAIL_BYTES = 2 * 1024 * 1024
# How long to wait for the transcript to catch up with the card, well inside the hook's 10 s timeout.
CARD_WAIT = 3.0
CARD_POLL = 0.1

ID = re.compile(r"[0-9a-f]{8}")
WHITESPACE = re.compile(r"\s+")


class Doubt(Exception):
    """Anything the gate can't be sure of: it then says nothing."""


# --- the pending file -----------------------------------------------------------------------------


def artifacts_dir() -> str:
    """`<artifacts_dir>` as approve_own_calls.artifacts_dir() resolves it (a test keeps the two the
    same): `$AGAMI_ARTIFACTS_DIR`, the pointer file, `~/agami-artifacts`; a relative setting is a
    doubt. Copied, not imported: `python3 -I` puts no script folder on the import path."""
    art = os.environ.get("AGAMI_ARTIFACTS_DIR", "")
    if not art:
        pointer = os.path.join(os.path.expanduser("~"), ".config", "agami", "path")
        try:
            with open(pointer, encoding="utf-8") as fh:
                art = fh.read().strip()
        except OSError:
            art = ""
    art = os.path.expanduser(art or os.path.join("~", "agami-artifacts"))
    if not os.path.isabs(art):
        raise Doubt("relative artifacts folder")
    real = os.path.realpath(art)
    if not os.path.isdir(real):
        raise Doubt("no artifacts folder")
    return real


def pending_file() -> Optional[str]:
    """The pending file's path when it exists as a plain file (not a link, not hard-linked) in a real
    `<artifacts_dir>/local/tmp`; None when there is none. Doubt when something is off."""
    tmp = os.path.join(artifacts_dir(), "local", "tmp")
    if os.path.normcase(os.path.realpath(tmp)) != os.path.normcase(tmp):
        raise Doubt("scratch folder is a link")
    path = os.path.join(tmp, FILE_NAME)
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1 or st.st_size > MAX_PENDING_BYTES:
        raise Doubt("pending file is not a plain file of ours")
    return path


def _created(entry: dict) -> datetime:
    value = entry["created_at"]
    if not isinstance(value, str):
        raise Doubt("created_at")
    try:
        at = datetime.fromisoformat(value)
    except ValueError:
        raise Doubt("created_at") from None
    if at.tzinfo is None:
        raise Doubt("created_at without a zone")
    return at


def _check_entry(entry: Any) -> None:
    """Doubt unless `entry` is exactly what the engine writes."""
    if not isinstance(entry, dict) or set(entry) != set(FIELDS):
        raise Doubt("fields")
    if not isinstance(entry["id"], str) or not ID.fullmatch(entry["id"]):
        raise Doubt("id")
    if not isinstance(entry["needle"], str) or not isinstance(entry["say"], str):
        raise Doubt("needle or say")
    refused = entry["refused"]
    if type(refused) is not int or refused < 0:
        raise Doubt("refused")
    _created(entry)


def _first_line(say: str) -> str:
    """The engine's needle for `say` (say_pending.needle_of): its first non-empty line, trimmed and
    cut to NEEDLE_CHARS."""
    return next((line.strip()[:NEEDLE_CHARS] for line in say.splitlines() if line.strip()), "")


def well_formed(entry: dict) -> bool:
    """Whether `entry` is shaped as the engine writes it. Its `say` is handed to the assistant to
    post verbatim, so anything else is taken for forged and dropped: an unknown kind, a needle that
    isn't the say's first line (text hidden below a harmless first line would be demanded anyway), a
    control or format character (the engine's say_text drops them; only its own line breaks and a
    tab remain), or a say longer than the engine ever registers."""
    say = entry["say"]
    if entry["kind"] not in KINDS or not say.strip() or len(say) > MAX_SAY_CHARS:
        return False
    if any(unicodedata.category(c) in ("Cc", "Cf") and c not in "\n\t" for c in say):
        return False
    needle = _squash(entry["needle"])
    return bool(needle) and needle == _squash(_first_line(say))


def _open_regular(path: str):
    """`path` opened for reading only when it is a regular file, checked before opening (a FIFO
    would block the open) and again on the open file, never through a symlink where the platform
    can refuse one (O_NOFOLLOW)."""
    if not stat.S_ISREG(os.lstat(path).st_mode):
        raise Doubt("not a regular file")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) \
        | getattr(os, "O_BINARY", 0)
    fd = os.open(path, flags)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise Doubt("not a regular file")
        return os.fdopen(fd, "rb")
    except BaseException:
        os.close(fd)
        raise


def load(path: str) -> list:
    with _open_regular(path) as fh:
        doc = json.loads(fh.read(MAX_PENDING_BYTES + 1).decode("utf-8"))
    if not isinstance(doc, list):
        raise Doubt("not a list")
    for entry in doc:
        _check_entry(entry)
    return doc


def save(path: str, entries: list) -> None:
    """Replace the pending file atomically; any failure propagates (and becomes no opinion)."""
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=f".{FILE_NAME}.", suffix=".tmp")
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


# --- the transcript -------------------------------------------------------------------------------


def transcript_tail(path: Any) -> list:
    """The last TAIL_LINES lines (within the last TAIL_BYTES) of the transcript, parsed; lines that
    aren't JSON objects are skipped. Doubt when the path isn't an absolute path to a regular file."""
    if not isinstance(path, str) or not path or not os.path.isabs(path):
        raise Doubt("transcript path")
    with _open_regular(path) as fh:
        size = fh.seek(0, os.SEEK_END)
        start = max(0, size - TAIL_BYTES)
        fh.seek(start)
        data = fh.read(TAIL_BYTES)
    lines = data.split(b"\n")
    if start > 0:
        lines = lines[1:]  # cut mid-line
    rows = []
    for raw in lines[-TAIL_LINES:]:
        try:
            row = json.loads(raw.decode("utf-8"))
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def _blocks(row: dict, kind: str, role: str) -> list:
    """The content blocks of a main-conversation `kind` line from `role`; [] for anything else (a
    request copy, a subagent's line, an attachment, a string prompt)."""
    message = row.get("message")
    if row.get("type") != kind or row.get("isSidechain") is True or not isinstance(message, dict):
        return []
    content = message.get("content")
    if message.get("role") != role or not isinstance(content, list):
        return []
    return [b for b in content if isinstance(b, dict)]


def _result_text(block: dict) -> str:
    content = block.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b["text"] for b in content
                         if isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str))
    return ""


def read_events(rows: list) -> tuple:
    """(tool result texts, visible assistant texts), each as (position, text) in transcript order."""
    results, texts = [], []
    for n, row in enumerate(rows):
        for block in _blocks(row, "user", "user"):
            if block.get("type") == "tool_result":
                results.append((n, _result_text(block)))
        for block in _blocks(row, "assistant", "assistant"):
            if block.get("type") == "text" and isinstance(block.get("text"), str):
                texts.append((n, block["text"]))
    return results, texts


def _has_card(rows: list, tool_use_id: str) -> bool:
    return any(b.get("type") == "tool_use" and b.get("id") == tool_use_id
               for row in rows for b in _blocks(row, "assistant", "assistant"))


def card_rows(path: Any, tool_use_id: Any, rows: list) -> list:
    """`rows` (the tail already read) if it holds the card's own tool_use line, so that the text
    written before the card is there too; else the tail read again until it does, for up to
    CARD_WAIT seconds. Doubt when it never does."""
    if not isinstance(tool_use_id, str) or not tool_use_id:
        raise Doubt("no tool_use_id")
    deadline = time.monotonic() + CARD_WAIT
    while not _has_card(rows, tool_use_id):
        if time.monotonic() >= deadline:
            raise Doubt("the transcript has not caught up with the card")
        time.sleep(CARD_POLL)
        rows = transcript_tail(path)
    return rows


def _squash(text: str) -> str:
    """Runs of whitespace (wrapped lines and non-breaking spaces included) as one space."""
    return WHITESPACE.sub(" ", text).strip()


def printed_at(entry_id: str, results: list) -> Optional[int]:
    """Where the engine's output carrying `entry_id` first appears, or None (not this session's).
    The id must stand alone: a longer run of hex holding it is something else."""
    pattern = re.compile(r"(?<![0-9a-fA-F])" + entry_id + r"(?![0-9a-fA-F])")
    return next((n for n, text in results if pattern.search(text)), None)


def shown_after(needle: str, position: int, texts: list) -> bool:
    visible = _squash(" ".join(text for n, text in texts if n > position))
    return _squash(needle) in visible


# --- the decision ---------------------------------------------------------------------------------


def _sort(entries: list, rows: list) -> tuple:
    """(entries to keep, the kept ones of this session not yet shown), given the transcript tail:
    an entry shown after the output carrying its id is done with; one whose id isn't there is
    another session's, kept and ignored."""
    results, texts = read_events(rows)
    kept, waiting = [], []
    for entry in entries:
        at = printed_at(entry["id"], results)
        if at is not None and shown_after(entry["needle"], at, texts):
            continue
        kept.append(entry)
        if at is not None:
            waiting.append(entry)
    return kept, waiting


def decide(event: Any, now: Optional[datetime] = None) -> Optional[str]:
    """The deny reason for `event`, or None to say nothing. Updates the pending file as it goes."""
    if not isinstance(event, dict) or event.get("hook_event_name") != "PreToolUse" \
            or event.get("tool_name") != "AskUserQuestion":
        return None
    path = pending_file()
    if path is None:
        return None
    entries = load(path)
    if not entries:
        return None
    now = now or datetime.now(timezone.utc)
    if any(_created(e) - now > MAX_SKEW for e in entries):
        raise Doubt("an entry from the future")
    fresh = [e for e in entries if now - _created(e) <= MAX_AGE and well_formed(e)]
    if len(fresh) != len(entries):
        save(path, fresh)
        entries = fresh
    transcript = event.get("transcript_path")
    rows = transcript_tail(transcript)
    kept, waiting = _sort(entries, rows)
    if waiting:
        # Only now, with text of this session still unshown, is the lag worth waiting out (see the
        # module docstring); another session's entry or a shown one costs no wait.
        kept, waiting = _sort(entries, card_rows(transcript, event.get("tool_use_id"), rows))
    if not waiting:
        if len(kept) != len(entries):
            save(path, kept)
        return None
    # File order is the order the engine registered them in; created_at to the second may tie.
    oldest = min(waiting, key=lambda e: (_created(e), kept.index(e)))
    if oldest["refused"] >= MAX_REFUSALS:
        save(path, [e for e in kept if e is not oldest])
        return None
    oldest["refused"] += 1
    save(path, kept)  # before the deny: a count that can't be saved must not refuse again for ever
    return f"{REASON}\n\n{oldest['say']}"


def main() -> int:
    try:
        data = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
        if len(data) > MAX_INPUT_BYTES:
            return 0
        reason = decide(json.loads(data.decode("utf-8")))
    except Exception:  # noqa: BLE001 — any doubt is silence, never a decision
        return 0
    if reason:
        sys.stdout.write(json.dumps({"hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }}) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
