"""An area named where a table was wanted also names that area's own tables like it (#567).

Models often name an area after its main table with a suffix (area `orders`, table `orders_v`),
and agents ask for the area's name as a table. The hint said "pass it as `area`" and nothing else,
so the agent spent a second call finding the table. The suggestion is drawn only from that area's
own tables: a near-miss elsewhere is the unrelated guess the hint exists to avoid. The activity log
also records that a hint was given, so an empty suggestion list no longer reads as "given nothing".
"""

from __future__ import annotations

import contextvars
import json
from pathlib import Path

import pytest

pytest.importorskip("pydantic")
pytest.importorskip("yaml")

import tools  # noqa: E402


def _write(root: Path, areas: dict[str, list[str]]) -> None:
    import yaml

    (root / "datasources" / "c").mkdir(parents=True)
    (root / "datasources" / "c" / "storage.yaml").write_text(
        yaml.safe_dump({"name": "c", "storage_type": "PostgreSQL"})
    )
    for area, tables in areas.items():
        adir = root / "subject_areas" / area
        (adir / "tables").mkdir(parents=True)
        for t in tables:
            (adir / "tables" / f"{t}.yaml").write_text(
                yaml.safe_dump(
                    {
                        "name": t,
                        "schema": "public",
                        "storage_connection": "c",
                        "grain": ["id"],
                        "description": f"{t} table",
                        "columns": [{"name": "id", "type": "integer", "primary_key": True}],
                    }
                )
            )
        (adir / "subject_area.yaml").write_text(
            yaml.safe_dump(
                {
                    "name": area,
                    "description": f"{area} area",
                    "tables": [
                        {"storage_connection": "c", "schema": "public", "table": t} for t in tables
                    ],
                }
            )
        )
    (root / "datasource.yaml").write_text(
        yaml.safe_dump(
            {
                "datasource": "acme",
                "version": 1,
                "storage_connections": [{"name": "c", "ref": "datasources/c/storage.yaml"}],
                "subject_areas": [f"subject_areas/{a}" for a in areas],
            }
        )
    )


@pytest.fixture()
def profile(tmp_path, monkeypatch):
    art = tmp_path / "art"
    _write(
        art / "acme",
        {
            "orders": ["orders_v", "orders_line_v"],
            "returns": ["returns_v"],
            # An area whose tables are not named like it, beside a table elsewhere that is.
            "sales": ["invoices"],
            "archive": ["sales_old"],
            # An area whose own table ranks below three closer names in another area.
            "refund": ["refund_request_v"],
            "ops": ["refund_sla", "refund_log", "refund_task"],
        },
    )
    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(art))
    return "acme"


def _call(profile: str, **args):
    """The reply's JSON head, and the misses the call noted."""
    ctx = contextvars.copy_context()
    out = ctx.run(tools.tool_get_datasource_schema, {"datasource": profile, **args})
    return json.JSONDecoder().raw_decode(out)[0], ctx.get(tools._call_misses) or []


def test_an_area_alone_suggests_its_own_table(profile):
    reply, misses = _call(profile, dataset_names=["orders"])
    err = reply["error"]
    assert "pass it as `area`" in err["hints"]["orders"]
    assert "'orders_v'" in err["hints"]["orders"]
    assert err["did_you_mean"]["orders"][0] == "orders_v"
    assert set(err["did_you_mean"]["orders"]) <= {"orders_v", "orders_line_v"}
    [miss] = misses
    assert miss["hinted"] is True and miss["did_you_mean"][0] == "orders_v"


def test_an_area_beside_a_real_table_suggests_its_own_table(profile):
    reply, misses = _call(profile, dataset_names=["invoices", "returns"])
    entry = reply["tables"]["returns"]
    assert entry["did_you_mean"] == ["returns_v"]
    assert "'returns_v'" in entry["hint"]
    assert [m for m in misses if m["name"] == "returns"][0]["hinted"] is True


def test_closer_names_in_another_area_do_not_crowd_out_the_areas_own(profile):
    reply, _misses = _call(profile, dataset_names=["refund"])
    assert reply["error"]["did_you_mean"]["refund"] == ["refund_request_v"]


def test_a_like_named_table_in_another_area_is_not_suggested(profile):
    # `sales_old` is close to `sales` but lives in `archive`: guessing it is the unrelated near-miss.
    reply, misses = _call(profile, dataset_names=["sales"])
    err = reply["error"]
    assert err["hints"]["sales"].endswith("pass it as `area`.")
    assert "sales" not in err["did_you_mean"]
    assert misses[0]["hinted"] is True and misses[0]["did_you_mean"] == []


def test_an_area_named_inside_a_declared_area_is_flagged_too(profile):
    reply, misses = _call(profile, area="sales", dataset_names=["invoices", "returns"])
    assert "'returns_v'" in json.dumps(reply)
    miss = [m for m in misses if m["name"] == "returns"][0]
    assert miss == {
        "kind": "table",
        "name": "returns",
        "did_you_mean": ["returns_v"],
        "hinted": True,
    }


def test_a_column_named_as_a_table_is_flagged_too(profile):
    _reply, misses = _call(profile, dataset_names=["id"])
    assert misses[0]["hinted"] is True


def test_a_typo_is_not_marked_as_hinted(profile):
    _reply, misses = _call(profile, dataset_names=["ordrs_v"])
    assert "hinted" not in misses[0]


def test_many_spellings_of_one_area_search_once(profile, monkeypatch):
    calls = []
    real = tools._did_you_mean

    def counting(name, candidates, n=3):
        candidates = list(candidates)
        if set(candidates) == {"orders_v", "orders_line_v"}:
            calls.append(name)
        return real(name, candidates, n)

    monkeypatch.setattr(tools, "_did_you_mean", counting)
    spellings = ["orders", "ORDERS", "Orders", "oRders", "orDers", "ordErs", "ordeRs", "ordersS"]
    _call(profile, dataset_names=spellings[:7] * 3)
    assert len(calls) == 1


def test_the_flag_survives_the_bound_and_the_prose_does_not():
    stored = json.loads(
        tools._bounded_missed(
            [{"kind": "table", "name": "orders", "did_you_mean": [], "hinted": True}]
        )
    )
    [entry] = stored["entries"]
    assert entry["hinted"] is True and "hint" not in entry
