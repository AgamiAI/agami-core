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
     version-pinned cache dir that moves on every update. We copy the server
     files (`mcp_server.py` + `execute_sql.py` + the `semantic_model/` package)
     to a STABLE `~/.agami/serve/` so the Desktop config never needs to change
     again — and keeps working even if the plugin is later uninstalled.
  3. **The merge gotcha.** `mcpServers` is a top-level key; a stray comma breaks
     the whole file. We back up, merge (preserving every other key), write
     atomically, and validate.

Usage:
    python3 setup_desktop_mcp.py                 # wire active profile into Desktop
    python3 setup_desktop_mcp.py --profile main  # pin a specific profile
    python3 setup_desktop_mcp.py --dry-run       # show the plan, write nothing
    python3 setup_desktop_mcp.py --in-place      # point at this checkout (dev mode; no copy)
    python3 setup_desktop_mcp.py --python /abs/python3   # force an interpreter
    python3 setup_desktop_mcp.py --config /path/to/config.json  # override target file

Stdlib only. Reads nothing secret beyond ~/.agami/credentials (to learn the
active profile's db type → which driver to require). Writes only the stable
serve dir + the desktop config (with a timestamped backup).
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

AGAMI_HOME = Path.home() / ".agami"
CREDENTIALS_PATH = AGAMI_HOME / "credentials"
CONFIG_PATH = AGAMI_HOME / ".config"
STABLE_SERVE_DIR = AGAMI_HOME / "serve"
SCRIPT_DIR = Path(__file__).resolve().parent

# Files the server needs at runtime (both are self-contained stdlib modules).
SERVE_FILES = ("mcp_server.py", "execute_sql.py")

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
    """Read the profile's db type from ~/.agami/credentials (best-effort)."""
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


def stage_serve_files(scripts_dir: Path, in_place: bool) -> Path:
    """Return the directory the server will run from.

    Default: copy the server files to a stable ~/.agami/serve/ so the Desktop
    config is update-proof. --in-place: run straight from scripts_dir.

    Besides the two entry scripts, we also copy the `semantic_model/` package so
    the model-backed tools (get_datasource_schema / traversal) work from the
    serve dir — mcp_server.py adds its own dir to sys.path and imports it. Those
    tools additionally need `pydantic` + `sqlglot` in the Python the Desktop app
    launches; if they're absent the tool returns a clear "install the model deps"
    error and the stdlib execute_sql path keeps working regardless.
    """
    if in_place:
        return scripts_dir
    STABLE_SERVE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(STABLE_SERVE_DIR, 0o700)
    except OSError:
        pass
    for name in SERVE_FILES:
        src = scripts_dir / name
        if not src.exists():
            raise FileNotFoundError(f"missing {src} — run from the plugin's scripts dir or pass --scripts-dir")
        shutil.copy2(src, STABLE_SERVE_DIR / name)
    # Copy the semantic_model package (skip caches), replacing any prior copy.
    pkg_src = scripts_dir / "semantic_model"
    if pkg_src.is_dir():
        pkg_dst = STABLE_SERVE_DIR / "semantic_model"
        if pkg_dst.exists():
            shutil.rmtree(pkg_dst)
        shutil.copytree(pkg_src, pkg_dst, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    return STABLE_SERVE_DIR


def build_server_entry(python: str, serve_dir: Path, profile: str, version: str) -> dict:
    return {
        "command": python,
        "args": [str(serve_dir / "mcp_server.py")],
        "env": {"AGAMI_PROFILE": profile, "AGAMI_VERSION": version},
    }


def read_version(scripts_dir: Path) -> str:
    for rel in ("../../.claude-plugin/marketplace.json", "../.claude-plugin/plugin.json"):
        p = (scripts_dir / rel).resolve()
        try:
            text = p.read_text()
        except OSError:
            continue
        import re
        m = re.search(r'"version"\s*:\s*"([^"]+)"', text)
        if m:
            return m.group(1)
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
    p = argparse.ArgumentParser(description="Wire `agami serve` into the Claude Desktop app.")
    p.add_argument("--profile", default=None, help="agami profile to serve (default: active profile).")
    p.add_argument("--server-name", default="agami", help="Name of the MCP server entry (default: agami).")
    p.add_argument("--python", default=None, help="Force a specific python interpreter (absolute path).")
    p.add_argument("--scripts-dir", default=None, help="Where mcp_server.py lives (default: this script's dir).")
    p.add_argument("--config", default=None, help="Override the desktop config path (for testing / other clients).")
    p.add_argument("--in-place", action="store_true", help="Point at the scripts dir directly instead of copying to ~/.agami/serve.")
    p.add_argument("--dry-run", action="store_true", help="Print the plan; write nothing.")
    args = p.parse_args()

    scripts_dir = Path(args.scripts_dir).expanduser().resolve() if args.scripts_dir else SCRIPT_DIR
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
        serve_dir = stage_serve_files(scripts_dir, args.in_place)
    except FileNotFoundError as e:
        print(f"\nERROR: {e}", file=sys.stderr)
        return 2
    mode = "in-place (dev)" if args.in_place else f"copied to {serve_dir}"
    print(f"• server files   : {mode}")

    version = read_version(scripts_dir)
    entry = build_server_entry(python, serve_dir, profile, version)
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
