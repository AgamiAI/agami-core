#!/usr/bin/env python3
"""
Tier 3 — Python execution helper.

Reads <artifacts_dir>/local/credentials (INI), opens a connection to the configured
database via the appropriate Python driver, runs ONE SQL statement, and
writes the result as RFC 4180 CSV to stdout. Stdlib + driver-only.

The agami skill calls this when it detects tier=python in <artifacts_dir>/local/.config
(meaning native CLI tools are unavailable but the relevant Python driver
is importable). Connect-side and query-database both shell out to:

    python3 scripts/execute_sql.py --profile <profile> --sql-file <path>

The --sql-file form is preferred over --sql so SQL containing quotes,
backticks, or `$` doesn't get mangled by the shell.

Credentials resolve env-first then file: a DSN in DATASOURCE_URL[__<PROFILE>] (the
self-host channel), else <artifacts_dir>/local/credentials. Connects ONLY to the
host/port that resolution yields. Never substitutes localhost. Never asks for
credentials. Hard exits with a clear message if neither source has them.

Drivers (install only what you need):
    pip install psycopg2-binary             # Postgres / Redshift
    pip install pymysql                     # MySQL
    pip install snowflake-connector-python  # Snowflake
    pip install google-cloud-bigquery       # BigQuery
    # SQLite uses the stdlib `sqlite3` module — no install needed.

Exit codes:
    0  — success, CSV on stdout
    1  — SQL rejected by the read-only guard (kind="permission" JSON on stderr)
    2  — usage / config error (missing credentials, bad profile, etc.)
    3  — driver missing for the configured db type
    4  — connection / authentication failed
    5  — SQL execution error (syntax, unknown column, etc.)
"""

from __future__ import annotations

import argparse
import configparser
import csv
import json
import os
import stat
import sys
import urllib.parse
from pathlib import Path
from typing import Any

import agami_paths

# Credentials + config now live under <artifacts_dir>/local/ (the consolidated,
# gitignored replacement for ~/.agami). The path is stable regardless of migration
# timing — bootstrap() just moves the files into it. See agami_paths.
CREDENTIALS_PATH = agami_paths.credentials_path()
CONFIG_PATH = agami_paths.config_path()
ALLOWED_PERMS = (0o600, 0o400)


