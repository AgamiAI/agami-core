"""Self-heal the interpreter for directly-invoked scripts.

A renderer run as `python3 render_model_explorer.py …` inherits whatever `python3` is on
PATH — which often lacks PyYAML / the model deps, while agami's *configured* interpreter
(`~/.agami/.config` → `tool_paths.python3`, the same one the `sm` wrapper resolves) has them.
Importing this module checks for the deps and, if they're missing, re-execs the current
script under the configured interpreter — so the caller never has to remember to use `$PY`.

Pure stdlib (so it imports under any interpreter); the check + at-most-one re-exec happen as
an import side effect, before the script's real (dep-requiring) imports run.
"""
from __future__ import annotations

import json
import os
import pathlib
import sys


def _configured_interpreter() -> str:
    env = os.environ.get("AGAMI_PYTHON")
    if env:
        return env
    try:
        cfg = json.loads(pathlib.Path("~/.agami/.config").expanduser().read_text())
        return (cfg.get("tool_paths") or {}).get("python3") or ""
    except Exception:
        return ""


def ensure_deps(canary: str = "yaml") -> None:
    """If `canary` (default PyYAML) can't be imported, re-exec under the configured
    interpreter. Re-execs at most once (guarded by AGAMI_REEXEC) so a genuinely-missing
    dep surfaces its real ImportError instead of looping."""
    try:
        __import__(canary)
        return
    except ImportError:
        pass
    if os.environ.get("AGAMI_REEXEC"):
        return
    interp = _configured_interpreter()
    if (interp and os.path.exists(interp)
            and os.path.realpath(interp) != os.path.realpath(sys.executable)):
        os.environ["AGAMI_REEXEC"] = "1"
        os.execv(interp, [interp, *sys.argv])  # replace this process under the right python


ensure_deps()
