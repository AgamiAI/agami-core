"""The native-CLI query tier reports a failed query as a failure, not an empty result.

`psql -f <file>` prints a statement's error and exits 0 unless `ON_ERROR_STOP` is set (psql's own
EXIT STATUS section: 3 only "if an error occurred in a script and the variable ON_ERROR_STOP was
set"), and snowsql carries on past an error unless `exit_on_error` is true. The cli tier read
those exits as success and showed the person an empty table. Every file form the plugin shows now
carries the flag; sqlite3's `.read` already exits 1, which is checked against the real binary.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

PLUGIN = Path(__file__).resolve().parent.parent / "plugins" / "agami"
DOCS = sorted(PLUGIN.rglob("*.md"))


def _lines(pattern: str) -> list[tuple[str, str]]:
    found = []
    for doc in DOCS:
        for n, line in enumerate(doc.read_text(encoding="utf-8").splitlines(), 1):
            if re.search(pattern, line):
                found.append((f"{doc.relative_to(PLUGIN)}:{n}", line))
    return found


def test_every_psql_file_form_stops_on_an_error():
    forms = _lines(r"\bpsql\b.* -f ")
    assert len(forms) >= 4
    for where, line in forms:
        assert "-v ON_ERROR_STOP=1" in line, where


def test_every_snowsql_form_exits_on_an_error():
    forms = _lines(r"\bsnowsql --config")
    assert len(forms) >= 3
    for where, line in forms:
        assert "-o exit_on_error=true" in line, where


@pytest.mark.skipif(shutil.which("sqlite3") is None, reason="no sqlite3 CLI")
def test_sqlite3_read_exits_non_zero_on_a_failed_statement(tmp_path):
    db = tmp_path / "t.db"
    sql = tmp_path / "agami-query.txt"
    sql.write_text("SELECT 1 AS a;\nSELECT nope FROM missing;\n")
    out = subprocess.run(
        ["sqlite3", "-csv", "-header", str(db), f'.read "{sql}"'], capture_output=True, text=True
    )
    assert out.returncode != 0
    assert "missing" in out.stderr
