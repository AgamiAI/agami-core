"""`execute_sql`'s description states the row cap and statement deadline (#326).

It said a result over "the deployment row ceiling" was refused and never said what the ceiling was,
so a client learned the number by being refused — a warehouse round trip and a retry each time.
"""

from __future__ import annotations

import pytest

pytest.importorskip("pydantic")

import execute_sql  # noqa: E402
import tools  # noqa: E402


def test_statement_limits_are_the_enforced_ones(monkeypatch):
    monkeypatch.setenv("AGAMI_SQL_MAX_ROWS", "250")
    monkeypatch.setenv("AGAMI_SQL_TIMEOUT_S", "12")

    assert tools.statement_limits() == {
        "max_rows": execute_sql._resolve_row_cap(),
        "timeout_s": execute_sql._resolve_timeout_s(),
    }
    assert tools.statement_limits() == {"max_rows": 250, "timeout_s": 12}


def test_the_sentence_names_both_numbers_and_what_to_do(monkeypatch):
    monkeypatch.setenv("AGAMI_SQL_MAX_ROWS", "2500")
    monkeypatch.setenv("AGAMI_SQL_TIMEOUT_S", "45")

    sentence = tools._execute_sql_limits_sentence()

    assert "2,500 rows" in sentence
    assert "45s" in sentence
    assert "LIMIT" in sentence and "ORDER BY" in sentence


def test_the_description_carries_the_sentence(monkeypatch):
    """Built when the registry is built, from the environment the executor also reads — the process
    environment, fixed at start-up — so what the client is told is what is enforced."""
    monkeypatch.delenv("AGAMI_SQL_MAX_ROWS", raising=False)
    monkeypatch.delenv("AGAMI_SQL_TIMEOUT_S", raising=False)

    assert tools._execute_sql_limits_sentence() in tools.TOOLS["execute_sql"]["description"]
