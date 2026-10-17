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
    """Each signpost's `open`, less the datasource it must always carry (checked here)."""
    out = {}
    for s in reply.get("ambiguity_signposts", []):
        assert s["open"]["datasource"] == "shop", "a signpost must name its datasource"
        out[s["name"]] = {k: v for k, v in s["open"].items() if k != "datasource"}
    return out


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


def _follow(tmp_path, monkeypatch, reply, name):
    """Make exactly the call a signpost names, adding nothing, the way the note tells the agent to."""
    (post,) = [p["open"] for p in reply["ambiguity_signposts"] if p["name"] == name]
    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(tmp_path))
    out, _ = json.JSONDecoder().raw_decode(tools.tool_get_datasource_schema(dict(post)))
    return out


def test_a_metric_entry_opens_with_its_metric_and_its_signpost_does_not_loop(tmp_path, monkeypatch):
    root = _build(tmp_path, [_entry("rev_basis", metrics=["revenue"])])
    _add_metric(root)
    # A query that matches no metric, so `revenue` is only indexed, never sent in full.
    overview = _reply(tmp_path, monkeypatch, query="zzz", mode="summary")
    assert _full(overview) == [] and _posts(overview) == {
        "rev_basis": {"metric_names": ["revenue"]}
    }
    assert _full(_follow(tmp_path, monkeypatch, overview, "rev_basis")) == ["rev_basis"]


def test_a_metric_entry_is_signposted_not_dropped_when_a_query_narrows_the_metrics(
    tmp_path, monkeypatch
):
    root = _build(tmp_path, [_entry("rev_basis", metrics=["revenue"])])
    area = _add_metric(root)
    reply = _reply(tmp_path, monkeypatch, area=area, query="customers", mode="summary")
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
    reply = _reply(tmp_path, monkeypatch, dataset_names=["orders"])
    post = _posts(reply)["order_date"]
    assert post == {"dataset_names": ["orders"], "column_groups": ["orders.dates"]}, "bare names"
    assert _full(_follow(tmp_path, monkeypatch, reply, "order_date")) == ["order_date"]
    overview = _reply(tmp_path, monkeypatch, mode="summary")
    assert _full(_follow(tmp_path, monkeypatch, overview, "order_date")) == ["order_date"]


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


# --- Copilot review on #484 ---------------------------------------------------------------------


def test_an_undescribed_metric_entry_is_still_signposted_in_an_area_summary(tmp_path, monkeypatch):
    """Undescribed metrics are counted, not indexed, so reach comes from the scope's registry."""
    root = _build(tmp_path, [_entry("rev_basis", metrics=["revenue"])])
    area = _add_metric(root)
    (mf,) = root.rglob("revenue.yaml")
    doc = yaml.safe_load(mf.read_text())
    doc["description"] = ""
    mf.write_text(yaml.safe_dump(doc))
    reply = _reply(tmp_path, monkeypatch, area=area, query="zzz", mode="summary")
    assert _posts(reply) == {"rev_basis": {"metric_names": ["revenue"]}}
    assert _full(_follow(tmp_path, monkeypatch, reply, "rev_basis")) == ["rev_basis"]


def test_an_entry_tied_to_an_excluded_column_or_a_rejected_metric_is_dropped(tmp_path):
    from semantic_model import curate as C

    root = _build(
        tmp_path,
        [
            _entry("by_status", columns=["orders.status"]),
            _entry("rev_basis", metrics=["revenue"]),
            _entry("kept", applies_to=["orders"]),
        ],
    )
    area = _add_metric(root)
    res = C.apply(
        root,
        [{"op": "exclude", "kind": "table", "area": area, "name": "orders", "column": "status"}],
    )
    assert not res.errors, res.errors
    (mf,) = root.rglob("revenue.yaml")
    doc = yaml.safe_load(mf.read_text())
    doc["review_state"] = "rejected"
    mf.write_text(yaml.safe_dump(doc))
    assert [a.name for a in load_datasource(root).ambiguities] == ["kept"]
    assert len(load_datasource(root, include_rejected=True).ambiguities) == 3


def test_a_reversed_join_keeps_which_side_the_outer_join_preserves():
    """`lines LEFT JOIN orders` keeps every line; read from orders it is `orders RIGHT JOIN lines`."""
    left = {
        "from_table": "lines",
        "to_table": "orders",
        "from_column": "order_id",
        "to_column": "id",
        "relationship": "many_to_one",
        "join_type": "LEFT",
    }
    (entry,) = tools._compact_other_joins([left], {"orders"}, {})[1]
    assert entry["join_type"] == "RIGHT" and entry["relationship"] == "one_to_many"
    (entry,) = tools._compact_other_joins([{**left, "join_type": "RIGHT"}], {"orders"}, {})[1]
    assert "join_type" not in entry, "RIGHT read from the other side is the default LEFT"


