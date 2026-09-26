"""A client on an engine with known gaps is told what that engine rejects (#325), once (#405).

It was told the engine and still wrote PostgreSQL that Redshift rejects, and every such statement
cost a warehouse round trip and a retry. The rules rode on the schema response until #405: they
describe the ENGINE, so they did not vary with the scope being asked about, and the same ~1,750
chars were re-sent on every call of a multi-call question. `list_datasources` carries them now —
the call that answers "which datasource, on what engine", made once — and a schema response names
the engine and points back at it.
"""

from __future__ import annotations

import builtins
import json

import pytest

pytest.importorskip("pydantic")
pytest.importorskip("yaml")

import contracts  # noqa: E402
import sql_dialect_rules  # noqa: E402
import tools  # noqa: E402
import yaml  # noqa: E402
from semantic_model import build  # noqa: E402
from semantic_model.models import Datasource, StorageConnection, SubjectArea  # noqa: E402


def _serve(tmp_path, monkeypatch, storage_type: str, *, second_engine: str | None = None,
           dangling_ref: bool = False) -> None:
    for var in ("AGAMI_DB_URL", "APP_DATABASE_URL", "AGAMI_PROFILE", "AGAMI_ORG_ID"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(tmp_path))
    tools.resolved_org_id.cache_clear()
    # `list_datasources` enumerates credentials profiles on this path, not artifact directories, so
    # a model on disk with no profile beside it is listed nowhere. The schema tools don't need it.
    (tmp_path / "local").mkdir(parents=True, exist_ok=True)
    (tmp_path / "local" / "credentials").write_text("[crm]\nurl = postgresql://u:p@h/db\n")
    conns = [StorageConnection(name="warehouse", storage_type=storage_type)]
    if second_engine:
        conns.append(StorageConnection(name="lake", storage_type=second_engine))
    org = Datasource(
        datasource="crm",
        storage_connections=conns,
        subject_areas=[SubjectArea(name="Sales", description="Sales area")],
    )
    build.write_tree(org, tmp_path / "crm")
    if dangling_ref:
        # A second connection whose pointer resolves to nothing. Written after `write_tree` because
        # the model itself could not hold one — `storage_type` is required on a StorageConnection,
        # which is exactly why the empty case only arises on the raw on-disk form.
        doc = yaml.safe_load((tmp_path / "crm" / "datasource.yaml").read_text())
        doc["storage_connections"].append({"name": "missing", "ref": "datasources/gone/x.yaml"})
        (tmp_path / "crm" / "datasource.yaml").write_text(yaml.safe_dump(doc))
    tools.bootstrap_paths()


def _head(out: str) -> dict:
    head, _end = json.JSONDecoder().raw_decode(out)
    return head


def test_list_datasources_carries_the_rules(tmp_path, monkeypatch):
    _serve(tmp_path, monkeypatch, "Redshift")

    entry = json.loads(tools.tool_list_datasources({}))["datasources"][0]

    assert entry["engine"] == "Redshift"
    assert entry["dialect_rules"] == sql_dialect_rules.dialect_rules_for("Redshift")
    # The gaps observed in real failures, each with its rewrite.
    for construct in ("FILTER", "LISTAGG", "DATEADD", "SUBSTRING", "BOOLEAN"):
        assert construct in entry["dialect_rules"]


@pytest.mark.parametrize("args", [
    {},                              # datasource scope
    {"dataset_names": ["x"]},        # the call made immediately before writing SQL
])
def test_a_schema_response_points_at_them_instead_of_repeating_them(tmp_path, monkeypatch, args):
    """Both branches build their response separately, so both are checked. Neither carries the
    rules; each names the engine and says where they are, so a client that reached here without
    listing datasources knows to go back."""
    _serve(tmp_path, monkeypatch, "Redshift")

    head = _head(tools.tool_get_datasource_schema({"datasource": "crm", **args}))

    assert "dialect_rules" not in head, "the rules must not ride every schema response"
    assert head["dialect"] == {"engine": "Redshift", "rules_from": "list_datasources"}


def test_the_pointer_costs_a_fraction_of_the_rules(tmp_path, monkeypatch):
    """The point of the move. Asserted so a future change cannot quietly put them back."""
    _serve(tmp_path, monkeypatch, "Redshift")

    head = _head(tools.tool_get_datasource_schema({"datasource": "crm"}))

    assert len(json.dumps(head["dialect"])) * 10 < len(
        sql_dialect_rules.dialect_rules_for("Redshift"))


