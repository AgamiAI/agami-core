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
    python3 setup_desktop_mcp.py --in-place      # editable install from this checkout (dev mode)
    python3 setup_desktop_mcp.py --python /abs/python3   # force an interpreter
    python3 setup_desktop_mcp.py --config /path/to/config.json  # override target file

Stdlib only (shells out to pip). Reads nothing secret beyond the credentials file
(to learn the active profile's db type → which driver to require). Installs the
agami-core package into the chosen interpreter and writes the desktop config (with a
timestamped backup).
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
# This installer can run before the package is pip-installed, so bootstrap the package source
# onto sys.path to import agami_paths; the Desktop entry it writes runs the installed package
# via `-m mcp_harness`.
PACKAGE_DIR = (SCRIPT_DIR.parent.parent.parent / "packages" / "agami-core").resolve()
_sys = sys
_sys.path.insert(0, str(PACKAGE_DIR / "src"))
import agami_paths  # noqa: E402

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
        return subprocess.run([py, "-c", code], capture_output=True, timeout=20).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def find_interpreter(module: str | None, forced: str | None) -> str | None:
    """Return an absolute python that can import `module`, or None."""
    candidates: list[str] = []
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
    seen: set[str] = set()
    for c in candidates:
        if not c:
            continue
        real = str(Path(c).resolve()) if Path(c).exists() else c
        if real in seen or not Path(c).exists():
            continue
        seen.add(real)
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


def ensure_package_installed(python: str, package_dir: Path, editable: bool, dry_run: bool) -> None:
    """Install agami-core[model] into `python` so it can run `python -m mcp_harness`.

    Non-editable by default so the Desktop registration survives the plugin's
    version-pinned cache dir moving on update (the code lands in site-packages, not a path
    that moves); --in-place uses an editable install for development. The [model] extra
    pulls pydantic/sqlglot/pyyaml so the model-backed tools work; the stdlib execute_sql
    path works regardless. Idempotent. Raises RuntimeError on failure.
    """
    spec = f"{package_dir}[model]"
    base = [python, "-m", "pip", "install"]
    if editable:
        base.append("-e")
    if dry_run:
        print(f"• would install  : {' '.join(base + [spec])}")
        return
    # Plain install first, then --user (system pythons whose site-packages aren't
    # writable). Mirrors the `sm` launcher's strategy.
    proc = None
    for extra in ([], ["--user"]):
        proc = subprocess.run(base + extra + [spec], capture_output=True, text=True)
        if proc.returncode == 0:
            return
    raise RuntimeError((proc.stderr or "").strip() or "pip install agami-core failed")


def build_server_entry(python: str, profile: str, version: str) -> dict:
    return {
        "command": python,
        "args": ["-m", "mcp_harness"],
        "env": {"AGAMI_PROFILE": profile, "AGAMI_VERSION": version},
    }


def read_version(package_dir: Path) -> str:
    """The agami-core version, read from the package's pyproject (single source)."""
    import re
    try:
        text = (package_dir / "pyproject.toml").read_text()
    except OSError:
        return "0.0.0"
    m = re.search(r'(?m)^\s*version\s*=\s*"([^"]+)"', text)
    return m.group(1) if m else "0.0.0"


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
    p.add_argument("--in-place", action="store_true", help="Editable install from this checkout (dev mode) instead of a stable site-packages install.")
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
        ensure_package_installed(python, PACKAGE_DIR, editable=args.in_place, dry_run=args.dry_run)
    except RuntimeError as e:
        print(f"\nERROR: couldn't install agami-core into {python}:\n  {e}", file=sys.stderr)
        return 2
    if not args.dry_run:
        print(f"• agami-core     : {'editable (dev)' if args.in_place else 'installed'} into the interpreter")

    version = read_version(PACKAGE_DIR)
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
