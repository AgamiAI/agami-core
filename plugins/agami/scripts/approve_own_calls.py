"""PreToolUse hook: approve agami's own launcher calls and its own scratch files, and nothing else.

A live onboarding run stopped for permission 71 times, 42 of them on agami's own commands: a skill's
`allowed-tools` lasts only for the turn it was invoked in, and matches only a launcher call written
exactly one way. This hook answers "allow" for three exact shapes, and says nothing (so the normal
permission flow applies) for everything else. It never denies.

  a. Bash: the whole command is one call of this plugin's `scripts/sm`, or of `scripts/py` running
     one of the plugin's own scripts by bare name, optionally after `bash ` and the two documented
     env prefixes (`AGAMI_SQL_MAX_ROWS=<digits>`, `AGAMI_PROFILE=<word chars>`). No shell syntax at
     all: no `;`, `&`, `|`, backtick, `$`, `<`, `>`, newline, glob or grouping outside quotes.
  b. Write / Read of a file directly inside `<artifacts_dir>/local/tmp/` named `agami-*.json|txt|
     tsv|md`, symlinks resolved.
  c. Read of a file under this plugin's root.

The plugin root is where this file is, never anything the call names. Every check fails closed: any
doubt (an input it can't parse, a path it can't resolve, an exception) prints nothing. Stdlib only,
no engine import, so it is fast and never triggers an install.

Contract (https://code.claude.com/docs/en/hooks, "PreToolUse decision control"): stdin is JSON with
`hook_event_name`, `tool_name` and `tool_input`; printing `hookSpecificOutput.permissionDecision`
"allow" skips the prompt (deny and ask rules still apply); exit 0 with no output is no decision.
"""

from __future__ import annotations

import importlib.util
import json
import ntpath
import os
import re
import sys
from pathlib import Path
from typing import Any, Optional

PLUGIN_ROOT = Path(os.path.abspath(__file__)).parent.parent
SCRIPTS = PLUGIN_ROOT / "scripts"
LAUNCHERS = ("sm", "py")

# A huge stdin (a Write of a large file) is read, but past this it is not worth parsing.
MAX_INPUT_BYTES = 64 * 1024 * 1024

# Characters that end, chain, redirect or substitute in a shell command, anywhere in it, quoted or
# not: refusing them outright is simpler to trust than reasoning about each one in quotes.
FORBIDDEN_ANYWHERE = (";", "&", "|", "`", "$(", "<", ">", "\n", "\r")
# Outside quotes these have a meaning to the shell (expansion, globbing, grouping, comments).
UNQUOTED_SPECIAL = set("$*?[]{}()!#")

ENV_PREFIXES = {
    "AGAMI_SQL_MAX_ROWS": re.compile(r"[0-9]+"),
    "AGAMI_PROFILE": re.compile(r"[A-Za-z0-9_]+"),
}
TMP_NAME = re.compile(r"agami-[A-Za-z0-9._-]+\.(json|txt|tsv|md)")
PLUGIN_SCRIPT = re.compile(r"[A-Za-z][A-Za-z0-9_]*\.py")

TOOL_KEYS = {
    "Bash": {"command", "description", "timeout", "run_in_background"},
    "Write": {"file_path", "content"},
    "Read": {"file_path", "offset", "limit", "pages"},
}


class Doubt(Exception):
    """Anything the hook can't be sure of: it then says nothing."""


# --- Bash -----------------------------------------------------------------------------------------


def split_words(command: str) -> list[tuple[str, str]]:
    """The command as (raw text, value) per word, under the shell's quoting rules, or Doubt.

    Single quotes keep everything literal. Double quotes may hold no `$` or backslash (an expansion
    or an escape this parser would have to model). Outside quotes, only plain characters, `~` and
    `=`, and a backslash before an ordinary character, are accepted."""
    words: list[tuple[str, str]] = []
    raw, value, i, in_word = "", "", 0, False
    while i < len(command):
        ch = command[i]
        if ch == " " or ch == "\t":
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
            if i + 1 >= len(command) or not (command[i + 1].isalnum() or command[i + 1] in "-_./,:@+%="):
                raise Doubt("escape of a special character")
            value += command[i + 1]
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


def _plain_path(value: str) -> bool:
    """An absolute path with no `.` or `..` segment and no `~` (which the shell would expand)."""
    if value.startswith("~") or not (os.path.isabs(value) or ntpath.isabs(value)):
        return False
    parts = re.split(r"[\\/]", value)
    return not any(p in (".", "..") for p in parts)


def _same_path(a: str, b: Path) -> bool:
    """`a` names `b` exactly, as written or with `b`'s folders resolved; on Windows also with `/`
    for `\\` and case folded. Never by resolving `a`: a symlink elsewhere that points at the
    launcher would run it with the wrong scripts folder beside it."""
    candidates = {str(b), os.path.join(os.path.realpath(b.parent), b.name)}
    if os.name == "nt":
        norm = os.path.normcase(os.path.normpath(a))
        return any(norm == os.path.normcase(os.path.normpath(c)) for c in candidates)
    return a in candidates


