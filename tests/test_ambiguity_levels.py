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
        "order_date": {"column_groups": ["orders.dates"]}
    }
    opened = _reply(tmp_path, monkeypatch, dataset_names=["orders"], column_groups=["dates"])
    assert _full(opened) == ["order_date"] and "ambiguity_signposts" not in opened


def test_a_group_entry_on_a_table_not_requested_says_to_request_it(tmp_path, monkeypatch):
    _build(tmp_path, [_entry("order_date", column_groups=["orders.dates"])], GROUPS)
    reply = _reply(tmp_path, monkeypatch, dataset_names=["customers"])
    assert _full(reply) == [] and "ambiguity_signposts" not in reply, "out of this reply's scope"
    overview = _reply(tmp_path, monkeypatch)
    assert _posts(overview) == {"order_date": {"dataset_names": ["orders"]}}


def test_a_column_entry_opens_with_an_ungrouped_table(tmp_path, monkeypatch):
    """A table with no groups sends every column in full, so naming the table opens the column."""
    _build(tmp_path, [_entry("order_status", columns=["orders.status"])])
    assert _full(_reply(tmp_path, monkeypatch, dataset_names=["orders"])) == ["order_status"]


def test_a_column_entry_on_a_grouped_table_points_at_its_group(tmp_path, monkeypatch):
    _build(tmp_path, [_entry("order_status", columns=["orders.status"])], GROUPS)
    reply = _reply(tmp_path, monkeypatch, dataset_names=["orders"])
    assert _posts(reply) == {"order_status": {"column_groups": ["orders.money"]}}
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
