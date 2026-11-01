"""A profile whose password is a `keyring:` reference must never reach a plain-text auth file.

psql / mysql / snowsql read passwords from .pgpass / .mysql.cnf / .snowsql.cnf, so those profiles
are left to the Python driver (`python -m execute_sql`), which resolves the reference from the OS
credential store at query time.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agami" / "scripts"))

CREDENTIALS = """
[plain]
type     = postgres
host     = db.example.com
port     = 5432
database = mydb
user     = myuser
password = plainpw

[demo]
type     = postgres
host     = demo.example.com
port     = 5432
database = demodb
user     = demouser
password = keyring:agami/demo
"""


@pytest.fixture
def agami_home(tmp_path, monkeypatch):
    art = tmp_path / "artifacts"
    local = art / "local"
    local.mkdir(parents=True, mode=0o700)
    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(art))
    monkeypatch.delenv("AGAMI_PROFILE", raising=False)
    (local / "credentials").write_text(CREDENTIALS)
    (local / "credentials").chmod(0o600)
    monkeypatch.setattr(sys, "argv", ["setup_pgauth.py", "--all"])
    import setup_pgauth

    importlib.reload(setup_pgauth)
    return setup_pgauth, local


def test_keyring_profile_gets_no_pgpass_line(agami_home, capsys):
    setup_pgauth, local = agami_home
    assert setup_pgauth.main() == 0
    body = (local / ".pgpass").read_text()
    assert "db.example.com:5432:mydb:myuser:plainpw" in body
    assert "demo.example.com" not in body
    assert "keyring:" not in body
    assert "skipped demo: password is in the OS credential store" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("db_type", "auth_file"),
    [("mysql", ".mysql.cnf"), ("snowflake", ".snowsql.cnf")],
)
def test_keyring_profile_gets_no_other_auth_file(agami_home, db_type, auth_file, capsys):
    setup_pgauth, local = agami_home
    (local / "credentials").write_text(
        f"[demo]\ntype = {db_type}\nhost = h.example.com\nuser = u\naccount = acme\n"
        "password = keyring:agami/demo\n"
    )
    assert setup_pgauth.main() == 0
    assert not (local / auth_file).exists()
    assert "skipped demo" in capsys.readouterr().out
