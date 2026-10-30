"""An execution error no classifier reads says whose fault it was, by its SQLSTATE (#561).

A statement the database rejected for itself ("within group ORDER BY clauses for aggregate functions
must be the same") came back as `other` with the generic sentence, so the client could not tell a
statement to rewrite from a broken server. The database's text still never travels (ACE-039). The
SQLSTATE does: five characters, no values, and a class that means the same on every engine.
"""

from __future__ import annotations

import execute_sql
import pytest
import tools

PROFILE = "acme"
_ORDER_BY = "within group ORDER BY clauses for aggregate functions must be the same"


class _Raiser:
    def __init__(self, message: str, *, code: int = 5, sqlstate: str | None = None) -> None:
        self._message, self._code, self._sqlstate = message, code, sqlstate

    def execute(self, vetted_sql, creds, *, profile):  # noqa: ANN001, ANN201
        raise execute_sql.ExecutorError(self._message, code=self._code, sqlstate=self._sqlstate)


class _DriverError(Exception):
    """A driver exception carrying its code the way psycopg2 does."""

    def __init__(self, message: str, pgcode: str) -> None:
        super().__init__(message)
        self.pgcode = pgcode


class _BuiltInStyle:
    """Raises inside the `except` that caught the driver's exception, as the built-in engines do."""

    def __init__(self, driver_exc: BaseException) -> None:
        self._driver_exc = driver_exc

    def execute(self, vetted_sql, creds, *, profile):  # noqa: ANN001, ANN201
        try:
            raise self._driver_exc
        except Exception as e:
            raise execute_sql.ExecutorError(f"Postgres execution error: {e}", code=5)  # noqa: B904


@pytest.fixture
def engine(monkeypatch):
    monkeypatch.setattr(execute_sql, "_model_safety", lambda s, p, a: (s, None))
    monkeypatch.setattr(execute_sql, "_load_credentials", lambda p, o: {"type": "postgres"})


def _run(executor):  # noqa: ANN001, ANN202
    return execute_sql.execute_guarded("SELECT 1", PROFILE, None, executor=executor, no_safety=True)


@pytest.mark.parametrize(
    ("sqlstate", "kind"),
    [
        ("42803", "syntax"),  # a rule the statement broke
        ("42P10", "syntax"),  # a vendor code in a standard class
        ("0A000", "syntax"),  # a feature this engine lacks
        ("22012", "syntax"),  # division by zero
        ("53200", "other"),  # out of memory
        ("54001", "other"),  # statement too complex
        ("40001", "other"),  # serialization failure
        ("XX000", "other"),  # a class the table does not name
    ],
)
def test_the_code_and_a_sentence_come_back_and_the_text_does_not(sqlstate, kind, engine):
    env = _run(_Raiser(f"Redshift execution error: {_ORDER_BY} amount=4521.17", sqlstate=sqlstate))
    assert env.status == "failed"
    assert env.failure.kind == kind
    assert f"(SQLSTATE {sqlstate})" in env.failure.message
    for token in ("within group", "ORDER BY", "4521.17", "amount"):
        assert token not in env.failure.message


def test_the_issues_own_example_is_told_to_rewrite(engine):
    # Redshift sends 0A000 for two ordered-set aggregates ordered differently (checked live).
    env = _run(_Raiser(_ORDER_BY, sqlstate="0A000"))
    assert env.failure.kind == "syntax"
    assert "does not support something this statement uses (SQLSTATE 0A000)" in env.failure.message


def test_a_syntax_class_tells_the_client_to_rewrite(engine):
    env = _run(_Raiser(_ORDER_BY, sqlstate="42803"))
    assert "Rewrite the statement" in env.failure.message


