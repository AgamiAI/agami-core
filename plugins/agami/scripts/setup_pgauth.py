#!/usr/bin/env python3
"""
Materialize provider-native auth files from <artifacts_dir>/local/credentials so the
agami skill can run psql / mysql WITHOUT ever putting the password on the
command line.

Why this exists: invoking psql via `export PGPASSWORD='<literal>' psql ...`
puts the password in the visible Bash command. Claude Code hosts (CLI, VS
Code extension, Cursor extension) render Bash tool calls in their UI, so
the password leaks into the chat transcript. The fix is provider-native
auth files that psql / mysql read automatically:

    <artifacts_dir>/local/.pgpass         (chmod 600) — postgres "host:port:db:user:password"
    <artifacts_dir>/local/.mysql.cnf      (chmod 600) — mysql "[client_<profile>]\\npassword=..."

Skills then run:
    PGPASSFILE=<artifacts_dir>/local/.pgpass psql -h ... -p ... -U ... -d ... -c "$SQL" --csv
    mysql --defaults-file=<artifacts_dir>/local/.mysql.cnf --defaults-group-suffix=_<profile> ...

The visible Bash command contains NO password. The auth files are chmod 600,
same protection as `<artifacts_dir>/local/credentials` itself.

Usage:
    # Materialize for the active profile (init invokes it like this):
    python3 setup_pgauth.py

    # Or specify a profile:
    python3 setup_pgauth.py --profile staging

    # Or all profiles in the credentials file at once:
    python3 setup_pgauth.py --all

The script is idempotent. It rewrites the auth files atomically, never appends
duplicate entries, never echoes credentials to stdout/stderr.

Exit codes:
    0  success
    2  configuration error (missing credentials, bad profile)
"""

from __future__ import annotations

import argparse
import configparser
import json
import os
import sys
import tempfile
from pathlib import Path

# agami_paths + the executor live in the agami-core package; the resolver makes them importable in
# every layout (pip-installed / the plugin's bundled lib / a dev checkout) with no pip required.
from _agami_lib import ensure_importable  # noqa: E402

ensure_importable()
import agami_paths  # noqa: E402
from execute_sql import ExecutorError, _parse_dsn  # reuse DSN parsing logic  # noqa: E402

# NOTE: never bootstrap() at import — this module is imported by build_duckdb_attach and
# tests. The one-shot legacy migration runs only from main() (and the other entry points).
AGAMI_HOME = agami_paths.local_dir()  # <artifacts_dir>/local — materialized auth files live here
CREDENTIALS_PATH = AGAMI_HOME / "credentials"
CONFIG_PATH = AGAMI_HOME / ".config"
PGPASS_PATH = AGAMI_HOME / ".pgpass"
MYSQL_CNF_PATH = AGAMI_HOME / ".mysql.cnf"
# Snowflake's official CLI (snowsql) reads from ~/.snowsql/config — agami
# writes its own copy in <artifacts_dir>/local/ so the password file stays alongside the
# other agami creds with chmod 600. The skill invokes snowsql with
# `--config <path>` to point at this file.
SNOWSQL_CONFIG_PATH = AGAMI_HOME / ".snowsql.cnf"


def _load_section(profile: str) -> dict[str, str]:
    """Load credentials for one profile, parsing url= if present.

    Returns a dict with at least {type, host, port, database, user, password}
    for postgres/mysql, or {type, path} for sqlite.
    """
    if not CREDENTIALS_PATH.exists():
        sys.stderr.write(
            "<artifacts_dir>/local/credentials is missing. Run the agami init skill to set it up.\n"
        )
        sys.exit(2)

    # Strip inline comments so values don't carry trailing "# notes" — see
    # execute_sql.py for the same fix and why it matters (configparser default
    # leaves "xy12345 # locator..." as the value).
    cfg = configparser.ConfigParser(inline_comment_prefixes=("#", ";"))
    cfg.read(CREDENTIALS_PATH)
    if profile not in cfg:
        sys.stderr.write(
            f"Profile [{profile}] not found in <artifacts_dir>/local/credentials. "
            f"Sections present: {cfg.sections()}\n"
        )
        sys.exit(2)

    section = {k: (v.strip() if isinstance(v, str) else v) for k, v in cfg[profile].items()}
    if "url" in section and section["url"]:
        # `_parse_dsn` now raises ExecutorError (not sys.exit) on a bad scheme so it's safe in-process;
        # this script keeps its clean CLI UX (message on stderr, exit 2) by translating it here.
        try:
            from_dsn = _parse_dsn(section["url"])
        except ExecutorError as e:
            sys.stderr.write(e.msg + "\n")
            sys.exit(2)
        merged = dict(from_dsn)
        for k, v in section.items():
            if k == "url":
                continue
            merged[k] = v
        return merged
    return section


