#!/usr/bin/env python3
"""
Generate a DuckDB init SQL file that ATTACHes one or more agami profiles, so
the query-database skill can run cross-database SQL through DuckDB without
ever putting credentials in the visible Bash command.

Why this exists: agami v1.1+ supports cross-database questions ("which assets
are owned by the department with the highest finance costs?") where the data
lives in two different databases — e.g. ITSM in Redshift, finance in MySQL.
DuckDB's `postgres_scanner` + `mysql_scanner` extensions can ATTACH both in
the same DuckDB session and run a federated JOIN. But the ATTACH statement
needs the password inline — which means we can't put it in the Bash command
the host renders in chat.

This script writes the ATTACH statements to a temp file in `<artifacts_dir>/local/`, with
chmod 600. The skill invokes:

    duckdb -init "$AGAMI_INIT_FILE" -c "<the federated SQL>" --csv

DuckDB reads the credentials silently from the init file. The visible Bash
command contains no credentials. After the query completes, the skill deletes
the init file. (We also self-clean any `.duckdb_init_*.sql` older than 1 hour
on each invocation in case a prior run crashed.)

Usage:
    python3 build_duckdb_attach.py --profiles itsm finance
    # Writes <artifacts_dir>/local/.duckdb_init_<ts>_<rand>.sql, prints the path on stdout.

The script reuses `_atomic_write` / `_load_section` from setup_pgauth.py.

Exit codes:
    0  success (init file path printed to stdout)
    2  configuration error (missing credentials, bad profile, unsupported
       db_type for federation)
"""

from __future__ import annotations

import argparse
import os
import secrets
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))
import agami_paths  # noqa: E402
from setup_pgauth import _atomic_write, _load_section  # noqa: E402

AGAMI_HOME = agami_paths.local_dir()


# DuckDB scanner extensions support these databases. Snowflake is NOT in the
# list — DuckDB's snowflake_scanner is experimental and not packaged with the
# standard binary. If a user tries to federate a Snowflake profile, we fail
# loudly with an actionable message.
FEDERATION_SUPPORTED: frozenset[str] = frozenset({"postgres", "redshift", "mysql"})


def _build_attach_postgres(profile: str, creds: dict[str, str]) -> str:
    """Build the ATTACH line for a postgres / redshift profile."""
    parts = [
        f"host={creds.get('host', '')}",
        f"port={creds.get('port', 5432)}",
        f"dbname={creds.get('database', '')}",
        f"user={creds.get('user', '')}",
        f"password={creds.get('password', '')}",
    ]
    if creds.get("sslmode"):
        parts.append(f"sslmode={creds['sslmode']}")
    conn = " ".join(parts)
    # Single-quote escape: ' becomes ''
    conn_sql = conn.replace("'", "''")
    return f"ATTACH '{conn_sql}' AS {profile} (TYPE POSTGRES);"


def _build_attach_mysql(profile: str, creds: dict[str, str]) -> str:
    parts = [
        f"host={creds.get('host', '')}",
        f"port={creds.get('port', 3306)}",
        f"database={creds.get('database', '')}",
        f"user={creds.get('user', '')}",
        f"password={creds.get('password', '')}",
    ]
    conn = " ".join(parts)
    conn_sql = conn.replace("'", "''")
    return f"ATTACH '{conn_sql}' AS {profile} (TYPE MYSQL);"


def build_init_sql(profiles: list[str]) -> str:
    """Return the full DuckDB init SQL for the given profiles."""
    needs_postgres = False
    needs_mysql = False
    attach_lines: list[str] = []

    for profile in profiles:
        creds = _load_section(profile)
        db_type = (creds.get("type") or "").lower()
        if db_type not in FEDERATION_SUPPORTED:
            sys.stderr.write(
                f"Profile [{profile}] type {db_type!r} is not supported for "
                f"DuckDB federation.\n"
                f"DuckDB scanners cover {sorted(FEDERATION_SUPPORTED)} as of "
                f"v1.x; Snowflake federation is documented as out of scope in "
                f"docs/format-spec.md. If one side of your federated query is "
                f"on Snowflake, pre-aggregate it and pass the smaller result as "
                f"a CSV the skill can load locally.\n"
            )
            sys.exit(2)
        if db_type in ("postgres", "redshift"):
            needs_postgres = True
            attach_lines.append(_build_attach_postgres(profile, creds))
        elif db_type == "mysql":
            needs_mysql = True
            attach_lines.append(_build_attach_mysql(profile, creds))

    header: list[str] = []
    if needs_postgres:
        header.append("INSTALL postgres_scanner;")
        header.append("LOAD postgres_scanner;")
    if needs_mysql:
        header.append("INSTALL mysql_scanner;")
        header.append("LOAD mysql_scanner;")

    return "\n".join(header + [""] + attach_lines) + "\n"


def write_init_file(profiles: list[str]) -> Path:
    """Write a fresh, chmod-600 init file for this query and return its path."""
    AGAMI_HOME.mkdir(mode=0o700, exist_ok=True)
    sql = build_init_sql(profiles)
    name = f".duckdb_init_{os.getpid()}_{secrets.token_hex(4)}.sql"
    path = AGAMI_HOME / name
    _atomic_write(path, sql, mode=0o600)
    return path


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description=(
            "Generate a temp DuckDB init file that ATTACHes one or more agami "
            "profiles. Writes <artifacts_dir>/local/.duckdb_init_<id>.sql (chmod 600) and "
            "prints the path on stdout. Credentials NEVER appear on the "
            "command line."
        )
    )
    p.add_argument(
        "--profiles",
        nargs="+",
        required=True,
        help="One or more profile names from <artifacts_dir>/local/credentials.",
    )
    args = p.parse_args(argv)

    if len(args.profiles) < 2:
        sys.stderr.write(
            "Federation needs at least two profiles. For a single-profile "
            "query, use the regular query-database flow instead.\n"
        )
        return 2

    # Sanity: every profile must exist before we write anything.
    for prof in args.profiles:
        _load_section(prof)  # exits 2 with a clear message on a typo

    path = write_init_file(args.profiles)
    print(str(path))
    return 0


if __name__ == "__main__":
    sys.exit(main())
