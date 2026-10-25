"""A database error a dialect rule explains comes back with that rule (#566).

The rules reach a client only through list_datasources. A client that already knows its datasource
skips that call, writes the PostgreSQL the rules warn against, and the engine rejects it. The error
text itself is never relayed (ACE-039), and "function string_agg(...) does not exist" used to be
classified as a missing column or as unclassified, so the client was told to re-introspect or nothing. Answering the error with
the rule turns the wasted turn into a correct retry, without letting any of the database's words
through: the rule is our own text, chosen FROM the driver's.
"""

from __future__ import annotations

import execute_sql
import pytest
import sql_dialect_rules
import tools

PROFILE = "acme"

# Real Redshift messages (shapes as the driver reports them), and the rule each one needs.
_REDSHIFT_ERRORS = [
    (
        "{'S': 'ERROR', 'C': '42883', 'M': 'function string_agg(character varying, \"unknown\") does "
        "not exist', 'H': 'No function matches the given name and argument types.'}",
        "No STRING_AGG",
    ),
    (
        "{'S': 'ERROR', 'C': '42883', 'M': 'function pg_catalog.date_add(\"unknown\", integer, "
        "timestamp with time zone) does not exist'}",
        "Date arithmetic",
    ),
    (
        "{'S': 'ERROR', 'C': 'XX000', 'M': 'Using LISTAGG/PERCENTILE_CONT/MEDIAN aggregate functions "
        "with other distinct aggregate function not supported'}",
        "LISTAGG, MEDIAN and PERCENTILE_CONT",
    ),
    (
        "{'S': 'ERROR', 'C': '42846', 'M': 'cannot cast type boolean to character varying'}",
        "A BOOLEAN cannot",
    ),
    ("function substr(character varying, integer, integer) does not exist", "Use SUBSTRING"),
    (
        "This type of correlated subquery pattern is not supported due to internal error",
        "Many correlated subqueries",
    ),
    (
        "FULL JOIN is only supported with merge-joinable or hash-joinable join conditions",
        "FULL JOIN",
    ),
    ('function regexp_matches(character varying, "unknown") does not exist', "No GENERATE_SERIES"),
]


def _rule(starts: str) -> str:
    block = sql_dialect_rules.dialect_rules_for("Redshift")
    return next(line[2:] for line in block.splitlines() if line.startswith(f"- {starts}"))


@pytest.mark.parametrize(("text", "starts"), _REDSHIFT_ERRORS, ids=lambda v: str(v)[:40])
def test_each_rejected_construct_gets_its_own_rule(text, starts):
    fix = sql_dialect_rules.dialect_fix_for("redshift", text)
    assert fix is not None and fix.endswith(_rule(starts))


@pytest.mark.parametrize("engine", ["Redshift", "redshift", " REDSHIFT "])
def test_the_engine_is_matched_however_it_is_spelled(engine):
    # The model says `Redshift`; the executor only knows the credentials' `redshift`.
    assert sql_dialect_rules.dialect_fix_for(engine, _REDSHIFT_ERRORS[0][0]) is not None


@pytest.mark.parametrize(
    ("engine", "text"),
    [
        ("redshift", 'column "amount" does not exist'),  # a real missing column stays one
        ("redshift", "permission denied for schema finance"),
        ("postgres", _REDSHIFT_ERRORS[0][0]),  # the rules are Redshift's, not every engine's
        (None, _REDSHIFT_ERRORS[0][0]),
        ("redshift", ""),
        ("redshift", None),
    ],
)
def test_nothing_is_claimed_without_an_engine_rule_that_fits(engine, text):
    assert sql_dialect_rules.dialect_fix_for(engine, text) is None


def test_every_fix_names_a_rule_that_exists():
    # A fix whose `starts` matched no bullet would raise at the moment an agent needed it.
    messages = sql_dialect_rules.FIX_MESSAGES
    block = sql_dialect_rules.dialect_rules_for("Redshift")
    assert len(messages) == len(sql_dialect_rules._REDSHIFT_FIXES)
    for message in messages:
        assert message.split(". ", 1)[1] in block


