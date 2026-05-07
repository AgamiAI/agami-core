"""
Tests for plugins/agami/scripts/setup_pgauth.py.

The script materializes provider-native auth files (~/.agami/.pgpass and
~/.agami/.mysql.cnf) from ~/.agami/credentials so psql/mysql can run
WITHOUT the password ever appearing on a Bash command line.

These tests verify:
- the auth files get written with correct content + chmod 600
- pgpass colon/backslash escaping is correct
- mysql section names use --defaults-group-suffix-friendly format
- DSN-style url= profiles parse correctly
- sqlite profiles are skipped (no auth file needed)
- the password is never written to stdout/stderr (no leaks)
"""

from __future__ import annotations

import io
import os
import stat
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agami" / "scripts"))


@pytest.fixture
def tmp_agami_home(tmp_path, monkeypatch):
    """Set HOME to a temp dir so all ~/.agami paths land there."""
    home = tmp_path / "home"
    home.mkdir()
    agami = home / ".agami"
    agami.mkdir(mode=0o700)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("AGAMI_PROFILE", raising=False)

    # Force re-import to pick up the patched HOME
    import importlib
    import setup_pgauth
    importlib.reload(setup_pgauth)
    return setup_pgauth, agami


def _write_credentials(agami: Path, contents: str) -> None:
    creds = agami / "credentials"
    creds.write_text(contents)
    creds.chmod(0o600)


def test_pgpass_basic(tmp_agami_home):
    setup_pgauth, agami = tmp_agami_home
    _write_credentials(agami, """
[main]
type     = postgres
host     = db.example.com
port     = 5432
database = mydb
user     = myuser
password = mypassword
""")
    setup_pgauth.materialize(["main"])
    pgpass = (agami / ".pgpass")
    assert pgpass.exists()
    assert stat.S_IMODE(pgpass.stat().st_mode) == 0o600
    body = pgpass.read_text()
    assert "db.example.com:5432:mydb:myuser:mypassword" in body


def test_pgpass_escapes_colons_and_backslashes(tmp_agami_home):
    setup_pgauth, agami = tmp_agami_home
    _write_credentials(agami, r"""
[main]
type     = postgres
host     = db.example.com
port     = 5432
database = mydb
user     = myuser
password = pass:colon\backslash
""")
    setup_pgauth.materialize(["main"])
    body = (agami / ".pgpass").read_text()
    # Backslash escaped first, then colon
    assert r"pass\:colon\\backslash" in body


def test_supabase_url_form_works(tmp_agami_home):
    """User pastes a Supabase DSN; setup_pgauth still produces .pgpass."""
    setup_pgauth, agami = tmp_agami_home
    _write_credentials(agami, """
[supabase]
url     = postgresql+asyncpg://postgres.proj_ref:secretpw@aws-1-ap-northeast-1.pooler.supabase.com:5432/postgres
sslmode = require
""")
    setup_pgauth.materialize(["supabase"])
    body = (agami / ".pgpass").read_text()
    assert "aws-1-ap-northeast-1.pooler.supabase.com:5432:postgres:postgres.proj_ref:secretpw" in body


def test_mysql_section_uses_group_suffix_format(tmp_agami_home):
    setup_pgauth, agami = tmp_agami_home
    _write_credentials(agami, """
[analytics]
type     = mysql
host     = analytics-db.example.com
port     = 3306
database = analytics
user     = readonly
password = mysqlpw
""")
    setup_pgauth.materialize(["analytics"])
    cnf = agami / ".mysql.cnf"
    assert cnf.exists()
    assert stat.S_IMODE(cnf.stat().st_mode) == 0o600
    body = cnf.read_text()
    # mysql --defaults-group-suffix=_analytics expects [client_analytics]
    assert "[client_analytics]" in body
    assert "host=analytics-db.example.com" in body
    assert "port=3306" in body
    assert "user=readonly" in body
    assert "password=mysqlpw" in body
    assert "database=analytics" in body


def test_sqlite_profile_skipped(tmp_agami_home):
    setup_pgauth, agami = tmp_agami_home
    _write_credentials(agami, """
[local]
type = sqlite
path = /tmp/local.db
""")
    setup_pgauth.materialize(["local"])
    # No auth files needed for sqlite
    assert not (agami / ".pgpass").exists()
    assert not (agami / ".mysql.cnf").exists()


def test_mixed_profiles_each_in_their_file(tmp_agami_home):
    setup_pgauth, agami = tmp_agami_home
    _write_credentials(agami, """
[main]
type     = postgres
host     = pg.example.com
port     = 5432
database = mydb
user     = u
password = p1

[staging]
type     = mysql
host     = mysql.example.com
port     = 3306
database = stg
user     = u2
password = p2

[local]
type = sqlite
path = /tmp/local.db
""")
    setup_pgauth.materialize(["main", "staging", "local"])
    assert (agami / ".pgpass").read_text().count("p1") == 1
    assert "p2" in (agami / ".mysql.cnf").read_text()
    # sqlite leaves no auth file
    assert "/tmp/local.db" not in (agami / ".pgpass").read_text()


def test_missing_credentials_exits(tmp_agami_home, capsys):
    setup_pgauth, _agami = tmp_agami_home
    with pytest.raises(SystemExit) as exc:
        setup_pgauth._load_section("main")
    assert exc.value.code == 2


def test_unknown_profile_exits(tmp_agami_home, capsys):
    setup_pgauth, agami = tmp_agami_home
    _write_credentials(agami, """
[main]
type = postgres
host = h
port = 5432
database = d
user = u
password = p
""")
    with pytest.raises(SystemExit) as exc:
        setup_pgauth._load_section("nonexistent")
    assert exc.value.code == 2


def test_password_does_not_appear_in_stdout(tmp_agami_home, capsys):
    """Most important security test: setup_pgauth prints NOTHING to stdout/stderr
    by default, so the password never leaks to the host's tool-call display.
    """
    setup_pgauth, agami = tmp_agami_home
    _write_credentials(agami, """
[main]
type     = postgres
host     = h
port     = 5432
database = d
user     = u
password = SUPER_SECRET_PASSWORD_DO_NOT_LEAK
""")
    setup_pgauth.materialize(["main"])
    captured = capsys.readouterr()
    # The password (or any obvious leak) must not be on stdout or stderr
    assert "SUPER_SECRET_PASSWORD_DO_NOT_LEAK" not in captured.out
    assert "SUPER_SECRET_PASSWORD_DO_NOT_LEAK" not in captured.err


def test_atomic_write_does_not_leave_tempfile(tmp_agami_home):
    setup_pgauth, agami = tmp_agami_home
    _write_credentials(agami, """
[main]
type     = postgres
host     = h
port     = 5432
database = d
user     = u
password = p
""")
    setup_pgauth.materialize(["main"])
    # Only the final files should exist
    contents = sorted(p.name for p in agami.iterdir())
    # Allow .pgpass and credentials; no orphan temp files like ".pgpass.abc123"
    leftover = [c for c in contents if c.startswith(".pgpass.") and c != ".pgpass"]
    assert leftover == []
