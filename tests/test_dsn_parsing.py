"""
DSN parsing tests for plugins/agami/scripts/execute_sql.py.

Exercises the URL forms agami accepts as the `url = ...` field inside
`<artifacts_dir>/local/credentials`. The provider-specific forms (Supabase pooler, Neon,
etc.) are the load-bearing cases — those shapes appear in real users'
connection panels and we copy-paste them.

Run: pytest tests/test_dsn_parsing.py -v
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agami" / "scripts"))

from execute_sql import _parse_dsn  # noqa: E402


# --- Postgres family -------------------------------------------------------

def test_plain_postgresql_scheme():
    out = _parse_dsn("postgresql://u:p@host:5432/db")
    assert out == {
        "type": "postgres", "host": "host", "port": "5432",
        "user": "u", "password": "p", "database": "db",
    }


def test_short_postgres_scheme():
    out = _parse_dsn("postgres://u:p@host:5432/db")
    assert out["type"] == "postgres"


def test_supabase_asyncpg_scheme():
    """The user's actual Supabase URL — pasted from their dashboard."""
    dsn = (
        "postgresql+asyncpg://postgres.odzuxljstuccrblqcevo:HDsA1qduFmivRWzZ"
        "@aws-1-ap-northeast-1.pooler.supabase.com:5432/postgres"
    )
    out = _parse_dsn(dsn)
    assert out["type"] == "postgres"
    assert out["host"] == "aws-1-ap-northeast-1.pooler.supabase.com"
    assert out["port"] == "5432"
    assert out["user"] == "postgres.odzuxljstuccrblqcevo"
    assert out["password"] == "HDsA1qduFmivRWzZ"
    assert out["database"] == "postgres"


@pytest.mark.parametrize("driver", ["asyncpg", "psycopg2", "psycopg"])
def test_postgres_driver_suffixes_stripped(driver):
    out = _parse_dsn(f"postgresql+{driver}://u:p@host:5432/db")
    assert out["type"] == "postgres"
    assert out["host"] == "host"


def test_postgres_default_port():
    out = _parse_dsn("postgresql://u:p@host/db")
    assert out["port"] == "5432"


# --- MySQL family ----------------------------------------------------------

def test_plain_mysql_scheme():
    out = _parse_dsn("mysql://u:p@host:3306/db")
    assert out["type"] == "mysql"


def test_mysql_pymysql_suffix_stripped():
    out = _parse_dsn("mysql+pymysql://u:p@host:3306/db")
    assert out["type"] == "mysql"


def test_mariadb_scheme():
    out = _parse_dsn("mariadb://u:p@host:3306/db")
    assert out["type"] == "mysql"


def test_mysql_default_port():
    out = _parse_dsn("mysql://u:p@host/db")
    assert out["port"] == "3306"


# --- Query string params ---------------------------------------------------

def test_sslmode_query_param_carried_over():
    out = _parse_dsn("postgresql://u:p@host:5432/db?sslmode=require")
    assert out["sslmode"] == "require"


def test_multiple_query_params_merged():
    out = _parse_dsn(
        "postgresql://u:p@host:5432/db?sslmode=verify-full&connect_timeout=10"
    )
    assert out["sslmode"] == "verify-full"
    assert out["connect_timeout"] == "10"


# --- URL-encoded credentials -----------------------------------------------

def test_url_encoded_password():
    """Passwords with @ / : / etc. must be URL-encoded in the DSN."""
    out = _parse_dsn("postgres://u:p%40ss@host:5432/db")
    assert out["password"] == "p@ss"


def test_url_encoded_username():
    out = _parse_dsn("postgres://my%40user:p@host:5432/db")
    assert out["user"] == "my@user"


# --- Redshift --------------------------------------------------------------

def test_redshift_dsn():
    out = _parse_dsn(
        "redshift://readonly:secret@my-cluster.abc123.us-west-2.redshift.amazonaws.com:5439/analytics"
    )
    assert out["type"] == "redshift"
    assert out["host"] == "my-cluster.abc123.us-west-2.redshift.amazonaws.com"
    assert out["port"] == "5439"
    assert out["user"] == "readonly"
    assert out["password"] == "secret"
    assert out["database"] == "analytics"
    # Redshift defaults SSL to require
    assert out["sslmode"] == "require"


def test_redshift_default_port():
    out = _parse_dsn("redshift://u:p@cluster.us-west-2.redshift.amazonaws.com/db")
    assert out["port"] == "5439"