@pytest.mark.parametrize(
    ("sqlstate", "kind"),
    [("42501", "permission"), ("08006", "network"), ("28000", "auth")],
)
def test_a_code_whose_fix_is_not_a_rewrite_keeps_its_fixed_sentence(sqlstate, kind, engine):
    env = _run(_Raiser("an error no needle reads", sqlstate=sqlstate))
    assert env.failure.kind == kind
    assert env.failure.message == execute_sql._ERROR_MESSAGES[kind]


def test_the_code_is_read_from_the_driver_exception_the_engine_raised_inside(engine):
    env = _run(_BuiltInStyle(_DriverError(_ORDER_BY, "42803")))
    assert env.failure.kind == "syntax"
    assert "(SQLSTATE 42803)" in env.failure.message


def test_the_redshift_driver_field_dict_is_read():
    exc = Exception({"S": "ERROR", "C": "0A000", "M": "not supported"})
    assert execute_sql.sqlstate_of(exc) == "0A000"
    assert execute_sql.sqlstate_of(Exception({b"C": b"42883"})) == "42883"


@pytest.mark.parametrize(
    "exc",
    [
        Exception("ERROR 42803: within group"),  # a code inside the text is not the server's
        Exception({"C": "42-03"}),
        Exception({"C": "4280"}),
        None,
    ],
)
def test_nothing_but_a_well_formed_driver_field_is_a_code(exc):
    assert execute_sql.sqlstate_of(exc) is None


def test_a_malformed_code_passed_at_the_raise_site_is_dropped():
    assert execute_sql.ExecutorError("m", code=5, sqlstate="42803; x").sqlstate is None


def test_without_a_code_the_failure_is_unchanged(engine):
    env = _run(_Raiser(_ORDER_BY))
    assert env.failure.kind == "other"
    assert env.failure.message == execute_sql.UNEXPECTED_FAILURE_MESSAGE


def test_a_kind_the_text_classifier_read_is_not_overridden(engine):
    env = _run(_Raiser('column "amount" does not exist', sqlstate="42703"))
    assert env.failure.kind == "column_not_found"
    assert env.failure.message == execute_sql._ERROR_MESSAGES["column_not_found"]


def test_a_connect_failure_is_not_read_as_the_statements_fault(engine):
    env = _run(_Raiser("server said no", code=4, sqlstate="42803"))
    assert env.failure.kind == "auth"
    assert env.failure.message == execute_sql._ERROR_MESSAGES["auth"]


@pytest.mark.parametrize(
    ("text", "code"),
    [
        (
            "canceling statement due to user request",
            4,
        ),  # a connect error the classifier calls other
        ("this deployment has no database configured", 6),  # a deployment's own `other`
    ],
)
def test_only_an_execution_error_is_read_by_its_code(text, code, engine):
    env = _run(_Raiser(text, code=code, sqlstate="42803"))
    assert env.failure.kind == "other"
    assert "SQLSTATE" not in env.failure.message


def test_the_databricks_attribute_is_read():
    exc = Exception("boom")
    exc.sql_state = "42P01"
    assert execute_sql.sqlstate_of(exc) == "42P01"


class _Loud(str):
    def __format__(self, spec):  # noqa: ANN001, ANN204
        return "LEAK amount=4521.17"


class _ThrowingDriverError(Exception):
    @property
    def sqlstate(self):  # noqa: ANN201
        raise RuntimeError("driver bug")


def test_a_code_is_a_plain_string_whatever_the_driver_returned():
    exc = Exception("boom")
    exc.sqlstate = _Loud("42803")
    code = execute_sql.sqlstate_of(exc)
    assert type(code) is str
    assert "LEAK" not in execute_sql._sqlstate_failure(code)[1]


def test_a_driver_attribute_that_throws_reads_as_no_code(engine):
    assert execute_sql.sqlstate_of(_ThrowingDriverError()) is None
    env = _run(_BuiltInStyle(_ThrowingDriverError(_ORDER_BY)))
    assert env.status == "failed"
    assert env.failure.message == execute_sql.UNEXPECTED_FAILURE_MESSAGE