class _Raiser:
    def __init__(self, message: str) -> None:
        self._message = message

    def execute(self, vetted_sql, creds, *, profile):  # noqa: ANN001, ANN201
        raise execute_sql.ExecutorError(self._message, code=5)


@pytest.fixture
def redshift(monkeypatch):
    monkeypatch.setattr(execute_sql, "_model_safety", lambda s, p, a: (s, None))
    monkeypatch.setattr(execute_sql, "_load_credentials", lambda p, o: {"type": "redshift"})


@pytest.mark.parametrize(("text", "starts"), _REDSHIFT_ERRORS, ids=lambda v: str(v)[:40])
def test_the_failure_carries_the_rule_and_none_of_the_databases_words(text, starts, redshift):
    env = execute_sql.execute_guarded(
        "SELECT 1", PROFILE, None, executor=_Raiser(text), no_safety=True
    )
    assert env.status == "failed"
    # The statement's fault, fixed by a rewrite: `syntax`, not the `column_not_found` the generic
    # classifier read into "function ... does not exist".
    assert env.failure.kind == "syntax"
    assert env.failure.message.endswith(_rule(starts))
    for token in ("42883", "XX000", "character varying", "pg_catalog", "does not exist", "'M'"):
        assert token not in env.failure.message


def test_an_unrelated_error_keeps_its_fixed_sentence(redshift):
    env = execute_sql.execute_guarded(
        "SELECT 1",
        PROFILE,
        None,
        executor=_Raiser('column "amount" does not exist'),
        no_safety=True,
    )
    assert env.failure.kind == "column_not_found"
    assert env.failure.message == execute_sql._ERROR_MESSAGES["column_not_found"]


def test_another_engine_keeps_its_fixed_sentence(monkeypatch):
    monkeypatch.setattr(execute_sql, "_model_safety", lambda s, p, a: (s, None))
    monkeypatch.setattr(execute_sql, "_load_credentials", lambda p, o: {"type": "postgres"})
    env = execute_sql.execute_guarded(
        "SELECT 1", PROFILE, None, executor=_Raiser(_REDSHIFT_ERRORS[0][0]), no_safety=True
    )
    assert env.failure.message not in sql_dialect_rules.FIX_MESSAGES
    assert env.failure.kind != "syntax"


# --- The forked transport ---------------------------------------------------------------------
#
# The parent rebuilds the sanitized band from the exit code and never relays the child's stderr,
# because that stream is shared with anything a library logs. A rule is the one message it cannot
# rebuild, so it passes only as an EXACT match against the messages built from our own rules.

_SYNTAX = execute_sql.FAILURE_KIND_TO_EXIT["syntax"]
_FIX = sql_dialect_rules.dialect_fix_for("redshift", _REDSHIFT_ERRORS[0][0])


def test_the_parent_relays_a_rule_the_child_chose():
    stderr = f"some library warning naming internal_ref\n{_FIX}\n"
    assert tools._child_failure_message(_SYNTAX, stderr) == _FIX


@pytest.mark.parametrize(
    "stderr",
    [
        "function string_agg(character varying) does not exist",  # the driver's own words
        f"{_FIX} internal_ref",  # a rule with anything appended is not a rule
        f"prefix {_FIX}",
        "",
    ],
)
def test_anything_but_an_exact_rule_is_rebuilt_not_relayed(stderr):
    assert tools._child_failure_message(_SYNTAX, stderr) == execute_sql._ERROR_MESSAGES["syntax"]


def test_a_rule_line_on_another_code_is_not_relayed():
    # Only a `syntax` failure can carry a rule; on any other sanitized code it is just stderr.
    code = execute_sql.FAILURE_KIND_TO_EXIT["column_not_found"]
    assert (
        tools._child_failure_message(code, _FIX) == execute_sql._ERROR_MESSAGES["column_not_found"]
    )


class _CodeRaiser:
    def __init__(self, message: str, code: int) -> None:
        self._message, self._code = message, code

    def execute(self, vetted_sql, creds, *, profile):  # noqa: ANN001, ANN201
        raise execute_sql.ExecutorError(self._message, code=self._code)


