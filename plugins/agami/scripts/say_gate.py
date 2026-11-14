"""PreToolUse hook on AskUserQuestion: show the person the text the engine handed over for them, if
the assistant has not posted it, as the card is asked.

Live onboarding runs never posted the closing summary, and lately not the examples table, although
the engine printed both as `say` with a `then` saying to post it before the card: the model wrote its
wrap-up into a thinking block the person never sees, then asked the card. Instructions did not fix
it, and neither did refusing the card: three rounds of live runs put the text into thinking again
after each refusal. So the engine records those two `say`s in `<artifacts_dir>/local/tmp/
say-pending.json` (format owned by `semantic_model/say_pending.py`; the reader below is a copy, kept
in step by tests/test_say_gate.py), and this hook, before a card, checks the session's transcript:

  1. Not AskUserQuestion, no pending file, or nothing in it: nothing to say.
  2. Entries older than MAX_AGE, and entries not shaped as the engine writes them (the needle not
     the say's first line, a control character, longer than the engine registers, an unknown
     kind: forged, since the file's text is shown verbatim), are dropped (the file is rewritten).
  3. An entry belongs to this session only if the engine's output carrying its id as the engine
     prints it (`"say_id": "<id>"`) is, in the transcript's tail, the result of one of the main
     thread's Bash calls, or of its Read of exactly the output file that Claude Code's reply to one
     of its Bash calls run in the background named (a long `sm` call, the example run, is run that
     way, and its output reaches the main thread only through that file). Others are left alone.
  4. One that belongs is done with once its needle (the first line of its `say`) is in visible
     assistant text after that tool result: text blocks only, never thinking. It goes, unshown by
     the hook, so the person never sees it twice.
  5. The ones that belong and were not posted are printed as one `systemMessage` (which Claude Code
     shows the person), in the order the engine registered them, as many as fit MAX_MESSAGE_CHARS
     (the rest wait for the next card; one that alone is too long is cut at a whole line). Each is
     first claimed (a `say-claim-<id>` file created with O_EXCL: of two hook processes for two
     cards in one reply, only one prints it), then removed from the file, saved before printing: a
     text is shown once at most, and if the save fails the claim is given back and nothing printed.
  Every save reads the file again just before its atomic replace and changes only the entries this
  run handled, so an entry the engine registered meanwhile is kept.

Its only output is `systemMessage`: it makes no decision about any call, so the card always goes
ahead through the normal permission flow. It reads only the pending file and the transcript path it
is handed, and writes only the pending file (through a temp file beside it, for an atomic replace)
and claim files beside it (removed once older than MAX_AGE).
Every doubt prints nothing: input it can't parse, a file it can't read or trust, a change it can't
save, an exception. Like approve_own_calls.py it imports nothing of the plugin or the agami package,
and hooks.json runs it with `python3 -I` from PATH.

Contract (https://code.claude.com/docs/en/hooks): stdin has `transcript_path`, `hook_event_name`,
`tool_name`, `tool_input` and `tool_use_id` ("Common input fields", "PreToolUse input"); a top-level
`systemMessage` is a "Warning message shown to the user" ("JSON output"), and exit 0 with no decision
lets the call take the normal permission flow. The transcript "is written asynchronously and may lag
the in-memory conversation": in a live session the text posted right before a card was not on disk
yet when the hook ran. Lines are appended in order, so when this session has unposted text the hook
waits (at most CARD_WAIT seconds) until the card's own tool_use line (`tool_use_id`) is in the
transcript, which puts everything before it there too; without that it would show again text the
assistant had just posted. If the line never appears: nothing, and the entry is marked `waited`, so
later cards decide at once from what the transcript holds (one wait per entry, not one per card).
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

# Not `agami-…`: approve_own_calls.py lets Write, Edit and Read of `agami-*` scratch files through.
FILE_NAME = "say-pending.json"
KINDS = ("summary", "examples")
FIELDS = ("id", "kind", "needle", "say", "created_at")
# Counts an earlier engine kept for the card refusals this hook no longer makes. An entry it wrote
# may still be on disk: read as usual (the types it wrote, or a doubt), never used.
LEGACY = ("refused", "resumed")
# Set by this hook, through the same keep-what-others-wrote save, once it waited CARD_WAIT for a
# card's line that never came: later cards decide from the transcript at once, not after 3 s each.
WAITED = "waited"
# A file `say-claim-<id>` beside the pending file, created with O_EXCL by the one hook process that
# shows the entry: two cards in one reply run two processes, and only one may print the text. Not
# `agami-…`, so approve_own_calls.py never lets a subagent create one.
CLAIM_PREFIX = "say-claim-"
NEEDLE_CHARS = 120
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
# The most the hook prints. https://code.claude.com/docs/en/hooks ("JSON output"): a hook's
# `systemMessage` is "capped at 10,000 characters"; over that, Claude Code shows a file path and a
# 2,000-character preview instead, which the person would not read.
MAX_MESSAGE_CHARS = 10_000
SEPARATOR = "\n\n"
# Ends a text cut to fit MAX_MESSAGE_CHARS. Fixed: nothing in it comes from the model or the data.
CUT_LINE = "The rest is in the model summary: ask agami to show it."
TAIL_LINES = 400
TAIL_BYTES = 2 * 1024 * 1024
# How long to wait for the transcript to catch up with the card, well inside the hook's 10 s timeout.
CARD_WAIT = 3.0
CARD_POLL = 0.1

ID = re.compile(r"[0-9a-f]{8}")
# Claude Code's reply to a Bash call with `run_in_background` (seen in live transcripts, version
# 2.1.x), from its first character: the task id, then the absolute path of the file the output goes
# to, which ends in `<task id>.output`; the hint after it, on the same line, may be worded otherwise.
# Nothing else in the transcript ties that file to the call that started it.
BACKGROUND_STARTED = re.compile(r"Command running in background with ID: ([A-Za-z0-9]+)\. "
                                r"Output is being written to: (/[^\n]*/\1\.output)\.(?: [^\n]*)?")
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
    if not isinstance(entry, dict) or set(entry) - set(LEGACY) - {WAITED} != set(FIELDS):
        raise Doubt("fields")
    if type(entry.get("resumed", False)) is not bool or type(entry.get(WAITED, False)) is not bool:
        raise Doubt("resumed or waited")
    refused = entry.get("refused", 0)
    if type(refused) is not int or refused < 0:
        raise Doubt("refused")
    if not isinstance(entry["id"], str) or not ID.fullmatch(entry["id"]):
        raise Doubt("id")
    if not isinstance(entry["needle"], str) or not isinstance(entry["say"], str):
        raise Doubt("needle or say")
    _created(entry)


def _first_line(say: str) -> str:
    """The engine's needle for `say` (say_pending.needle_of): its first non-empty line, trimmed and
    cut to NEEDLE_CHARS."""
    return next((line.strip()[:NEEDLE_CHARS] for line in say.splitlines() if line.strip()), "")


def well_formed(entry: dict) -> bool:
    """Whether `entry` is shaped as the engine writes it. Its `say` is shown to the person verbatim,
    so anything else is taken for forged and dropped: an unknown kind, a needle that isn't the say's
    first line (text hidden below a harmless first line would be shown even when that line was), a
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


