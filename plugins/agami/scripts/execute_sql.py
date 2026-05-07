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
CONFIG_PATH = Path.home() / ".agami" / ".config"
ALLOWED_PERMS = (0o600, 0o400)


def _resolve_default_profile() -> str:
    """Pick the default profile when --profile isn't passed and AGAMI_PROFILE is unset.

    Resolution order:
      1. AGAMI_PROFILE env var
      2. active_profile field in ~/.agami/.config
      3. The literal string "default" (legacy fallback)
    """
    env = os.environ.get("AGAMI_PROFILE")
    if env:
        return env
    if CONFIG_PATH.exists():
        try:
            import json as _json
            cfg = _json.loads(CONFIG_PATH.read_text())
            active = cfg.get("active_profile")
            if isinstance(active, str) and active:
                return active
        except (OSError, ValueError):
            pass
    return "default"


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
_REDSHIFT_SCHEMES = {"redshift"}        # speaks Postgres wire protocol; port 5439, SSL required
_SNOWFLAKE_SCHEMES = {"snowflake"}      # native CLI (snowsql) + snowflake-connector-python


def _parse_dsn(dsn: str) -> dict[str, str]:
    """Parse a database DSN into a credentials dict.

    Supported schemes (with or without `+driver` suffix):
      postgresql://, postgres://, postgresql+asyncpg://, postgresql+psycopg2://,
      postgresql+psycopg://, postgres+asyncpg:// — all map to type=postgres.
      mysql://, mariadb://, mysql+pymysql:// — all map to type=mysql.
      sqlite:///absolute/path/to.db — maps to type=sqlite.
      redshift://user:pass@cluster.region.redshift.amazonaws.com:5439/db — type=redshift
      snowflake://user:pass@account.region.cloud/database/schema?warehouse=wh&role=r
        — type=snowflake. The path is `/database` or `/database/schema`. Query
        params (warehouse, role, application, authenticator) are carried over.

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
    elif base_scheme in _REDSHIFT_SCHEMES:
        # Redshift speaks Postgres wire protocol → reuse postgres execution path.
        # The only thing that's different is the default port (5439 vs 5432) and
        # that SSL is required by default.
        db_type = "redshift"
        default_port = 5439
    elif base_scheme in _MYSQL_SCHEMES:
        db_type = "mysql"
        default_port = 3306
    elif base_scheme in _SNOWFLAKE_SCHEMES:
        db_type = "snowflake"
        default_port = 443  # Snowflake is HTTPS-only; port not used by snowsql/connector
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
            f"Supported: postgresql[+driver], postgres[+driver], redshift, "
            f"mysql[+driver], mariadb, snowflake, sqlite.\n"
        )
        sys.exit(2)

    # Snowflake's URL is account-shaped, not host:port. The "hostname" portion
    # of `snowflake://user:pw@xy12345.us-east-1.aws/MYDB/PUBLIC` is the account
    # identifier, and the path holds DATABASE[/SCHEMA].
    if db_type == "snowflake":
        path_parts = (u.path or "").lstrip("/").split("/")
        out = {
            "type": "snowflake",
            "account": u.hostname or "",
            "user": urllib.parse.unquote(u.username or ""),
            "password": urllib.parse.unquote(u.password or ""),
            "database": path_parts[0] if path_parts and path_parts[0] else "",
        }
        if len(path_parts) > 1 and path_parts[1]:
            out["schema"] = path_parts[1]
        # Carry warehouse, role, application, authenticator from query params
        if u.query:
            for k, v in urllib.parse.parse_qsl(u.query):
                out[k.lower()] = v
        return out

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

    # Redshift defaults: SSL required if not explicitly set
    if db_type == "redshift" and "sslmode" not in out:
        out["sslmode"] = "require"

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


def _execute_snowflake(creds: dict[str, str], sql: str) -> int:
    """Tier-3 path for Snowflake using snowflake-connector-python."""
    try:
        import snowflake.connector  # type: ignore
    except ImportError:
        return _err(
            "snowflake-connector-python not installed. "
            "Run: pip install snowflake-connector-python",
            code=3,
        )
    _require(creds, "account", "user")
    if not (creds.get("password") or creds.get("authenticator")):
        return _err(
            "Snowflake profile is missing 'password' or 'authenticator'. "
            "Add one to ~/.agami/credentials.",
            code=2,
        )
    conn_kwargs: dict[str, Any] = {
        "account": creds["account"],
        "user": creds["user"],
        "client_session_keep_alive": False,
        "login_timeout": 15,
    }
    for k in ("password", "warehouse", "database", "schema", "role", "authenticator"):
        if creds.get(k):
            conn_kwargs[k] = creds[k]
    try:
        conn = snowflake.connector.connect(**conn_kwargs)
    except Exception as e:
        return _err(f"Snowflake connect failed: {e}", code=4)
    try:
        cur = conn.cursor()
        cur.execute(sql)
        _write_cursor_csv(cur)
    except Exception as e:
        return _err(f"Snowflake execution error: {e}", code=5)
    finally:
        try:
            conn.close()
        except Exception:
            pass
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
    p.add_argument(
        "--profile",
        default=None,
        help="Credentials profile to use. Defaults to AGAMI_PROFILE env, then .config.active_profile, then 'default'.",
    )
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--sql", help="SQL statement (use --sql-file for SQL with special characters)")
    src.add_argument("--sql-file", help="Path to a file containing one SQL statement")
    args = p.parse_args()

    if args.sql_file:
        sql = Path(os.path.expanduser(args.sql_file)).read_text()
    else:
        sql = args.sql

    profile = args.profile or _resolve_default_profile()
    creds = _load_credentials(profile)
    db_type = creds.get("type", "").lower()
    if not db_type:
        return _err(f"Credentials profile [{profile}] is missing the 'type' field.")
    if db_type == "postgres":
        return _execute_postgres(creds, sql)
    if db_type == "redshift":
        # Redshift speaks Postgres wire protocol; psycopg2 connects fine.
        # The credentials dict has type=redshift, but _execute_postgres reads
        # host/port/etc. directly so the type field doesn't matter.
        if "sslmode" not in creds:
            creds = {**creds, "sslmode": "require"}
        return _execute_postgres(creds, sql)
    if db_type == "mysql":
        return _execute_mysql(creds, sql)
    if db_type == "sqlite":
        return _execute_sqlite(creds, sql)
    if db_type == "snowflake":
        return _execute_snowflake(creds, sql)
    return _err(
        f"Unsupported db type {db_type!r}. "
        f"Supported: postgres, redshift, mysql, sqlite, snowflake."
    )


if __name__ == "__main__":
    sys.exit(main())
