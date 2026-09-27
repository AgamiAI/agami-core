"""#386 — a column-scope refusal names what the tables it read DO declare.

The refusal said THAT a column was not declared and not what the agent could use, so the agent
repaired by guessing again: refused, guessed, refused. Its `remediation` now lists the declared
columns of the tables the refused statement itself reads, capped, with the closest declared name for
each refused column — the one recorded exception to the echo-only rule (see
`test_ace035_no_enumeration.py` and SECURITY.md). `detail` is unchanged: still the caller's own
names, bounded.

Synthetic throughout — agami-core is public.
"""

from __future__ import annotations

import pytest

pytest.importorskip("pydantic")
pytest.importorskip("sqlglot")

import guardrail  # noqa: E402
from semantic_model import models as m  # noqa: E402
from semantic_model import runtime as rt  # noqa: E402


def _t(name: str, cols: list[str]) -> m.Table:
    return m.Table(
        name=name,
        schema="public",
        storage_connection="c",
        grain=["id"],
        columns=[m.Column(name=c, type="string") for c in cols],
    )


def _org(*tables: m.Table) -> m.Datasource:
    return m.Datasource(
        datasource="Shop", subject_areas=[m.SubjectArea(name="sales", tables_defined=list(tables))]
    )


ORDERS = _t("orders", ["id", "created_at", "amount", "customer_id"])
# A table the statements below never read. Nothing of it may appear in a refusal.
LEDGER = _t("ledger_archive", ["id", "entry_code"])


def _remediation(sql: str, org: m.Datasource) -> str:
    refusal = rt.check_column_scope(sql, org)
    assert refusal is not None and refusal.rule == guardrail.RULE_COLUMN_SCOPE
    return refusal.remediation


def test_the_refusal_lists_the_read_tables_declared_columns():
    """The incident's own shape: every other column declared, one plausible name invented."""
    text = _remediation("SELECT id, created FROM orders", _org(ORDERS, LEDGER))
    assert "orders: id, created_at, amount, customer_id" in text


def test_it_lists_no_table_the_statement_does_not_read():
    text = _remediation("SELECT id, created FROM orders", _org(ORDERS, LEDGER))
    assert "ledger_archive" not in text and "entry_code" not in text


def test_a_near_miss_is_named_as_a_typo_hint_never_an_instruction():
    """`created` is one suffix from `created_at`. A typo and a column excluded on purpose look
    identical at the gate, so the hint says to use it only if it is the same column."""
    text = _remediation("SELECT id, created FROM orders", _org(ORDERS))
    assert "created → created_at" in text
    assert "only if it is the same column" in text


def test_a_column_left_out_of_the_model_is_not_listed():
    """The modelling rule: a column that must not be readable is not declared. It is absent from
    the list with nothing special done — asserted, since a later change could break it easily."""
    table = _t("customers", ["id", "region"])  # the warehouse also has `ssn`; the model does not
    text = _remediation("SELECT id, ssn FROM customers", _org(table))
    assert "customers: id, region." in text
    assert "ssn →" not in text


def test_an_unqualified_column_lists_every_table_its_select_reads():
    customers = _t("customers", ["id", "region"])
    text = _remediation(
        "SELECT o.id FROM orders o JOIN customers c ON o.customer_id = c.id WHERE regoin = 'EU'",
        _org(ORDERS, customers, LEDGER),
    )
    assert "customers: id, region" in text and "orders: id" in text
    assert "regoin → region" in text
    assert "ledger_archive" not in text


def test_the_columns_per_table_are_capped_and_the_rest_counted():
    wide = _t("wide", [f"col_{i:03d}" for i in range(rt._LIST_MAX_COLUMNS + 7)])
    text = _remediation("SELECT nope FROM wide", _org(wide))
    assert "and 7 more" in text
    assert f"col_{rt._LIST_MAX_COLUMNS:03d}" not in text


def test_the_tables_are_capped_and_the_rest_counted():
    """The statement is the caller's: one naming every declared table must not turn one refusal into
    the whole model."""
    tables = [_t(f"t{i}", ["id"]) for i in range(rt._LIST_MAX_TABLES + 2)]
    joins = " ".join(f"JOIN t{i} ON t{i}.id = t0.id" for i in range(1, len(tables)))
    text = _remediation(f"SELECT nope FROM t0 {joins}", _org(*tables))
    assert "and 2 more table(s)" in text


def test_the_detail_is_still_the_callers_own_names_only():
    """Only `remediation` changed. `detail` stays the bounded echo, byte for byte."""
    refusal = rt.check_column_scope("SELECT id, created FROM orders", _org(ORDERS, LEDGER))
    assert refusal.detail == (
        "query references column(s) not in the semantic model: created — "
        "only columns declared on the model's tables may be queried."
    )