def test_a_signpost_keeps_the_declared_spelling_of_tables_and_groups(tmp_path, monkeypatch):
    root = _build(tmp_path, [_entry("order_date", column_groups=["orders.dates"])], GROUPS)
    (path,) = root.rglob("orders.yaml")
    t = yaml.safe_load(path.read_text())
    t["name"] = "ORDERS"
    t["column_groups"] = {"Dates": GROUPS["dates"], "money": GROUPS["money"]}
    t["column_group_descriptions"] = {"Dates": "dates", "money": "money"}
    path.write_text(yaml.safe_dump(t, sort_keys=False))
    for sa in root.rglob("subject_area.yaml"):
        sa.write_text(sa.read_text().replace("table: orders", "table: ORDERS"))
    overview = _reply(tmp_path, monkeypatch, mode="summary")
    assert _posts(overview)["order_date"] == {
        "dataset_names": ["ORDERS"],
        "column_groups": ["ORDERS.Dates"],
    }
    assert _full(_follow(tmp_path, monkeypatch, overview, "order_date")) == ["order_date"]
    assert V.validate(load_datasource(root)).ok, (
        "the anchor resolves the way the validator reads it"
    )


def test_a_rejected_metric_alone_drops_its_entry(tmp_path):
    """With nothing else hidden, the loader's other pruning is skipped; this one must still run."""
    root = _build(
        tmp_path, [_entry("rev_basis", metrics=["revenue"]), _entry("kept", applies_to=["orders"])]
    )
    _add_metric(root)
    (mf,) = root.rglob("revenue.yaml")
    doc = yaml.safe_load(mf.read_text())
    doc["review_state"] = "rejected"
    mf.write_text(yaml.safe_dump(doc))
    assert [a.name for a in load_datasource(root).ambiguities] == ["kept"]


# --- Copilot re-review on #484 ------------------------------------------------------------------


def test_a_rejected_copy_of_a_shared_metric_name_keeps_the_entry(tmp_path):
    """Two definitions may share a name; rejecting one doesn't remove the one still served."""
    root = _build(tmp_path, [_entry("rev_basis", metrics=["revenue"])])
    _add_metric(root)
    (mf,) = root.rglob("revenue.yaml")
    doc = yaml.safe_load(mf.read_text())
    doc["review_state"] = "rejected"
    mf.write_text(yaml.safe_dump(doc))
    ds = yaml.safe_load((root / "datasource.yaml").read_text())
    ds["cross_subject_area_metrics"] = [{**doc, "review_state": "unreviewed"}]
    (root / "datasource.yaml").write_text(yaml.safe_dump(ds, sort_keys=False))
    assert [a.name for a in load_datasource(root).ambiguities] == ["rev_basis"]


def test_an_entry_on_a_group_emptied_by_exclusion_is_dropped(tmp_path):
    from semantic_model import curate as C

    root = _build(
        tmp_path,
        [
            _entry("order_date", column_groups=["orders.dates"]),
            _entry("kept", applies_to=["orders"]),
        ],
        GROUPS,
    )
    area = load_datasource(root).subject_areas[0].name
    ops = [
        {"op": "exclude", "kind": "table", "area": area, "name": "orders", "column": c}
        for c in GROUPS["dates"]
    ]
    assert not C.apply(root, ops).errors
    org = load_datasource(root)
    assert [a.name for a in org.ambiguities] == ["kept"]
    assert not [f for f in V.validate(org).findings if f.code == "ambiguity_unknown_anchor"]


def test_an_anchor_its_area_does_not_expose_is_never_signposted(tmp_path, monkeypatch):
    """A group hidden by the owning area's `expose_column_groups` can't be opened by any call, so
    a signpost to it would only loop. A reachable anchor beside it still works."""
    root = _build(
        tmp_path,
        [
            _entry("order_date", column_groups=["orders.dates"]),
            _entry("order_status", columns=["orders.status"]),
        ],
        GROUPS,
    )
    (sa_file,) = root.rglob("subject_area.yaml")
    sa = yaml.safe_load(sa_file.read_text())
    for ref in sa["tables"]:
        if ref["table"] == "orders":
            ref["expose_column_groups"] = ["money"]
    sa_file.write_text(yaml.safe_dump(sa, sort_keys=False))
    for args in ({"dataset_names": ["orders"]}, {"mode": "summary"}):
        reply = _reply(tmp_path, monkeypatch, **args)
        assert "order_date" not in _posts(reply) and "order_date" not in _full(reply), args
    reply = _reply(tmp_path, monkeypatch, dataset_names=["orders"])
    if "order_status" in _posts(reply):
        assert _full(_follow(tmp_path, monkeypatch, reply, "order_status")) == ["order_status"]
    else:
        assert "order_status" in _full(reply)


# --- Copilot's third review on #484 -------------------------------------------------------------


