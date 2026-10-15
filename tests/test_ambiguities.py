"""Declared ambiguities: a model says which questions can mean more than one thing (#473).

An entry names the kind of question (`when`), its options (each with how to write the SQL, at most
one default), whether the agent should `ask` or `answer_and_offer`, and the tables it concerns.

Synthetic, generic names only.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

pytest.importorskip("pydantic")
pytest.importorskip("sqlglot")

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agami" / "scripts"))

from semantic_model import introspect as I  # noqa: E402
from semantic_model import models as m  # noqa: E402
from semantic_model import validator as V  # noqa: E402
from semantic_model.loader import load_datasource  # noqa: E402

from catalog_helpers import col, make_catalog_runner  # noqa: E402

ORDER_DATE = {
    "name": "order_period",
    "when": "a question groups or filters orders by month or year without naming a date",
    "options": [
        {"label": "Order date", "how": "orders.order_date", "default": True},
        {"label": "Ship date", "how": "orders.ship_date"},
    ],
    "behaviour": "answer_and_offer",
    "applies_to": ["orders"],
}

_runner = make_catalog_runner(
    tables=["orders"],
    columns={
        "orders": [
            col("id", "integer", nullable=False),
            col("order_date", "date"),
            col("ship_date", "date"),
        ]
    },
)


def _model(tmp_path, ambiguities=None):
    I.introspect("shop", "postgres", runner=_runner, artifacts_dir=tmp_path)
    root = tmp_path / "shop"
    if ambiguities is not None:
        ds = yaml.safe_load((root / "datasource.yaml").read_text())
        ds["ambiguities"] = ambiguities
        (root / "datasource.yaml").write_text(yaml.safe_dump(ds, sort_keys=False))
    return root


def test_a_declared_ambiguity_loads_and_validates(tmp_path):
    org = load_datasource(_model(tmp_path, [ORDER_DATE]))
    (a,) = org.ambiguities
    assert a.behaviour == "answer_and_offer" and [o.label for o in a.options] == [
        "Order date",
        "Ship date",
    ]
    assert V.validate(org).ok


def test_a_model_without_ambiguities_is_unchanged(tmp_path):
    org = load_datasource(_model(tmp_path))
    assert org.ambiguities == [] and V.validate(org).ok


@pytest.mark.parametrize(
    "change, message",
    [
        ({"options": [{"label": "only one", "how": "x"}]}, "at least two options"),
        (
            {
                "options": [
                    {"label": "a", "how": "x", "default": True},
                    {"label": "b", "how": "y", "default": True},
                ]
            },
            "at most one",
        ),
        (
            {"options": [{"label": "a", "how": "x"}, {"label": "b", "how": "y"}]},
            "no default option",
        ),
    ],
)
def test_a_badly_shaped_entry_is_refused_by_name(change, message):
    with pytest.raises(ValueError, match=message) as e:
        m.Ambiguity(**{**ORDER_DATE, **change})
    assert "order_period" in str(e.value)


def test_ask_needs_no_default():
    a = m.Ambiguity(
        **{
            **ORDER_DATE,
            "behaviour": "ask",
            "options": [{"label": "a", "how": "x"}, {"label": "b", "how": "y"}],
        }
    )
    assert a.behaviour == "ask"


@pytest.mark.parametrize(
    "entries, code",
    [
        ([ORDER_DATE, ORDER_DATE], "ambiguity_duplicate_name"),
        ([{**ORDER_DATE, "applies_to": ["invoices"]}], "ambiguity_unknown_table"),
    ],
)
def test_cross_checks_name_the_entry(tmp_path, entries, code):
    res = V.validate(load_datasource(_model(tmp_path, entries)))
    assert not res.ok and any(f.code == code and "order_period" in f.message for f in res.findings)


def test_a_schema_qualified_table_counts(tmp_path):
    res = V.validate(
        load_datasource(_model(tmp_path, [{**ORDER_DATE, "applies_to": ["public.ORDERS"]}]))
    )
    assert res.ok


def test_a_refresh_keeps_the_declared_ambiguities(tmp_path):
    root = _model(tmp_path, [ORDER_DATE])
    I.introspect("shop", "postgres", runner=_runner, artifacts_dir=tmp_path, append=True)
    assert [a.name for a in load_datasource(root).ambiguities] == ["order_period"]


# --- the schema reply carries the entries for its scope ---------------------------------------

sys.path.insert(0, str(REPO_ROOT / "packages" / "agami-core" / "src"))
import json  # noqa: E402

import tools  # noqa: E402

_two_tables = make_catalog_runner(
    tables=["orders", "customers"],
    columns={
        "orders": [
            col("id", "integer", nullable=False),
            col("order_date", "date"),
            col("ship_date", "date"),
        ],
        "customers": [col("id", "integer", nullable=False), col("region", "varchar")],
    },
)
CUSTOMER_REGION = {
    "name": "customer_region",
    "when": "a question asks about a customer's region",
    "options": [
        {"label": "Billing region", "how": "customers.billing_region"},
        {"label": "Shipping region", "how": "customers.shipping_region"},
    ],
    "behaviour": "ask",
    "applies_to": ["customers"],
}
EVERYWHERE = {**CUSTOMER_REGION, "name": "currency", "applies_to": []}


def _schema(tmp_path, monkeypatch, entries, **args):
    I.introspect("shop", "postgres", runner=_two_tables, artifacts_dir=tmp_path)
    root = tmp_path / "shop"
    if entries is not None:
        ds = yaml.safe_load((root / "datasource.yaml").read_text())
        ds["ambiguities"] = entries
        (root / "datasource.yaml").write_text(yaml.safe_dump(ds, sort_keys=False))
    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(tmp_path))
    out = tools.tool_get_datasource_schema({"datasource": "shop", **args})
    reply, _ = json.JSONDecoder().raw_decode(out)
    return reply


def _names(reply):
    return sorted(a["name"] for a in reply.get("ambiguities", []))


def test_the_whole_datasource_carries_every_entry(tmp_path, monkeypatch):
    reply = _schema(tmp_path, monkeypatch, [ORDER_DATE, CUSTOMER_REGION, EVERYWHERE])
    assert _names(reply) == ["currency", "customer_region", "order_period"]


def test_named_tables_carry_their_entries_and_the_untargeted_ones(tmp_path, monkeypatch):
    reply = _schema(
        tmp_path, monkeypatch, [ORDER_DATE, CUSTOMER_REGION, EVERYWHERE], dataset_names=["orders"]
    )
    assert _names(reply) == ["currency", "order_period"]
    (order,) = [a for a in reply["ambiguities"] if a["name"] == "order_period"]
    assert order["behaviour"] == "answer_and_offer" and order["options"][0]["default"] is True


def test_an_area_carries_the_entries_for_its_tables(tmp_path, monkeypatch):
    area = load_datasource(_model(tmp_path / "probe")).subject_areas[0].name
    reply = _schema(tmp_path, monkeypatch, [ORDER_DATE, CUSTOMER_REGION, EVERYWHERE], area=area)
    assert _names(reply) == ["currency", "customer_region", "order_period"]


def test_no_entries_means_no_key(tmp_path, monkeypatch):
    assert "ambiguities" not in _schema(tmp_path, monkeypatch, None, dataset_names=["orders"])
    assert "ambiguities" not in _schema(
        tmp_path / "b", monkeypatch, [CUSTOMER_REGION], dataset_names=["orders"]
    )


def test_a_model_without_ambiguities_dumps_without_the_key(tmp_path):
    """So a model deployed by this version still loads on an older one (rollback safety)."""
    org = load_datasource(_model(tmp_path))
    assert "ambiguities" not in org.model_dump()
    assert "ambiguities" not in org.model_dump(exclude={"subject_areas"})


def test_a_model_with_ambiguities_round_trips_through_a_dump(tmp_path):
    org = load_datasource(_model(tmp_path, [ORDER_DATE]))
    again = m.Datasource.model_validate(org.model_dump())
    assert [a.name for a in again.ambiguities] == ["order_period"]


def test_an_option_must_say_how_to_write_it():
    with pytest.raises(ValueError):
        m.Ambiguity(
            **{**ORDER_DATE, "options": [{"label": "a", "how": " "}, {"label": "b", "how": "y"}]}
        )
    with pytest.raises(ValueError):
        m.AmbiguityOption(label="a")


def test_an_entry_on_a_hidden_table_is_not_served(tmp_path, monkeypatch):
    """Excluding `customers` hides it from the agent, and its ambiguity with it; an entry naming
    `customers` and `orders` keeps `orders`. The file still has both."""
    both = {**ORDER_DATE, "name": "both", "applies_to": ["orders", "customers"]}
    reply = _schema(tmp_path, monkeypatch, [CUSTOMER_REGION, both, EVERYWHERE])
    root = tmp_path / "shop"
    area = load_datasource(root).subject_areas[0].name
    from semantic_model import curate as C

    res = C.apply(root, [{"op": "exclude", "kind": "table", "area": area, "name": "customers"}])
    assert not res.errors, res.errors
    served = load_datasource(root)
    assert sorted(a.name for a in served.ambiguities) == ["both", "currency"]
    assert [a for a in served.ambiguities if a.name == "both"][0].applies_to == ["orders"]
    assert len(load_datasource(root, include_rejected=True).ambiguities) == 3
    assert _names(reply) == ["both", "currency", "customer_region"]
