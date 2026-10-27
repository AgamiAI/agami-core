"""#518: value_transform and relationship `on:` parse in the datasource's own SQL dialect."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytest.importorskip("pydantic")
pytest.importorskip("sqlglot")

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agami" / "scripts"))

from semantic_model import models as m  # noqa: E402
from semantic_model import validator as v  # noqa: E402

PG_REGEX = "CASE WHEN code ~ '^[0-9]+$' THEN code::int END"


def _org(storage_type, *, transform=None, on=None, connections=None):
    cols = [m.Column(name="id", type="integer"), m.Column(name="code", type="string"),
            m.Column(name="profile", type="json", value_transform=transform)]
    t = m.Table(name="t", schema="s", storage_connection="c", grain=["id"], description="d",
                columns=cols)
    u = m.Table(name="u", schema="s", storage_connection="c", grain=["id"], description="d",
                columns=[m.Column(name="id", type="integer"), m.Column(name="code", type="string")])
    rels = []
    if on:
        rels = [m.Relationship(from_table="t", to_table="u", on=on, relationship="many_to_one",
                               confidence="inferred")]
    refs = [m.TableRef(storage_connection="c", schema="s", table=n) for n in ("t", "u")]
    area = m.SubjectArea(name="a", tables=refs, tables_defined=[t, u], relationships=rels)
    conns = connections or [m.StorageConnection(name="c", storage_type=storage_type)]
    return m.Datasource(datasource="O", storage_connections=conns, subject_areas=[area])


def _codes(res):
    return {f.code for f in res.findings}


def test_postgres_regex_transform_validates_in_postgres():
    assert "value_transform_unparseable" not in _codes(v.validate(_org("PostgreSQL", transform=PG_REGEX)))


def test_postgres_regex_transform_is_still_refused_on_sqlite():
    assert "value_transform_unparseable" in _codes(v.validate(_org("SQLite", transform=PG_REGEX)))


@pytest.mark.parametrize("storage_type, transform", [
    ("PostgreSQL", "(profile->>'signup_date')::date"),
    ("SQLite", "date(json_extract(profile, '$.signup_date'))"),
    ("DuckDB", "CAST(json_extract_string(profile, '$.signup_date') AS DATE)"),
])
def test_a_transform_reading_a_json_key_validates(storage_type, transform):
    assert "value_transform_unparseable" not in _codes(v.validate(_org(storage_type, transform=transform)))


def test_relationship_on_parses_in_the_dialect():
    on = "t.code ~ '^[0-9]+$' AND t.code::int = u.id"
    assert "relationship_on_unparseable" not in _codes(v.validate(_org("PostgreSQL", on=on)))
    assert "relationship_on_unparseable" in _codes(v.validate(_org("SQLite", on=on)))


def test_unresolved_dialect_falls_back_to_the_generic_grammar():
    two = [m.StorageConnection(name="c", storage_type="PostgreSQL"),
           m.StorageConnection(name="d", storage_type="MySQL")]
    res = v.validate(_org(None, transform=PG_REGEX, connections=two))
    assert "value_transform_unparseable" in _codes(res)
    res = v.validate(_org(None, transform="lower(code)", connections=two))
    assert "value_transform_unparseable" not in _codes(res)


def test_cache_does_not_reuse_a_verdict_across_dialects():
    cache: dict = {}
    assert "value_transform_unparseable" not in _codes(
        v.validate(_org("PostgreSQL", transform=PG_REGEX), cache=cache))
    assert "value_transform_unparseable" in _codes(
        v.validate(_org("SQLite", transform=PG_REGEX), cache=cache))
