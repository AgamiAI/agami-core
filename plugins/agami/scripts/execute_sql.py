#!/usr/bin/env python3
"""
Tier 3 — Python execution helper.

Reads ~/.agami/credentials (INI), opens a connection to the configured
database via the appropriate Python driver, runs ONE SQL statement, and
writes the result as RFC 4180 CSV to stdout. Stdlib + driver-only.

The agami skill calls this when it detects tier=python in ~/.agami/.config
(meaning native CLI tools are unavailable but the relevant Python driver
is importable). Connect-side and query-database both shell out to:

    python3 scripts/execute_sql.py --profile <profile> --sql-file <path>

The --sql-file form is preferred over --sql so SQL containing quotes,
backticks, or `$` doesn't get mangled by the shell.

Connects ONLY to the host/port in ~/.agami/credentials (or
AGAMI_DATABASE_URL). Never substitutes localhost. Never asks for
credentials. Hard exits with a clear message if credentials are missing.

Drivers (install only what you need):
    pip install psycopg2-binary    # Postgres
    pip install pymysql            # MySQL
    # SQLite uses the stdlib `sqlite3` module — no install needed.

Exit codes:
    0  — success, CSV on stdout
    2  — usage / config error (missing credentials, bad profile, etc.)
    3  — driver missing for the configured db type
    4  — connection / authentication failed
    5  — SQL execution error (syntax, unknown column, etc.)
"""

from __future__ import annotations

import argparse
import configparser
import csv
import os
import stat
import sys
import urllib.parse
from pathlib import Path
from typing import Any


CREDENTIALS_PATH = Path.home() / ".agami" / "credentials"
ALLOWED_PERMS = (0o600, 0o400)


def _err(msg: str, *, code: int = 2) -> int:
    sys.stderr.write(f"{msg}\n")
    return code


def _load_credentials(profile: str) -> dict[str, str]:
    """Resolve credentials from ~/.agami/credentials or AGAMI_DATABASE_URL."""
    env_dsn = os.environ.get("AGAMI_DATABASE_URL")
    if env_dsn:
        return _parse_dsn(env_dsn)

    if not CREDENTIALS_PATH.exists():
        sys.stderr.write(
            "~/.agami/credentials is missing. Run the agami `init` skill to set it up.\n"
            "Never type credentials into chat — they belong in the file.\n"
        )
        sys.exit(2)

    # chmod check: refuse if too permissive
    mode = stat.S_IMODE(CREDENTIALS_PATH.stat().st_mode)
    if mode not in ALLOWED_PERMS:
        sys.stderr.write(
            f"~/.agami/credentials must be chmod 600 (currently {oct(mode)[2:]})\n"
            f"Run: chmod 600 ~/.agami/credentials\n"
        )
        sys.exit(2)

    cfg = configparser.ConfigParser()
    cfg.read(CREDENTIALS_PATH)
    if profile not in cfg:
        sys.stderr.write(
            f"Profile [{profile}] not found in ~/.agami/credentials. "
            f"Sections present: {cfg.sections()}\n"
        )
        sys.exit(2)
    return {k: v for k, v in cfg[profile].items()}


def _parse_dsn(dsn: str) -> dict[str, str]:
    """Parse postgres://user:pass@host:port/database or mysql:// or sqlite:///path."""
    u = urllib.parse.urlparse(dsn)
    scheme = u.scheme
    if scheme.startswith("postgres"):
        scheme = "postgres"
    elif scheme == "mysql" or scheme.startswith("mariadb"):
        scheme = "mysql"
    elif scheme == "sqlite":
        return {"type": "sqlite", "path": dsn[len("sqlite://"):].lstrip("/") or u.path}
    else:
        sys.stderr.write(f"AGAMI_DATABASE_URL has unsupported scheme: {scheme}\n")
        sys.exit(2)

    return {
        "type": scheme,
        "host": u.hostname or "",
        "port": str(u.port or {"postgres": 5432, "mysql": 3306}[scheme]),
        "user": urllib.parse.unquote(u.username or ""),
        "password": urllib.parse.unquote(u.password or ""),
        "database": (u.path or "").lstrip("/"),
    }


