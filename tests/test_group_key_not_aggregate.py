"""A grouping label is never reported as a figure, whatever a step did to it.

A step that collapses duplicates (`MAX(priority)` per `sys_id`) makes the column it passes on look
like an aggregate when followed through steps (ACE-178). When the final SELECT then GROUPS BY that
column, it is the answer's label, not a number, and its output item must say `aggregate: false` so
a consumer says nothing about it. The aggregates in the same statement keep their verdicts.
"""

from __future__ import annotations

import pytest

pytest.importorskip("pydantic")
pytest.importorskip("sqlglot")
pytest.importorskip("yaml")

from test_ace178_metrics_traced_through_steps import _org, _outputs  # noqa: E402

METRICS = {"incident_count": ("COUNT(DISTINCT number)", ["incidents"], {})}

STEP = ("WITH t AS (SELECT number, MAX(state) AS state, MAX(team) AS team, MIN(opened) AS opened "
        "FROM incidents GROUP BY number), "
        "a AS (SELECT number, state, team, opened FROM t) ")


@pytest.fixture()
def org(tmp_path):
    return _org(tmp_path, metrics=METRICS)


def _items(org, sql):
    return {i["column"]: i for i in _outputs(org, sql)}


@pytest.mark.parametrize("sql", [
    STEP + "SELECT COALESCE(NULLIF(TRIM(state), ''), '(none)') AS state, "
           "COUNT(DISTINCT number) AS incidents, MAX(opened) AS latest FROM a GROUP BY 1 ORDER BY 1",
    STEP + "SELECT state, COUNT(DISTINCT number) AS incidents, MAX(opened) AS latest "
           "FROM a GROUP BY state",
    STEP + "SELECT a.state AS state, COUNT(DISTINCT number) AS incidents, MAX(opened) AS latest "
           "FROM a GROUP BY a.state",
    STEP + "SELECT UPPER(state) AS state, COUNT(DISTINCT number) AS incidents, "
           "MAX(opened) AS latest FROM a GROUP BY UPPER(state)",
], ids=["ordinal", "alias", "qualified-column", "expression"])
def test_a_step_aggregated_group_key_is_not_a_figure(org, sql):
    items = _items(org, sql)
    assert items["state"]["aggregate"] is False
    # The real figures beside it are untouched.
    assert items["incidents"]["aggregate"] is True
    assert items["latest"]["aggregate"] is True


def test_statuses_are_unchanged_by_the_label_rule(org):
    """Only the `aggregate` flag moves; every status is what it was."""
    sql = (STEP + "SELECT state, COUNT(DISTINCT number) AS incidents, MAX(opened) AS latest "
           "FROM a GROUP BY state")
    items = _items(org, sql)
    # As computed before the rule existed: the label's own status stays, it is only not a figure.
    assert {c: items[c]["status"] for c in items} == {
        "state": "unmatched", "incidents": "undetermined", "latest": "undetermined"}


def test_a_plain_grouped_dimension_stays_not_aggregate(org):
    items = _items(org, "SELECT team, COUNT(DISTINCT number) AS incidents FROM incidents "
                        "GROUP BY team")
    assert items["team"]["aggregate"] is False
    assert (items["incidents"]["aggregate"], items["incidents"]["status"],
            items["incidents"]["name"]) == (True, "matched", "incident_count")


@pytest.mark.parametrize("sql", [
    "SELECT team, COUNT(DISTINCT number) AS incidents FROM incidents GROUP BY 1",
    "SELECT team, SUM(1) AS incidents FROM incidents GROUP BY team",
])
def test_the_aggregate_of_a_grouped_select_stays_a_figure(org, sql):
    items = _items(org, sql)
    assert (items["team"]["aggregate"], items["incidents"]["aggregate"]) == (False, True)


def test_a_step_figure_passed_on_ungrouped_stays_a_figure(org):
    """A step's count projected plainly (not grouped) is still a figure."""
    sql = ("WITH x AS (SELECT team, COUNT(DISTINCT number) AS n FROM incidents GROUP BY team) "
           "SELECT team, n FROM x")
    items = _items(org, sql)
    assert items["n"]["aggregate"] is True


DEDUP = ("WITH x AS (SELECT team, MAX(state) AS state, COUNT(DISTINCT number) AS n, "
         "SUM(1) AS rows_seen FROM incidents GROUP BY team) ")


@pytest.mark.parametrize("sql", [
    DEDUP + "SELECT team, state, n, rows_seen FROM x GROUP BY 1, 2, 3, 4",
    DEDUP + "SELECT team, state, n, rows_seen FROM x GROUP BY team, state, n, rows_seen",
], ids=["ordinal", "names"])
def test_grouping_on_every_column_to_deduplicate_keeps_the_figures(org, sql):
    """Grouping on every column only removes duplicate rows: a step's count, matched or not, is
    still a figure; only a step's MAX/MIN collapse grouped on is the answer's label."""
    items = _items(org, sql)
    assert (items["n"]["aggregate"], items["n"]["status"]) == (True, "matched")
    assert items["rows_seen"]["aggregate"] is True
    assert items["state"]["aggregate"] is False
    assert items["team"]["aggregate"] is False



TWO = ("WITH s AS (SELECT team, MAX(opened) AS total FROM incidents GROUP BY team), "
       "o AS (SELECT team, MAX(state) AS total FROM incidents GROUP BY team) ")


@pytest.mark.parametrize("sql", [
    TWO + "SELECT s.total FROM s JOIN o ON s.team = o.team GROUP BY o.total",
    TWO + "SELECT s.total FROM s JOIN o ON s.team = o.team GROUP BY total",
], ids=["other-prefix", "bare-name-over-a-join"])
def test_a_grouping_key_of_another_table_is_not_this_columns_label(org, sql):
    """A bare name matches a grouping key only when the tables agree, or the SELECT reads one."""
    assert _items(org, sql)["total"]["aggregate"] is True


def test_the_same_prefix_or_one_source_still_matches(org):
    assert _items(org, TWO + "SELECT s.total FROM s JOIN o ON s.team = o.team "
                             "GROUP BY s.total")["total"]["aggregate"] is False
    assert _items(org, TWO + "SELECT s.total FROM s GROUP BY total")["total"]["aggregate"] is False
