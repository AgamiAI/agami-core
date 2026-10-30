#!/usr/bin/env python3
"""Resolve everything deterministic about the agami environment in ONE call.

agami-connect's Phase 0 preflight was ~5 prose steps the LLM executed by hand
(resolve profile, check credentials + chmod, resolve artifacts dir, detect tools,
score the Python interpreter) — the cluster where the fidelity bugs lived (the
wrong interpreter written to `.config`, a path mis-wire). This script does it
deterministically and emits the state + the next-phase decision as JSON; the
skill just reads it and branches.

Output (the standard refactor contract):
  {
    "ok": true,
    "data": {
      "profile": "main" | null,   # null on a first-time bootstrap (no profile yet — don't invent a name)
      "profile_source": "explicit" | "env" | "active_profile" | "default",
      "artifacts_dir": "/Users/me/agami-artifacts",
      "credentials": {"present": bool, "chmod_ok": bool, "type": "sqlite"|null,
                      "fields": {...}, "missing_fields": [...]},
      "example_present": bool,
      "config": {"present": bool, "active_profile": ..., "tier": ..., "tool_paths": {...}},
      "interpreter": {"python3": "/path", "has_model_deps": bool,
                      "has_driver": bool|null, "chosen_because": "...",
                      "plugin_version": "0.9.18"|null, "agami_core": "0.9.18"|null,
                      "candidates_scored": [{..., "has_pip", "agami_core", "skipped", "reason"?}]},
      "tools": {"psql": "/path"|null, "sqlite3": ...},
      "next": "ready" | "promote" | "bootstrap"
    },
    "anomalies": [...],   # incl. library_older_than_plugin, agami_python_cannot_be_brought_current
    "needs_judgment": null
  }

`next`:
  - "ready"     — the active profile has a valid `[section]`; continue to introspect /
                  existing-model check.
  - "promote"   — a filled-in `credentials.example` exists but no section yet → run the
                  promote step (0a.10).
  - "bootstrap" — neither → first-time credential bootstrap (Phase 0a).

Best-effort + stdlib only. Never raises; reports problems as anomalies / ok:false.
"""

from __future__ import annotations

import argparse
import configparser
import json
import os
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path

# agami_paths lives in the agami-core package; the resolver puts it on the path in every layout
# (pip-installed / the plugin's bundled lib / a dev checkout) with no pip required.
from _agami_lib import ensure_importable  # noqa: E402

ensure_importable()
import agami_paths  # noqa: E402
from _plugin_version import (  # noqa: E402
    cannot_be_brought_current,
    is_older,
    plugin_version,
    stale_warning,
)

_VERSION_HELPER = Path(__file__).resolve().parent / "_plugin_version.py"

# import module to probe per DB type (sqlite/duckdb need no external driver)
_DRIVER_MOD = {
    "postgres": "psycopg2", "redshift": "psycopg2", "mysql": "pymysql",
    "snowflake": "snowflake.connector", "bigquery": "google.cloud.bigquery",
    "sqlserver": "pymssql", "oracle": "oracledb", "databricks": "databricks.sql",
    "trino": "trino", "duckdb": "duckdb", "sqlite": "",
}
# Extra runtime modules a driver needs but doesn't pull in itself, folded into has_driver so
# the "driver missing" branch installs them up front. duckdb needs pytz to materialize
# TIMESTAMP WITH TIME ZONE values into Python — without it a timestamptz query fails at runtime.
_DRIVER_EXTRA = {"duckdb": ["pytz"]}
_MODEL_DEPS = ("pydantic", "sqlglot", "yaml")

# The output lands in the agent's transcript, so a secret's VALUE never goes in it: the skill only
# needs to know a field is set. A connection URL carries its password inline, so it is masked too.
_SECRET_FIELD_RE = re.compile(r"pass|pwd|token|secret|private_key|api_key|credential", re.I)
_URL_PASSWORD_RE = re.compile(r"(://[^:/@\s]*:)[^@\s]*@")
_REDACTED = "<set, hidden>"


def _mode_is_private(mode: int, posix: bool = os.name == "posix") -> bool:
    """No group or other access. POSIX only, as execute_sql's own check is: NTFS reports 0o666 for
    every file and `chmod` cannot change that, so on Windows the warning could never clear."""
    return not posix or not (mode & (stat.S_IRWXG | stat.S_IRWXO))


def _redact(fields: dict) -> dict:
    out = {}
    for key, value in fields.items():
        if _SECRET_FIELD_RE.search(key):
            out[key] = _REDACTED if value else value
        else:
            out[key] = _URL_PASSWORD_RE.sub(rf"\g<1>{_REDACTED}@", value)
    return out
_NATIVE_TOOLS = ("psql", "mysql", "snowsql", "sqlite3", "duckdb", "bq")


def _probe(py: str, mods: list[str]) -> bool:
    """True iff `py` can import every module in `mods` (empty mods → True)."""
    mods = [m for m in mods if m]
    if not mods:
        return True
    try:
        r = subprocess.run([py, "-c", "import " + ", ".join(mods)],
                           capture_output=True, timeout=15)
        return r.returncode == 0
    except Exception:
        return False