def background_output(block: dict, row: dict) -> Optional[str]:
    """The output file named by Claude Code's reply to a Bash call run in the background (`block`, a
    tool_result of such a call, in `row`), or None. The reply's whole text must be Claude Code's
    sentence, its task id must be the file's name, and the line's `toolUseResult` must carry the same
    task id: a foreground call's output can print the sentence, never that field."""
    content = block.get("content")
    found = BACKGROUND_STARTED.fullmatch(content) if isinstance(content, str) else None
    record = row.get("toolUseResult")
    if found is None or not isinstance(record, dict) or record.get("backgroundTaskId") != found[1]:
        return None
    return found[2]


def read_events(rows: list) -> tuple:
    """(engine output texts, visible assistant texts), each as (position, text) in transcript order.
    The engine prints `say_id` in the result of the main thread's own Bash call; a long call run in
    the background (the example run) instead names a file, and its output reaches the main thread
    as the result of a Read of exactly that file, after that reply. Nothing else counts: a subagent's
    report, any other file read back, or a tool's input may quote the id without being this
    session's print."""
    results, texts = [], []
    bash, background, reads, outputs = set(), set(), set(), set()
    for n, row in enumerate(rows):
        for block in _blocks(row, "user", "user"):
            if block.get("type") != "tool_result":
                continue
            use_id = block.get("tool_use_id")
            if use_id in bash or use_id in reads:
                results.append((n, _result_text(block)))
            if use_id in background:
                out = background_output(block, row)
                if out is not None:
                    outputs.add(out)
        for block in _blocks(row, "assistant", "assistant"):
            kind, use_id = block.get("type"), block.get("id")
            if kind == "text" and isinstance(block.get("text"), str):
                texts.append((n, block["text"]))
            if kind != "tool_use" or not isinstance(use_id, str):
                continue
            tool_input = block.get("input") if isinstance(block.get("input"), dict) else {}
            if block.get("name") == "Bash":
                bash.add(use_id)
                if tool_input.get("run_in_background") is True:
                    background.add(use_id)
            elif block.get("name") == "Read" and tool_input.get("file_path") in outputs:
                reads.add(use_id)
    return results, texts


