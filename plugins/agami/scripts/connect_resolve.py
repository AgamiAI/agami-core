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
      "profile": "main",
      "artifacts_dir": "/Users/me/agami-artifacts",
      "credentials": {"present": bool, "chmod_ok": bool, "type": "sqlite"|null,
                      "fields": {...}, "missing_fields": [...]},
      "example_present": bool,
      "config": {"present": bool, "active_profile": ..., "tier": ..., "tool_paths": {...}},
      "interpreter": {"python3": "/path", "has_model_deps": bool,
                      "has_driver": bool|null, "candidates_scored": [...]},
      "tools": {"psql": "/path"|null, "sqlite3": ...},
      "next": "ready" | "promote" | "bootstrap"
    },
    "anomalies": [...],
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
import shutil
import stat
import subprocess
import sys
from pathlib import Path

# agami_paths lives in the agami-core package; add its src so this resolves whether or not
# the package is pip-installed yet.
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "packages" / "agami-core" / "src"))
import agami_paths  # noqa: E402

# import module to probe per DB type (sqlite/duckdb need no external driver)
_DRIVER_MOD = {
    "postgres": "psycopg2", "redshift": "psycopg2", "mysql": "pymysql",
    "snowflake": "snowflake.connector", "bigquery": "google.cloud.bigquery",
    "sqlserver": "pymssql", "oracle": "oracledb", "databricks": "databricks.sql",
    "trino": "trino", "duckdb": "duckdb", "sqlite": "",
}
_MODEL_DEPS = ("pydantic", "sqlglot", "yaml")
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
        out.append(str(Path(venv) / "bin" / "python"))
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


def _resolve_interpreter(db_type: str | None, configured: str | None) -> dict:
    """Score candidates on (driver + model deps); pick the best. A configured
    interpreter that still satisfies everything wins (no churn). Mirrors 0a.5 — but
    deterministically, so we never record a Python that's missing a dep."""
    driver = _DRIVER_MOD.get((db_type or "").lower(), None)
    want_driver = bool(driver)
    candidates = []
    if configured and os.access(configured, os.X_OK):
        candidates.append(configured)
    candidates += [c for c in _candidate_interpreters() if c != configured]

    scored = []
    best = None
    for py in candidates:
        has_deps = _probe(py, list(_MODEL_DEPS))
        has_driver = _probe(py, [driver]) if want_driver else None
        score = (1 if has_deps else 0) + (1 if (has_driver or not want_driver) else 0)
        # canonical path
        try:
            canon = subprocess.run([py, "-c", "import sys;print(sys.executable)"],
                                   capture_output=True, text=True, timeout=10).stdout.strip() or py
        except Exception:
            canon = py
        entry = {"python3": canon, "has_model_deps": has_deps, "has_driver": has_driver, "score": score}
        scored.append(entry)
        full = has_deps and (has_driver or not want_driver)
        if full and best is None:
            best = entry
    if best is None:
        # nothing fully equipped → first working base interpreter (caller installs into it)
        best = scored[0] if scored else {"python3": shutil.which("python3") or "python3",
                                         "has_model_deps": False, "has_driver": None, "score": 0}
    return {**{k: best[k] for k in ("python3", "has_model_deps", "has_driver")},
            "candidates_scored": scored}


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

    # profile: explicit → AGAMI_PROFILE → .config active_profile → "main"
    profile = (args.profile or os.environ.get("AGAMI_PROFILE")
               or config["active_profile"] or "main")

    # credentials section + chmod
    creds = {"present": False, "chmod_ok": True, "type": None, "fields": {}, "missing_fields": []}
    example_present = (creds_path.parent / "credentials.example").exists()
    if creds_path.exists():
        mode = creds_path.stat().st_mode
        creds["chmod_ok"] = not (mode & (stat.S_IRWXG | stat.S_IRWXO))
        if not creds["chmod_ok"]:
            anomalies.append({"kind": "credentials_world_readable", "where": str(creds_path),
                              "detail": f"mode {oct(mode & 0o777)}; run chmod 600"})
        cp = configparser.ConfigParser()
        try:
            cp.read(creds_path)
            if cp.has_section(profile):
                creds["present"] = True
                creds["fields"] = dict(cp.items(profile))
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
    tools = {t: shutil.which(t) for t in _NATIVE_TOOLS}

    data = {
        "profile": profile,
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