def _venv_python(venv: str, windows: bool = os.name == "nt") -> str:
    return str(Path(venv) / "Scripts" / "python.exe") if windows else str(Path(venv) / "bin" / "python")


def _candidate_interpreters() -> list[str]:
    """The bounded candidate list from the skill's 0a.5 prose, in priority order."""
    out: list[str] = []
    if os.environ.get("AGAMI_PYTHON"):
        out.append(os.environ["AGAMI_PYTHON"])
    for c in (shutil.which("python3"), shutil.which("python")):
        if c:
            out.append(c)
    venv = os.environ.get("VIRTUAL_ENV")
    if venv:
        out.append(_venv_python(venv))
    # Every other python3 / python on PATH, after the first ones: when the first is skipped (no pip, an
    # old library), the next one on PATH is the one the user would expect instead. Not the Windows Store
    # stubs, which run nothing.
    for d in os.environ.get("PATH", "").split(os.pathsep):
        for name in ("python3", "python"):
            c = shutil.which(name, path=d) if d and "windowsapps" not in d.lower() else None
            if c:
                out.append(c)
    globs = ["/opt/homebrew/bin/python3.*", "/usr/local/bin/python3.*",
             "/Library/Frameworks/Python.framework/Versions/*/bin/python3",
             str(Path.home() / ".pyenv/versions/*/bin/python3")]
    import glob as _glob
    for g in globs:
        out.extend(sorted(_glob.glob(g)))
    # de-dup, keep order, executables only
    seen, uniq = set(), []
    for c in out:
        if c and c not in seen and os.access(c, os.X_OK):
            seen.add(c)
            uniq.append(c)
    return uniq


def _probe_install(py: str) -> dict:
    """Whether `py` has pip and which agami-core it carries, read from metadata (nothing is imported or
    installed). Unknown values come back as None, and an unknown never gets a candidate skipped."""
    try:
        r = subprocess.run([py, "-X", "utf8", str(_VERSION_HELPER), "--probe"],
                           capture_output=True, text=True, encoding="utf-8", timeout=15)
        got = json.loads(r.stdout.strip().splitlines()[-1]) if r.returncode == 0 else {}
    except Exception:
        got = {}
    return {"has_pip": got.get("has_pip"), "agami_core": got.get("agami_core"),
            "this_checkout": bool(got.get("this_checkout"))}


