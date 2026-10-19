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