def _resolve_default_profile() -> str:
    """Active profile resolution (mirrors execute_sql._resolve_default_profile)."""
    env = os.environ.get("AGAMI_PROFILE")
    if env:
        return env
    if CONFIG_PATH.exists():
        try:
            cfg = json.loads(CONFIG_PATH.read_text())
            active = cfg.get("active_profile")
            if isinstance(active, str) and active:
                return active
        except (OSError, ValueError):
            pass
    return "default"


# --- pgpass ---------------------------------------------------------------

def _pgpass_escape(value: str) -> str:
    """Escape `:` and `\\` per the pgpass file format."""
    return value.replace("\\", "\\\\").replace(":", "\\:")


def _build_pgpass_line(creds: dict[str, str]) -> str:
    parts = [
        creds.get("host", ""),
        str(creds.get("port", 5432)),
        creds.get("database", "*"),
        creds.get("user", ""),
        creds.get("password", ""),
    ]
    return ":".join(_pgpass_escape(p) for p in parts)


def _write_pgpass(profile_lines: dict[str, str]) -> None:
    """Write <artifacts_dir>/local/.pgpass with one line per postgres profile.

    profile_lines maps profile name → pgpass-format line. The profile name
    is written as a comment above each line for traceability.
    """
    AGAMI_HOME.mkdir(mode=0o700, exist_ok=True)
    body = []
    for profile, line in sorted(profile_lines.items()):
        body.append(f"# profile: {profile}")
        body.append(line)
    contents = "\n".join(body) + "\n" if body else ""
    _atomic_write(PGPASS_PATH, contents, mode=0o600)


# --- mysql cnf -----------------------------------------------------------

def _build_mysql_section(profile: str, creds: dict[str, str]) -> str:
    """Build a [client_<profile>] section for ~/.my.cnf-style file.

    psql honors PGPASSFILE; mysql honors --defaults-file with
    --defaults-group-suffix=_<profile> which reads [client_<profile>].
    """
    suffix = profile  # group is [client_<profile>] when --defaults-group-suffix=_<profile>
    lines = [f"[client_{suffix}]"]
    if creds.get("host"):
        lines.append(f"host={creds['host']}")
    if creds.get("port"):
        lines.append(f"port={creds['port']}")
    if creds.get("user"):
        lines.append(f"user={creds['user']}")
    if creds.get("password"):
        lines.append(f"password={creds['password']}")
    if creds.get("database"):
        lines.append(f"database={creds['database']}")
    return "\n".join(lines)


def _write_mysql_cnf(profile_sections: dict[str, str]) -> None:
    AGAMI_HOME.mkdir(mode=0o700, exist_ok=True)
    body = []
    for profile in sorted(profile_sections):
        body.append(profile_sections[profile])
        body.append("")  # blank line between sections
    contents = "\n".join(body).rstrip() + "\n" if body else ""
    _atomic_write(MYSQL_CNF_PATH, contents, mode=0o600)


# --- snowsql config -------------------------------------------------------

def _build_snowsql_section(profile: str, creds: dict[str, str]) -> str:
    """Build a [connections.<profile>] section for snowsql config.

    snowsql reads `[connections.<name>]` blocks via `snowsql -c <name>`.
    Required: account, user, password (or authenticator). Optional:
    warehouse, database, schema, role.
    """
    lines = [f"[connections.{profile}]"]
    if creds.get("account"):
        lines.append(f"accountname = {creds['account']}")
    if creds.get("user"):
        lines.append(f"username = {creds['user']}")
    if creds.get("password"):
        lines.append(f"password = {creds['password']}")
    if creds.get("authenticator"):
        lines.append(f"authenticator = {creds['authenticator']}")
    if creds.get("warehouse"):
        lines.append(f"warehousename = {creds['warehouse']}")
    if creds.get("database"):
        lines.append(f"dbname = {creds['database']}")
    if creds.get("schema"):
        lines.append(f"schemaname = {creds['schema']}")
    if creds.get("role"):
        lines.append(f"rolename = {creds['role']}")
    return "\n".join(lines)


