"""PreToolUse hook: approve the few agami calls that are harmless even if a hostile document is
steering the model, and say nothing about anything else.

A live onboarding run stopped for permission 71 times, 42 of them on agami's own commands. This
hook fires in every session, every turn and every subagent, so it approves only what can do no
harm whoever chose the arguments: no database connection, no credentials, no package install, no
write outside agami's scratch folder and a model's own `.introspect/` (`timeline.jsonl`, and
`steps.last`, the step list `steps` and `progress` last printed), no read of a file the call
names outside those, and no change to a model's content or trust state. Everything
else gets no output, so the normal permission flow applies (a skill's `allowed-tools` still cover
its own turn). It never denies: the plugin's hooks deny only `AskUserQuestion`
(say_gate.py), at most twice per pending text, and only to have the engine's own text shown.

  A. Bash: one call of this plugin's `scripts/sm` (an optional leading `bash`, nothing else before
     it, no shell syntax at all) whose verb is in VERBS and whose every argument passes that
     verb's row: the options it may carry, written in full, and the kind of each value.
  B. Write / Read of a file directly inside `<artifacts_dir>/local/tmp/` named
     `agami-*.json|txt|tsv|md` (case-sensitive), symlinks resolved, not a hard link.
  C. Read of a file under this plugin's root.

The plugin root is where this file is, never anything the call names. Every check fails closed:
any doubt (an input it can't parse, a path it can't resolve, an exception) prints nothing. It
imports nothing from the plugin or the agami package, and hooks.json starts it with `python3` from
PATH, not through `scripts/py`, which reads its interpreter from `<artifacts_dir>/local/.config`.

Contract (https://code.claude.com/docs/en/hooks, "PreToolUse decision control"): stdin is JSON with
`hook_event_name`, `tool_name` and `tool_input`; printing `hookSpecificOutput.permissionDecision`
"allow" skips the prompt (deny and ask rules still apply); exit 0 with no output is no decision.
"""

from __future__ import annotations

import json
import ntpath
import os
import re
import stat
import sys
from typing import Any, Callable, Optional

PLUGIN_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LAUNCHER = os.path.join(PLUGIN_ROOT, "scripts", "sm")

# A huge stdin (a Write of a large file) is read, but past this it is not worth parsing.
MAX_INPUT_BYTES = 64 * 1024 * 1024

# Characters that end, chain, redirect or substitute in a shell command, anywhere in it, quoted or
# not: refusing them outright is simpler to trust than reasoning about each one in quotes.
FORBIDDEN_ANYWHERE = (";", "&", "|", "`", "$(", "<", ">", "\n", "\r")
# Outside quotes these have a meaning to the shell (expansion, globbing, grouping, comments).
UNQUOTED_SPECIAL = set("$*?[]{}()!#")

TMP_NAME = re.compile(r"agami-[A-Za-z0-9._-]+\.(json|txt|tsv|md)")
MARK = re.compile(r"[a-z_]+=(in_progress|done|skipped)")
AREA = re.compile(r"[A-Za-z0-9_.-]+")
SECONDS = re.compile(r"[0-9]{1,3}")

# A background or long call is not something to wave through, so only these fields.
TOOL_KEYS = {
    "Bash": {"command", "description"},
    "Write": {"file_path", "content"},
    "Read": {"file_path", "offset", "limit"},
}


class Doubt(Exception):
    """Anything the hook can't be sure of: it then says nothing."""


# --- paths ----------------------------------------------------------------------------------------


def _norm(p: str) -> str:
    return os.path.normcase(os.path.normpath(p))


def _plain_path(value: str) -> bool:
    """An absolute path with no `.` or `..` segment and no `~` (which the shell would expand)."""
    if value.startswith("~") or not (os.path.isabs(value) or ntpath.isabs(value)):
        return False
    return not any(p in (".", "..") for p in re.split(r"[\\/]", value))


def artifacts_dir() -> str:
    """`<artifacts_dir>` resolved as `agami_paths.artifacts_dir()` and the launchers do it:
    `$AGAMI_ARTIFACTS_DIR`, then the first line of `~/.config/agami/path`, then `~/agami-artifacts`.
    Mirrored, not imported: the hook imports nothing of the plugin. Unlike the resolver it refuses a
    relative setting, which would resolve against whatever folder the hook happens to run in."""
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


def scratch_dir() -> str:
    """`<artifacts_dir>/local/tmp`, resolved, when `local` and `tmp` are real folders, not links."""
    tmp = os.path.join(artifacts_dir(), "local", "tmp")
    real = os.path.realpath(tmp)
    if _norm(real) != _norm(tmp) or not os.path.isdir(real):
        raise Doubt("scratch folder missing or a link")
    return real


