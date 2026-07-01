#!/usr/bin/env python3
"""
setup_desktop_mcp.py — one-command wiring of `agami serve` into the Claude
Desktop app (and other clients that read a `claude_desktop_config.json`-style
file).

Hand-editing `claude_desktop_config.json` has three sharp edges that this script
removes:

  1. **The GUI-PATH gotcha.** The desktop app launches helpers with a minimal
     PATH, so a bare `python3` isn't found — and it must be the interpreter that
     can import your DB driver (psycopg2 / pymysql / …). We auto-detect it.
  2. **The install-path gotcha.** A marketplace-installed plugin lives in a
     cache dir that moves on every update. We `pip install` the agami-core package
     into the chosen interpreter and register `python -m mcp_harness`, so the config
     survives plugin updates (the code is in site-packages, not a moving path).
  3. **The merge gotcha.** `mcpServers` is a top-level key; a stray comma breaks
     the whole file. We back up, merge (preserving every other key), write
     atomically, and validate.

Usage:
    python3 setup_desktop_mcp.py                 # wire active profile into Desktop
    python3 setup_desktop_mcp.py --profile main  # pin a specific profile
    python3 setup_desktop_mcp.py --dry-run       # show the plan, write nothing
    python3 setup_desktop_mcp.py --python /abs/python3   # force an interpreter
    python3 setup_desktop_mcp.py --config /path/to/config.json  # override target file

Stdlib only. Reads nothing secret beyond the credentials file (to learn the active
profile's db type → which driver to require). Installs the agami-core package into the
chosen interpreter via `sm install` (the single install chokepoint) and writes the
desktop config (with a timestamped backup).
"""

from __future__ import annotations

import argparse
import configparser
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
# agami_paths lives in the agami-core package; the resolver puts it on the path in every layout
# (pip-installed / the plugin's bundled lib / a dev checkout) with no pip required. A bare import off
# packages/src breaks on a marketplace install, which ships no packages/ (mirrors connect_resolve.py).
from _agami_lib import ensure_importable  # noqa: E402

ensure_importable()
import agami_paths  # noqa: E402

# A dev checkout has packages/agami-core; a marketplace install does NOT. Optional — used only as a
# version-source fallback. The package INSTALL goes through `sm install`, never a path to this dir.
_DEV_PKG_DIR = (SCRIPT_DIR.parent.parent.parent / "packages" / "agami-core").resolve()

# Never bootstrap() at import (tests import this module); main() does it.
AGAMI_HOME = agami_paths.local_dir()
CREDENTIALS_PATH = AGAMI_HOME / "credentials"
CONFIG_PATH = AGAMI_HOME / ".config"

# db_type → the Python module that must be importable to execute against it.
DB_DRIVER_MODULE = {
    "postgres": "psycopg2",
    "redshift": "psycopg2",
    "mysql": "pymysql",
    "snowflake": "snowflake.connector",
    "bigquery": "google.cloud.bigquery",
    "sqlite": None,  # stdlib
}

DRIVER_PIP = {
    "psycopg2": "psycopg2-binary",
    "pymysql": "pymysql",
    "snowflake.connector": "snowflake-connector-python",
    "google.cloud.bigquery": "google-cloud-bigquery",
}


def _load_config() -> dict:
    if CONFIG_PATH.exists():
        try:
            return json.loads(CONFIG_PATH.read_text())
        except (OSError, ValueError):
            pass
    return {}


def resolve_profile(explicit: str | None) -> str:
    if explicit:
        return explicit
    env = os.environ.get("AGAMI_PROFILE")
    if env:
        return env
    active = _load_config().get("active_profile")
    if isinstance(active, str) and active:
        return active
    return "default"


def db_type_for_profile(profile: str) -> str | None:
    """Read the profile's db type from the credentials file (best-effort)."""
    if not CREDENTIALS_PATH.exists():
        return None
    cfg = configparser.ConfigParser(inline_comment_prefixes=("#", ";"))
    try:
        cfg.read(CREDENTIALS_PATH)
    except configparser.Error:
        return None
    if profile not in cfg:
        return None
    sect = cfg[profile]
    t = (sect.get("type") or "").strip().lower()
    if not t and sect.get("url"):
        scheme = sect["url"].split("://", 1)[0].split("+", 1)[0].lower()
        t = {"postgresql": "postgres", "postgres": "postgres", "mysql": "mysql",
             "mariadb": "mysql", "redshift": "redshift", "snowflake": "snowflake",
             "bigquery": "bigquery", "bq": "bigquery", "sqlite": "sqlite"}.get(scheme, scheme)
    return t or None


