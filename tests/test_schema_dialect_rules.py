"""A schema response on an engine with known gaps carries what that engine rejects (#325).

The client was told the engine and still wrote PostgreSQL that Redshift rejects, and every such
statement cost a warehouse round trip and a retry. The rules ride on the schema response, the call
made before any SQL is written, so the statement is right the first time.
"""

from __future__ import annotations

import json

import pytest

pytest.importorskip("pydantic")
pytest.importorskip("yaml")

import contracts  # noqa: E402
import sql_dialect_rules  # noqa: E402
import tools  # noqa: E402
from semantic_model import build  # noqa: E402
from semantic_model.models import Datasource, StorageConnection, SubjectArea  # noqa: E402


def _serve(tmp_path, monkeypatch, storage_type: str) -> None:
    for var in ("AGAMI_DB_URL", "APP_DATABASE_URL", "AGAMI_PROFILE", "AGAMI_ORG_ID"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(tmp_path))
    tools.resolved_org_id.cache_clear()
    org = Datasource(
        datasource="crm",
        storage_connections=[StorageConnection(name="warehouse", storage_type=storage_type)],
        subject_areas=[SubjectArea(name="Sales", description="Sales area")],
    )
    build.write_tree(org, tmp_path / "crm")
    tools.bootstrap_paths()


def _head(out: str) -> dict:
    head, _end = json.JSONDecoder().raw_decode(out)
    return head


def test_a_redshift_schema_carries_the_rules(tmp_path, monkeypatch):
    _serve(tmp_path, monkeypatch, "Redshift")

    head = _head(tools.tool_get_datasource_schema({"datasource": "crm"}))

    assert head["dialect_rules"] == sql_dialect_rules.dialect_rules_for("Redshift")
    # The gaps observed in real failures, each with its rewrite.
    for construct in ("FILTER", "LISTAGG", "DATEADD", "SUBSTRING", "BOOLEAN"):
        assert construct in head["dialect_rules"]


def test_a_table_scoped_call_carries_them_too(tmp_path, monkeypatch):
    """The call made immediately before writing SQL is the table-scoped one; it builds its response
    on a separate branch, so it is checked separately."""
    _serve(tmp_path, monkeypatch, "Redshift")

    head = _head(tools.tool_get_datasource_schema({"datasource": "crm", "dataset_names": ["x"]}))

    assert head["dialect_rules"]


def test_an_engine_without_known_gaps_gets_nothing(tmp_path, monkeypatch):
    _serve(tmp_path, monkeypatch, "PostgreSQL")

    head = _head(tools.tool_get_datasource_schema({"datasource": "crm"}))

    assert "dialect_rules" not in head


def test_unknown_or_missing_engines_have_no_rules():
    assert sql_dialect_rules.dialect_rules_for(None) is None
    assert sql_dialect_rules.dialect_rules_for("NotAnEngine") is None


def test_the_field_is_declared_and_the_client_is_told_to_follow_it():
    assert "dialect_rules" in contracts.DatasourceSchemaResult.model_fields
    assert "dialect_rules" in tools.TOOLS["get_datasource_schema"]["description"]
    assert "dialect_rules" in tools._SHARED_INSTRUCTIONS


def test_rewrites_keep_the_result_not_just_avoid_the_error():
    """A rewrite that trades an error for a quietly different answer is worse than the error: an
    empty conditional count must stay 0, a NULL boolean must stay NULL, and an array must not
    silently become text."""
    rules = sql_dialect_rules.dialect_rules_for("Redshift")
    assert "COUNT(CASE WHEN c THEN 1 END)" in rules  # SUM(CASE ...) is NULL on an empty input
    assert "WHEN NOT col THEN 'false' END" in rules  # an ELSE 'false' would turn NULL into 'false'
    assert "No STRING_AGG. Use LISTAGG" in rules
    assert "ARRAY_AGG. Use LISTAGG" not in rules


def test_rules_cover_reads_only():
    """The guard admits only SELECT, so a write-path rule is tokens spent on the impossible."""
    rules = sql_dialect_rules.dialect_rules_for("Redshift")
    for write_only in ("INSERT", "UPDATE", "RETURNING"):
        assert write_only not in rules