def scratch_file(file_path: Any) -> bool:
    """A file directly inside the scratch folder with an `agami-` name, not a link out, not a folder,
    not a hard link (whose other name could be anywhere)."""
    if not isinstance(file_path, str) or not _plain_path(file_path):
        return False
    if not TMP_NAME.fullmatch(re.split(r"[\\/]", file_path)[-1]):
        return False
    try:
        tmp = scratch_dir()
    except Doubt:
        return False
    real = os.path.realpath(file_path)
    if _norm(os.path.dirname(real)) != _norm(tmp) or not TMP_NAME.fullmatch(os.path.basename(real)):
        return False
    try:
        st = os.stat(real)
    except FileNotFoundError:
        return True
    except OSError:
        return False
    return stat.S_ISREG(st.st_mode) and st.st_nlink == 1


# A profile name as agami makes them. Anything looser let through `local ` and `local.` (which some
# filesystems treat as `local`), a full-width `ｌocal`, `acme.yaml`, `-x` and names with odd spaces.
ROOT_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*")


def model_root(value: str) -> bool:
    """A model folder: a direct child of `<artifacts_dir>` (symlinks resolved) with a plain profile
    name, not `local`. It need not exist yet: onboarding marks its first step before the build
    makes it."""
    if not _plain_path(value):
        return False
    try:
        art = artifacts_dir()
    except Doubt:
        return False
    real = os.path.realpath(value)
    name = os.path.basename(real)
    if _norm(os.path.dirname(real)) != _norm(art) or not ROOT_NAME.fullmatch(name):
        return False
    # Both as written and resolved, and case-folded: on a case-insensitive disk `Local` is `local`.
    written = re.split(r"[\\/]", value)[-1]
    if not ROOT_NAME.fullmatch(written) or "local" in (name.lower(), written.lower()):
        return False
    return not os.path.exists(real) or os.path.isdir(real)


def plugin_file(file_path: Any) -> bool:
    if not isinstance(file_path, str) or not _plain_path(file_path):
        return False
    root = _norm(os.path.realpath(PLUGIN_ROOT))
    real = _norm(os.path.realpath(file_path))
    try:
        inside = os.path.commonpath([root, real]) == root
    except ValueError:  # different drives on Windows
        return False
    return inside and real != root and os.path.isfile(real)


# --- the verb table ---------------------------------------------------------------------------------


def _area(value: str) -> bool:
    return AREA.fullmatch(value) is not None and value.strip(".") != ""


def _seconds(value: str) -> bool:
    return SECONDS.fullmatch(value) is not None and int(value) <= 600


Kind = Callable[[str], bool]


class Verb:
    """One approved verb: its positionals' kinds, in order, and its options. `options` maps a full
    option name to (kind, repeatable); a kind of None is a flag that takes no value."""

    def __init__(self, positionals: tuple[Kind, ...] = (),
                 options: Optional[dict[str, tuple[Optional[Kind], bool]]] = None,
                 required: tuple[str, ...] = ()) -> None:
        self.positionals = positionals
        self.options = options or {}
        self.required = required


ROOT = (model_root,)

# Every verb here only reads the model at <root> (and, for `steps --mark`, appends to its
# .introspect/timeline.jsonl), or writes only to a scratch file named by --out. Read from cli.py.
# `steps` and `progress` also keep the list they last printed in <root>/.introspect/steps.last,
# replaced atomically and only inside that folder, so the rule above still holds.
#
# NOT approved, by design; do not add them: each connects to a database, uses credentials, installs
# a package, reads a file the call names, writes elsewhere, or changes the model or its sign-off:
# install, introspect, discover, enrich-input, enrich-metadata, seed-examples, seed-validate,
# describe-file, curate, approve-queue, apply-enrichment, apply-spec, add, add-example,
# remove-example, set-org, set-terminology, set-description, set-units, suggest-metrics (--write),
# org-draft, org-context, context, examples, preflight, prepare, receipt, claims, compare-results,
# join-probes, mentions, filter-values, format-table, history, curate-gate, sensitive,
# choice-coverage, suggest-units, validate, snapshot, ensure-org-id, and every verb added later.
VERBS: dict[str, Verb] = {
    "steps": Verb(ROOT, {"--mark": (lambda v: MARK.fullmatch(v) is not None, True),
                         "--full": (None, False)}),
    "progress": Verb(ROOT, {"--wait": (_seconds, False)}),
    "timing": Verb(ROOT),
    "summary": Verb(ROOT, {"--json": (None, False)}),
    "overview": Verb(ROOT),
    "areas": Verb(ROOT),
    "coverage": Verb(ROOT),
    "review-queue": Verb(ROOT),
    "review-items": Verb(ROOT, {"--scope": (lambda v: v in ("all", "rule1", "rule2", "preseed"),
                                            False)}),
    "model-tree": Verb(ROOT, {"--out": (scratch_file, False)}),
    "bundle": Verb(ROOT, {"--area": (_area, False), "--out": (scratch_file, False)},
                   required=("--area",)),
    "batch-id": Verb(),
    "wait-files": Verb((), {"--file": (scratch_file, True), "--wait": (_seconds, False)},
                       required=("--file",)),
}