def _interpreter_can_import(py: str, module: str | None) -> bool:
    if module is None:
        # sqlite / unknown — any working python3 is fine
        code = "import sys; assert sys.version_info >= (3, 8)"
    else:
        code = f"import {module}"
    try:
        # cwd="/" so a `semantic_model`/module dir in the caller's cwd can't shadow the real import
        # (`python -c` puts cwd on sys.path[0]).
        return subprocess.run([py, "-c", code], capture_output=True, timeout=20, cwd="/").returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def find_interpreter(module: str | None, forced: str | None) -> str | None:
    """Return an absolute python that can import `module`, or None.

    Two passes when not forced: first prefer a candidate that imports BOTH the driver `module` AND the
    full model stack (`semantic_model.cli` — i.e. one that already has agami-core[model], so no install
    is needed); fall back to driver-only (the `sm install` step, with its PEP-668 tier, equips it).
    """
    if forced:
        candidates = [forced]
    else:
        candidates = [
            sys.executable,
            shutil.which("python3") or "",
            shutil.which("python") or "",   # Windows usually exposes `python`, not `python3`
            "/Library/Frameworks/Python.framework/Versions/3.12/bin/python3",
            "/Library/Frameworks/Python.framework/Versions/3.11/bin/python3",
            "/usr/local/bin/python3",
            "/opt/homebrew/bin/python3",
            "/usr/bin/python3",
            str(Path.home() / ".pyenv/shims/python3"),
        ]
    require = [True, False] if not forced else [False]  # pass 1: driver+agami-core; pass 2: driver only
    for want_agami in require:
        seen: set[str] = set()
        for c in candidates:
            if not c:
                continue
            real = str(Path(c).resolve()) if Path(c).exists() else c
            if real in seen or not Path(c).exists():
                continue
            seen.add(real)
            if want_agami and not _interpreter_can_import(c, "semantic_model.cli"):
                continue
            if _interpreter_can_import(c, module):
                # Prefer the absolute resolved path (GUI apps need it).
                return str(Path(c).resolve())
    return None


def desktop_config_path(override: str | None) -> Path:
    if override:
        return Path(override).expanduser()
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
    if sys.platform.startswith("win"):
        appdata = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(appdata) / "Claude" / "claude_desktop_config.json"
    # Linux / other
    return Path.home() / ".config" / "Claude" / "claude_desktop_config.json"


def ensure_package_installed(python: str, dry_run: bool) -> None:
    """Make agami-core + its model deps importable in `python` by delegating to the `sm` launcher — the
    single install chokepoint (resolver-style source order dev-editable → PyPI → git, plus the PEP-668
    tier and path isolation). We ALWAYS call `sm install`: it's idempotent (a fast no-op when the
    interpreter is already ready), so there's no separate, weaker "already present?" check to drift —
    important because `agami-core` has no base deps and the model deps live in the [model] extra, so a
    base-only install would pass a bare `import mcp_harness` yet fail later in semantic-model tooling.
    Verifies the REAL entrypoint (`semantic_model.cli`, which pulls pydantic/sqlglot/pyyaml) afterward.
    Raises RuntimeError if bash is unavailable, or the model stack still won't import.
    """
    if dry_run:
        print(f"• would ensure   : bash {SCRIPT_DIR / 'sm'} install   (AGAMI_PYTHON={python})")
        return
    if shutil.which("bash") is None:
        raise RuntimeError(
            "`bash` is required to run the agami installer (sm) but wasn't found on PATH. "
            f"Install bash, or pre-install agami-core yourself:  {python} -m pip install 'agami-core[model]'"
        )
    subprocess.run(
        ["bash", str(SCRIPT_DIR / "sm"), "install"],
        env={**os.environ, "AGAMI_PYTHON": python},
        check=False,
    )
    # The real readiness bar: the semantic-model entrypoint + its [model] deps, not just that
    # agami-core is importable (base has no deps — see docstring).
    if not _interpreter_can_import(python, "semantic_model.cli"):
        raise RuntimeError(
            f"agami-core (with its model deps) still isn't importable in {python} after `sm install`. "
            "Point agami at a Python that has it:  export AGAMI_PYTHON=/abs/path/to/python3"
        )


def build_server_entry(python: str, profile: str, version: str) -> dict:
    return {
        "command": python,
        "args": ["-m", "mcp_harness"],
        "env": {"AGAMI_PROFILE": profile, "AGAMI_VERSION": version},
    }


def read_version() -> str:
    """The agami-core version for the AGAMI_VERSION env hint (informational only). Prefer the
    version-pinned plugin cache-dir name (…/agami-core/<version>/scripts/); fall back to a dev
    checkout's pyproject; else 0.0.0. Never crash on a missing packages/ (marketplace ships none)."""
    import re
    ver = SCRIPT_DIR.parent.name  # …/agami-core/<version>/scripts → <version>
    if re.match(r"^\d+\.\d+", ver):
        return ver
    try:
        text = (_DEV_PKG_DIR / "pyproject.toml").read_text()
        m = re.search(r'(?m)^\s*version\s*=\s*"([^"]+)"', text)
        if m:
            return m.group(1)
    except OSError:
        pass
    return "0.0.0"


