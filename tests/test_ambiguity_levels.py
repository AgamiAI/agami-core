"""Ambiguities tied finer than a table, disclosed progressively (#477).

An entry can name `columns` (`table.column`), `column_groups` (`table.group`) or `metrics`. The full
entry arrives with the reply that opens one of them; a reply that reaches it without opening it
carries a signpost naming the call to make. Synthetic, generic names only.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
import yaml

pytest.importorskip("pydantic")
pytest.importorskip("sqlglot")

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agami" / "scripts"))
sys.path.insert(0, str(REPO_ROOT / "packages" / "agami-core" / "src"))

import tools  # noqa: E402
from semantic_model import introspect as I  # noqa: E402
from semantic_model import models as m  # noqa: E402
from semantic_model import validator as V  # noqa: E402
from semantic_model.loader import load_datasource  # noqa: E402

from catalog_helpers import col, make_catalog_runner  # noqa: E402

_runner = make_catalog_runner(
    tables=["orders", "customers"],
    columns={
        "orders": [
            col("id", "integer", nullable=False),
            col("ordered_on", "date"),
            col("shipped_on", "date"),
            col("status", "varchar"),
            col("amount", "numeric"),
        ],
        "customers": [col("id", "integer", nullable=False), col("region", "varchar")],
    },
)
OPTS = [
    {"label": "Ordered", "how": "ordered_on", "default": True},
    {"label": "Shipped", "how": "shipped_on"},
]


def _entry(name, **anchors):
    return {
        "name": name,
        "when": f"a question that needs {name}",
        "options": OPTS,
        "behaviour": "answer_and_offer",
        **anchors,
    }


def _build(tmp_path, entries, groups=None):
    I.introspect("shop", "postgres", runner=_runner, artifacts_dir=tmp_path)
    root = tmp_path / "shop"
    if groups:
        (path,) = [p for p in root.rglob("orders.yaml")]
        t = yaml.safe_load(path.read_text())
        t["column_groups"] = groups
        t["column_group_descriptions"] = {g: f"{g} columns" for g in groups}
        path.write_text(yaml.safe_dump(t, sort_keys=False))
    ds = yaml.safe_load((root / "datasource.yaml").read_text())
    ds["ambiguities"] = entries
    (root / "datasource.yaml").write_text(yaml.safe_dump(ds, sort_keys=False))
    return root


def _reply(tmp_path, monkeypatch, **args):
    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(tmp_path))
    out, _ = json.JSONDecoder().raw_decode(
        tools.tool_get_datasource_schema({"datasource": "shop", **args})
    )
    return out


def _full(reply):
    return [a["name"] for a in reply.get("ambiguities", [])]


def _posts(reply):
    return {s["name"]: s["open"] for s in reply.get("ambiguity_signposts", [])}


GROUPS = {"dates": ["ordered_on", "shipped_on"], "money": ["amount", "status"]}


def test_a_group_entry_is_signposted_until_its_group_is_opened(tmp_path, monkeypatch):
    _build(tmp_path, [_entry("order_date", column_groups=["orders.dates"])], GROUPS)
    plain = _reply(tmp_path, monkeypatch, dataset_names=["orders"])
    assert _full(plain) == [] and _posts(plain) == {
        "order_date": {"dataset_names": ["orders"], "column_groups": ["orders.dates"]}
    }
    opened = _reply(tmp_path, monkeypatch, dataset_names=["orders"], column_groups=["dates"])
    assert _full(opened) == ["order_date"] and "ambiguity_signposts" not in opened


def test_a_group_entry_on_a_table_not_requested_says_to_request_it(tmp_path, monkeypatch):
    _build(tmp_path, [_entry("order_date", column_groups=["orders.dates"])], GROUPS)
    reply = _reply(tmp_path, monkeypatch, dataset_names=["customers"])
    assert _full(reply) == [] and "ambiguity_signposts" not in reply, "out of this reply's scope"
    overview = _reply(tmp_path, monkeypatch, mode="summary")
    assert _posts(overview) == {
        "order_date": {"dataset_names": ["orders"], "column_groups": ["orders.dates"]}
    }


def test_a_column_entry_opens_with_an_ungrouped_table(tmp_path, monkeypatch):
    """A table with no groups sends every column in full, so naming the table opens the column."""
    _build(tmp_path, [_entry("order_status", columns=["orders.status"])])
    assert _full(_reply(tmp_path, monkeypatch, dataset_names=["orders"])) == ["order_status"]


def test_a_column_entry_on_a_grouped_table_points_at_its_group(tmp_path, monkeypatch):
    _build(tmp_path, [_entry("order_status", columns=["orders.status"])], GROUPS)
    reply = _reply(tmp_path, monkeypatch, dataset_names=["orders"])
    assert _posts(reply) == {
        "order_status": {"dataset_names": ["orders"], "column_groups": ["orders.money"]}
    }
    opened = _reply(tmp_path, monkeypatch, dataset_names=["orders"], column_groups=["money"])
    assert _full(opened) == ["order_status"]


def test_the_finer_anchors_must_exist(tmp_path):
    root = _build(
        tmp_path,
        [
            _entry("a", columns=["orders.nope"]),
            _entry("b", column_groups=["orders.nope"]),
            _entry("c", metrics=["no_such_metric"]),
            _entry("d", columns=["status"]),
        ],
        GROUPS,
    )
    res = V.validate(load_datasource(root))
    bad = sorted(
        f.message.split("'")[1] for f in res.findings if f.code == "ambiguity_unknown_anchor"
    )
    assert bad == ["a", "b", "c", "d"]


def test_an_entry_whose_only_anchor_is_hidden_is_dropped_not_widened(tmp_path):
    root = _build(tmp_path, [_entry("region", columns=["customers.region"])])
    from semantic_model import curate as C

    area = load_datasource(root).subject_areas[0].name
    assert not C.apply(
        root, [{"op": "exclude", "kind": "table", "area": area, "name": "customers"}]
    ).errors
    assert load_datasource(root).ambiguities == []


def test_a_model_without_finer_anchors_dumps_as_before(tmp_path):
    org = load_datasource(_build(tmp_path, [_entry("plain", applies_to=["orders"])]))
    dumped = org.model_dump()["ambiguities"][0]
    assert not {"columns", "column_groups", "metrics"} & set(dumped)
    assert m.Datasource.model_validate(org.model_dump()).ambiguities[0].applies_to == ["orders"]


# --- review findings: every signpost must open, and metric entries must never vanish ---------------


def _add_metric(root, name="revenue"):
    (area,) = [p for p in (root / "subject_areas").iterdir() if p.is_dir()]
    (area / "metrics").mkdir(exist_ok=True)
    (area / "metrics" / f"{name}.yaml").write_text(
        yaml.safe_dump(
            {
                "name": name,
                "calculation": "total order amount",
                "description": "Total order amount",
                "bindings": {"PostgreSQL": "SUM(orders.amount)"},
                "source_tables": ["orders"],
                "confidence": "proposed",
                "review_state": "unreviewed",
            }
        )
    )
    return area.name


def _follow(tmp_path, monkeypatch, open_args):
    """Make exactly the call a signpost names, the way the note tells the agent to."""
    return _reply(tmp_path, monkeypatch, **open_args)


def test_a_metric_entry_opens_with_its_metric_and_its_signpost_does_not_loop(tmp_path, monkeypatch):
    root = _build(tmp_path, [_entry("rev_basis", metrics=["revenue"])])
    _add_metric(root)
    # A query that matches no metric, so `revenue` is only indexed, never sent in full.
    overview = _reply(tmp_path, monkeypatch, query="zzz", mode="summary")
    assert _full(overview) == [] and _posts(overview) == {
        "rev_basis": {"metric_names": ["revenue"]}
    }
    assert _full(_follow(tmp_path, monkeypatch, {"metric_names": ["revenue"]})) == ["rev_basis"]


def test_a_metric_entry_is_signposted_not_dropped_when_a_query_narrows_the_metrics(
    tmp_path, monkeypatch
):
    root = _build(tmp_path, [_entry("rev_basis", metrics=["revenue"])])
    area = _add_metric(root)
    reply = _reply(tmp_path, monkeypatch, area=area, query="customers")
    assert "rev_basis" in _full(reply) or "rev_basis" in _posts(reply)


def test_a_table_and_column_entry_opens_with_its_table_and_never_points_nowhere(
    tmp_path, monkeypatch
):
    _build(tmp_path, [_entry("mixed", applies_to=["orders"], columns=["customers.region"])])
    assert _full(_reply(tmp_path, monkeypatch, dataset_names=["orders"])) == ["mixed"]
    post = _posts(_reply(tmp_path, monkeypatch, dataset_names=["customers"]))
    assert post == {} or all(v for v in post["mixed"].values())


def test_following_a_group_signpost_literally_opens_the_entry(tmp_path, monkeypatch):
    _build(tmp_path, [_entry("order_date", column_groups=["public.orders.dates"])], GROUPS)
    post = _posts(_reply(tmp_path, monkeypatch, dataset_names=["orders"]))["order_date"]
    assert post == {"dataset_names": ["orders"], "column_groups": ["orders.dates"]}, "bare names"
    assert _full(_follow(tmp_path, monkeypatch, post)) == ["order_date"]
    overview_post = _posts(_reply(tmp_path, monkeypatch, mode="summary"))["order_date"]
    assert _full(_follow(tmp_path, monkeypatch, overview_post)) == ["order_date"]


def test_a_schema_qualified_edge_is_matched_by_its_bare_name():
    both = {
        "from_table": "public.orders",
        "to_table": "customers",
        "from_column": "customer_id",
        "to_column": "id",
        "relationship": "many_to_one",
    }
    kept, other = tools._compact_other_joins([both], {"orders", "customers"}, {})
    assert kept == [both] and other == [], "a join between two requested tables stays whole"
    one_side = {**both, "to_table": "users"}
    _, other = tools._compact_other_joins([one_side], {"orders"}, {})
    assert other[0]["table"] == "users" and other[0]["relationship"] == "many_to_one"


def test_condition_and_key_joins_to_one_table_stay_separate_and_read_from_the_named_side():
    cond = "reps.rep_natural_key = orders.rep_key AND reps.current_flag = 'Y'"
    edges = [
        {
            "from_table": "orders",
            "to_table": "reps",
            "from_column": "rep_key",
            "to_column": "rep_key",
            "relationship": "many_to_one",
        },
        {"from_table": "reps", "to_table": "orders", "on": cond, "relationship": "many_to_one"},
    ]
    _, other = tools._compact_other_joins(edges, {"orders"}, {})
    keyed, conditioned = other
    assert keyed["keys"] == ["rep_key=rep_key"] and "on" not in keyed
    assert conditioned["on"] == [cond] and conditioned["relationship"] == "one_to_many"