def test_a_group_emptied_in_one_copy_but_served_by_another_keeps_its_entry(tmp_path):
    """Two areas may each define `orders`; a group is removed only when no copy still serves it."""
    import shutil

    from semantic_model import curate as C

    root = _build(tmp_path, [_entry("order_date", column_groups=["orders.dates"])], GROUPS)
    second = root / "subject_areas" / "shop_copy"
    shutil.copytree(root / "subject_areas" / "shop", second)
    sa = yaml.safe_load((second / "subject_area.yaml").read_text())
    sa["name"] = "shop_copy"
    sa["tables"] = [r for r in sa["tables"] if r["table"] == "orders"]
    (second / "subject_area.yaml").write_text(yaml.safe_dump(sa, sort_keys=False))
    (second / "tables" / "customers.yaml").unlink()
    ds = yaml.safe_load((root / "datasource.yaml").read_text())
    ds["subject_areas"].append("subject_areas/shop_copy")
    (root / "datasource.yaml").write_text(yaml.safe_dump(ds, sort_keys=False))
    ops = [
        {"op": "exclude", "kind": "table", "area": "shop", "name": "orders", "column": c}
        for c in GROUPS["dates"]
    ]
    assert not C.apply(root, ops).errors
    assert [a.name for a in load_datasource(root).ambiguities] == ["order_date"]


def test_a_column_in_two_groups_opens_through_the_one_its_area_exposes(tmp_path, monkeypatch):
    groups = {"internal": ["status"], "public": ["status", "amount"], "dates": GROUPS["dates"]}
    root = _build(tmp_path, [_entry("order_status", columns=["orders.status"])], groups)
    (sa_file,) = root.rglob("subject_area.yaml")
    sa = yaml.safe_load(sa_file.read_text())
    for ref in sa["tables"]:
        if ref["table"] == "orders":
            ref["expose_column_groups"] = ["public", "dates"]
    sa_file.write_text(yaml.safe_dump(sa, sort_keys=False))
    overview = _reply(tmp_path, monkeypatch, mode="summary")
    assert _posts(overview)["order_status"] == {
        "dataset_names": ["orders"],
        "column_groups": ["orders.public"],
    }
    assert _full(_follow(tmp_path, monkeypatch, overview, "order_status")) == ["order_status"]


def test_a_join_from_a_same_named_table_in_another_schema_stays_whole():
    edge = {
        "from_table": "orders",
        "from_schema": "archive",
        "to_table": "ledger",
        "to_schema": "public",
        "from_column": "id",
        "to_column": "order_id",
        "relationship": "one_to_many",
    }
    kept, other = tools._compact_other_joins([edge], {"orders"}, {"orders": "public"})
    assert kept == [edge] and other == []


# --- Copilot's fourth review on #484 ------------------------------------------------------------


def test_an_empty_condition_beside_keys_is_a_key_join():
    edge = {
        "from_table": "orders",
        "to_table": "dates",
        "from_column": "ordered_on",
        "to_column": "date_key",
        "on": "",
        "relationship": "many_to_one",
    }
    (entry,) = tools._compact_other_joins([edge], {"orders"}, {})[1]
    assert entry["keys"] == ["ordered_on=date_key"] and "on" not in entry


def _restrict(root, area_file, groups):
    sa = yaml.safe_load(area_file.read_text())
    for ref in sa["tables"]:
        if ref["table"] == "orders":
            ref["expose_column_groups"] = groups
    area_file.write_text(yaml.safe_dump(sa, sort_keys=False))


def test_a_column_in_no_group_is_hidden_by_a_restriction_too(tmp_path, monkeypatch):
    """Under `expose_column_groups` the loader serves only the exposed groups' columns."""
    root = _build(tmp_path, [_entry("order_id", columns=["orders.id"])], GROUPS)
    (sa_file,) = root.rglob("subject_area.yaml")
    _restrict(root, sa_file, ["money"])
    for args in ({"dataset_names": ["orders"]}, {"mode": "summary"}):
        reply = _reply(tmp_path, monkeypatch, **args)
        assert "order_id" not in _posts(reply) and "order_id" not in _full(reply), args


def test_the_copy_a_table_request_returns_decides_what_is_exposed(tmp_path, monkeypatch):
    """A later area's unrestricted copy is the one served, so an earlier copy's restriction must
    not hide an anchor that the served copy shows."""
    import shutil

    root = _build(tmp_path, [_entry("order_date", column_groups=["orders.dates"])], GROUPS)
    first = root / "subject_areas" / "shop"
    _restrict(root, first / "subject_area.yaml", ["money"])
    second = root / "subject_areas" / "shop_copy"
    shutil.copytree(first, second)
    sa = yaml.safe_load((second / "subject_area.yaml").read_text())
    sa["name"] = "shop_copy"
    sa["tables"] = [
        {k: v for k, v in r.items() if k != "expose_column_groups"}
        for r in sa["tables"]
        if r["table"] == "orders"
    ]
    (second / "subject_area.yaml").write_text(yaml.safe_dump(sa, sort_keys=False))
    (second / "tables" / "customers.yaml").unlink()
    ds = yaml.safe_load((root / "datasource.yaml").read_text())
    ds["subject_areas"].append("subject_areas/shop_copy")
    (root / "datasource.yaml").write_text(yaml.safe_dump(ds, sort_keys=False))
    reply = _reply(tmp_path, monkeypatch, dataset_names=["orders"])
    assert "order_date" in _posts(reply), "the served copy shows `dates`, so the entry is reachable"
    assert _full(_follow(tmp_path, monkeypatch, reply, "order_date")) == ["order_date"]
