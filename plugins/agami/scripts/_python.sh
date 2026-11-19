# _python.sh — which Python agami runs, on every OS. Sourced (never run) by `sm` and `py`, and asked by
# connect_resolve.py's preflight, so none of them can disagree about the interpreter: $AGAMI_PYTHON, then the one agami-connect configured
# (`.config` tool_paths.python3), then python3 / python on PATH — never the Windows Store stub, and
# not a pip-less one `sm install` could never make current while a better one is on PATH (configured
# or not).
# shellcheck shell=bash

# On Windows, `python3` (and often `python`) may be the Microsoft Store's App Execution Alias: a stub
# under ...\WindowsApps\ that runs nothing, and may even exit 0 after opening the Store. A Python
# installed FROM the Store has its aliases in the same folder, so neither the path nor the exit status
# can tell them apart; only Python output can. Other paths are taken as found, as before.
usable_python() {
  case "$1" in
    *[Ww]indows[Aa]pps*)
      [ "$("$1" -c 'print("agami-python-ok")' 2>/dev/null </dev/null | tr -d '\r')" = "agami-python-ok" ] ;;
    *) [ -x "$1" ] ;;
  esac
}

# The first Python on PATH that `sm install` can bring current, so an activated project venv with no
# pip and an old or missing agami-core doesn't shadow a working one later on PATH (#529). With none,
# the first usable one, so `sm` can say what is wrong with it.
_PY_VERSION_HELPER="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_plugin_version.py"
never_current() {
  local rc=0
  "$1" "$_PY_VERSION_HELPER" --usable </dev/null >/dev/null 2>&1 || rc=$?
  [ "$rc" -eq 3 ]
}
path_python() {
  local c p first=""
  for c in python3 python; do
    while IFS= read -r p; do
      usable_python "$p" || continue
      [ -n "$first" ] || first="$p"
      never_current "$p" || { echo "$p"; return; }
    done < <(type -ap "$c" 2>/dev/null)
  done
  # return 0 when nothing is found: `PY="$(resolve_python)"` runs under set -e, and the caller
  # explains an empty answer
  if [ -n "$first" ]; then echo "$first"; fi
}

# The configured interpreter, read in plain shell: it is how a machine whose only `python3` is that stub
# finds the real one, so reading it must not need a Python. `.config` is JSON the skills write, and
# `python3` is a key only under `tool_paths`; JSON escapes a Windows path's backslashes, which Git
# Bash accepts as forward slashes. The skills write the file as raw UTF-8; a path stored with a
# `\uXXXX` escape would fail the check below and fall back to PATH.
# The artifacts folder, resolved as agami_paths.artifacts_dir() does: env var, the saved pointer, then
# ~/agami-artifacts.
agami_artifacts_dir() {
  local art ptr="$HOME/.config/agami/path"
  art="${AGAMI_ARTIFACTS_DIR:-}"
  if [ -z "$art" ] && [ -f "$ptr" ]; then
    art="$(head -n 1 "$ptr" | tr -d '\r')"
    art="${art#"${art%%[![:space:]]*}"}"; art="${art%"${art##*[![:space:]]}"}"  # trim, as .strip() did
  fi
  art="${art:-$HOME/agami-artifacts}"
  case "$art" in "~"*) art="$HOME${art#\~}" ;; esac
  printf '%s\n' "$art"
}

# The skills' scratch folder, <artifacts_dir>/local/tmp (git-ignored with the rest of local/), made
# if missing so a skill's `> <artifacts_dir>/local/tmp/x` never fails. Not /tmp: on Windows, Claude's
# file tools and Git Bash don't agree where /tmp is, so a file written to one isn't read by the other.
# Only inside an artifacts folder that already exists: before agami-connect has chosen one, creating
# it would leave a stray ~/agami-artifacts behind.
# `local/` and `tmp/` are made 0700, and an existing `local/` tightened to 0700: this runs before
# every `sm` and `py` call, so it is usually what creates `local/`, and a plain `mkdir -p` under the
# usual umask left it readable by anyone on the machine. A `local` or `tmp` that is a link is left
# alone, nothing made through it: that would create a folder wherever it points (`sm init-local`
# refuses one and says how to fix it).
# On POSIX a `local/` another user owns, or one chmod can't make 0700, fails closed (stderr, return 1,
# nothing made in it), as agami_paths.ensure_local_dir does: that user could change it back, and the
# credentials and `.config` go in next. Git Bash on Windows has no such mode to check.
# The way out is another folder, but `sm choose-folder` and `py connect_resolve.py` start here too, so
# the refusal names the form that works: AGAMI_ARTIFACTS_DIR set to the new folder for that one call,
# which then checks (and makes) nothing in the old one, and saves the new folder for every call after.
_RECOVER="To use another folder, run: AGAMI_ARTIFACTS_DIR=\"<new folder>\" bash \"${_PY_VERSION_HELPER%/*}/sm\" choose-folder \"<new folder>\""
_local_is_mine() { [ -O "$1" ]; }
_windows_shell() { case "$(uname -s 2>/dev/null || true)" in MINGW*|MSYS*|CYGWIN*) return 0 ;; esac; return 1; }
agami_scratch_dir() {
  local art d
  art="$(agami_artifacts_dir)"
  d="$art/local/tmp"
  if [ -d "$art" ] && [ ! -L "$art/local" ] && [ ! -L "$d" ]; then
    [ -d "$art/local" ] || mkdir -m 700 "$art/local" 2>/dev/null || true
    if [ -d "$art/local" ] && ! _windows_shell; then
      if ! _local_is_mine "$art/local"; then
        echo "agami: $art/local belongs to another user, so agami won't keep credentials or settings in it. Choose another artifacts folder, or have its owner remove it. ${_RECOVER}" >&2
        return 1
      fi
      if ! chmod 700 "$art/local" 2>/dev/null; then
        echo "agami: $art/local could not be made private (0700), so agami won't keep credentials or settings in it until it can be. ${_RECOVER}" >&2
        return 1
      fi
    else
      chmod 700 "$art/local" 2>/dev/null || true
    fi
    [ -d "$d" ] || mkdir -m 700 "$d" 2>/dev/null || true
  fi
  printf '%s\n' "$d"
}

config_python() {
  local art cfg
  art="$(agami_artifacts_dir)"
  cfg="$art/local/.config"
  [ -f "$cfg" ] || cfg="$HOME/.agami/.config"  # legacy location, during the transition
  [ -f "$cfg" ] || return 0
  tr -d '\r\n' < "$cfg" \
    | grep -o '"python3"[[:space:]]*:[[:space:]]*"[^"]*"' | head -n 1 \
    | sed -e 's/.*:[[:space:]]*"//' -e 's/"$//' -e 's#\\\\#/#g' || true
}

resolve_python() {
  if [ -n "${AGAMI_PYTHON:-}" ]; then echo "$AGAMI_PYTHON"; return; fi
  local cfg alt
  cfg="$(config_python)"
  if [ -n "$cfg" ] && usable_python "$cfg"; then
    # A configured one `sm install` can never bring current gives way to a PATH one that passes the
    # same check, as connect_resolve.py does; with none, keep it so `sm` can say what is wrong.
    if never_current "$cfg"; then
      alt="$(path_python)"
      if [ -n "$alt" ] && ! never_current "$alt"; then echo "$alt"; return; fi
    fi
    echo "$cfg"; return
  fi
  path_python
}