def _resolve_interpreter(db_type: str | None, configured: str | None) -> dict:
    """Score candidates on (driver + model deps); pick the best. A configured
    interpreter that still satisfies everything wins (no churn). Mirrors 0a.5 — but
    deterministically, so we never record a Python that's missing a dep.

    A candidate that can't install packages and whose agami-core is missing or older than the plugin is
    skipped: `sm install` could never fix it. $AGAMI_PYTHON is the user's explicit choice and is never
    skipped; it gets a warning instead."""
    db_key = (db_type or "").lower()
    driver = _DRIVER_MOD.get(db_key, None)
    want_driver = bool(driver)
    driver_mods = [driver, *_DRIVER_EXTRA.get(db_key, [])] if want_driver else []
    want = plugin_version()
    pinned = os.environ.get("AGAMI_PYTHON") or None
    candidates = []
    if configured and os.access(configured, os.X_OK):
        candidates.append(configured)
    candidates += [c for c in _candidate_interpreters() if c != configured]

    scored = []
    best = None
    warnings: list[dict] = []
    current = set()  # ids of entries whose library is this checkout's own source
    because = ""
    for py in candidates:
        has_deps = _probe(py, list(_MODEL_DEPS))
        has_driver = _probe(py, driver_mods) if want_driver else None
        score = (1 if has_deps else 0) + (1 if (has_driver or not want_driver) else 0)
        # canonical path
        try:
            canon = subprocess.run([py, "-X", "utf8", "-c", "import sys;print(sys.executable)"],
                                   capture_output=True, text=True, encoding="utf-8", timeout=10).stdout.strip() or py
        except Exception:
            canon = py
        inst = _probe_install(py)
        why_not = cannot_be_brought_current(inst, want)
        is_pinned = py == pinned
        if is_pinned and why_not:
            warnings.append({"kind": "agami_python_cannot_be_brought_current", "where": canon,
                             "detail": f"$AGAMI_PYTHON is kept as set, but it has {why_not}"})
        entry = {"python3": canon, "has_model_deps": has_deps, "has_driver": has_driver, "score": score,
                 "has_pip": inst["has_pip"], "agami_core": inst["agami_core"],
                 "skipped": bool(why_not) and not is_pinned}
        if entry["skipped"]:
            entry["reason"] = f"skipped: {why_not}"
        scored.append(entry)
        if inst["this_checkout"]:
            current.add(id(entry))
        full = has_deps and (has_driver or not want_driver)
        if full and best is None and not entry["skipped"]:
            best = entry
            because = ("the first candidate with the model deps"
                       + (f" and the {db_key} driver" if want_driver else "")
                       + (" ($AGAMI_PYTHON)" if is_pinned else
                          " (the configured interpreter)" if py == configured else ""))
    if best is None:
        # nothing fully equipped → first working base interpreter (caller installs into it)
        usable = [e for e in scored if not e["skipped"]]
        if usable:
            best = usable[0]
            because = "no candidate has every dependency; the first one not skipped (the caller installs the rest)"
        elif scored:
            best = scored[0]
            because = "every candidate was skipped; the first one, which needs pip or a newer agami-core"
        else:
            best = {"python3": shutil.which("python3") or "python3", "has_model_deps": False,
                    "has_driver": None, "agami_core": None}
            because = "no candidate interpreter found"
    if id(best) not in current and is_older(best.get("agami_core"), want):
        warnings.append({"kind": "library_older_than_plugin", "where": best["python3"],
                         "detail": stale_warning(best["python3"], best["agami_core"], want)})
    return {**{k: best[k] for k in ("python3", "has_model_deps", "has_driver")}, "chosen_because": because,
            "plugin_version": want, "agami_core": best.get("agami_core"),
            "candidates_scored": scored, "warnings": warnings}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Resolve agami env state + next-phase decision.")
    ap.add_argument("--db-type", default=None, help="bind the interpreter probe to this dialect's driver")
    ap.add_argument("--profile", default=None, help="override the resolved profile")
    args = ap.parse_args(argv)

    anomalies: list = []
    art = agami_paths.artifacts_dir()
    cfg_path = agami_paths.config_path(art)
    creds_path = agami_paths.credentials_path(art)

    config = {"present": False, "active_profile": None, "tier": None, "tool_paths": {}}
    if cfg_path.exists():
        try:
            c = json.loads(cfg_path.read_text(encoding="utf-8"))
            config = {"present": True, "active_profile": c.get("active_profile"),
                      "tier": c.get("tier"), "tool_paths": c.get("tool_paths") or {}}
        except Exception as e:
            anomalies.append({"kind": "config_unreadable", "where": str(cfg_path), "detail": str(e)})

    # profile: explicit → AGAMI_PROFILE → .config active_profile → "main". Track WHERE it resolved from so
    # a first-time bootstrap (nothing set → the bare "main" fallback) doesn't narrate an invented profile
    # name — the skill's rule is "let the user name it", so we null it out in the emitted data below.
    if args.profile:
        profile, profile_source = args.profile, "explicit"
    elif os.environ.get("AGAMI_PROFILE"):
        profile, profile_source = os.environ["AGAMI_PROFILE"], "env"
    elif config["active_profile"]:
        profile, profile_source = config["active_profile"], "active_profile"
    else:
        profile, profile_source = "main", "default"

    # credentials section + chmod
    creds = {"present": False, "chmod_ok": True, "type": None, "fields": {}, "missing_fields": []}
    example_present = (creds_path.parent / "credentials.example").exists()
    if creds_path.exists():
        mode = creds_path.stat().st_mode
        creds["chmod_ok"] = _mode_is_private(mode)
        if not creds["chmod_ok"]:
            anomalies.append({"kind": "credentials_world_readable", "where": str(creds_path),
                              "detail": f"mode {oct(mode & 0o777)}; run chmod 600"})
        cp = configparser.ConfigParser()
        try:
            cp.read(creds_path, encoding="utf-8-sig")
            if cp.has_section(profile):
                creds["present"] = True
                creds["fields"] = _redact(dict(cp.items(profile)))
                creds["type"] = creds["fields"].get("type")
        except Exception as e:
            anomalies.append({"kind": "credentials_unparseable", "where": str(creds_path), "detail": str(e)})

    # next-phase decision (mirrors preflight step 2)
    if creds["present"]:
        nxt = "ready"
    elif example_present:
        nxt = "promote"
    else:
        nxt = "bootstrap"

    db_type = args.db_type or creds["type"]
    interpreter = _resolve_interpreter(db_type, config["tool_paths"].get("python3"))
    for w in interpreter.pop("warnings"):
        anomalies.append(w)
        print(w["detail"] if w["detail"].startswith("agami:") else f"agami: {w['detail']}", file=sys.stderr)
    tools = {t: shutil.which(t) for t in _NATIVE_TOOLS}

    # On a first-time bootstrap the profile is genuinely unresolved (the "main" fallback is a placeholder
    # for the section lookup above, never a real choice) — emit null so the skill narrates "first-time
    # setup" without inventing a name. A real [main] section (ready/promote) is never hidden.
    emitted_profile = None if (profile_source == "default" and nxt == "bootstrap") else profile

    data = {
        "profile": emitted_profile,
        "profile_source": profile_source,
        "artifacts_dir": str(art),
        "credentials": creds,
        "example_present": example_present,
        "config": config,
        "interpreter": interpreter,
        "tools": tools,
        "next": nxt,
    }
    print(json.dumps({"ok": True, "data": data, "anomalies": anomalies, "needs_judgment": None}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