def _has_card(rows: list, tool_use_id: str) -> bool:
    return any(b.get("type") == "tool_use" and b.get("id") == tool_use_id
               for row in rows for b in _blocks(row, "assistant", "assistant"))


class NeverCaughtUp(Doubt):
    """The card's own line did not reach the transcript within CARD_WAIT."""


def card_rows(path: Any, tool_use_id: str, rows: list) -> list:
    """`rows` (the tail already read) if it holds the card's own tool_use line, so that the text
    written before the card is there too; else the tail read again until it does, for up to
    CARD_WAIT seconds. NeverCaughtUp when it never does."""
    deadline = time.monotonic() + CARD_WAIT
    while not _has_card(rows, tool_use_id):
        if time.monotonic() >= deadline:
            raise NeverCaughtUp("the transcript has not caught up with the card")
        time.sleep(CARD_POLL)
        rows = transcript_tail(path)
    return rows


def _squash(text: str) -> str:
    """Runs of whitespace (wrapped lines and non-breaking spaces included) as one space."""
    return WHITESPACE.sub(" ", text).strip()


def printed_at(entry_id: str, results: list) -> Optional[int]:
    """Where the engine's output carrying `entry_id` first appears, or None (not this session's).
    Only as the engine prints it, the `say_id` key with the id as its value (`cli._print_json`,
    indented or not): a bare run of hex in prose, or a date, is something else."""
    if not ID.fullmatch(entry_id):
        return None
    pattern = re.compile(r'"say_id"\s*:\s*"' + entry_id + '"')
    return next((n for n, text in results if pattern.search(text)), None)


def shown_after(needle: str, position: int, texts: list) -> bool:
    visible = _squash(" ".join(text for n, text in texts if n > position))
    return _squash(needle) in visible


# --- the decision ---------------------------------------------------------------------------------


def _sort(entries: list, rows: list) -> tuple:
    """(entries to keep, the kept ones of this session not yet posted), given the transcript tail:
    an entry posted after the output carrying its id is done with; one whose id isn't there is
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


def _cut(say: str) -> str:
    """`say` cut after its last whole line that leaves room for CUT_LINE within MAX_MESSAGE_CHARS,
    with CUT_LINE after a blank line; CUT_LINE alone when not even the first line fits."""
    kept: list = []
    for line in say.splitlines():
        if len("\n".join([*kept, line]).rstrip()) + len(SEPARATOR) + len(CUT_LINE) > MAX_MESSAGE_CHARS:
            break
        kept.append(line)
    head = "\n".join(kept).rstrip()
    return f"{head}{SEPARATOR}{CUT_LINE}" if head else CUT_LINE


def plan(waiting: list) -> list:
    """What to show of `waiting` (in file order, the order the engine registered them), as
    (entry, text) pairs within MAX_MESSAGE_CHARS once joined by SEPARATOR: from the oldest, each
    whole while it fits, stopping at the first that does not; the rest stay pending for the next
    card, so nothing leaves the file unshown. An oldest that alone does not fit is shown cut at a
    whole line (`_cut`)."""
    shown: list = []
    for entry in waiting:
        if len(SEPARATOR.join([*(text for _, text in shown), entry["say"]])) > MAX_MESSAGE_CHARS:
            break
        shown.append((entry, entry["say"]))
    if waiting and not shown:
        shown.append((waiting[0], _cut(waiting[0]["say"])))
    return shown


# --- concurrency ----------------------------------------------------------------------------------


def claim_name(entry_id: str) -> str:
    if not isinstance(entry_id, str) or not ID.fullmatch(entry_id):
        raise Doubt("not an id")
    return CLAIM_PREFIX + entry_id


def claim(folder: str, entry_id: str) -> bool:
    """Whether this process now owns showing `entry_id`: it created its claim file (O_EXCL, never
    through a link). False when another process did first; any other failure is a doubt."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(os.path.join(folder, claim_name(entry_id)), flags, 0o600)
    except FileExistsError:
        return False
    os.close(fd)
    return True


