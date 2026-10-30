"""The validator warns on figures measured on the data in model text (#479).

"covers only 57 of the 231 operations" is true the day enrichment runs it and wrong after the next
load, yet it reads as part of the definition and sessions repeat it in answers. These tests pin what
is flagged, and the larger half, what must not be: the constants a definition is made of.
"""

from __future__ import annotations

import pytest

pytest.importorskip("pydantic")

from semantic_model import validator as V  # noqa: E402
from semantic_model.models import Datasource  # noqa: E402


@pytest.mark.parametrize(
    "text",
    [
        "covers 57 of 231 rows",
        "covers only 57 of the 231 operations",
        "427 of 670 accounts have a value",
        "516 out of 797 customers qualify",
        "1,204 of 12,880 orders carry no region",
        "covers only 40% of the table",
        "Populated on about 38% of rows",
        "12.5% of records are duplicates",
        "62% null",
        "roughly 30% are missing",
        "joining on the site key alone fans out (829 rows)",
        "negative on 53 rows, so the ratio blows up",
        "NULL on 576 (not quantifiable)",
        "JSON null on ~15%",
        "NULL for 100 accounts",
    ],
)
def test_a_measured_figure_is_found(text):
    assert V._measured_figure(text)


@pytest.mark.parametrize(
    "text",
    [
        "score > 0.8",
        "amount x 1.5 when the order is expedited",
        "price_cents / 100",
        "10% of revenue, paid as commission",  # a rate in a definition, not a share of rows
        "a customer qualifies when 2 of 3 criteria hold",  # a rule, not a count
        "fiscal Q1 of 2024 starts in April",
        "NULL means the value was not recorded",
        "covers EU and US regions",
        "Order total can be negative on refunds.",
        "2016-2020 rows have no category return",  # a year range, not a count
        "stored as '1500 records' or a bare '12'",  # a sample value, not a count
        "NULL means the row was never scored",
        "",
    ],
)
def test_a_definition_constant_is_not(text):
    assert V._measured_figure(text) is None


def _org(**area) -> Datasource:
    return Datasource.model_validate(
        {"datasource": "acme", "version": 1, "subject_areas": [{"name": "ops", **area}]}
    )


def _found(org: Datasource) -> list[V.Finding]:
    return [f for f in V.validate(org).findings if f.code == "measured_figure"]


def test_a_table_caveat_produces_the_warning():
    org = _org(tables_defined=[{"name": "orders", "caveats": ["covers 57 of 231 rows"]}])
    [finding] = _found(org)
    assert finding.severity == "warning"
    assert "table 'orders' caveat ('57 of 231')" in finding.message


def test_every_text_enrichment_writes_is_read():
    org = _org(
        tables_defined=[
            {
                "name": "orders",
                "columns": [
                    {"name": "region", "type": "string", "caveats": ["62% null"]},
                ],
            }
        ],
        metrics=[
            {"name": "active", "calculation": "516 of 797 customers qualify"},
            {"name": "churn", "calculation": "c", "description": "covers only 40% of rows"},
        ],
        entities=[{"name": "account", "caveats": ["427 of 670 accounts have a value"]}],
    )
    [finding] = _found(org)
    assert finding.message.startswith("4 model text(s)")
    for where in ("column 'orders.region'", "metric 'active'", "metric 'churn'", "entity 'account'"):
        assert where in finding.message


def test_definition_constants_raise_nothing():
    org = _org(
        metrics=[
            {"name": "hot_leads", "calculation": "leads with score > 0.8"},
            {"name": "weighted", "calculation": "amount x 1.5 for expedited orders"},
        ]
    )
    assert _found(org) == []


def test_the_warning_is_one_for_the_whole_model():
    org = _org(
        tables_defined=[
            {"name": f"t{i}", "caveats": [f"covers {i} of 100 rows"]} for i in range(7)
        ]
    )
    [finding] = _found(org)
    assert finding.message.startswith("7 model text(s)") and "and 2 more" in finding.message