def _write_snowsql_config(profile_sections: dict[str, str]) -> None:
    AGAMI_HOME.mkdir(mode=0o700, exist_ok=True)
    body = []
    for profile in sorted(profile_sections):
        body.append(profile_sections[profile])
        body.append("")  # blank line between sections
    contents = "\n".join(body).rstrip() + "\n" if body else ""
    _atomic_write(SNOWSQL_CONFIG_PATH, contents, mode=0o600)


# --- atomic write ---------------------------------------------------------

def _atomic_write(path: Path, contents: str, mode: int) -> None:
    """Atomic write: temp file in same dir + rename, with chmod set before rename."""
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(contents)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# --- main -----------------------------------------------------------------

def materialize(profiles: list[str]) -> int:
    """Read credentials for the given profiles; write the right auth files.

    Per-type routing:
      postgres / redshift → <artifacts_dir>/local/.pgpass entry (Redshift speaks Postgres
        wire protocol; psql/.pgpass work as-is, port 5439 vs 5432)
      mysql → <artifacts_dir>/local/.mysql.cnf section
      snowflake → <artifacts_dir>/local/.snowsql.cnf [connections.<profile>] section
      sqlite → no auth file (file path is in credentials directly)
    """
    pg_lines: dict[str, str] = {}
    mysql_sections: dict[str, str] = {}
    snowsql_sections: dict[str, str] = {}

    for profile in profiles:
        creds = _load_section(profile)
        db_type = creds.get("type", "").lower()
        if db_type in ("postgres", "redshift"):
            pg_lines[profile] = _build_pgpass_line(creds)
        elif db_type == "mysql":
            mysql_sections[profile] = _build_mysql_section(profile, creds)
        elif db_type == "snowflake":
            snowsql_sections[profile] = _build_snowsql_section(profile, creds)
        elif db_type == "sqlite":
            pass  # no auth file needed for sqlite
        else:
            sys.stderr.write(
                f"Profile [{profile}] has unsupported type {db_type!r}; skipping.\n"
            )

    if pg_lines:
        _write_pgpass(pg_lines)
    if mysql_sections:
        _write_mysql_cnf(mysql_sections)
    if snowsql_sections:
        _write_snowsql_config(snowsql_sections)
    return 0


def main() -> int:
    global AGAMI_HOME, CREDENTIALS_PATH, CONFIG_PATH, PGPASS_PATH, MYSQL_CNF_PATH, SNOWSQL_CONFIG_PATH
    agami_paths.bootstrap()
    AGAMI_HOME = agami_paths.local_dir()
    CREDENTIALS_PATH = AGAMI_HOME / "credentials"
    CONFIG_PATH = AGAMI_HOME / ".config"
    PGPASS_PATH = AGAMI_HOME / ".pgpass"
    MYSQL_CNF_PATH = AGAMI_HOME / ".mysql.cnf"
    SNOWSQL_CONFIG_PATH = AGAMI_HOME / ".snowsql.cnf"
    p = argparse.ArgumentParser(
        description=(
            "Materialize provider-native auth files (.pgpass / .mysql.cnf) from "
            "the credentials file so psql / mysql can run without exposing passwords "
            "on the Bash command line."
        )
    )
    g = p.add_mutually_exclusive_group()
    g.add_argument("--profile", help="Profile name (default: AGAMI_PROFILE / .config.active_profile / 'default')")
    g.add_argument("--all", action="store_true", help="Materialize all profiles in the credentials file")
    args = p.parse_args()

    if args.all:
        if not CREDENTIALS_PATH.exists():
            sys.stderr.write("<artifacts_dir>/local/credentials is missing.\n")
            return 2
        cfg = configparser.ConfigParser(inline_comment_prefixes=("#", ";"))
        cfg.read(CREDENTIALS_PATH)
        profiles = cfg.sections()
    else:
        profiles = [args.profile or _resolve_default_profile()]

    return materialize(profiles)


if __name__ == "__main__":
    sys.exit(main())