def test_an_engine_without_known_gaps_is_named_but_carries_no_rules(tmp_path, monkeypatch):
    """Asserted on the LISTING, which is where the rules live now. On the schema response the
    claim would be vacuous — no engine carries them there any more, so it would pass whatever
    engine the model declared."""
    _serve(tmp_path, monkeypatch, "PostgreSQL")

    entry = json.loads(tools.tool_list_datasources({}))["datasources"][0]

    assert entry["engine"] == "PostgreSQL"
    assert "dialect_rules" not in entry
    assert _head(tools.tool_get_datasource_schema({"datasource": "crm"}))["dialect"] == {
        "engine": "PostgreSQL", "rules_from": "list_datasources"}


def test_a_model_that_names_two_engines_claims_neither(tmp_path, monkeypatch):
    """"Which dialect" has no single answer then, and a guess sends the client to write SQL in the
    wrong one. Asserted because both resolvers state this as their safety property, and a version
    that picked one arbitrarily passed the whole suite."""
    _serve(tmp_path, monkeypatch, "Redshift", second_engine="Snowflake")

    entry = json.loads(tools.tool_list_datasources({}))["datasources"][0]

    assert "engine" not in entry and "dialect_rules" not in entry
    assert "dialect" not in _head(tools.tool_get_datasource_schema({"datasource": "crm"}))


def test_a_connection_that_declares_nothing_does_not_hide_the_engine(tmp_path, monkeypatch):
    """A pointer that does not resolve is a connection we know nothing about, not a second opinion
    — treating it as one would drop an engine the model does state."""
    _serve(tmp_path, monkeypatch, "Redshift", dangling_ref=True)

    entry = json.loads(tools.tool_list_datasources({}))["datasources"][0]

    assert entry["engine"] == "Redshift"


@pytest.mark.parametrize("content", [
    "just a string, not a mapping\n",   # parses fine, and then has no `.get`
    "storage_connections: [\n",         # does not parse at all — yaml.YAMLError, NOT a ValueError
    "\xff\xfe not utf-8",               # unreadable bytes
])
def test_a_malformed_model_costs_its_own_engine_not_the_whole_listing(
        tmp_path, monkeypatch, content):
    """This path never parsed these files before #405, so it could not break on them. A listing
    that dies on one unparseable model is a worse regression than the saving is a win — and the
    three ways it can be unparseable raise three unrelated exception types."""
    _serve(tmp_path, monkeypatch, "Redshift")
    (tmp_path / "crm" / "datasource.yaml").write_text(content, errors="surrogateescape")

    entry = json.loads(tools.tool_list_datasources({}))["datasources"][0]

    assert entry["datasource"] == "crm"
    assert "engine" not in entry and "dialect_rules" not in entry


def test_without_the_yaml_extra_the_listing_loses_the_engine_not_the_tool(tmp_path, monkeypatch):
    """The base install declares no dependencies; YAML comes with `[model]`. `list_datasources` is
    how an operator finds out what a deployment has, so it has to answer on a bare install."""
    _serve(tmp_path, monkeypatch, "Redshift")
    real_import = builtins.__import__

    def no_yaml(name, *a, **kw):
        if name == "yaml":
            raise ImportError("No module named 'yaml'")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", no_yaml)
    entry = json.loads(tools.tool_list_datasources({}))["datasources"][0]

    assert entry["datasource"] == "crm" and entry["database_type"] == "postgres"
    assert "engine" not in entry and "dialect_rules" not in entry


def test_unknown_or_missing_engines_have_no_rules():
    assert sql_dialect_rules.dialect_rules_for(None) is None
    assert sql_dialect_rules.dialect_rules_for("NotAnEngine") is None


def test_the_fields_are_declared_where_they_are_now_sent():
    """The contract has to move with the payload. Asserting `dialect_rules` on the schema result
    was how this test kept passing after the field stopped being sent from there — a consumer
    building against `contracts` would have been told the opposite of what ships."""
    assert "dialect_rules" not in contracts.DatasourceSchemaResult.model_fields
    assert "dialect" in contracts.DatasourceSchemaResult.model_fields
    for field in ("engine", "dialect_rules"):
        assert field in contracts.DatasourceInfo.model_fields
    # And the client is pointed at the call that carries them.
    assert "dialect_rules" in tools.TOOLS["list_datasources"]["description"]
    assert "dialect_rules" in tools._SHARED_INSTRUCTIONS
    assert "list_datasources" in tools.TOOLS["get_datasource_schema"]["description"]


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