@pytest.mark.parametrize(
    ("text", "code"),
    [
        # A connect failure (code 4) whose text happens to carry a fragment is still not a rewrite.
        ("Redshift connect failed: function string_agg( in a proxy banner", 4),
        # A permission failure is a GRANT, whatever else the message mentions.
        ("permission denied for function string_agg(text)", 5),
    ],
)
def test_a_failure_whose_fix_is_not_a_rewrite_keeps_its_kind(text, code, redshift):
    env = execute_sql.execute_guarded(
        "SELECT 1", PROFILE, None, executor=_CodeRaiser(text, code), no_safety=True
    )
    assert env.failure.kind != "syntax"
    assert env.failure.message not in sql_dialect_rules.FIX_MESSAGES


# --- Matching on the primary message only (Copilot review on #569) ----------------------------


def test_a_construct_named_only_in_the_echoed_statement_claims_nothing(redshift):
    # psycopg2 puts the failing line of SQL after the message; a literal there is not the failure.
    text = "column \"amount\" does not exist\nLINE 1: SELECT string_agg(note, ',') FROM orders"
    env = execute_sql.execute_guarded(
        "SELECT 1", PROFILE, None, executor=_Raiser(text), no_safety=True
    )
    assert env.failure.kind == "column_not_found"
    assert env.failure.message not in sql_dialect_rules.FIX_MESSAGES


def test_a_real_function_error_keeps_its_rule_whatever_the_other_fields_say(redshift):
    # The other fields mention a table, which the whole-text classifier reads as table_not_found.
    text = (
        "{'S': 'ERROR', 'C': '42883', 'M': 'function string_agg(character varying, \"unknown\") "
        "does not exist', 'H': 'see the table sales_table', 'P': '8', 'R': 'ParseFuncOrColumn'}"
    )
    env = execute_sql.execute_guarded(
        "SELECT 1", PROFILE, None, executor=_Raiser(text), no_safety=True
    )
    assert env.failure.kind == "syntax"
    assert env.failure.message.endswith(_rule("No STRING_AGG"))


def test_a_boolean_cast_to_a_number_is_not_given_the_text_rule():
    # The rule rewrites a boolean into 'true'/'false' text, which does not fix a numeric cast.
    assert (
        sql_dialect_rules.dialect_fix_for("redshift", "cannot cast type boolean to numeric") is None
    )


def test_the_primary_message_is_the_m_field_or_the_first_line():
    fields = "{'S': 'ERROR', 'C': '42883', 'M': 'function substr(text) does not exist', 'H': 'x'}"
    assert sql_dialect_rules.primary_diagnostic(fields) == "function substr(text) does not exist"
    assert sql_dialect_rules.primary_diagnostic("first line\nLINE 1: SELECT 1") == "first line"
    assert sql_dialect_rules.primary_diagnostic("") == ""


def test_a_field_spelled_inside_the_echoed_statement_is_not_the_primary_message(redshift):
    # A later line can be the statement, and a comment in it can spell the driver's field syntax.
    text = (
        'column "amount" does not exist\n'
        "LINE 1: SELECT amount /* 'M': 'function string_agg(' */ FROM orders"
    )
    assert sql_dialect_rules.primary_diagnostic(text) == 'column "amount" does not exist'
    env = execute_sql.execute_guarded(
        "SELECT 1", PROFILE, None, executor=_Raiser(text), no_safety=True
    )
    assert env.failure.message not in sql_dialect_rules.FIX_MESSAGES


def test_an_executor_prefix_before_the_field_dict_still_finds_the_message():
    text = (
        "Redshift execution error: {'S': 'ERROR', 'C': '42883', 'M': 'function string_agg(text) "
        "does not exist', 'H': 'cannot cast type boolean to text'}"
    )
    assert sql_dialect_rules.primary_diagnostic(text) == "function string_agg(text) does not exist"
    assert sql_dialect_rules.dialect_fix_for("redshift", text).endswith(_rule("No STRING_AGG"))
