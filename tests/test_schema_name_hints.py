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
        return {
            "name": name,
            "calculation": f"the {name}",
            "description": f"the {name}",
            "bindings": {"PostgreSQL": f"SUM({name})"},
            "source_tables": source_tables,
            "confidence": "proposed",
            "review_state": "unreviewed",
        }

    def _area(name: str, tables: dict[str, list[str]], metrics: list[dict]) -> None:
        adir = root / "subject_areas" / name
        (adir / "tables").mkdir(parents=True)
        (adir / "metrics").mkdir(parents=True)
        for t, cols in tables.items():
            (adir / "tables" / f"{t}.yaml").write_text(
                yaml.safe_dump(
                    {
                        "name": t,
                        "schema": "public",
                        "storage_connection": "c",
                        "grain": ["id"],
                        "description": f"{t} table",
                        "columns": [{"name": "id", "type": "integer", "primary_key": True}]
                        + [{"name": c, "type": "string"} for c in cols],
                    }
                )
            )
        for m in metrics:
            (adir / "metrics" / f"{m['name']}.yaml").write_text(yaml.safe_dump(m))
        (adir / "subject_area.yaml").write_text(
            yaml.safe_dump(
                {
                    "name": name,
                    "description": f"{name} area",
                    "tables": [
                        {"storage_connection": "c", "schema": "public", "table": t} for t in tables
                    ],
                }
            )
        )

    _area(
        SALES, {"orders": ["amount"], "order_items": ["sku"]}, [_metric("order_count", ["orders"])]
    )
    _area(PEOPLE, {"users": ["email"]}, [_metric("headcount", ["users"])])
    (root / "datasource.yaml").write_text(
        yaml.safe_dump(
            {
                "datasource": "acme",
                "version": 1,
                "storage_connections": [{"name": "c", "ref": "datasources/c/storage.yaml"}],
                "subject_areas": [f"subject_areas/{SALES}", f"subject_areas/{PEOPLE}"],
            }
        )
    )


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
        ("ordrs", "orders"),  # a typo
        ("Orders", "orders"),  # case alone
        ("order", "orders"),  # a plural dropped
        ("orderitems", "order_items"),  # a separator dropped
        ("public.ordrs", "orders"),  # schema-qualified, as agents often write them
        ("order_count", "order count"),  # snake_case for a metric named in words
    ],
)
def test_the_closest_real_name_comes_first(name, expected_first):
    candidates = ["orders", "order_items", "users", "order count"]
    assert tools._did_you_mean(name, candidates)[0] == expected_first


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
    assert err["remediation"].startswith(
        f"No subject area named 'salez' in 'acme'. Did you mean {SALES!r}?"
    )


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
    # A typo of a model that exists is not a model to go and build.
    assert "agami-connect" not in err["remediation"]


# --- review findings (#410): each reproduced before the fix ---------------------------------------


def _add_metric(root: Path, area: str, name: str, source_tables: list[str]) -> None:
    import yaml

    (root / "subject_areas" / area / "metrics" / f"{name}.yaml").write_text(
        yaml.safe_dump(
            {
                "name": name,
                "calculation": f"the {name}",
                "description": f"the {name}",
                "bindings": {"PostgreSQL": f"SUM({name})"},
                "source_tables": source_tables,
                "confidence": "proposed",
                "review_state": "unreviewed",
            }
        )
    )


def test_an_exact_match_is_suggested_alone():
    """`Order_Count` means `order_count`; a fuzzy extra beside it only invites the wrong pick."""
    assert tools._did_you_mean("Order_Count", ["order_count", "headcount"]) == ["order_count"]


def test_the_name_asked_for_is_never_suggested_back():
    assert tools._did_you_mean("orders", ["orders", "order_items"]) == ["order_items"]


def test_a_metric_name_two_areas_share_points_at_the_in_scope_key(tmp_path, monkeypatch):
    """`_all_metrics` keys the second copy `order_count (people)`, so inside `people` the bare name
    misses. It used to be told the metric was "outside this call's scope" — the scope it was in."""
    art = tmp_path / "art"
    _write_model(art / "acme")
    _add_metric(art / "acme", PEOPLE, "order_count", ["users"])
    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(art))

    [miss] = _call("acme", area=PEOPLE, metric_names=["order_count"])["unknown_metric_names"]
    assert miss["did_you_mean"] == ["order_count (people)"]
    assert "outside" not in miss["hint"]

    # The other direction: the other area's key, from inside `sales`, is outside — and is never
    # answered with the same-named metric that happens to be in scope.
    [miss] = _call("acme", area=SALES, metric_names=["order_count (people)"])[
        "unknown_metric_names"
    ]
    assert "did_you_mean" not in miss
    assert PEOPLE in miss["hint"] and "outside" in miss["hint"].lower()


