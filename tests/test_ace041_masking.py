"""ACE-041 slice 3 — the behaviour flip: a *maskable* sensitive projection is no longer refused,
it is EXECUTED and the sensitive OUTPUT column is redacted to a token post-execution; an
*untraceable* sensitive projection still refuses (fail-closed). This pins the whole chain:

  guard classifies (runtime.check_sensitive_projection) -> _model_safety builds a data_protection
  Verdict + runs guardrail.policy -> execute_guarded applies the mask plan to result.rows -> the
  tool edge records `applied[{mask}]` on BOTH surfaces.

Also covers the slice-3 case-fold fix (`SELECT SSN` / `SELECT "SSN"` now match a lower-case declared
`ssn`, consistent with the table/column-scope gates) and the unparseable fail-closed decision (this
branch has NO ACE-037 upstream unscopable gate, so the PII path itself refuses an unparseable
statement when the model declares PII).

Synthetic, generic names only — `customers`, `orders`, `x`, `AcmeCorp`.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

pytest.importorskip("pydantic")
pytest.importorskip("sqlglot")

REPO_ROOT = Path(__file__).resolve().parent.parent
PKG_SRC = REPO_ROOT / "packages" / "agami-core" / "src"
if str(PKG_SRC) not in sys.path:
    sys.path.insert(0, str(PKG_SRC))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agami" / "scripts"))

import execute_sql  # noqa: E402
from semantic_model import models as m  # noqa: E402
from semantic_model import runtime as rt  # noqa: E402


def _org() -> "m.Organization":
    """`customers` has two sensitive columns (`ssn`, `email`); `orders` is non-sensitive for joins;
    `x` carries a column literally named `ssn` that is NOT sensitive (the same-name decoy)."""
    customers = m.Table(
        name="customers", schema="public", storage_connection="c", grain=["id"],
        columns=[
            m.Column(name="id", type="integer"),
            m.Column(name="name", type="string"),
            m.Column(name="ssn", type="string", sensitive=True),
            m.Column(name="email", type="string", sensitive=True),
        ],
    )
    orders = m.Table(
        name="orders", schema="public", storage_connection="c", grain=["id"],
        columns=[m.Column(name="id", type="integer"), m.Column(name="cust_id", type="integer")],
    )
    x = m.Table(
        name="x", schema="public", storage_connection="c", grain=["ssn"],
        columns=[m.Column(name="id", type="integer"), m.Column(name="ssn", type="string")],
    )
    sa = m.SubjectArea(name="area", description="d", tables_defined=[customers, orders, x])
    return m.Organization(organization="AcmeCorp", version=1, subject_areas=[sa])


class _SpyExecutor:
    """Records calls and returns a canned ``ExecResult`` — so a test can assert the executor RAN and
    what it returned was redacted downstream (not blocked)."""

    def __init__(self, result: "execute_sql.ExecResult"):
        self.calls: list[tuple] = []
        self._result = result

    def execute(self, vetted_sql: str, creds: dict, *, profile: str) -> "execute_sql.ExecResult":
        self.calls.append((vetted_sql, creds, profile))
        return self._result


@pytest.fixture
def guarded(monkeypatch):
    """Wire ``execute_guarded`` to run the REAL ``_model_safety`` over ``_org()`` with dummy creds, so
    a test drives the whole guard->policy->redact chain with only the executor faked."""
    monkeypatch.setattr(execute_sql, "_resolve_guard_model", lambda profile: _org())
    monkeypatch.setattr(execute_sql, "_load_credentials", lambda p: {"type": "sqlite", "path": ":memory:"})

    def _run(sql: str, spy: "_SpyExecutor", **kw):
        return execute_sql.execute_guarded(sql, "acme", None, executor=spy, **kw)

    return _run


def _R(columns, rows, truncated=False):
    return execute_sql.ExecResult(columns=list(columns), rows=[tuple(r) for r in rows], truncated=truncated)


# ---------------------------------------------------------------------------
# execute_guarded: a maskable projection PROCEEDS and the value is redacted.
# ---------------------------------------------------------------------------


def test_maskable_projection_redacts_the_output_value_and_does_not_block(guarded):
    spy = _SpyExecutor(_R(["ssn"], [("111-22-3333",), ("999-88-7777",)]))
    result = guarded("SELECT ssn FROM customers", spy)
    assert spy.calls, "the query must PROCEED to the executor, not be blocked"
    assert result.rows == [(execute_sql.REDACTION_TOKEN,), (execute_sql.REDACTION_TOKEN,)]
    assert result.columns == ["ssn"]
    assert result.masked_columns == ("customers.ssn",)


def test_only_the_sensitive_output_column_is_redacted(guarded):
    spy = _SpyExecutor(_R(["name", "ssn"], [("Alice", "111"), ("Bob", "222")]))
    result = guarded("SELECT name, ssn FROM customers", spy)
    # name (index 0, non-sensitive) is untouched; ssn (index 1) is redacted.
    tok = execute_sql.REDACTION_TOKEN
    assert result.rows == [("Alice", tok), ("Bob", tok)]
    assert result.masked_columns == ("customers.ssn",)


def test_masking_holds_on_a_row_capped_result(guarded):
    # The redaction runs on the ALREADY-capped rows and preserves the truncated flag (composes with
    # the existing cap, does not fight it).
    spy = _SpyExecutor(_R(["ssn"], [("a",), ("b",)], truncated=True))
    result = guarded("SELECT ssn FROM customers", spy)
    tok = execute_sql.REDACTION_TOKEN
    assert result.rows == [(tok,), (tok,)]
    assert result.truncated is True


def test_two_sensitive_columns_both_redacted(guarded):
    spy = _SpyExecutor(_R(["ssn", "email"], [("111", "a@x.io")]))
    result = guarded("SELECT ssn, email FROM customers", spy)
    tok = execute_sql.REDACTION_TOKEN
    assert result.rows == [(tok, tok)]
    assert result.masked_columns == ("customers.email", "customers.ssn")


def test_join_query_masks_the_sensitive_column_and_returns(guarded):
    spy = _SpyExecutor(_R(["ssn"], [("111",)]))
    result = guarded(
        "SELECT c.ssn FROM customers c JOIN orders o ON o.cust_id = c.id", spy
    )
    assert spy.calls
    assert result.rows == [(execute_sql.REDACTION_TOKEN,)]


# ---------------------------------------------------------------------------
# Untraceable / star projections still REFUSE (fail-closed), executor never runs.
# ---------------------------------------------------------------------------


def test_untraceable_projection_still_refuses(guarded):
    spy = _SpyExecutor(_R(["s"], [("x",)]))
    with pytest.raises(execute_sql.GuardRefused) as ei:
        guarded("SELECT UPPER(ssn) FROM customers", spy)
    assert ei.value.refusal.kind == "sensitive_columns"
    assert spy.calls == []  # buried value cannot be masked -> refused before the executor


def test_star_projection_still_refuses(guarded):
    spy = _SpyExecutor(_R(["a"], [("x",)]))
    with pytest.raises(execute_sql.GuardRefused) as ei:
        guarded("SELECT * FROM customers", spy)
    # SELECT * is caught by the star-ban safety gate FIRST (before the sensitive gate) — still refused.
    assert spy.calls == []
    assert ei.value.refusal.kind in ("select_star", "sensitive_columns")


def test_partially_maskable_union_fails_closed(guarded):
    # One arm buries ssn in UPPER(...) -> not every offending projection is maskable -> uncertain ->
    # reject (fail closed). The executor never runs.
    spy = _SpyExecutor(_R(["s"], [("x",)]))
    with pytest.raises(execute_sql.GuardRefused) as ei:
        guarded("SELECT UPPER(ssn) FROM customers UNION ALL SELECT id FROM customers", spy)
    assert ei.value.refusal.kind == "sensitive_columns"
    assert spy.calls == []


# ---------------------------------------------------------------------------
# Allowed queries are not masked at all (COUNT / WHERE / non-sensitive).
# ---------------------------------------------------------------------------


def test_count_of_sensitive_is_not_masked(guarded):
    spy = _SpyExecutor(_R(["n"], [(5,)]))
    result = guarded("SELECT COUNT(ssn) FROM customers", spy)
    assert result.rows == [(5,)]  # untouched
    assert result.masked_columns == ()


def test_nonsensitive_projection_is_not_masked(guarded):
    spy = _SpyExecutor(_R(["name"], [("Alice",), ("Bob",)]))
    result = guarded("SELECT name FROM customers WHERE ssn = :x", spy)
    assert result.rows == [("Alice",), ("Bob",)]
    assert result.masked_columns == ()


# ---------------------------------------------------------------------------
# Case-fold fix (ACE-041 slice 3): the sensitive-name match is now case-insensitive.
# ---------------------------------------------------------------------------


def test_uppercase_unquoted_sensitive_column_is_now_masked(guarded):
    spy = _SpyExecutor(_R(["SSN"], [("111",)]))
    result = guarded("SELECT SSN FROM customers", spy)
    assert spy.calls  # proceeds
    assert result.rows == [(execute_sql.REDACTION_TOKEN,)]
    assert result.masked_columns == ("customers.ssn",)


def test_quoted_uppercase_sensitive_column_is_now_masked(guarded):
    spy = _SpyExecutor(_R(["SSN"], [("111",)]))
    result = guarded('SELECT "SSN" FROM customers', spy)
    assert result.rows == [(execute_sql.REDACTION_TOKEN,)]
    assert result.masked_columns == ("customers.ssn",)


def test_mixed_case_sensitive_column_is_now_masked(guarded):
    spy = _SpyExecutor(_R(["Ssn"], [("111",)]))
    result = guarded("SELECT Ssn FROM customers", spy)
    assert result.rows == [(execute_sql.REDACTION_TOKEN,)]
    assert result.masked_columns == ("customers.ssn",)


def test_same_named_nonsensitive_column_on_other_table_still_not_flagged(guarded):
    # `x.ssn` is literally named `ssn` but NOT sensitive on `x` — projecting it (any case) is allowed.
    spy = _SpyExecutor(_R(["ssn"], [("v",)]))
    result = guarded("SELECT SSN FROM x", spy)
    assert result.rows == [("v",)]  # untouched
    assert result.masked_columns == ()


# ---------------------------------------------------------------------------
# Unparseable fail-closed (this branch has no upstream ACE-037 unscopable gate):
# an unparseable statement + a model that declares PII is REFUSED, never run.
# ---------------------------------------------------------------------------


def test_unparseable_with_declared_pii_fails_closed(guarded, monkeypatch):
    # Force sqlglot to fail to parse (tree=None) while it is otherwise available, so we exercise the
    # PARSE-FAILURE fail-closed branch (distinct from sqlglot-unavailable, which stays a fail-open).
    monkeypatch.setattr(rt, "_parse_sql", lambda sql: None)
    spy = _SpyExecutor(_R(["ssn"], [("111",)]))
    with pytest.raises(execute_sql.GuardRefused) as ei:
        guarded("SELECT ssn FROM customers", spy)
    assert ei.value.refusal.kind == "sensitive_columns"
    assert spy.calls == []


# ---------------------------------------------------------------------------
# _model_safety returns a typed (sql, Refusal|None, MaskPlan|None).
# ---------------------------------------------------------------------------


def test_model_safety_returns_a_mask_plan_for_a_maskable_projection(monkeypatch):
    monkeypatch.setattr(execute_sql, "_resolve_guard_model", lambda profile: _org())
    sql, refusal, plan = execute_sql._model_safety("SELECT id, ssn FROM customers", "acme", None)
    assert refusal is None
    assert plan is not None
    assert plan.indices == (1,)
    assert plan.columns == ("customers.ssn",)
    assert plan.token == execute_sql.REDACTION_TOKEN


def test_model_safety_rejects_an_untraceable_projection(monkeypatch):
    monkeypatch.setattr(execute_sql, "_resolve_guard_model", lambda profile: _org())
    sql, refusal, plan = execute_sql._model_safety("SELECT UPPER(ssn) FROM customers", "acme", None)
    assert plan is None
    assert refusal is not None and refusal.kind == "sensitive_columns"


def test_model_safety_allows_a_clean_query_with_no_plan(monkeypatch):
    monkeypatch.setattr(execute_sql, "_resolve_guard_model", lambda profile: _org())
    sql, refusal, plan = execute_sql._model_safety("SELECT name FROM customers", "acme", None)
    assert refusal is None and plan is None


# ---------------------------------------------------------------------------
# The subprocess metadata channel: _emit_result_csv flags masked columns on stderr, and
# tools._executor_masked parses that line.
# ---------------------------------------------------------------------------


def test_emit_result_csv_flags_masked_columns_on_stderr(capsys):
    result = execute_sql.ExecResult(
        columns=["ssn"], rows=[("***",)], truncated=False, masked_columns=("customers.ssn",)
    )
    execute_sql._emit_result_csv(result)
    err = capsys.readouterr().err
    lines = [json.loads(x) for x in err.splitlines() if x.strip().startswith("{")]
    masked = [d["masked"] for d in lines if "masked" in d]
    assert masked == [["customers.ssn"]]


def test_tools_executor_masked_parses_the_stderr_marker():
    import tools

    stderr = 'some notice\n{"masked": ["customers.ssn", "customers.email"]}\n'
    assert tools._executor_masked(stderr) == ["customers.ssn", "customers.email"]
    assert tools._executor_masked("nothing here") == []


# ---------------------------------------------------------------------------
# tool_execute_sql (in-process, injected executor): masked VALUES in `data`, `applied[{mask}]` note.
# ---------------------------------------------------------------------------


def test_tool_execute_sql_masks_value_and_records_applied(monkeypatch):
    import tools

    monkeypatch.setattr(tools, "resolve_profile", lambda ds: "acme")
    monkeypatch.setattr(execute_sql, "_resolve_guard_model", lambda profile: _org())
    monkeypatch.setattr(execute_sql, "_load_credentials", lambda p: {"type": "sqlite", "path": ":memory:"})

    spy = _SpyExecutor(_R(["name", "ssn"], [("Alice", "111-22-3333")]))
    tools.set_injected_executor(spy)
    out = json.loads(tools.tool_execute_sql({"sql": "SELECT name, ssn FROM customers", "datasource": "acme"}))

    assert out["status"] == "ok"  # NOT refused — the query ran
    assert spy.calls  # the injected executor ran behind the guard
    assert out["data"]["rows"] == [["Alice", execute_sql.REDACTION_TOKEN]]  # ssn redacted, name intact
    assert {"mask": "customers.ssn"} in out["applied"]


def test_tool_execute_sql_untraceable_pii_is_refused_no_data(monkeypatch):
    import tools

    monkeypatch.setattr(tools, "resolve_profile", lambda ds: "acme")
    monkeypatch.setattr(execute_sql, "_resolve_guard_model", lambda profile: _org())
    monkeypatch.setattr(execute_sql, "_load_credentials", lambda p: {"type": "sqlite", "path": ":memory:"})

    spy = _SpyExecutor(_R(["s"], [("x",)]))
    tools.set_injected_executor(spy)
    out = json.loads(tools.tool_execute_sql({"sql": "SELECT UPPER(ssn) FROM customers", "datasource": "acme"}))

    assert out["status"] == "refused"
    assert out["refusal"]["kind"] == "sensitive_columns"
    assert "data" not in out
    assert spy.calls == []