# #541: figures written as ranges, in words, with a hedge or tied to now, and counts in a caveat.
@pytest.mark.parametrize(
    "text",
    [
        "values range 49–142 here",
        "values range 49-142 here",
        "values range 49 to 142",
        "values vary between 49 and 142",
        "seen values 3 to 80",
        "order dates range from 2019 to 2024",
        "covers twelve of forty sites",
        "twelve of the forty sites report a manager",
        "only three out of five sites report it",
        "three out of five sites here have a manager",
        "hundreds of rows have no region",
        "thousands of orders carry no channel",
        "a dozen sites are missing a region",
        "twenty-two stores lack a manager",
        "about 1,200 sites",
        "roughly forty stores",
        "~15% of accounts",
        "40 sites here",
        "12% currently",
        "$3.2M currently",
        "currently 812 accounts",
        "there are currently 40 active sites",
        "as of June 2024, 812 accounts",
        "1,204 orders carry no region",
        "40% of sites have no owner",
        "Region is blank for 1,204 orders, so group by it with care.",
        "212 accounts have no owner, so the join drops them",
    ],
)
def test_a_figure_in_another_form_is_found(text):
    assert V._measured_figure(text)


@pytest.mark.parametrize(
    "text",
    [
        "returns 0 because no row meets the threshold",
        "this formula returns 0 because no row meets the threshold",
        "CASE WHEN status = 'shipped' THEN 1 ELSE 0 END",
        "1 when the order is late, else 0",
        "a value of 1 here means the row is active",
        "SUM(amount) / 100 for cents; divide by 100 here",
        "fiscal year starts in April",
        "fiscal year 2024 starts in April",
        "the fiscal 2023 orders are restated here",
        "Rows from the 2019 migration are duplicated",
        "as of 2024 the field is required",
        "version 2.3 of the export",
        "UTF-8 encoded text here",
        "date 2024-01-15 here",
        "scores range 0 to 100",
        "probability ranges 0-1",
        "ratings are on a 1–5 scale here",
        "in the range 18–65",
        "age band between 18 and 65",
        "SLA ranges from 3 to 5 days depending on tier",
        "Discount varies between 5 and 15 by tier",
        "uses columns 3 and 5 here",
        "2 rows per order",
        "returns NULL when fewer than 10 rows exist",
        "Requires at least 30 records per store",
        "orders older than 90 days from today",
        "NOW() - 30 days",
        "rounds to 2 decimal places here",
        "top 10 customers currently ranked by spend",
        "accounts with more than 5 users have no discount",
        "0 items have no revenue",
        "Products in category 7 have no price",
        "Status 9 rows are test data",
        "three out of five checks must pass",
        "values stored in thousands of dollars",
        "10% of revenue is paid as commission",
        "Currently 4 statuses are open: new, paid, packed, shipped",
        "one row per order",
    ],
)
def test_a_behaviour_or_definition_is_not(text):
    assert V._measured_figure(text) is None


def test_a_range_in_a_caveat_produces_the_warning():
    org = _org(
        tables_defined=[
            {
                "name": "sites",
                "caveats": ["values range 49–142 here", "returns 0 because no row meets the threshold"],
            }
        ]
    )
    [finding] = _found(org)
    assert finding.severity == "warning"
    assert finding.message.startswith("1 model text(s)") and "('range 49–142')" in finding.message


# Review of #541: a compound ending in "one", explicit row counts after "with"/"returns", counts
# that look like years, now-cued ranges that are definitions, and compound storage units.
@pytest.mark.parametrize(
    "text",
    [
        "one hundred and one rows",
        "currently one hundred and one sites",
        "The table is populated with 1,204 rows",
        "The join returns 829 rows",
        "There are 2024 rows",
        "2024 of 5000 accounts have a value",
        "hundreds of thousands of rows have no region",
    ],
)
def test_a_count_the_review_found_missed_is_found(text):
    assert V._measured_figure(text)


@pytest.mark.parametrize(
    "text",
    [
        "scores range 0–100 here",
        "currently 3 to 5 days depending on tier",
        "values stored in hundreds of thousands of dollars",
        "returns 0 rows when nothing matches",
        "since 2024 the field is required",
        "Q1 2024 orders are restated",
    ],
)
def test_a_definition_the_review_found_flagged_is_not(text):
    assert V._measured_figure(text) is None


def test_a_count_that_looks_like_a_year_is_a_count_unless_used_as_a_date():
    for year in range(1900, 2100):
        assert V._measured_figure(f"There are {year} rows"), year
        assert V._measured_figure(f"{year} of 5000 accounts have a value"), year
        assert V._measured_figure(f"fiscal year {year} starts in April") is None, year
        assert V._measured_figure(f"since {year} the field is required") is None, year