def release(folder: str, ids: list) -> None:
    for entry_id in ids:
        try:
            os.unlink(os.path.join(folder, claim_name(entry_id)))
        except OSError:
            pass


CLAIM = re.compile(re.escape(CLAIM_PREFIX) + r"[0-9a-f]{8}")


def clear_stale_claims(folder: str, now: float) -> None:
    """Claim files older than MAX_AGE: their entries are gone by now. Only plain files named as
    this hook names them are touched."""
    for name in os.listdir(folder):
        if not CLAIM.fullmatch(name):
            continue
        path = os.path.join(folder, name)
        try:
            st = os.lstat(path)
            if stat.S_ISREG(st.st_mode) and now - st.st_mtime > MAX_AGE.total_seconds():
                os.unlink(path)
        except OSError:
            pass


def update(path: str, drop: set, mark: set = frozenset()) -> None:
    """Remove the entries `drop` names, and mark the ones `mark` names as waited for, in the file as
    it is NOW: read again just before the atomic replace, so an entry the engine registered (or
    another hook process changed) since this one first read it is kept."""
    if not drop and not mark:
        return
    current = load(path)
    after = [{**e, WAITED: True} if e["id"] in mark else e for e in current if e["id"] not in drop]
    if after != current:
        save(path, after)


def decide(event: Any, now: Optional[datetime] = None) -> Optional[dict]:
    """The hook's output for `event`: `{"systemMessage": <text>}` when this session has text the
    assistant did not post, else None to say nothing. Updates the pending file as it goes."""
    if not isinstance(event, dict) or event.get("hook_event_name") != "PreToolUse" \
            or event.get("tool_name") != "AskUserQuestion":
        return None
    path = pending_file()
    if path is None:
        return None
    folder = os.path.dirname(path)
    entries = load(path)
    if not entries:
        return None
    now = now or datetime.now(timezone.utc)
    if any(_created(e) - now > MAX_SKEW for e in entries):
        raise Doubt("an entry from the future")
    clear_stale_claims(folder, time.time())
    fresh = [e for e in entries if now - _created(e) <= MAX_AGE and well_formed(e)]
    update(path, {e["id"] for e in entries if not any(e is f for f in fresh)})
    entries = fresh
    transcript, tool_use_id = event.get("transcript_path"), event.get("tool_use_id")
    if not isinstance(tool_use_id, str) or not tool_use_id:
        raise Doubt("no tool_use_id")
    rows = transcript_tail(transcript)
    kept, waiting = _sort(entries, rows)
    unwaited = {e["id"] for e in waiting if not e.get(WAITED)}
    if unwaited:
        # Only now, with text of this session not yet seen posted, is the lag worth waiting out (see
        # the module docstring): text posted just before the card would otherwise be shown twice.
        # Once per entry: after a wait that timed out, later cards decide from what is there.
        try:
            kept, waiting = _sort(entries, card_rows(transcript, tool_use_id, rows))
        except NeverCaughtUp:
            update(path, set(), unwaited)
            raise
    posted = {e["id"] for e in entries if not any(e is k for k in kept)}
    shown = plan(waiting)
    mine = [(e, text) for e, text in shown if claim(folder, e["id"])]
    try:
        # Saved before anything is printed, so a text is shown once at most; a save that fails
        # gives the claims back, and the text waits for the next card.
        update(path, posted | {e["id"] for e, _ in mine})
    except BaseException:
        release(folder, [e["id"] for e, _ in mine])
        raise
    text = SEPARATOR.join(text for _, text in mine)
    return {"systemMessage": text} if text else None


def main() -> int:
    try:
        data = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
        if len(data) > MAX_INPUT_BYTES:
            return 0
        output = decide(json.loads(data.decode("utf-8")))
    except Exception:  # noqa: BLE001 — any doubt is silence, never a decision
        return 0
    if output:
        sys.stdout.write(json.dumps(output) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
