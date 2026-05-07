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
    """Resolve credentials from ~/.agami/credentials or AGAMI_DATABASE_URL.

    Resolution order:
      1. AGAMI_DATABASE_URL env var (full DSN, supports +driver suffixes)
      2. The selected profile in ~/.agami/credentials. Within the profile:
         - If `url = ...` is set, parse as a DSN (overrides per-field values)
         - Otherwise read host / port / user / password / database / type / sslmode
    """
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

    section = {k: v for k, v in cfg[profile].items()}

    # If the profile has `url = ...` (e.g. a Supabase / Neon / RDS DSN), parse it
    # and merge with any per-field overrides (sslmode, etc.) defined alongside.
    if "url" in section and section["url"]:
        from_dsn = _parse_dsn(section["url"])
        # Per-field values in the same section override DSN values, except for
        # `url` itself which we drop from the output.
        merged = dict(from_dsn)
        for k, v in section.items():
            if k == "url":
                continue
            merged[k] = v
        return merged

    return section


# Schemes we accept. Strip "+driver" suffixes (e.g. postgresql+asyncpg, postgres+psycopg2).
_POSTGRES_SCHEMES = {"postgres", "postgresql"}
_MYSQL_SCHEMES = {"mysql", "mariadb"}


def _parse_dsn(dsn: str) -> dict[str, str]:
    """Parse a database DSN into a credentials dict.

    Supported schemes (with or without `+driver` suffix):
      postgresql://, postgres://, postgresql+asyncpg://, postgresql+psycopg2://,
      postgresql+psycopg://, postgres+asyncpg:// — all map to type=postgres.
      mysql://, mariadb://, mysql+pymysql:// — all map to type=mysql.
      sqlite:///absolute/path/to.db — maps to type=sqlite.

    Cloud Postgres providers (Supabase, Neon, RDS, etc.) frequently use the
    SQLAlchemy-style `postgresql+asyncpg://...` form. We accept it.

    Query-string parameters on the DSN (e.g. `?sslmode=require`) are merged
    into the output dict — useful for SSL settings.
    """
    u = urllib.parse.urlparse(dsn)
    raw_scheme = u.scheme.lower()

    # Strip "+driver" suffix: "postgresql+asyncpg" → "postgresql"
    base_scheme = raw_scheme.split("+", 1)[0]

    if base_scheme in _POSTGRES_SCHEMES:
        db_type = "postgres"
        default_port = 5432
    elif base_scheme in _MYSQL_SCHEMES:
        db_type = "mysql"
        default_port = 3306
    elif base_scheme == "sqlite":
        # sqlite:///absolute/path or sqlite:relative/path
        path = dsn[len("sqlite://"):]
        if path.startswith("/"):
            path = path[1:] if path[1:2] == "/" else path  # handle `sqlite:////abs`
        # Trailing path normalization
        result = {"type": "sqlite", "path": path or u.path.lstrip("/")}
        return result
    else:
        sys.stderr.write(
            f"Unsupported scheme {raw_scheme!r}. "
            f"Supported: postgresql[+driver], postgres[+driver], "
            f"mysql[+driver], mariadb, sqlite.\n"
        )
        sys.exit(2)

    out: dict[str, str] = {
        "type": db_type,
        "host": u.hostname or "",
        "port": str(u.port or default_port),
        "user": urllib.parse.unquote(u.username or ""),
        "password": urllib.parse.unquote(u.password or ""),
        "database": (u.path or "").lstrip("/"),
    }

    # Merge any query-string params (e.g. ?sslmode=require)
    if u.query:
        for k, v in urllib.parse.parse_qsl(u.query):
            out[k.lower()] = v

    return out


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
