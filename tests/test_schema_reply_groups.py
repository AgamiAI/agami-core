"""A scoped schema reply sends a wide table's groups, and merges repeated joins (#449).

Two changes to a `dataset_names` reply:

- **Columns.** For a table with column groups, the reply lists every group (description and column
  names) and sends full detail only for the columns any query needs: the grain, columns a metric in
  the reply binds, and columns with a caveat, a value transform or a sensitive flag.
  `column_groups=[...]` brings whole groups in full.
- **Joins.** Lean edges (to a table the caller didn't request) that join the same two tables the
  same way become one entry listing every column pair.

What must hold either way: no column and no join disappears from the reply.

Synthetic, generic names only.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytest.importorskip("pydantic")

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "packages" / "agami-core" / "src"))

import tools  # noqa: E402
from semantic_model import loader as L  # noqa: E402
from semantic_model import models as m  # noqa: E402

NAMES = [f"c{i:02d}" for i in range(30)]


def _org():
    cols = [m.Column(name="id", type="integer")]
    cols += [m.Column(name=n, type="string") for n in NAMES]
    cols[1] = m.Column(name="c00", type="string", caveats=["read this first"])
    cols[2] = m.Column(name="c01", type="string", sensitive=True)
    wide = m.Table(
        name="wide",
        schema="public",
        storage_connection="c",
        grain=["id"],
        columns=cols,
        column_groups={
            "keys": ["id"],
            "first": NAMES[:10],
            "second": NAMES[10:20],
            "third": NAMES[20:],
        },
        column_group_descriptions={
            "keys": "the key",
            "first": "first ten",
            "second": "second ten",
            "third": "last ten",
        },
    )
    narrow = m.Table(
        name="narrow",
        schema="public",
        storage_connection="c",
        grain=["id"],
        columns=[m.Column(name="id", type="integer"), m.Column(name="label", type="string")],
    )
    return m.Datasource(
        datasource="shop", subject_areas=[m.SubjectArea(name="s", tables_defined=[wide, narrow])]
    )


def _result(metric_binding: str = ""):
    """The shape `_tool_get_datasource_schema` has built before grouping."""

    def full(t):
        return [
            c.model_dump(exclude_none=True, exclude_defaults=True) | {"name": c.name}
            for c in t.columns
        ]

    org = _org()
    wide, narrow = org.subject_areas[0].tables_defined
    return org, {
        "tables": {
            "wide": {"name": "wide", "columns": full(wide)},
            "narrow": {"name": "narrow", "columns": full(narrow)},
        },
        "metrics": [{"name": "m", "binding": metric_binding}] if metric_binding else [],
    }


def _full_names(result, table="wide"):
    return {c["name"] for c in result["tables"][table]["columns"]}


def test_a_wide_table_sends_its_groups_and_only_the_columns_every_query_needs():
    org, result = _result(metric_binding="SUM(wide.c15)")
    tools._group_wide_columns(result, org, [])
    groups = result["tables"]["wide"]["column_groups"]
    assert groups["first"] == {"description": "first ten", "columns": NAMES[:10]}
    # Every column is still named somewhere in the reply.
    assert {c for g in groups.values() for c in g["columns"]} == {"id", *NAMES}
    # Grain, caveat, sensitive and metric-bound columns, in full; nothing else.
    assert _full_names(result) == {"id", "c00", "c01", "c15"}


@pytest.mark.parametrize("asked", [["second"], ["wide.second"]])
def test_a_named_group_comes_back_in_full(asked):
    org, result = _result()
    tools._group_wide_columns(result, org, asked)
    assert set(NAMES[10:20]) <= _full_names(result)
    assert "c25" not in _full_names(result)
    assert "unknown_column_groups" not in result


def test_an_unknown_group_is_reported_not_ignored():
    org, result = _result()
    tools._group_wide_columns(result, org, ["nope", "wide.first", "other.first"])
    assert result["unknown_column_groups"] == ["nope", "other.first"]


def test_a_table_without_groups_is_unchanged():
    org, result = _result()
    before = list(result["tables"]["narrow"]["columns"])
    tools._group_wide_columns(result, org, [])
    assert result["tables"]["narrow"]["columns"] == before
    assert "column_groups" not in result["tables"]["narrow"]


def _lean(frm, to, fc, tc, **extra):
    return {
        "from_table": frm,
        "to_table": to,
        "from_column": fc,
        "to_column": tc,
        "join_type": "LEFT",
        "relationship": "many_to_one",
        **extra,
    }


def test_lean_edges_between_the_same_two_tables_become_one_entry_with_every_join():
    edges = [
        _lean("facts", "dates", "submitted_key", "date_key"),
        _lean("facts", "dates", "approved_key", "date_key"),
        _lean("facts", "people", "owner_key", "person_key"),
    ]
    merged = L._merge_lean_pairs(edges)
    assert len(merged) == 2
    dates = next(e for e in merged if e["to_table"] == "dates")
    assert dates["column_pairs"] == [["submitted_key", "date_key"], ["approved_key", "date_key"]]
    # A pair with one join keeps the ordinary shape.
    people = next(e for e in merged if e["to_table"] == "people")
    assert people["from_column"] == "owner_key" and "column_pairs" not in people


def test_full_edges_and_special_joins_are_never_merged():
    edges = [
        _lean("facts", "dates", "a", "date_key", review_state="approved"),
        _lean("facts", "dates", "b", "date_key", review_state="approved"),
        _lean("facts", "dims", "c", "id", on="facts.c = dims.id AND dims.current = 'Y'"),
        _lean("facts", "dims", "d", "id", on="facts.d = dims.id AND dims.current = 'Y'"),
        _lean("facts", "far", "e", "id", executable="split"),
        _lean("facts", "far", "f", "id", executable="split"),
    ]
    assert L._merge_lean_pairs(edges) == edges


# --- found in review --------------------------------------------------------------------------


def test_a_lone_group_name_is_treated_as_a_list():
    args, err = tools._normalized_args({"dataset_names": ["wide"], "column_groups": "first"})
    assert err is None and args["column_groups"] == ["first"]


def test_a_non_string_group_request_is_ignored_not_a_crash():
    org, result = _result()
    tools._group_wide_columns(result, org, [1, None, "first"])
    assert set(NAMES[:10]) <= _full_names(result)


def test_a_column_in_no_group_is_sent_in_full():
    org, result = _result()
    wide = org.subject_areas[0].tables_defined[0]
    wide.column_groups = {k: v for k, v in wide.column_groups.items() if k != "third"}
    tools._group_wide_columns(result, org, [])
    assert set(NAMES[20:]) <= _full_names(result)


def test_a_column_with_a_date_encoding_is_sent_in_full():
    org, result = _result()
    result["tables"]["wide"]["columns"][20]["date_format"] = "epoch_s"
    tools._group_wide_columns(result, org, [])
    assert NAMES[19] in _full_names(result)


def test_merging_keeps_joins_that_differ_only_in_join_type():
    """The reply dedupes raw edges before merging, so two merged entries between the same tables
    can't collide and drop one."""
    facts = m.Table(
        name="facts",
        schema="public",
        storage_connection="c",
        grain=["id"],
        columns=[m.Column(name=n, type="integer") for n in ["id", "a", "b", "c", "d"]],
    )
    dates = m.Table(
        name="dates",
        schema="public",
        storage_connection="c",
        grain=["k"],
        columns=[m.Column(name="k", type="integer")],
    )
    rels = [
        m.Relationship(
            from_table="facts",
            to_table="dates",
            from_column=c,
            to_column="k",
            relationship="many_to_one",
            join_type=j,
        )
        for c, j in [("a", "LEFT"), ("b", "LEFT"), ("c", "INNER"), ("d", "INNER")]
    ]
    org = m.Datasource(
        datasource="shop",
        subject_areas=[m.SubjectArea(name="s", tables_defined=[facts, dates], relationships=rels)],
    )
    ctx = tools._table_contexts(org, ["facts"], L, index=L.build_table_index(org))
    pairs = [
        p
        for r in ctx["relationships"]
        for p in (r.get("column_pairs") or [[r["from_column"], r["to_column"]]])
    ]
    assert sorted(pairs) == [["a", "k"], ["b", "k"], ["c", "k"], ["d", "k"]]


@pytest.mark.parametrize("binding", ['SUM(wide."C15")', "SUM(WIDE.C15)", "sum(c15)"])
def test_a_metric_column_is_kept_however_the_binding_spells_it(binding):
    org, result = _result(metric_binding=binding)
    tools._group_wide_columns(result, org, [])
    assert "c15" in _full_names(result)
