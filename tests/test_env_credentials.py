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


def test_env_dsn_is_stripped_and_whitespace_only_is_unset(monkeypatch):
    """A trailing newline (common from secret stores / `.env`) is stripped; blank → unset."""
    monkeypatch.setenv("DATASOURCE_URL", "  postgresql://u:p@h:5432/db\n")
    assert _env_datasource_dsn("default") == "postgresql://u:p@h:5432/db"
    # A whitespace-only per-datasource var must NOT shadow the real generic one — it falls through.
    monkeypatch.setenv("DATASOURCE_URL__SALES", "   \n")
    assert _env_datasource_dsn("sales") == "postgresql://u:p@h:5432/db"


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


# --- per-field BigQuery: service_account alias normalizes to the path key ---

def _bq_creds(tmp_path, *extra_lines):
    """Write a minimal BigQuery credentials file (chmod 600) with the given extra
    per-field lines, and return its path — the per-field form these tests exercise."""
    creds = tmp_path / "credentials"
    creds.write_text(
        "[gcp]\ntype = bigquery\nproject = my-proj\n" + "".join(f"{ln}\n" for ln in extra_lines),
        encoding="utf-8",
    )
    import os
    if os.name == "posix":
        creds.chmod(0o600)
    return creds


@pytest.mark.parametrize("alias", ["service_account", "credentials_path"])
def test_bigquery_alias_normalizes_to_path_key(monkeypatch, tmp_path, alias):
    """Both per-field spellings (`service_account`, `credentials_path`) map to
    `service_account_path` (what the BigQuery executor reads) — the friendlier
    spellings the docs use must not be silently ignored (falling back to ADC)."""
    monkeypatch.setattr(execute_sql, "CREDENTIALS_PATH", _bq_creds(tmp_path, f"{alias} = /abs/path/key.json"))

    assert _load_credentials("gcp")["service_account_path"] == "/abs/path/key.json"


def test_bigquery_explicit_service_account_path_wins_over_alias(monkeypatch, tmp_path):
    """When both the explicit `service_account_path` and an alias appear, the explicit
    path wins and the alias does NOT clobber it — pins the `not …get(...)` guard, which
    without it would let the alias overwrite the explicitly-set path."""
    monkeypatch.setattr(
        execute_sql,
        "CREDENTIALS_PATH",
        _bq_creds(tmp_path, "service_account_path = /explicit/key.json", "service_account = /alias/key.json"),
    )

    assert _load_credentials("gcp")["service_account_path"] == "/explicit/key.json"


def test_bigquery_empty_alias_falls_through_to_adc(monkeypatch, tmp_path):
    """An empty `service_account =` must not synthesize a `service_account_path` — it
    stays unset so the client falls back to ADC, rather than loading a key from ''."""
    monkeypatch.setattr(execute_sql, "CREDENTIALS_PATH", _bq_creds(tmp_path, "service_account ="))

    assert "service_account_path" not in _load_credentials("gcp")


# --- neither source → an error that names both -----------------------------

def test_missing_both_sources_names_env_and_file(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(execute_sql, "CREDENTIALS_PATH", tmp_path / "absent")
    with pytest.raises(SystemExit):
        _load_credentials("default")
    err = capsys.readouterr().err
    assert "DATASOURCE_URL" in err
    assert "credentials" in err