def _launcher(value: str) -> Optional[str]:
    """`sm` or `py` when `value` is this plugin's launcher of that name, else None."""
    if not _plain_path(value):
        return None
    for name in LAUNCHERS:
        path = SCRIPTS / name
        if path.is_file() and _same_path(value, path):
            return name
    return None


def _own_script(value: str) -> bool:
    """A bare `<name>.py` that `py` maps to the plugin's own script of that name. `py` passes a name
    it can't find to Python as is, which would run a file of that name from the working folder."""
    if not PLUGIN_SCRIPT.fullmatch(value):
        return False
    path = SCRIPTS / value
    return path.is_file() and not path.is_symlink()


def _out_targets(values: list[str]) -> list[str]:
    """Every `--out` value among the arguments (`--out X` and `--out=X`)."""
    found = []
    for n, v in enumerate(values):
        if v == "--out":
            found.append(values[n + 1] if n + 1 < len(values) else "")
        elif v.startswith("--out="):
            found.append(v[len("--out="):])
    return found


def bash_allowed(command: Any) -> bool:
    if not isinstance(command, str) or not command.strip():
        return False
    if any(bad in command for bad in FORBIDDEN_ANYWHERE):
        return False
    try:
        words = split_words(command)
    except Doubt:
        return False
    seen_env: set[str] = set()
    n = 0
    while n < len(words):
        raw, _ = words[n]
        name, eq, val = raw.partition("=")
        if not eq:
            break
        # Raw text, not the value: an assignment the shell sees is unquoted, and only these two.
        pattern = ENV_PREFIXES.get(name)
        if pattern is None or name in seen_env or not pattern.fullmatch(val):
            return False
        seen_env.add(name)
        n += 1
    if n < len(words) and words[n][0] == "bash":
        n += 1
    if n >= len(words):
        return False
    which = _launcher(words[n][1])
    if which is None:
        return False
    args = [v for _, v in words[n + 1:]]
    if which == "py" and not (args and _own_script(args[0])):
        # `py -c …`, `py -m …` or `py <some path>` would run code that isn't the plugin's.
        return False
    # `--out` writes a file; only to agami's own scratch names, as a Write would be.
    return all(tmp_file_allowed(t) for t in _out_targets(args))


# --- Write / Read ---------------------------------------------------------------------------------


def _load_paths_module() -> Any:
    """The plugin's own `lib/agami_paths.py`, loaded from its file so nothing on sys.path can stand
    in for it: the launchers resolve the artifacts folder the same way (env var, pointer, default)."""
    spec = importlib.util.spec_from_file_location("_agami_paths_for_hook",
                                                  PLUGIN_ROOT / "lib" / "agami_paths.py")
    if spec is None or spec.loader is None:
        raise Doubt("agami_paths not found")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _norm(p: str) -> str:
    return os.path.normcase(os.path.normpath(p))


def scratch_dir() -> str:
    """`<artifacts_dir>/local/tmp`, resolved, when `local` and `tmp` are real folders, not links
    out of the artifacts folder; Doubt otherwise."""
    art = os.path.realpath(_load_paths_module().artifacts_dir())
    tmp = os.path.join(art, "local", "tmp")
    real = os.path.realpath(tmp)
    if _norm(real) != _norm(tmp) or not os.path.isdir(real):
        raise Doubt("scratch folder missing or a link")
    return real


def tmp_file_allowed(file_path: Any) -> bool:
    if not isinstance(file_path, str) or not _plain_path(file_path):
        return False
    name = re.split(r"[\\/]", file_path)[-1]
    if not TMP_NAME.fullmatch(name):
        return False
    try:
        tmp = scratch_dir()
    except Doubt:
        return False
    real = os.path.realpath(file_path)
    if os.path.isdir(real):
        return False
    return (_norm(os.path.dirname(real)) == _norm(tmp)
            and TMP_NAME.fullmatch(os.path.basename(real)) is not None)


def plugin_file_allowed(file_path: Any) -> bool:
    if not isinstance(file_path, str) or not _plain_path(file_path):
        return False
    root = _norm(os.path.realpath(PLUGIN_ROOT))
    real = _norm(os.path.realpath(file_path))
    try:
        inside = os.path.commonpath([root, real]) == root
    except ValueError:  # different drives on Windows
        return False
    return inside and real != root and os.path.isfile(real)


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
        return "agami: its own launcher" if bash_allowed(tool_input.get("command")) else None
    path = tool_input.get("file_path")
    if tmp_file_allowed(path):
        return "agami: its own scratch file"
    if tool == "Read" and plugin_file_allowed(path):
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