def merge_into_config(cfg_path: Path, server_name: str, entry: dict, dry_run: bool) -> tuple[dict, Path | None]:
    """Merge the entry under mcpServers; back up + atomic write. Returns (new_config, backup_path)."""
    existing: dict = {}
    if cfg_path.exists():
        try:
            existing = json.loads(cfg_path.read_text())
        except ValueError as e:
            raise SystemExit(
                f"ERROR: {cfg_path} exists but is not valid JSON ({e}). "
                f"Fix or remove it, then re-run."
            )
        if not isinstance(existing, dict):
            raise SystemExit(f"ERROR: {cfg_path} is not a JSON object.")

    new = dict(existing)
    servers = dict(new.get("mcpServers") or {})
    servers[server_name] = entry
    new["mcpServers"] = servers

    if dry_run:
        return new, None

    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    backup: Path | None = None
    if cfg_path.exists():
        backup = cfg_path.with_suffix(cfg_path.suffix + f".bak-{int(time.time())}")
        shutil.copy2(cfg_path, backup)

    tmp = cfg_path.with_suffix(cfg_path.suffix + ".tmp")
    tmp.write_text(json.dumps(new, indent=2) + "\n")
    # validate round-trip before swapping in
    json.loads(tmp.read_text())
    os.replace(tmp, cfg_path)
    return new, backup


def main() -> int:
    global AGAMI_HOME, CREDENTIALS_PATH, CONFIG_PATH
    agami_paths.bootstrap()
    AGAMI_HOME = agami_paths.local_dir()
    CREDENTIALS_PATH = AGAMI_HOME / "credentials"
    CONFIG_PATH = AGAMI_HOME / ".config"
    p = argparse.ArgumentParser(description="Wire `agami serve` into the Claude Desktop app.")
    p.add_argument("--profile", default=None, help="agami profile to serve (default: active profile).")
    p.add_argument("--server-name", default="agami", help="Name of the MCP server entry (default: agami).")
    p.add_argument("--python", default=None, help="Force a specific python interpreter (absolute path).")
    p.add_argument("--config", default=None, help="Override the desktop config path (for testing / other clients).")
    p.add_argument("--in-place", action="store_true", help="(deprecated no-op) install now goes through `sm install`, which does an editable install automatically in a dev checkout.")
    p.add_argument("--dry-run", action="store_true", help="Print the plan; write nothing.")
    args = p.parse_args()

    profile = resolve_profile(args.profile)
    db_type = db_type_for_profile(profile)
    module = DB_DRIVER_MODULE.get(db_type or "", "psycopg2")  # default-guess postgres if unknown

    print(f"• profile        : {profile}" + (f"  (db type: {db_type})" if db_type else "  (db type: unknown)"))
    print(f"• driver needed  : {module or 'none (sqlite/stdlib)'}")

    python = find_interpreter(module, args.python)
    if python is None:
        pip = DRIVER_PIP.get(module or "", module or "")
        print(
            f"\nERROR: could not find a python3 that can `import {module}`.\n"
            f"  Install the driver, e.g.:  python3 -m pip install {pip}\n"
            f"  then re-run, or pass --python /abs/path/to/python3.",
            file=sys.stderr,
        )
        return 3
    print(f"• interpreter    : {python}")

    try:
        ensure_package_installed(python, dry_run=args.dry_run)
    except RuntimeError as e:
        print(f"\nERROR: couldn't install agami-core into {python}:\n  {e}", file=sys.stderr)
        return 2
    if not args.dry_run:
        print("• agami-core     : present in the interpreter")

    version = read_version()
    entry = build_server_entry(python, profile, version)
    cfg_path = desktop_config_path(args.config)
    print(f"• desktop config : {cfg_path}")

    print("\nMCP server entry to be written:")
    print(json.dumps({args.server_name: entry}, indent=2))

    if args.dry_run:
        print("\n(--dry-run) nothing written.")
        return 0

    _new, backup = merge_into_config(cfg_path, args.server_name, entry, dry_run=False)
    if backup:
        print(f"\n✓ backed up previous config → {backup}")
    print(f"✓ wrote {args.server_name} into {cfg_path}")
    print(
        "\nNext: fully quit the Claude Desktop app (Cmd+Q on macOS) and reopen it,\n"
        "then ask: \"What datasources does agami see?\"\n"
        f"Logs (if it doesn't appear): ~/Library/Logs/Claude/mcp-server-{args.server_name}.log"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