def test_redshift_explicit_sslmode_not_overridden():
    out = _parse_dsn("redshift://u:p@host:5439/db?sslmode=verify-full")
    assert out["sslmode"] == "verify-full"


# --- Snowflake -------------------------------------------------------------

def test_snowflake_full_url():
    out = _parse_dsn(
        "snowflake://myuser:mypass@xy12345.us-east-1.aws/MYDB/PUBLIC"
        "?warehouse=COMPUTE_WH&role=ANALYST_ROLE"
    )
    assert out["type"] == "snowflake"
    assert out["account"] == "xy12345.us-east-1.aws"
    assert out["user"] == "myuser"
    assert out["password"] == "mypass"
    assert out["database"] == "MYDB"
    assert out["schema"] == "PUBLIC"
    assert out["warehouse"] == "COMPUTE_WH"
    assert out["role"] == "ANALYST_ROLE"
    # Snowflake doesn't use host/port — those keys must be absent
    assert "host" not in out
    assert "port" not in out


def test_snowflake_account_only():
    """Short account locator (legacy AWS US-West-2 form)."""
    out = _parse_dsn("snowflake://u:p@xy12345/MYDB?warehouse=WH")
    assert out["account"] == "xy12345"
    assert out["database"] == "MYDB"
    assert "schema" not in out
    assert out["warehouse"] == "WH"


def test_snowflake_no_schema():
    out = _parse_dsn("snowflake://u:p@xy12345.us-east-1.aws/MYDB")
    assert out["account"] == "xy12345.us-east-1.aws"
    assert out["database"] == "MYDB"
    assert "schema" not in out


def test_snowflake_org_account_form():
    """Newer org-account identifier."""
    out = _parse_dsn("snowflake://u:p@myorg-myaccount/ANALYTICS/PUBLIC")
    assert out["account"] == "myorg-myaccount"
    assert out["database"] == "ANALYTICS"


def test_snowflake_authenticator_via_query():
    """SSO setups pass authenticator=externalbrowser."""
    out = _parse_dsn(
        "snowflake://user@example.com:@xy12345.us-east-1.aws/MYDB"
        "?authenticator=externalbrowser&warehouse=WH"
    )
    assert out["user"] == "user@example.com"
    assert out["authenticator"] == "externalbrowser"


# --- SQLite ----------------------------------------------------------------

def test_sqlite_absolute_path():
    out = _parse_dsn("sqlite:///Users/me/data/local.db")
    assert out["type"] == "sqlite"
    assert out["path"] == "/Users/me/data/local.db"


# --- BigQuery --------------------------------------------------------------

def test_bigquery_project_only():
    out = _parse_dsn("bigquery://my-gcp-project")
    assert out["type"] == "bigquery"
    assert out["project"] == "my-gcp-project"
    assert "dataset" not in out


def test_bigquery_project_and_dataset():
    out = _parse_dsn("bigquery://my-gcp-project/analytics")
    assert out["type"] == "bigquery"
    assert out["project"] == "my-gcp-project"
    assert out["dataset"] == "analytics"


def test_bigquery_service_account_via_query():
    out = _parse_dsn(
        "bigquery://my-project/sales?service_account=/abs/path/key.json&location=US"
    )
    assert out["type"] == "bigquery"
    assert out["project"] == "my-project"
    assert out["dataset"] == "sales"
    assert out["service_account_path"] == "/abs/path/key.json"
    assert out["location"] == "US"


def test_bigquery_credentials_path_synonym():
    """`credentials_path` is the SQLAlchemy-bigquery name; accepted as a
    synonym for service_account."""
    out = _parse_dsn("bigquery://p?credentials_path=/k.json")
    assert out["service_account_path"] == "/k.json"


def test_bigquery_bq_scheme_alias():
    """`bq://` is the shorter form some tools use; treat as alias."""
    out = _parse_dsn("bq://my-project/my_dataset")
    assert out["type"] == "bigquery"
    assert out["project"] == "my-project"
    assert out["dataset"] == "my_dataset"


def test_bigquery_no_credentials_means_adc():
    """A URL with no service_account is valid — execute_sql.py falls back to
    Application Default Credentials at runtime."""
    out = _parse_dsn("bigquery://my-project")
    assert "service_account_path" not in out


# --- Rejection -------------------------------------------------------------

def test_unsupported_scheme_exits():
    with pytest.raises(SystemExit) as exc:
        _parse_dsn("redis://u:p@host:6379/0")
    assert exc.value.code == 2


def test_random_string_exits():
    with pytest.raises(SystemExit):
        _parse_dsn("not a url at all")
