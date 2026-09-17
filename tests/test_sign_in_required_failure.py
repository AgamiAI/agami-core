"""`sign_in_required` — the failure an injected per-person executor raises when the asking person's own
credential is missing or can no longer be renewed.

Before it existed, such an executor could only raise code 4, which reaches the caller as `auth` and
the sentence "The database rejected the connection's credentials." A client relaying that told a
person their warehouse was broken when a fresh sign-in was all it took. These tests pin that the new
code arrives as its own kind with its own sentence on every transport, and that the executor's own
text can neither leak nor reclassify it.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PKG_SRC = REPO_ROOT / "packages" / "agami-core" / "src"
if str(PKG_SRC) not in sys.path:
    sys.path.insert(0, str(PKG_SRC))

import execute_sql  # noqa: E402

SIGN_IN_CODE = execute_sql.FAILURE_KIND_TO_EXIT["sign_in_required"]

# Deliberately worded to trip the `auth` needle: the executor has already decided what happened, and
# a phrase in its message must not turn a sign-in back into an operator's credential problem.
EXECUTOR_TEXT = "no credential for you@example.com: authentication failed upstream"


class _NeedsSignIn:
    def execute(self, vetted_sql, creds, *, profile):
        raise execute_sql.ExecutorError(EXECUTOR_TEXT, code=SIGN_IN_CODE)


@pytest.fixture(autouse=True)
def _reset_injected_executor():
    import tools

    tools.set_injected_executor(None)
    yield
    tools.set_injected_executor(None)


@pytest.fixture
def _guarded_path_reaches_the_executor(monkeypatch):
    monkeypatch.setattr(
        execute_sql,
        "_load_credentials",
        lambda p, org_id="local": {"type": "sqlite", "path": ":memory:"},
    )
    monkeypatch.setattr(execute_sql, "_model_safety", lambda s, p, a: (s, None))


def test_the_code_is_its_own_and_is_not_auth():
    assert SIGN_IN_CODE not in (4, execute_sql._DEFAULT_FAILURE_EXIT)
    assert execute_sql.EXIT_TO_FAILURE_KIND[SIGN_IN_CODE] == "sign_in_required"
    assert SIGN_IN_CODE not in execute_sql._AUTHORED_EXIT_CODES


def test_the_executors_text_cannot_reclassify_it():
    assert execute_sql._classify_db_error(EXECUTOR_TEXT, SIGN_IN_CODE) == "sign_in_required"
    # The same words on code 4 are still `auth` — the arm keys on the code, not on the phrase.
    assert execute_sql._classify_db_error(EXECUTOR_TEXT, 4) == "auth"


def test_the_message_tells_the_person_to_sign_in_again():
    message = execute_sql._ERROR_MESSAGES["sign_in_required"].lower()
    assert "sign in again" in message
    assert "new conversation" in message
    # Not the sentence that sent everyone to the warehouse.
    assert message != execute_sql._ERROR_MESSAGES["auth"].lower()


def test_in_process_the_caller_gets_the_kind_and_the_fixed_sentence(
    monkeypatch, _guarded_path_reaches_the_executor
):
    import tools

    monkeypatch.setattr(tools, "resolve_profile", lambda ds: "acme")
    tools.set_injected_executor(_NeedsSignIn())
    out = json.loads(tools.tool_execute_sql({"sql": "SELECT 1", "datasource": "acme"}))

    assert out["status"] == "failed"
    assert out["failure"]["kind"] == "sign_in_required"
    assert out["failure"]["message"] == execute_sql._ERROR_MESSAGES["sign_in_required"]
    assert "you@example.com" not in json.dumps(out)


def test_the_cli_exits_with_the_code_and_writes_the_fixed_sentence(
    tmp_path, monkeypatch, capsys, _guarded_path_reaches_the_executor
):
    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(tmp_path))
    monkeypatch.setattr(execute_sql, "BUILTIN_EXECUTOR", _NeedsSignIn())
    monkeypatch.setattr(sys, "argv", ["execute_sql", "--profile", "acme", "--sql", "SELECT 1"])

    assert execute_sql.main() == SIGN_IN_CODE
    assert capsys.readouterr().err == execute_sql._ERROR_MESSAGES["sign_in_required"] + "\n"


def test_the_fork_parent_rebuilds_the_kind_and_sentence():
    import tools

    assert tools._classify_exit(SIGN_IN_CODE) == "sign_in_required"
    assert (
        tools._child_failure_message(SIGN_IN_CODE, EXECUTOR_TEXT)
        == execute_sql._ERROR_MESSAGES["sign_in_required"]
    )


def test_the_tool_tells_the_agent_what_to_do_with_it():
    import tools

    described = tools.TOOLS["execute_sql"]["description"]
    assert "sign_in_required" in described
    assert "sign in again" in described
