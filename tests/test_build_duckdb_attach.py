"""
Tests for plugins/agami/scripts/build_duckdb_attach.py.

The script generates a temp DuckDB init file that ATTACHes one or more
agami profiles for cross-database SQL via DuckDB's postgres_scanner +
mysql_scanner. The init file must:

- Land in <artifacts_dir>/local/.duckdb_init_*.sql (chmod 600)
- Single-quote-escape passwords correctly
- Refuse Snowflake federation (snowflake_scanner not in stable DuckDB)
- Refuse a single-profile call (federation needs ≥ 2)
- Never echo the password to stdout/stderr
"""

from __future__ import annotations

import stat
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agami" / "scripts"))


@pytest.fixture
def tmp_agami_home(tmp_path, monkeypatch):
    art = tmp_path / "artifacts"
    local = art / "local"
    local.mkdir(parents=True, mode=0o700)
    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(art))
    monkeypatch.delenv("AGAMI_PROFILE", raising=False)

    # Re-import so all module-level paths re-resolve to <art>/local.
    import importlib

    import setup_pgauth
    importlib.reload(setup_pgauth)
    import build_duckdb_attach
    importlib.reload(build_duckdb_attach)
    return build_duckdb_attach, local


def _write_credentials(agami: Path, contents: str) -> None:
    creds = agami / "credentials"
    creds.write_text(contents)
    creds.chmod(0o600)


def test_postgres_plus_mysql_init_sql(tmp_agami_home):
    bda, agami = tmp_agami_home
    _write_credentials(agami, """
[itsm]
type     = postgres
host     = pg.example.com
port     = 5432
database = itsm
user     = pguser
password = pgpass

[finance]
type     = mysql
host     = my.example.com
port     = 3306
database = finance
user     = myuser
password = mypass
""")
    sql = bda.build_init_sql(["itsm", "finance"])
    # Both extensions installed
    assert "INSTALL postgres_scanner;" in sql
    assert "LOAD postgres_scanner;" in sql
    assert "INSTALL mysql_scanner;" in sql
    assert "LOAD mysql_scanner;" in sql
    # Both ATTACH lines
    assert "ATTACH '" in sql
    assert "AS itsm (TYPE POSTGRES);" in sql
    assert "AS finance (TYPE MYSQL);" in sql
    # Credentials inline (correct, since DuckDB has no PGPASSFILE equivalent
    # for ATTACH — that's why this lives in a chmod-600 file the bash command
    # references via -init, not in the visible bash command itself)
    assert "password=pgpass" in sql
    assert "password=mypass" in sql


def test_snowflake_federation_refused(tmp_agami_home):
    bda, agami = tmp_agami_home
    _write_credentials(agami, """
[itsm]
type     = postgres
host     = pg.example.com
port     = 5432
database = itsm
user     = u
password = p

[snow]
type      = snowflake
account   = xy12345
user      = u
password  = p
warehouse = wh
""")
    with pytest.raises(SystemExit) as exc:
        bda.build_init_sql(["itsm", "snow"])
    assert exc.value.code == 2


def test_single_profile_refused(tmp_agami_home, capsys):
    bda, agami = tmp_agami_home
    _write_credentials(agami, """
[itsm]
type     = postgres
host     = pg.example.com
port     = 5432
database = itsm
user     = u
password = p
""")
    rc = bda.main(["--profiles", "itsm"])
    err = capsys.readouterr().err
    assert rc == 2
    assert "at least two profiles" in err


def test_password_with_single_quote_is_escaped(tmp_agami_home):
    bda, agami = tmp_agami_home
    _write_credentials(agami, """
[itsm]
type     = postgres
host     = pg.example.com
port     = 5432
database = itsm
user     = u
password = ab'cd

[finance]
type     = mysql
host     = my.example.com
port     = 3306
database = f
user     = u
password = p
""")
    sql = bda.build_init_sql(["itsm", "finance"])
    # ' inside the connection string must be doubled to '' per SQL string rules.
    assert "password=ab''cd" in sql
    # And the raw single-quote ' (with no double) must NOT appear in the
    # password segment — that would close the SQL string early.
    pg_attach = next(line for line in sql.splitlines() if "POSTGRES" in line)
    # Counting `'` occurrences in the connection string: opener + escaped pair
    # + escaped pair + closer = exactly four (opening ATTACH ', then ab'' ,
    # then closing ', then no more in the AS clause).
    inside_attach_quotes = pg_attach.split("'", 1)[1].rsplit("'", 1)[0]
    # Inside the quoted connection string, every ' is doubled.
    assert "'" not in inside_attach_quotes.replace("''", "")


def test_init_file_chmod_600(tmp_agami_home):
    bda, agami = tmp_agami_home
    _write_credentials(agami, """
[itsm]
type     = postgres
host     = h
port     = 5432
database = d
user     = u
password = p

[finance]
type     = mysql
host     = h
port     = 3306
database = d
user     = u
password = p
""")
    path = bda.write_init_file(["itsm", "finance"])
    assert path.exists()
    assert path.parent == agami
    assert path.name.startswith(".duckdb_init_")
    assert path.suffix == ".sql"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_main_prints_path_and_password_not_in_stdout(tmp_agami_home, capsys):
    bda, agami = tmp_agami_home
    _write_credentials(agami, """
[itsm]
type     = postgres
host     = h
port     = 5432
database = d
user     = u
password = SECRET_PG_PASSWORD_FOO

[finance]
type     = mysql
host     = h
port     = 3306
database = d
user     = u
password = SECRET_MYSQL_PASSWORD_BAR
""")
    rc = bda.main(["--profiles", "itsm", "finance"])
    out = capsys.readouterr()
    assert rc == 0
    # stdout is just the file path; no credentials anywhere
    assert "SECRET_PG_PASSWORD_FOO" not in out.out
    assert "SECRET_PG_PASSWORD_FOO" not in out.err
    assert "SECRET_MYSQL_PASSWORD_BAR" not in out.out
    assert "SECRET_MYSQL_PASSWORD_BAR" not in out.err
    # The path on stdout should resolve to a real file
    path = Path(out.out.strip())
    assert path.exists()


def test_redshift_treated_as_postgres(tmp_agami_home):
    bda, agami = tmp_agami_home
    _write_credentials(agami, """
[redshift_warehouse]
type     = redshift
host     = cluster.us-west-2.redshift.amazonaws.com
port     = 5439
database = analytics
user     = u
password = p

[finance]
type     = mysql
host     = h
port     = 3306
database = d
user     = u
password = p
""")
    sql = bda.build_init_sql(["redshift_warehouse", "finance"])
    # Redshift is wire-compatible with Postgres, so DuckDB's postgres_scanner
    # is what attaches it. The ATTACH line uses TYPE POSTGRES.
    assert "AS redshift_warehouse (TYPE POSTGRES);" in sql
    assert "port=5439" in sql
