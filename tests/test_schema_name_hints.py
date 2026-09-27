"""`get_datasource_schema` answers a name that is not in the model with the names that ARE.

An agent that passes a table, area, metric or datasource the model does not have is almost always
one edit away from a real one: a plural dropped, a case changed, an area named where a table was
wanted, a column named where its table was. A bare "not found" leaves it to guess again, and a
second guess is how an invented name reaches SQL. So every miss carries `did_you_mean` (the closest
real names, best first) and, where the name is real but of another KIND, a `hint` naming the
parameter it belongs in.

Nothing here widens a scope or resolves a guess on the caller's behalf: a suggestion is only ever
advice, and the call still answers exactly what was asked.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("pydantic")
pytest.importorskip("yaml")

import tools  # noqa: E402

# Synthetic throughout — agami-core is public.
SALES, PEOPLE = "sales", "people"


def _write_model(root: Path) -> None:
    """`sales` defines orders + order_items, `people` defines users (with an `email` column)."""
    import yaml

    (root / "datasources" / "c").mkdir(parents=True)
    (root / "datasources" / "c" / "storage.yaml").write_text(
        yaml.safe_dump({"name": "c", "storage_type": "PostgreSQL"})
    )

    def _metric(name: str, source_tables: list[str]) -> dict:
        return {"name": name, "calculation": f"the {name}", "description": f"the {name}",
                "bindings": {"PostgreSQL": f"SUM({name})"}, "source_tables": source_tables,
                "confidence": "proposed", "review_state": "unreviewed"}

    def _area(name: str, tables: dict[str, list[str]], metrics: list[dict]) -> None:
        adir = root / "subject_areas" / name
        (adir / "tables").mkdir(parents=True)
        (adir / "metrics").mkdir(parents=True)
        for t, cols in tables.items():
            (adir / "tables" / f"{t}.yaml").write_text(yaml.safe_dump({
                "name": t, "schema": "public", "storage_connection": "c", "grain": ["id"],
                "description": f"{t} table",
                "columns": [{"name": "id", "type": "integer", "primary_key": True}]
                + [{"name": c, "type": "string"} for c in cols]}))
        for m in metrics:
            (adir / "metrics" / f"{m['name']}.yaml").write_text(yaml.safe_dump(m))
        (adir / "subject_area.yaml").write_text(yaml.safe_dump({
            "name": name, "description": f"{name} area",
            "tables": [{"storage_connection": "c", "schema": "public", "table": t}
                       for t in tables]}))

    _area(SALES, {"orders": ["amount"], "order_items": ["sku"]},
          [_metric("order_count", ["orders"])])
    _area(PEOPLE, {"users": ["email"]}, [_metric("headcount", ["users"])])
    (root / "datasource.yaml").write_text(yaml.safe_dump({
        "datasource": "acme", "version": 1,
        "storage_connections": [{"name": "c", "ref": "datasources/c/storage.yaml"}],
        "subject_areas": [f"subject_areas/{SALES}", f"subject_areas/{PEOPLE}"]}))


@pytest.fixture()
def profile(tmp_path, monkeypatch):
    art = tmp_path / "art"
    _write_model(art / "acme")
    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(art))
    return "acme"


def _call(profile: str, **args) -> dict:
    """The JSON head of a call (any appended markdown context follows it)."""
    out = tools.tool_get_datasource_schema({"datasource": profile, **args})
    return json.JSONDecoder().raw_decode(out)[0]


# --- the matcher --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "expected_first"),
    [
        ("ordrs", "orders"),          # a typo
        ("Orders", "orders"),         # case alone
        ("order", "orders"),          # a plural dropped
        ("orderitems", "order_items"),  # a separator dropped
        ("public.ordrs", "orders"),   # schema-qualified, as agents often write them
    ],
)
def test_the_closest_real_name_comes_first(name, expected_first):
    assert tools._did_you_mean(name, ["orders", "order_items", "users"])[0] == expected_first


def test_nothing_close_suggests_nothing():
    """A suggestion unrelated to what was asked is worse than none: it reads as an answer."""
    assert tools._did_you_mean("zzzzzz", ["orders", "order_items", "users"]) == []
    assert tools._did_you_mean("", ["orders"]) == []


# --- dataset_names ------------------------------------------------------------------------------


def test_a_scope_of_only_unknown_tables_is_refused_with_the_closest_real_ones(profile):
    """Same rule `area` already follows: a scope that does not exist is an error, not an empty
    model — an empty model reads to an agent as "this datasource has none"."""
    err = _call(profile, dataset_names=["ordrs"])["error"]
    assert err["kind"] == "not_found"
    assert err["did_you_mean"] == {"ordrs": ["orders", "order_items"]}
    assert "orders" in err["remediation"]


def test_one_unknown_table_among_real_ones_is_named_beside_them(profile):
    """The real tables still come back in full; only the miss carries the suggestion."""
    tables = _call(profile, dataset_names=["orders", "ordr_items"])["tables"]
    assert tables["orders"]["columns"], "the real table is still answered"
    assert tables["ordr_items"]["error"] == "not found in scope"
    assert tables["ordr_items"]["did_you_mean"][0] == "order_items"


def test_a_table_with_no_close_match_says_how_to_list_the_real_ones(profile):
    err = _call(profile, dataset_names=["zzzzzz"])["error"]
    assert err["did_you_mean"] == {"zzzzzz": []}
    assert "`area`" in err["remediation"]


def test_a_mistaken_kind_beside_a_real_table_carries_its_hint(profile):
    tables = _call(profile, dataset_names=["orders", "email"])["tables"]
    assert "users" in tables["email"]["hint"]


def test_an_area_named_as_a_table_says_which_parameter_it_belongs_in(profile):
    err = _call(profile, dataset_names=[SALES])["error"]
    assert "`area`" in err["hints"][SALES]


def test_a_column_named_as_a_table_names_the_table_it_lives_in(profile):
    err = _call(profile, dataset_names=["email"])["error"]
    assert "users" in err["hints"]["email"]


# --- area ---------------------------------------------------------------------------------------


def test_an_unknown_area_leads_with_the_closest_real_one(profile):
    err = _call(profile, area="salez")["error"]
    assert err["did_you_mean"] == [SALES]
    assert err["remediation"].startswith(f"No subject area named 'salez' in 'acme'. Did you mean {SALES!r}?")


def test_a_table_named_as_an_area_says_which_parameter_it_belongs_in(profile):
    err = _call(profile, area="orders")["error"]
    assert "`dataset_names`" in err["hint"] and SALES in err["hint"]


def test_a_table_outside_the_declared_area_names_the_area_it_is_in(profile):
    """The refusal used to say only that the pair disagreed, leaving the agent to find the area."""
    err = _call(profile, area=PEOPLE, dataset_names=["orders"])["error"]
    assert "'orders' is in subject area 'sales'" in err["remediation"]


def test_a_misplaced_table_and_a_typo_are_each_named_for_what_they_are(profile):
    err = _call(profile, area=PEOPLE, dataset_names=["orders", "ordrs"])["error"]
    assert "'orders' is in subject area 'sales'" in err["remediation"]
    assert "'ordrs' is in no subject area" in err["remediation"]


def test_a_misspelt_table_inside_a_declared_area_is_a_typo_not_a_misplacement(profile):
    """Before this, `ordrs` with `area` was reported as "not in subject area" — true, and useless:
    it is in no area at all."""
    err = _call(profile, area=SALES, dataset_names=["ordrs"])["error"]
    assert err["did_you_mean"] == {"ordrs": ["orders", "order_items"]}


# --- metric_names -------------------------------------------------------------------------------


def test_an_unknown_metric_name_is_reported_not_silently_dropped(profile):
    head = _call(profile, metric_names=["order_cnt"])
    assert head["unknown_metric_names"] == [{"name": "order_cnt", "did_you_mean": ["order_count"]}]


def test_a_metric_outside_the_scope_says_so_and_stays_outside(profile):
    """It exists — telling the agent it does not would send it inventing one. But naming it must
    not widen the declared scope, so it is reported, not added."""
    head = _call(profile, dataset_names=["orders"], metric_names=["headcount"])
    assert "headcount" not in head["metric_index"]
    [miss] = head["unknown_metric_names"]
    assert miss["name"] == "headcount" and PEOPLE in miss["hint"]


def test_known_metric_names_add_no_report(profile):
    assert "unknown_metric_names" not in _call(profile, metric_names=["order_count"])


# --- datasource ---------------------------------------------------------------------------------


def test_an_unknown_datasource_names_the_real_ones(profile):
    err = _call("acm")["error"]
    assert err["kind"] == "not_found"
    assert err["did_you_mean"] == ["acme"]
    assert err["datasources"] == ["acme"]