def _resolve_default_profile() -> str:
    """Pick the default profile when --profile isn't passed and AGAMI_PROFILE is unset.

    Resolution order:
      1. AGAMI_PROFILE env var
      2. active_profile field in <artifacts_dir>/local/.config
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


def _env_token(profile: str) -> str:
    """The env-var suffix for a datasource: the profile id upper-cased with every non-alphanumeric char
    folded to `_` (so `sales-pg` → `SALES_PG`, used as `DATASOURCE_URL__SALES_PG`)."""
    return "".join(c if c.isalnum() else "_" for c in profile).upper()


def _env_datasource_dsn(profile: str) -> str | None:
    """A warehouse DSN supplied via the environment for `profile`, or None.

    Two forms, checked in order:
      1. DATASOURCE_URL__<PROFILE> — per-datasource. <PROFILE> is the profile id
         upper-cased with every non-alphanumeric char folded to `_` (so `sales-pg`
         → DATASOURCE_URL__SALES_PG). Lets a deployment carry heterogeneous
         warehouses side by side, one var each.
      2. DATASOURCE_URL — the single-datasource default.

    This is the container / self-host credential channel (cf. how the model reads
    from Postgres when configured, else the file): env carries no file mode and is
    inherited by this subprocess, so it sidesteps the mounted-secret + chmod-600
    problems the file has under a container uid that doesn't own it.

    Scope / gotchas (deliberately minimal — the file remains the fuller channel):
      - A DSN carries the same expressiveness as the file's `url = ...` field, so the
        env channel supports the schemes `_parse_dsn` handles (postgres / redshift /
        mysql / snowflake / bigquery / sqlite). A warehouse type without a DSN scheme
        (databricks, oracle, sqlserver, trino, duckdb) still uses the per-field file.
      - The value is stripped (secret stores / `.env` / `$(cat file)` commonly append a
        trailing newline, which would otherwise mis-parse), and an empty-or-whitespace-only
        value is treated as unset (falls through to the next source) — set the var to a real
        DSN to take effect; don't set it to "" expecting to *disable* one.
      - The token folds every non-alphanumeric char to `_`, so profiles differing only in
        punctuation (`sales-pg` vs `sales.pg`) map to the same var — name profiles distinctly.
    """
    token = _env_token(profile)
    for name in (f"DATASOURCE_URL__{token}", "DATASOURCE_URL"):
        val = os.environ.get(name)
        if val and val.strip():
            return val.strip()  # match the file path's per-field .strip()
    return None


def _load_credentials(profile: str) -> dict[str, str]:
    """Resolve credentials for `profile`, env-first then the file.

    Source order:
      1. A DSN from the environment (DATASOURCE_URL[__<PROFILE>]) — the self-host
         channel; parsed by `_parse_dsn`, no file read, no chmod gate.
      2. <artifacts_dir>/local/credentials (the local-plugin default), where within
         the selected profile a `url = ...` DSN (merged with per-field overrides) or
         per-field host / port / user / password / database / type / sslmode is read.

    The env is an added *source*, not a fork: the file path — and its chmod-600 gate
    (never on a command line) — is unchanged, and is skipped only when a deployment
    opts into the env var (and so has no file to protect).
    """
    dsn = _env_datasource_dsn(profile)
    if dsn:
        return _parse_dsn(dsn)

    if not CREDENTIALS_PATH.exists():
        sys.stderr.write(
            f"No warehouse credentials for profile [{profile}]. Set DATASOURCE_URL "
            f"(or DATASOURCE_URL__{_env_token(profile)}) "
            "in the environment, or create <artifacts_dir>/local/credentials via the agami `init` skill.\n"
            "Never type credentials into chat — they belong in the environment or the file.\n"
        )
        sys.exit(2)

    # chmod check: refuse if too permissive. POSIX only — Windows file modes don't
    # map to Unix permission bits (NTFS ACLs guard the file; a stat() there reports
    # ~0o666, which would wrongly trip this gate and block the credentials read).
    if os.name == "posix":
        mode = stat.S_IMODE(CREDENTIALS_PATH.stat().st_mode)
        if mode not in ALLOWED_PERMS:
            sys.stderr.write(
                f"<artifacts_dir>/local/credentials must be chmod 600 (currently {oct(mode)[2:]})\n"
                f"Run: chmod 600 <artifacts_dir>/local/credentials\n"
            )
            sys.exit(2)

    # IMPORTANT: enable inline-comment stripping for both `#` and `;`. Without
    # this, a credentials line like `account = xy12345  # locator + region`
    # parses as `xy12345  # locator + region` (the comment becomes part of the
    # value), which then gets fed to Snowflake/Postgres/MySQL as a junk
    # hostname/account and the connection hangs or fails confusingly.
    cfg = configparser.ConfigParser(inline_comment_prefixes=("#", ";"))
    cfg.read(CREDENTIALS_PATH)
    if profile not in cfg:
        sys.stderr.write(
            f"Profile [{profile}] not found in <artifacts_dir>/local/credentials. "
            f"Sections present: {cfg.sections()}\n"
        )
        sys.exit(2)

    section = {k: (v.strip() if isinstance(v, str) else v) for k, v in cfg[profile].items()}

    # Accept the friendlier `service_account` / `credentials_path` spellings in the
    # per-field form too — the BigQuery executor reads `service_account_path`, and the
    # DSN parser already treats all three as equivalent. Without this, a per-field
    # `service_account = ...` would be silently ignored (falling back to ADC).
    for alias in ("service_account", "credentials_path"):
        if section.get(alias) and not section.get("service_account_path"):
            section["service_account_path"] = section[alias]

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
_BIGQUERY_SCHEMES = {"bigquery", "bq"}  # google-cloud-bigquery — auth via service-account JSON or ADC


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
    elif base_scheme in _BIGQUERY_SCHEMES:
        # BigQuery URLs follow the SQLAlchemy-bigquery convention:
        #   bigquery://<project>             — default dataset comes from creds
        #   bigquery://<project>/<dataset>   — set the default dataset
        # Query params may carry: service_account, location.
        # No host:port — BigQuery is HTTPS-only via the Google Cloud REST API.
        project = u.hostname or ""
        path_parts = (u.path or "").lstrip("/").split("/") if u.path else []
        out = {
            "type": "bigquery",
            "project": project,
        }
        if path_parts and path_parts[0]:
            out["dataset"] = path_parts[0]
        if u.query:
            for k, v in urllib.parse.parse_qsl(u.query):
                key = k.lower()
                # `service_account` and `credentials_path` both map to the
                # file path of the JSON service-account key.
                if key in ("credentials_path", "service_account"):
                    out["service_account_path"] = v
                else:
                    out[key] = v
        return out
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
            f"Edit <artifacts_dir>/local/credentials and add them.\n"
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
            # A server-side (named) cursor so the row cap bounds TRANSFER, not just what we write:
            # psycopg2's default client-side cursor buffers the ENTIRE result before we can fetchmany,
            # so a runaway result would still be pulled whole. The named cursor streams from the
            # server in bounded batches (ACE-038). Read-only SELECTs (the only thing the guard admits)
            # are exactly what a server-side cursor supports.
            with conn.cursor(name="agami_bounded") as cur:
                cur.itersize = _resolve_row_cap() + 1  # server fetch batch = the bounded window
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
            "Add one to <artifacts_dir>/local/credentials.",
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


def _execute_bigquery(creds: dict[str, str], sql: str) -> int:
    """Tier-3 path for BigQuery using google-cloud-bigquery.

    Required: `project`. One of: `service_account_path` (path to a JSON key
    file), OR no auth at all (falls back to Application Default Credentials —
    `gcloud auth application-default login`). Optional: `dataset` (sets the
    default dataset so unqualified table refs resolve), `location` (e.g. `US`,
    `EU`, `asia-northeast1`).
    """
    try:
        from google.cloud import bigquery  # type: ignore
        from google.oauth2 import service_account  # type: ignore
    except ImportError:
        return _err(
            "google-cloud-bigquery not installed. "
            "Run: pip install google-cloud-bigquery",
            code=3,
        )

    _require(creds, "project")
    project = creds["project"]
    sa_path = creds.get("service_account_path")
    location = creds.get("location") or None

    client_kwargs: dict[str, Any] = {"project": project}
    if location:
        client_kwargs["location"] = location

    if sa_path:
        sa_path_expanded = os.path.expanduser(sa_path)
        if not os.path.exists(sa_path_expanded):
            return _err(
                f"service_account_path '{sa_path}' doesn't exist. "
                f"Point at the JSON key file you downloaded from GCP.",
                code=2,
            )
        # Defensive chmod check — service-account JSON contains a private key.
        try:
            mode = stat.S_IMODE(os.stat(sa_path_expanded).st_mode)
            if mode not in ALLOWED_PERMS:
                sys.stderr.write(
                    f"Warning: service_account_path '{sa_path}' has permissions "
                    f"{oct(mode)} — should be 0600. The file contains a private key.\n"
                )
        except Exception:
            pass
        try:
            creds_obj = service_account.Credentials.from_service_account_file(
                sa_path_expanded
            )
            client_kwargs["credentials"] = creds_obj
        except Exception as e:
            return _err(f"BigQuery credentials load failed: {e}", code=2)

    try:
        client = bigquery.Client(**client_kwargs)
    except Exception as e:
        return _err(f"BigQuery client init failed: {e}", code=4)

    # If `dataset` was set, prefix unqualified table references via the
    # default_dataset job config so the SQL can omit `<project>.<dataset>.`
    job_config_kwargs: dict[str, Any] = {}
    if creds.get("dataset"):
        try:
            job_config_kwargs["default_dataset"] = f"{project}.{creds['dataset']}"
        except Exception:
            pass

    try:
        if job_config_kwargs:
            job_config = bigquery.QueryJobConfig(**job_config_kwargs)
            job = client.query(sql, job_config=job_config)
        else:
            job = client.query(sql)
        results = job.result()  # waits for completion; raises on error
    except Exception as e:
        return _err(f"BigQuery execution error: {e}", code=5)

    # Stream rows to stdout as CSV. results.schema gives column metadata.
    writer = csv.writer(sys.stdout)
    if results.schema:
        writer.writerow([f.name for f in results.schema])
        for row in results:
            writer.writerow([row[i] for i in range(len(results.schema))])

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


def _execute_sqlserver(creds: dict[str, str], sql: str) -> int:
    """Tier-3 path for SQL Server / Azure SQL using pymssql."""
    try:
        import pymssql  # type: ignore
    except ImportError:
        return _err("pymssql not installed. Run: pip install pymssql", code=3)
    _require(creds, "host", "user", "password")
    try:
        conn = pymssql.connect(
            server=creds["host"], port=int(creds.get("port", 1433)),
            user=creds["user"], password=creds["password"],
            database=creds.get("database", ""), login_timeout=15,
        )
    except Exception as e:
        return _err(f"SQL Server connect failed: {e}", code=4)
    try:
        cur = conn.cursor()
        cur.execute(sql)
        _write_cursor_csv(cur)
    except Exception as e:
        return _err(f"SQL Server execution error: {e}", code=5)
    finally:
        try:
            conn.close()
        except Exception:
            pass
    return 0


def _execute_oracle(creds: dict[str, str], sql: str) -> int:
    """Tier-3 path for Oracle using python-oracledb (thin mode — no client libs)."""
    try:
        import oracledb  # type: ignore
    except ImportError:
        return _err("python-oracledb not installed. Run: pip install oracledb", code=3)
    _require(creds, "user", "password")
    dsn = creds.get("dsn") or creds.get("url")
    if not dsn:
        _require(creds, "host", "service_name")
        dsn = oracledb.makedsn(creds["host"], int(creds.get("port", 1521)),
                               service_name=creds["service_name"])
    try:
        conn = oracledb.connect(user=creds["user"], password=creds["password"], dsn=dsn)
    except Exception as e:
        return _err(f"Oracle connect failed: {e}", code=4)
    try:
        cur = conn.cursor()
        cur.execute(sql)
        _write_cursor_csv(cur)
    except Exception as e:
        return _err(f"Oracle execution error: {e}", code=5)
    finally:
        try:
            conn.close()
        except Exception:
            pass
    return 0


def _execute_databricks(creds: dict[str, str], sql: str) -> int:
    """Tier-3 path for Databricks SQL warehouses using databricks-sql-connector."""
    try:
        from databricks import sql as dbsql  # type: ignore
    except ImportError:
        return _err(
            "databricks-sql-connector not installed. Run: pip install databricks-sql-connector",
            code=3,
        )
    _require(creds, "host", "http_path", "token")
    try:
        conn = dbsql.connect(
            server_hostname=creds["host"], http_path=creds["http_path"],
            access_token=creds["token"],
        )
    except Exception as e:
        return _err(f"Databricks connect failed: {e}", code=4)
    try:
        cur = conn.cursor()
        cur.execute(sql)
        _write_cursor_csv(cur)
    except Exception as e:
        return _err(f"Databricks execution error: {e}", code=5)
    finally:
        try:
            conn.close()
        except Exception:
            pass
    return 0


def _execute_trino(creds: dict[str, str], sql: str) -> int:
    """Tier-3 path for Trino / Presto using the trino python client."""
    try:
        import trino  # type: ignore
    except ImportError:
        return _err("trino not installed. Run: pip install trino", code=3)
    _require(creds, "host", "user")
    try:
        auth = None
        if creds.get("password"):
            auth = trino.auth.BasicAuthentication(creds["user"], creds["password"])
        conn = trino.dbapi.connect(
            host=creds["host"], port=int(creds.get("port", 8080)), user=creds["user"],
            catalog=creds.get("catalog"), schema=creds.get("schema"),
            http_scheme="https" if creds.get("password") else "http", auth=auth,
        )
    except Exception as e:
        return _err(f"Trino connect failed: {e}", code=4)
    try:
        cur = conn.cursor()
        cur.execute(sql)
        _write_cursor_csv(cur)
    except Exception as e:
        return _err(f"Trino execution error: {e}", code=5)
    finally:
        try:
            conn.close()
        except Exception:
            pass
    return 0


def _execute_duckdb(creds: dict[str, str], sql: str) -> int:
    """Tier-3 path for DuckDB using the duckdb python module (file or in-memory)."""
    try:
        import duckdb  # type: ignore
    except ImportError:
        return _err("duckdb not installed. Run: pip install duckdb", code=3)
    path = creds.get("path") or creds.get("database") or ":memory:"
    try:
        conn = duckdb.connect(path, read_only=True)
    except Exception as e:
        return _err(f"DuckDB open failed: {e}", code=4)
    try:
        cur = conn.execute(sql)
        _write_cursor_csv(cur)
    except Exception as e:
        return _err(f"DuckDB execution error: {e}", code=5)
    finally:
        try:
            conn.close()
        except Exception:
            pass
    return 0


_DEFAULT_MAX_ROWS = 1000  # rows materialized per result before truncation (ACE-038)
_max_rows_override: int | None = None  # per-call cap from --max-rows (ACE-044); set in main()


def _resolve_row_cap() -> int:
    """Effective result-row cap = min(per-call `--max-rows`, `AGAMI_SQL_MAX_ROWS` env, default 1000).
    Bounds what a single query materializes; the executed SQL is never modified (no injected LIMIT).
    A missing/invalid env value falls back to the default."""
    raw = os.environ.get("AGAMI_SQL_MAX_ROWS", "").strip()
    cap = int(raw) if raw.isdigit() and int(raw) > 0 else _DEFAULT_MAX_ROWS
    if _max_rows_override is not None and _max_rows_override > 0:
        cap = min(cap, _max_rows_override)
    return cap


def _write_cursor_csv(cur: Any) -> None:
    """Stream at most the row cap to stdout as CSV. `fetchmany(cap + 1)` — never `fetchall` — so a
    huge result can't be buffered whole; the (cap+1)th row means the result was truncated, flagged
    on stderr as a non-error `{"truncated": …}` signal the caller surfaces (a truncated result must
    never be mistaken for a complete one). The SQL itself is untouched (no injected LIMIT)."""
    cap = _resolve_row_cap()
    writer = csv.writer(sys.stdout)
    if cur.description is not None:
        writer.writerow([d[0] for d in cur.description])
        rows = cur.fetchmany(cap + 1)
        for row in rows[:cap]:
            writer.writerow(row)
        if len(rows) > cap:
            json.dump({"truncated": {"row_cap": cap}}, sys.stderr)
            sys.stderr.write("\n")


def _hosted() -> bool:
    """The served (hosted) path is signalled by a configured database — the same signal
    `tools._load_org` / `Store.from_env` use. On it, a missing model is a safety failure (fail
    closed); locally (no DB) a not-yet-built model legitimately means 'no model yet'."""
    return bool(os.environ.get("AGAMI_DB_URL") or os.environ.get("APP_DATABASE_URL"))


def _resolve_guard_model(profile: str):
    """Resolve the semantic model for the safety pass, mirroring `tools._load_org` (ACE-051): from
    the DB when one is configured (hosted — the `/artifacts` disk mount may be absent), else the
    on-disk YAML (local). Returns an `Organization` or None if neither is available.

    The DB import is lazy AND env-guarded on purpose: the local executor runs from a stdlib-lean
    mirror that does not ship `store`/`model_store`, so we only reach for them when a DB is set.
    Any DB-load failure degrades to disk rather than crashing the executor."""
    from semantic_model import loader as L

    # Any load failure below degrades to the next source (DB → disk → None), silently: a freeform
    # error line here would (a) leak DB connection details from the exception and (b) precede the
    # JSON refusal `_model_safety` emits when both sources are absent on hosted, breaking the
    # single-JSON-object contract callers parse. The observable signal is the fail-closed refusal
    # itself, not a diagnostic line.
    if _hosted():
        try:
            from model_store import load_organization as _load_db
            from store import Store

            store = Store.from_env()
            if store is not None:
                try:
                    org = _load_db(store, profile)
                finally:
                    store.close()
                if org is not None:
                    return org
        except Exception:
            pass  # DB unreachable/misconfigured -> fall through to disk

    root = Path(os.environ.get("AGAMI_ARTIFACTS_DIR") or (Path.home() / "agami-artifacts")) / profile
    if (root / "org.yaml").exists():
        try:
            return L.load_organization(root)
        except Exception:
            pass  # unparseable/absent on disk -> None (hosted then fails closed)
    return None


def _model_safety(sql: str, profile: str, area: str | None):
    """Semantic-model safety pass before execution: fan-trap / chasm-trap pre-flight
    + default_filters auto-application, over a model resolved from the DB (hosted) or disk (local).

    Returns (sql_to_run, exit_code). exit_code is None to continue, or an int to
    short-circuit (a refusal the caller must consume). Inert (returns the SQL unchanged) when the
    model package isn't importable, or — on the LOCAL path only — when there is no model yet. On the
    HOSTED path a model that can't be resolved fails closed (refuses), never runs unguarded (ACE-051).
    """
    try:
        from semantic_model import runtime as RT
    except Exception:
        # The model package (pydantic) isn't importable, so the guards can't run at all. On the
        # hosted served path that is the same "can't guarantee safety" condition as a missing model
        # — fail closed. Locally it stays a no-op (a bare install legitimately has no model). (The
        # sqlglot-unavailable / unparseable-SQL degrade-to-allow is a distinct fail-open owned by
        # ACE-037, not closed here.)
        if _hosted():
            json.dump({"error": {"kind": "model_unavailable", "reason":
                       "semantic-model package not importable; refusing to run unguarded on the "
                       "hosted server"}}, sys.stderr)
            sys.stderr.write("\n")
            return sql, 1
        return sql, None  # local: model package not available -> no-op

    org = _resolve_guard_model(profile)
    if org is None:
        if _hosted():
            # Fail closed: a served query with no resolvable model must be refused, never run with
            # the fan/chasm/scope/PII guards silently off.
            json.dump({"error": {"kind": "model_unavailable", "reason":
                       "no semantic model could be resolved (checked DB and disk); refusing to run "
                       "unguarded on the hosted server"}}, sys.stderr)
            sys.stderr.write("\n")
            return sql, 1
        return sql, None  # local: no model yet -> no-op (unchanged)

    # Build the shared guard context ONCE — parse the SQL + build each model index a single
    # time — and thread it through the battery below, instead of every guard re-parsing and
    # rebuilding its index (audit P2 / ACE-045). Behaviour-preserving: a guard given `ctx`
    # returns the same verdict as one that builds its own.
    ctx = RT.build_guard_context(sql, org)

    # Table-scope guard — a query may only reference tables the semantic model
    # declares; any other table in the connected database is refused. Runs FIRST
    # so the fan/chasm and sensitive checks below only evaluate in-scope tables.
    ts = RT.check_table_scope(sql, org, ctx=ctx)
    if ts.action == "refuse":
        json.dump({"error": {"kind": "table_out_of_scope", "tables": ts.offending_tables,
                             "reason": ts.reason, "suggestion": ts.suggestion}}, sys.stderr)
        sys.stderr.write("\n")
        return sql, 1

    # SELECT * ban — force every projected column to be named, so the column-scope
    # guard below can check what is actually returned (and nothing hides behind *).
    star = RT.check_no_select_star(sql, ctx=ctx)
    if star.action == "refuse":
        json.dump({"error": {"kind": "select_star",
                             "reason": star.reason, "suggestion": star.suggestion}}, sys.stderr)
        sys.stderr.write("\n")
        return sql, 1

    # Column-scope guard — a column that binds to a declared table must be one that
    # table declares (a hallucinated column, or a physical column the model excluded).
    cs = RT.check_column_scope(sql, org, ctx=ctx)
    if cs.action == "refuse":
        json.dump({"error": {"kind": "column_out_of_scope", "columns": cs.columns,
                             "reason": cs.reason, "suggestion": cs.suggestion}}, sys.stderr)
        sys.stderr.write("\n")
        return sql, 1

    pf = RT.pre_flight_check(sql, org, ctx=ctx)
    if pf.risk and pf.action == "refuse":
        json.dump({"error": {"kind": "preflight_refused", "risk": pf.risk,
                             "reason": pf.reason, "suggestion": pf.suggestion,
                             "triggering_joins": pf.triggering_joins}}, sys.stderr)
        sys.stderr.write("\n")
        return sql, 1
    if pf.risk and pf.action == "auto_rewrite" and pf.rewritten_sql:
        sys.stderr.write(f"[agami] auto-corrected {pf.risk}: ran rewritten SQL. {pf.reason}\n")
        sql = pf.rewritten_sql
        ctx = RT.build_guard_context(sql, org)  # SQL changed -> refresh the shared context

    # Sensitive-column (PII) guard — refuse to PROJECT raw sensitive values. Same
    # deterministic chokepoint as the fan/chasm pre-flight, so the agami-query skill,
    # the local MCP server, and cron all protect PII identically (not just whichever
    # path happened to read a prose rule). Aggregates / filters / joins are allowed.
    sens = RT.check_sensitive_projection(sql, org, ctx=ctx)
    if sens.action == "refuse":
        json.dump({"error": {"kind": "sensitive_columns", "columns": sens.columns,
                             "reason": sens.reason, "suggestion": sens.suggestion}}, sys.stderr)
        sys.stderr.write("\n")
        return sql, 1

    new_sql, applied = RT.apply_default_filters(sql, org, area=area, ctx=ctx)
    if applied:
        sys.stderr.write(f"[agami] applied default_filters: {applied}\n")
        sql = new_sql
    return sql, None


def main() -> int:
    # One-shot migration of a legacy <artifacts_dir>/local into <artifacts_dir>/local/, then re-resolve
    # the paths (the migration can set the artifacts-dir pointer to a custom location).
    global CREDENTIALS_PATH, CONFIG_PATH
    agami_paths.bootstrap()
    CREDENTIALS_PATH = agami_paths.credentials_path()
    CONFIG_PATH = agami_paths.config_path()
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
    p.add_argument("--area", default=None,
                   help="Subject area for the semantic-model safety pass (pre-flight + default_filters).")
    p.add_argument("--no-safety", action="store_true",
                   help="Skip the semantic-model pre-flight / default_filters pass.")
    p.add_argument("--max-rows", type=int, default=None,
                   help="Cap rows returned for this call. Effective cap = min(this, AGAMI_SQL_MAX_ROWS, 1000).")
    args = p.parse_args()

    global _max_rows_override
    _max_rows_override = args.max_rows  # per-call cap (ACE-044); the sink reads it via _resolve_row_cap

    if args.sql_file:
        sql = Path(os.path.expanduser(args.sql_file)).read_text()
    else:
        sql = args.sql

    profile = args.profile or _resolve_default_profile()

    # Read-only / dangerous-SQL guard — the hard security gate, at the shared executor
    # chokepoint so EVERY caller (both MCP servers, the agami-query skill, cron) is
    # protected, not just whichever path happened to pre-check. This is NOT bypassable
    # via --no-safety: that flag skips only the *semantic-model* pass (fan/chasm +
    # default_filters), never write / RCE / DoS protection. Same gate the MCP tool layer
    # fail-fast pre-checks (tools.check_read_only -> sql_guard).
    import sql_guard

    guard_reason = sql_guard.check_read_only(sql)
    if guard_reason is not None:
        json.dump({"error": {"kind": "permission", "remediation": guard_reason}}, sys.stderr)
        sys.stderr.write("\n")
        return 1

    # Semantic-model safety pass (fan/chasm pre-flight + default_filters). Inert when
    # there's no model for the profile, so this is safe for every caller.
    if not args.no_safety:
        sql, _rc = _model_safety(sql, profile, args.area)
        if _rc is not None:
            return _rc
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
    if db_type == "bigquery":
        return _execute_bigquery(creds, sql)
    if db_type in ("sqlserver", "mssql"):
        return _execute_sqlserver(creds, sql)
    if db_type == "oracle":
        return _execute_oracle(creds, sql)
    if db_type == "databricks":
        return _execute_databricks(creds, sql)
    if db_type in ("trino", "presto"):
        return _execute_trino(creds, sql)
    if db_type == "duckdb":
        return _execute_duckdb(creds, sql)
    if db_type == "supabase":
        # Supabase is hosted Postgres.
        return _execute_postgres(creds, sql)
    return _err(
        f"Unsupported db type {db_type!r}. Supported: postgres, supabase, redshift, "
        f"mysql, sqlite, snowflake, bigquery, sqlserver, oracle, databricks, trino, duckdb."
    )


if __name__ == "__main__":
    sys.exit(main())
