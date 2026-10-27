"""The forked transport rebuilds an `other` failure, like every other sanitized one (#583).

`other` (exit 6) had no entry in `_ERROR_MESSAGES`, so the parent relayed the child's whole stderr
for it, as it does for the authored codes 2 and 3. stderr is a shared stream: anything a library
logs during the run lands on it before the failure line, and reached `failure.message`.
"""

from __future__ import annotations

import logging

import execute_sql
import tools

_OTHER = execute_sql.FAILURE_KIND_TO_EXIT["other"]
_LIBRARY = "WARNING somelib: row value 4521.17 for /srv/app/secret.yaml"
_CODED = execute_sql._sqlstate_failure("53200")[1]


def test_exit_6_is_the_other_code():
    assert _OTHER == 6


def test_a_library_line_never_reaches_the_caller(caplog):
    stderr = f"{_LIBRARY}\n{execute_sql.UNEXPECTED_FAILURE_MESSAGE}\n"
    with caplog.at_level(logging.ERROR):
        message = tools._child_failure_message(_OTHER, stderr)
    assert message == execute_sql.UNEXPECTED_FAILURE_MESSAGE
    # Kept for the operator, as for any stream the parent will not relay.
    assert any("4521.17" in r.getMessage() for r in caplog.records if r.name == "tools")


def test_a_coded_sentence_the_child_chose_is_relayed_alone():
    assert tools._child_failure_message(_OTHER, f"{_LIBRARY}\n{_CODED}\n") == _CODED


def test_a_coded_sentence_with_anything_appended_is_not():
    assert (
        tools._child_failure_message(_OTHER, f"{_CODED} {_LIBRARY}")
        == execute_sql.UNEXPECTED_FAILURE_MESSAGE
    )


def test_the_generic_sentence_alone_is_not_logged_as_unexpected(caplog):
    with caplog.at_level(logging.ERROR):
        message = tools._child_failure_message(_OTHER, execute_sql.UNEXPECTED_FAILURE_MESSAGE)
    assert message == execute_sql.UNEXPECTED_FAILURE_MESSAGE
    assert not [r for r in caplog.records if r.name == "tools"]


def test_the_authored_codes_are_still_relayed():
    # Codes 2 and 3 carry the operator's remediation, which is this module's own text.
    text = "No warehouse credentials for profile [acme]. Set DATASOURCE_URL…"
    assert tools._child_failure_message(2, text) == text
