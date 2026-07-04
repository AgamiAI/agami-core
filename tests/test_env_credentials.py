"""
Env-var warehouse-credential resolution in execute_sql (`_load_credentials`).

The self-host / container path supplies the warehouse DSN in the environment
(DATASOURCE_URL[__<PROFILE>]) instead of the mounted `local/credentials` file — env
carries no file mode, so it sidesteps the chmod-600 gate + the container-uid mismatch.
This covers env-first resolution, per-datasource precedence, the type-from-scheme reuse
of `_parse_dsn`, that the file path is untouched when no env is set, and the
both-sources-named error.

Run: pytest tests/test_env_credentials.py -v
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agami" / "scripts"))

import execute_sql  # noqa: E402
from execute_sql import _env_datasource_dsn, _load_credentials  # noqa: E402


@pytest.fixture(autouse=True)
def _clear_datasource_env(monkeypatch):
    """Start every test from a clean slate — no ambient DATASOURCE_URL* leaking in."""
    import os

    for k in list(os.environ):
        if k == "DATASOURCE_URL" or k.startswith("DATASOURCE_URL__"):
            monkeypatch.delenv(k, raising=False)


# --- env-first, no file needed ---------------------------------------------

def test_env_dsn_resolves_without_a_file(monkeypatch, tmp_path):
    """DATASOURCE_URL set + NO creds file → resolve from env, no file read, no chmod gate."""
    monkeypatch.setenv("DATASOURCE_URL", "postgresql://u:p@warehouse:5432/analytics")
    # Point the file path at something that does not exist: if the code touched it we'd SystemExit.
    monkeypatch.setattr(execute_sql, "CREDENTIALS_PATH", tmp_path / "nope" / "credentials")

    out = _load_credentials("default")
    assert out == {
        "type": "postgres", "host": "warehouse", "port": "5432",
        "user": "u", "password": "p", "database": "analytics",
    }


# --- per-datasource precedence ---------------------------------------------

def test_per_datasource_overrides_bare_default(monkeypatch, tmp_path):
    monkeypatch.setenv("DATASOURCE_URL", "postgresql://u:p@pg:5432/main")
    monkeypatch.setenv("DATASOURCE_URL__SALES", "mysql://mu:mp@mysqlhost:3306/salesdb")
    monkeypatch.setattr(execute_sql, "CREDENTIALS_PATH", tmp_path / "nope")

    assert _load_credentials("sales")["type"] == "mysql"       # the per-datasource var wins
    assert _load_credentials("other")["type"] == "postgres"    # unrelated profile → bare default


def test_profile_id_is_normalized_to_an_env_token(monkeypatch):
    """A profile like `sales-pg` maps to DATASOURCE_URL__SALES_PG (upper + non-alnum→_)."""
    monkeypatch.setenv("DATASOURCE_URL__SALES_PG", "postgresql://u:p@h:5432/db")
    assert _env_datasource_dsn("sales-pg") == "postgresql://u:p@h:5432/db"
    assert _env_datasource_dsn("SALES.PG") == "postgresql://u:p@h:5432/db"


# --- type + params come from the DSN (reusing _parse_dsn) -------------------

def test_snowflake_env_dsn_carries_scheme_type_and_query_params(monkeypatch, tmp_path):
    monkeypatch.setenv(
        "DATASOURCE_URL",
        "snowflake://u:p@acct.us-east-1/analytics/public?warehouse=WH&role=ANALYST",
    )
    monkeypatch.setattr(execute_sql, "CREDENTIALS_PATH", tmp_path / "nope")

    out = _load_credentials("default")
    assert out["type"] == "snowflake"
    assert out["warehouse"] == "WH"
    assert out["role"] == "ANALYST"


# --- no fork: the file path is unchanged when no env is set ----------------

def test_no_env_falls_through_to_the_file(monkeypatch, tmp_path):
    creds = tmp_path / "credentials"
    creds.write_text("[default]\nurl = postgresql://fu:fp@filehost:5432/filedb\n", encoding="utf-8")
    import os
    if os.name == "posix":
        creds.chmod(0o600)  # the gate the file path still enforces
    monkeypatch.setattr(execute_sql, "CREDENTIALS_PATH", creds)

    out = _load_credentials("default")
    assert out["host"] == "filehost" and out["type"] == "postgres"


# --- neither source → an error that names both -----------------------------

def test_missing_both_sources_names_env_and_file(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(execute_sql, "CREDENTIALS_PATH", tmp_path / "absent")
    with pytest.raises(SystemExit):
        _load_credentials("default")
    err = capsys.readouterr().err
    assert "DATASOURCE_URL" in err
    assert "credentials" in err