def test_a_dialect_rule_still_wins(monkeypatch):
    monkeypatch.setattr(execute_sql, "_model_safety", lambda s, p, a: (s, None))
    monkeypatch.setattr(execute_sql, "_load_credentials", lambda p, o: {"type": "redshift"})
    text = (
        "{'S': 'ERROR', 'C': '42883', 'M': 'function string_agg(character varying) does not exist'}"
    )
    env = _run(_Raiser(text, sqlstate="42883"))
    assert env.failure.kind == "syntax"
    assert "STRING_AGG" in env.failure.message
    assert "SQLSTATE" not in env.failure.message


# --- The forked transport ---------------------------------------------------------------------

_SYNTAX = execute_sql.FAILURE_KIND_TO_EXIT["syntax"]
_CODED = execute_sql._sqlstate_failure("42803")[1]


def test_the_parent_relays_a_coded_sentence_the_child_chose():
    assert tools._child_failure_message(_SYNTAX, f"a library line\n{_CODED}\n") == _CODED


@pytest.mark.parametrize(
    "stderr",
    [
        _ORDER_BY,  # the driver's own words
        f"{_CODED} amount=4521.17",  # anything appended
        _CODED.replace("42803", "42803 OR 1"),  # a code slot holding more than a code
        _CODED.replace("42803", "abcde"),  # lower case is not a SQLSTATE
    ],
)
def test_anything_but_an_exact_coded_sentence_is_rebuilt(stderr):
    assert tools._child_failure_message(_SYNTAX, stderr) == execute_sql._ERROR_MESSAGES["syntax"]


def test_a_coded_sentence_on_another_sanitized_code_is_not_relayed():
    code = execute_sql.FAILURE_KIND_TO_EXIT["column_not_found"]
    assert (
        tools._child_failure_message(code, _CODED)
        == execute_sql._ERROR_MESSAGES["column_not_found"]
    )


# --- An aggregate inside another (#519) -------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "sqlstate"),
    [
        ("Postgres execution error: aggregate function calls cannot be nested\n"
         "LINE 1: SELECT AVG(SUM(amount)) FROM orders", "42803"),
        ("SQLite execution error: misuse of aggregate function SUM()", None),
        ("Snowflake execution error: 002065 (42601): SQL compilation error:\n"
         "Aggregate functions cannot be nested: [SUM(AMOUNT)] nested in [AVG]", None),
        ("BigQuery execution error: Aggregations of aggregations are not allowed", None),
    ],
)
def test_a_nested_aggregate_is_explained_and_keeps_its_kind(text, sqlstate, engine, monkeypatch):
    env = _run(_Raiser(text, sqlstate=sqlstate))
    assert env.failure.message == execute_sql.NESTED_AGGREGATE_MESSAGE
    assert "amount" not in env.failure.message.lower()
    # the kind, and so the exit code, is what it was without the sentence: only the message is new
    monkeypatch.setattr(execute_sql, "_is_nested_aggregate_error", lambda text: False)
    before = _run(_Raiser(text, sqlstate=sqlstate))
    assert env.failure.kind == before.failure.kind
    assert env.status == before.status == "failed"


def test_an_echoed_statement_does_not_pick_the_nested_aggregate_sentence(engine):
    env = _run(_Raiser("Postgres execution error: something else\n"
                       "LINE 1: SELECT 'aggregate function calls cannot be nested'"))
    assert env.failure.message == execute_sql.UNEXPECTED_FAILURE_MESSAGE


@pytest.mark.parametrize("kind", ["syntax", "other"])
def test_the_parent_relays_the_nested_aggregate_sentence_exactly(kind):
    code = execute_sql.FAILURE_KIND_TO_EXIT[kind]
    msg = execute_sql.NESTED_AGGREGATE_MESSAGE
    assert tools._child_failure_message(code, f"a library line\n{msg}\n") == msg
    assert tools._child_failure_message(code, f"{msg} amount=4521.17") != f"{msg} amount=4521.17"
