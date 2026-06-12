"""Tests for promote_credentials.py — deterministic credentials.example -> credentials.

The old skill-inline `if [ ! -f credentials ]; then mv` only handled the first profile;
the Nth profile (file already exists) was left to LLM improvisation, which could duplicate
a profile or create a [main]/[main] collision. This locks the behavior down.
"""
from __future__ import annotations

import stat
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agami" / "scripts"))

from promote_credentials import promote  # noqa: E402

_FILLED = """\
# ~/.agami/credentials.example
[main]
type     = postgres
host     = db.example.com
port     = 5432
database = shop
user     = analyst
password = s3cret
"""

_TEMPLATE_WITH_PLACEHOLDERS = """\
[main]
type     = postgres
host     = your-host
port     = 5432
database = your-database
user     = your-username
password = your-password
"""


def _example(d: Path, body: str) -> None:
    (d / "credentials.example").write_text(body, encoding="utf-8")


def test_first_profile_is_moved_and_chmod_600(tmp_path):
    _example(tmp_path, _FILLED)
    msg, code = promote(tmp_path)
    assert code == 0 and msg.startswith("SECURED") and "main" in msg
    creds = tmp_path / "credentials"
    assert creds.exists() and not (tmp_path / "credentials.example").exists()  # template consumed
    assert stat.S_IMODE(creds.stat().st_mode) == 0o600
    assert "[main]" in creds.read_text()


def test_second_profile_is_appended(tmp_path):
    # an existing credentials file with [main] already present
    (tmp_path / "credentials").write_text("[main]\ntype = postgres\nhost = a\nuser = u\npassword = p\n")
    _example(tmp_path, _FILLED.replace("[main]", "[warehouse]"))
    msg, code = promote(tmp_path)
    assert code == 0 and msg.startswith("APPENDED") and "warehouse" in msg
    text = (tmp_path / "credentials").read_text()
    assert "[main]" in text and "[warehouse]" in text  # both profiles present
    assert not (tmp_path / "credentials.example").exists()  # template consumed
    assert stat.S_IMODE((tmp_path / "credentials").stat().st_mode) == 0o600


def test_name_collision_refuses_and_touches_nothing(tmp_path):
    original = "[main]\ntype = postgres\nhost = ORIGINAL\nuser = u\npassword = p\n"
    (tmp_path / "credentials").write_text(original)
    _example(tmp_path, _FILLED)  # also [main] -> clash
    msg, code = promote(tmp_path)
    assert code == 3 and msg.startswith("COLLISION") and "main" in msg
    # neither file changed — the user must rename the new profile
    assert (tmp_path / "credentials").read_text() == original
    assert (tmp_path / "credentials.example").exists()


def test_placeholders_refused(tmp_path):
    _example(tmp_path, _TEMPLATE_WITH_PLACEHOLDERS)
    msg, code = promote(tmp_path)
    assert code == 2 and msg.startswith("PLACEHOLDERS_REMAIN")
    assert not (tmp_path / "credentials").exists()
    assert (tmp_path / "credentials.example").exists()  # left for the user to finish


def test_no_example_is_noop(tmp_path):
    msg, code = promote(tmp_path)
    assert code == 1 and msg == "NOTHING"


def test_example_without_section_errors(tmp_path):
    _example(tmp_path, "# just a comment, no [section]\n")
    msg, code = promote(tmp_path)
    assert code == 4 and msg.startswith("ERROR")