def test_a_misspelt_metric_outside_the_scope_is_hinted_not_left_empty(profile):
    """`did_you_mean: []` reads as "no such metric" — the reading that sends an agent inventing
    one. The closest match outside the scope is named, as a hint, so the scope stays as declared."""
    [miss] = _call(profile, dataset_names=["orders"], metric_names=["headcnt"])[
        "unknown_metric_names"
    ]
    assert "headcount" in miss["hint"] and PEOPLE in miss["hint"]
    assert "did_you_mean" not in miss


def test_a_case_only_miss_inside_a_declared_area_is_a_typo(profile):
    """It once read "'Orders' is not in subject area 'sales'. 'Orders' is in subject area 'sales'"
    — both halves about a name that resolves nowhere."""
    err = _call(profile, area=SALES, dataset_names=["Orders"])["error"]
    assert err["did_you_mean"] == {"Orders": ["orders"]}
    assert "is in subject area" not in err["remediation"]


def test_a_table_named_with_its_schema_is_not_refused_as_its_own_typo(tmp_path, monkeypatch):
    """A table whose own `name` carries its schema is indexed in full while `dataset_names` arrives
    bare (#258), so it does not resolve. That is a known gap, not the caller's mistake: refusing it
    suggested back the very name that was sent."""
    import yaml

    art = tmp_path / "art"
    _write_model(art / "acme")
    tfile = art / "acme" / "subject_areas" / SALES / "tables" / "orders.yaml"
    doc = yaml.safe_load(tfile.read_text())
    doc["name"], doc["schema"] = "public.orders", None
    tfile.write_text(yaml.safe_dump(doc))
    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(art))

    head = _call("acme", dataset_names=["orders"])
    assert "error" not in head, "not refused"
    assert "orders" not in head["tables"]["orders"].get("did_you_mean", [])


def test_a_broken_model_keeps_the_loaders_error(profile, tmp_path):
    """A named datasource that exists but fails to load is not a typo of itself: the loader's
    message names the missing file, and "did you mean 'acme'?" would hide it."""
    import shutil

    shutil.rmtree(tmp_path / "art" / "acme" / "subject_areas" / PEOPLE)
    err = _call(profile)["error"]
    assert "did_you_mean" not in err
    assert PEOPLE in err["remediation"]


def test_a_served_deployment_suggests_the_served_list_or_nothing(profile, monkeypatch):
    """With a store configured, the disk is not a stand-in for it: an unreachable store says nothing
    rather than list whatever model folders sit in the container."""
    import execute_sql

    monkeypatch.setattr(execute_sql, "_hosted", lambda: True)
    monkeypatch.setattr(tools, "_served_datasources", lambda _org: ["acme-prod", "beta"])
    assert tools._known_datasources() == ["acme-prod", "beta"]
    monkeypatch.setattr(tools, "_served_datasources", lambda _org: None)
    assert tools._known_datasources() is None


def test_suggestions_are_capped_per_call(profile):
    """Each suggestion scans every name in the model and nothing bounds how many names a caller
    sends, so only the first few misses are searched. The rest are still named."""
    many = [f"bogus{i}" for i in range(30)]
    tables = _call(profile, dataset_names=["orders", *many])["tables"]
    assert (
        sum("did_you_mean" in tables[n] or "hint" in tables[n] for n in many)
        <= tools._SUGGESTED_MISSES
    )
    assert all(tables[n]["error"] for n in many)

    missed = _call(profile, metric_names=[*many, *many])["unknown_metric_names"]
    assert [m["name"] for m in missed] == many, "deduped, and every miss still named"
    assert all(set(m) == {"name"} for m in missed[tools._SUGGESTED_MISSES :])


# --- the advertised surface says where a name comes from ----------------------------------------
#
# The response can only repair a wrong name after the fact. What prevents one is the description
# read while the call is written, so each parameter says what a valid value looks like — and each
# claim is checked against the behaviour it describes, so the text cannot drift from it.


def _schema_tool() -> dict:
    return tools.TOOLS["get_datasource_schema"]


def test_each_name_parameter_says_where_its_values_come_from():
    props = _schema_tool()["inputSchema"]["properties"]
    assert "`subject_areas[].name`" in props["area"]["description"]
    assert "case-sensitive" in props["dataset_names"]["description"]
    assert "`metric_index`" in props["metric_names"]["description"]
    assert "`name (area)`" in props["metric_names"]["description"]


def test_the_tool_says_what_to_do_with_a_miss():
    desc = _schema_tool()["description"]
    assert "`did_you_mean`" in desc and "Never retry with another guessed name" in desc


def test_the_server_instructions_extend_the_column_rule_to_every_name():
    assert "area, table and metric names" in tools.server_instructions()


def test_the_described_matching_is_the_matching(profile):
    """Case-sensitive and schema prefix ignored, as `dataset_names` now says."""
    assert "error" in _call(profile, dataset_names=["Orders"])
    assert _call(profile, dataset_names=["public.orders"])["tables"]["orders"]["columns"]
