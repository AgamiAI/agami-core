"""Below the overview, a schema reply's domain context is cut to the tables in scope (#419).

The domain context used to ride every `get_datasource_schema` reply whole — on a wide model a third
of everything an agent received, re-sent on each of a question's several schema calls. The overview
still carries all of it. An `area` or `dataset_names` reply keeps the narrative (`datasource.md`),
whose rules change answers and which a conversation that skips the overview would otherwise never
see, and keeps only the glossary entries that name a table in scope. These tests pin both halves:
what a scoped reply drops, and what it must never drop.
"""

from __future__ import annotations

import json

import pytest

pytest.importorskip("pydantic")
yaml = pytest.importorskip("yaml")

import tools  # noqa: E402
from semantic_model import org_draft  # noqa: E402
from semantic_model.loader import load_datasource  # noqa: E402

NARRATIVE_RULE = "Always exclude test orders with orders.is_test = false."
GLOSSARY = {
    "Net revenue": "orders.amount minus any refunds",
    "Backlog": "tickets whose state is not closed (tickets.state)",
    # Names `orders_archive`, which must not be read as naming `orders`.
    "Archived order": "a row in orders_archive, never in the live table",
    # Ends in `tickets`, which must not be read as naming `tickets` either.
    "Escalation": "a row copied into escalated_tickets",
    "Fiscal year": "starts on 1 April",
}


def _table(name: str, cols: list[dict]) -> dict:
    return {"name": name, "schema": "public", "storage_connection": "w", "grain": ["id"],
            "description": f"one row per {name}",
            "columns": [{"name": "id", "type": "integer", "primary_key": True}, *cols]}


def _area(root, name: str, tables: list[dict]) -> None:
    d = root / "subject_areas" / name
    (d / "tables").mkdir(parents=True)
    (d / "subject_area.yaml").write_text(yaml.safe_dump({
        "name": name, "description": f"{name} things",
        "tables": [{"storage_connection": "w", "schema": "public", "table": t["name"]}
                   for t in tables]}))
    for t in tables:
        (d / "tables" / f"{t['name']}.yaml").write_text(yaml.safe_dump(t))


def _model(root) -> None:
    (root / "datasources" / "w").mkdir(parents=True)
    (root / "datasource.yaml").write_text(yaml.safe_dump({
        "datasource": "crm", "version": 1,
        "storage_connections": [{"name": "w", "ref": "datasources/w/storage.yaml"}],
        "subject_areas": ["subject_areas/sales", "subject_areas/support"],
        "key_terminology": GLOSSARY}))
    (root / "datasources" / "w" / "storage.yaml").write_text(
        yaml.safe_dump({"name": "w", "storage_type": "PostgreSQL"}))
    (root / "datasource.md").write_text(f"# About\n\n{NARRATIVE_RULE}\n")
    _area(root, "sales", [
        _table("orders", [{"name": "amount", "type": "decimal"},
                          {"name": "status", "type": "string",
                           "choice_field": {"P": "paid", "R": "refunded"}}]),
        _table("orders_archive", [{"name": "amount", "type": "decimal"}]),
    ])
    _area(root, "support", [
        _table("tickets", [{"name": "state", "type": "string",
                            "choice_field": {"O": "open", "C": "closed"}}]),
    ])


# --- the glossary filter itself ---------------------------------------------------------------


def test_no_tables_means_the_whole_glossary_unchanged(tmp_path):
    _model(tmp_path)
    org = load_datasource(tmp_path)
    assert org_draft.derived_context(org, glossary_tables=None) == org_draft.derived_context(org)


def test_a_term_is_kept_only_when_it_names_a_table_in_scope(tmp_path):
    _model(tmp_path)
    org = load_datasource(tmp_path)
    text = org_draft.derived_context(org, glossary_tables=frozenset({"orders"}))
    assert "**Net revenue**" in text  # names `orders`
    assert "**Archived order**" not in text  # names `orders_archive`, a different table
    assert "**Backlog**" not in text and "**Fiscal year**" not in text
    assert "4 more terms name none of the tables in scope." in text
    # Legends follow the same scope: this table's own coded columns, nobody else's.
    assert "**orders.status**" in text and "**tickets.state**" not in text


def test_the_match_ignores_case_but_not_word_boundaries(tmp_path):
    _model(tmp_path)
    org = load_datasource(tmp_path)
    text = org_draft.derived_context(org, glossary_tables=frozenset({"TICKETS"}))
    assert "**Backlog**" in text
    assert "**Escalation**" not in text  # `escalated_tickets` is another table


def test_nothing_in_scope_still_says_what_was_left_out(tmp_path):
    """An empty selection must not read as "this model has no glossary"."""
    _model(tmp_path)
    org = load_datasource(tmp_path)
    text = org_draft.derived_context(org, glossary_tables=frozenset())
    assert "### Key terminology" in text
    assert "5 more terms name none of the tables in scope." in text


def test_the_filter_threads_through_both_composition_paths(tmp_path):
    """The two-level path and the no-record fallback must both apply it."""
    _model(tmp_path)
    org = load_datasource(tmp_path)
    scoped = frozenset({"tickets"})
    for text in (org_draft.compose_context("", org, glossary_tables=scoped),
                 org_draft.compose_org_context(None, [org], glossary_tables=scoped)):
        assert "**Backlog**" in text and "**Net revenue**" not in text


# --- the schema reply ---------------------------------------------------------------------------


@pytest.fixture
def served(tmp_path, monkeypatch):
    for var in ("AGAMI_DB_URL", "APP_DATABASE_URL", "AGAMI_PROFILE", "AGAMI_ORG_ID"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(tmp_path))
    tools.resolved_org_id.cache_clear()
    (tmp_path / "local").mkdir()
    (tmp_path / "local" / "credentials").write_text("[crm]\nurl = postgresql://u:p@h/db\n")
    _model(tmp_path / "crm")
    tools.bootstrap_paths()

    def reply(**args) -> str:
        out = tools.tool_get_datasource_schema({"datasource": "crm", **args})
        _head, end = json.JSONDecoder().raw_decode(out)
        return out[end:]

    return reply


def test_the_overview_carries_everything(served):
    context = served()
    assert NARRATIVE_RULE in context
    assert all(f"**{term}**" in context for term in GLOSSARY)
    assert "more terms name none" not in context
    assert "called without `area`" not in context  # no pointer to itself


@pytest.mark.parametrize("args,kept,dropped", [
    ({"dataset_names": ["tickets"]}, ["Backlog"],
     ["Net revenue", "Archived order", "Escalation", "Fiscal year"]),
    # An area means every table in it: both `orders` and `orders_archive`.
    ({"area": "sales"}, ["Net revenue", "Archived order"], ["Backlog", "Escalation", "Fiscal year"]),
])
def test_a_scoped_reply_keeps_the_rules_and_its_own_terms(served, args, kept, dropped):
    context = served(**args)
    # The narrative is never cut: a conversation can reach SQL without the overview.
    assert NARRATIVE_RULE in context
    assert all(f"**{t}**" in context for t in kept)
    assert not any(f"**{t}**" in context for t in dropped)
    assert "### Subject areas" not in context
    assert "called without `area` or `dataset_names`" in context