def _require(creds: dict[str, str], *fields: str) -> None:
    missing = [f for f in fields if not creds.get(f)]
    if missing:
        sys.stderr.write(
            f"Credentials profile is missing required fields: {missing}. "
            f"Edit ~/.agami/credentials and add them.\n"
        )
        sys.exit(2)


def _execute_postgres(creds: dict[str, str], sql: str) -> int:
    try:
        import psycopg2  # type: ignore
    except ImportError:
        return _err("psycopg2 not installed. Run: pip install psycopg2-binary", code=3)
    _require(creds, "host", "port", "user", "password", "database")
    try:
        conn = psycopg2.connect(
            host=creds["host"],
            port=int(creds["port"]),
            user=creds["user"],
            password=creds["password"],
            dbname=creds["database"],
            sslmode=creds.get("sslmode", "prefer"),
            connect_timeout=10,
        )
    except Exception as e:
        return _err(f"Postgres connect failed: {e}", code=4)
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(sql)
                _write_cursor_csv(cur)
    except Exception as e:
        return _err(f"Postgres execution error: {e}", code=5)
    finally:
        conn.close()
    return 0


def _execute_mysql(creds: dict[str, str], sql: str) -> int:
    try:
        import pymysql  # type: ignore
    except ImportError:
        return _err("pymysql not installed. Run: pip install pymysql", code=3)
    _require(creds, "host", "port", "user", "password", "database")
    try:
        conn = pymysql.connect(
            host=creds["host"],
            port=int(creds["port"]),
            user=creds["user"],
            password=creds["password"],
            database=creds["database"],
            charset="utf8mb4",
            connect_timeout=10,
            autocommit=True,
        )
    except Exception as e:
        return _err(f"MySQL connect failed: {e}", code=4)
    try:
        with conn.cursor() as cur:
            cur.execute(sql)
            _write_cursor_csv(cur)
    except Exception as e:
        return _err(f"MySQL execution error: {e}", code=5)
    finally:
        conn.close()
    return 0


def _execute_sqlite(creds: dict[str, str], sql: str) -> int:
    import sqlite3  # always available in stdlib
    _require(creds, "path")
    path = os.path.expanduser(creds["path"])
    try:
        conn = sqlite3.connect(path)
    except Exception as e:
        return _err(f"SQLite connect failed: {e}", code=4)
    try:
        cur = conn.cursor()
        cur.execute(sql)
        _write_cursor_csv(cur)
    except Exception as e:
        return _err(f"SQLite execution error: {e}", code=5)
    finally:
        conn.close()
    return 0


def _write_cursor_csv(cur: Any) -> None:
    writer = csv.writer(sys.stdout)
    if cur.description is not None:
        writer.writerow([d[0] for d in cur.description])
        for row in cur.fetchall():
            writer.writerow(row)


def main() -> int:
    p = argparse.ArgumentParser(
        description="Tier-3 Python SQL executor for agami. Reads credentials, runs SQL, emits CSV.",
    )
    p.add_argument("--profile", default=os.environ.get("AGAMI_PROFILE", "default"))
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--sql", help="SQL statement (use --sql-file for SQL with special characters)")
    src.add_argument("--sql-file", help="Path to a file containing one SQL statement")
    args = p.parse_args()

    if args.sql_file:
        sql = Path(os.path.expanduser(args.sql_file)).read_text()
    else:
        sql = args.sql

    creds = _load_credentials(args.profile)
    db_type = creds.get("type", "").lower()
    if not db_type:
        return _err(f"Credentials profile [{args.profile}] is missing the 'type' field.")
    if db_type == "postgres":
        return _execute_postgres(creds, sql)
    if db_type == "mysql":
        return _execute_mysql(creds, sql)
    if db_type == "sqlite":
        return _execute_sqlite(creds, sql)
    return _err(f"Unsupported db type {db_type!r}. Supported: postgres, mysql, sqlite.")


if __name__ == "__main__":
    sys.exit(main())