def args_allowed(args: list[str]) -> bool:
    """The verb and every argument after the launcher pass the verb's row. Only `--name value`:
    an option is a token starting with `-` that is a full name in the row (no `--name=value`, no
    abbreviation, no `--`), a single-value option appears once, and a value never starts with `-`."""
    if not args or args[0] not in VERBS:
        return False
    verb = VERBS[args[0]]
    positionals: list[str] = []
    seen: set[str] = set()
    i = 1
    while i < len(args):
        token = args[i]
        if token.startswith("-"):
            if token not in verb.options:
                return False
            kind, repeatable = verb.options[token]
            if token in seen and not repeatable:
                return False
            seen.add(token)
            if kind is None:
                i += 1
                continue
            if i + 1 >= len(args):
                return False
            value = args[i + 1]
            if value.startswith("-") or not kind(value):
                return False
            i += 2
        else:
            positionals.append(token)
            i += 1
    if len(positionals) != len(verb.positionals):
        return False
    if not all(kind(v) for kind, v in zip(verb.positionals, positionals)):
        return False
    return all(r in seen for r in verb.required)


# --- Bash -----------------------------------------------------------------------------------------


def split_words(command: str) -> list[tuple[str, str]]:
    """The command as (raw text, value) per word, under the shell's quoting rules, or Doubt.

    Single quotes keep everything literal. Double quotes may hold no `$`, backslash or backtick.
    Outside quotes, only plain characters and a backslash before an ordinary character pass."""
    words: list[tuple[str, str]] = []
    raw, value, i, in_word = "", "", 0, False
    while i < len(command):
        ch = command[i]
        if ch in (" ", "\t"):
            if in_word:
                words.append((raw, value))
                raw, value, in_word = "", "", False
            i += 1
            continue
        in_word = True
        if ch == "'":
            end = command.find("'", i + 1)
            if end < 0:
                raise Doubt("unclosed single quote")
            value += command[i + 1:end]
            raw += command[i:end + 1]
            i = end + 1
        elif ch == '"':
            end = command.find('"', i + 1)
            if end < 0:
                raise Doubt("unclosed double quote")
            inner = command[i + 1:end]
            if "$" in inner or "\\" in inner or "`" in inner:
                raise Doubt("expansion or escape inside double quotes")
            value += inner
            raw += command[i:end + 1]
            i = end + 1
        elif ch == "\\":
            nxt = command[i + 1] if i + 1 < len(command) else ""
            if not nxt or not (nxt.isalnum() or nxt in "-_./,:@+%="):
                raise Doubt("escape of a special character")
            value += nxt
            raw += command[i:i + 2]
            i += 2
        elif ch in UNQUOTED_SPECIAL or ord(ch) < 0x20 or ord(ch) == 0x7F:
            raise Doubt(f"unquoted {ch!r}")
        else:
            value += ch
            raw += ch
            i += 1
    if in_word:
        words.append((raw, value))
    return words


def _is_launcher(value: str) -> bool:
    """`value` names this plugin's `sm` exactly, as written or with its folder resolved; on Windows
    also with `/` for `\\` and case folded. Never by resolving `value`: a symlink elsewhere that
    points at `sm` would run it with the wrong scripts folder beside it."""
    if not _plain_path(value) or not os.path.isfile(LAUNCHER):
        return False
    candidates = {LAUNCHER, os.path.join(os.path.realpath(os.path.dirname(LAUNCHER)), "sm")}
    if os.name == "nt":
        return _norm(value) in {_norm(c) for c in candidates}
    return value in candidates


def bash_allowed(command: Any) -> bool:
    if not isinstance(command, str) or not command.strip():
        return False
    if any(bad in command for bad in FORBIDDEN_ANYWHERE):
        return False
    try:
        words = split_words(command)
    except Doubt:
        return False
    n = 1 if words and words[0][0] == "bash" else 0
    if n >= len(words) or not _is_launcher(words[n][1]):
        return False
    return args_allowed([v for _, v in words[n + 1:]])


# --- the hook -------------------------------------------------------------------------------------


def decide(event: Any) -> Optional[str]:
    """The reason to allow `event`, or None to say nothing."""
    if not isinstance(event, dict) or event.get("hook_event_name") != "PreToolUse":
        return None
    tool, tool_input = event.get("tool_name"), event.get("tool_input")
    if tool not in TOOL_KEYS or not isinstance(tool_input, dict):
        return None
    # A field this hook doesn't know (a sandbox override, say) could change what "allow" lets through.
    if not set(tool_input) <= TOOL_KEYS[tool]:
        return None
    if tool == "Bash":
        return "agami: a read-only sm call" if bash_allowed(tool_input.get("command")) else None
    path = tool_input.get("file_path")
    if scratch_file(path):
        return "agami: its own scratch file"
    if tool == "Read" and plugin_file(path):
        return "agami: a file of its own plugin"
    return None


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
            "permissionDecision": "allow",
            "permissionDecisionReason": reason,
        }}) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
