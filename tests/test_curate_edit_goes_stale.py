"""#537: editing an approved metric, entity or join sends it back to review.

An edit that changes what an approved item computes or connects used to keep `review_state:
approved` with the old signer, so a receipt reported a definition nobody approved as signed off. It
now sets the item `stale`; only the cosmetic fields keep the approval, and the review surfaces show
what changed since the approval from the curation log.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("pydantic")
pytest.importorskip("sqlglot")
yaml = pytest.importorskip("yaml")

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agami" / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from semantic_model import cli, curate  # noqa: E402
from semantic_model import loader as L  # noqa: E402
from semantic_model import runtime as rt  # noqa: E402

from test_model_explorer_bulk_reject import DOM, NODE, needs_node  # noqa: E402

SIGNED = {"review_state": "approved", "signed_off_by": "you@example.com",
          "signed_off_role": "data_lead", "signed_off_at": "2026-01-01T00:00:00Z"}


def _write_model(root: Path) -> None:
    """Area `sales`: orders -> customers, an approved metric, entity and join."""
    sa = root / "subject_areas" / "sales"
    (root / "datasources" / "c").mkdir(parents=True)
    for d in ("tables", "metrics", "entities"):
        (sa / d).mkdir(parents=True)
    (root / "datasource.yaml").write_text(yaml.safe_dump({
        "datasource": "demo", "version": 1,
        "storage_connections": [{"name": "c", "ref": "datasources/c/storage.yaml"}],
        "subject_areas": ["subject_areas/sales"]}))
    (root / "datasources" / "c" / "storage.yaml").write_text(
        yaml.safe_dump({"name": "c", "storage_type": "SQLite"}))
    (sa / "subject_area.yaml").write_text(yaml.safe_dump({
        "name": "sales",
        "tables": [{"storage_connection": "c", "table": "orders"},
                   {"storage_connection": "c", "table": "customers"}]}))
    (sa / "tables" / "orders.yaml").write_text(yaml.safe_dump({
        "name": "orders", "storage_connection": "c", "grain": ["id"], "description": "orders",
        "columns": [{"name": "id", "type": "integer", "primary_key": True},
                    {"name": "customer_id", "type": "integer"},
                    {"name": "buyer_id", "type": "integer"},
                    {"name": "amount", "type": "decimal"},
                    {"name": "discount", "type": "decimal"}]}))
    (sa / "tables" / "customers.yaml").write_text(yaml.safe_dump({
        "name": "customers", "storage_connection": "c", "grain": ["id"], "description": "customers",
        "columns": [{"name": "id", "type": "integer", "primary_key": True},
                    {"name": "email", "type": "string"}]}))
    (sa / "metrics" / "revenue.yaml").write_text(yaml.safe_dump({
        "name": "revenue", "calculation": "total order amount",
        "bindings": {"SQLite": "SUM(orders.amount)"}, "source_tables": ["orders"],
        "confidence": "proposed", **SIGNED}))
    (sa / "entities" / "customer.yaml").write_text(yaml.safe_dump({
        "name": "customer", "description": "a buyer",
        "maps_to": [{"table": "customers", "column": "id", "primary": True}], **SIGNED}))
    (sa / "relationships.yaml").write_text(yaml.safe_dump({"relationships": [{
        "from_table": "orders", "from_column": "customer_id", "to_table": "customers",
        "to_column": "id", "relationship": "many_to_one", "confidence": "confirmed", **SIGNED}]}))


@pytest.fixture
def root(tmp_path: Path) -> Path:
    r = tmp_path / "demo"
    _write_model(r)
    return r


def _edit(kind: str, name: str, field: str, value, **extra) -> dict:
    return {"op": "edit", "kind": kind, "area": "sales", "name": name, "field": field,
            "value": value, **extra}


def _approve(kind: str, name: str, at: str = "2026-10-27T10:00:00Z", **extra) -> dict:
    return {"op": "approve", "kind": kind, "area": "sales", "name": name, "at": at, **extra}


BINDING = _edit("metric", "revenue", "bindings", {"SQLite": "SUM(orders.amount) - SUM(orders.discount)"})
MAPS = _edit("entity", "customer", "maps_to", [{"table": "customers", "column": "email", "primary": True}])
CARDINALITY = _edit("relationship", "orders->customers", "relationship", "one_to_one")


def _state(root: Path, kind: str):
    org = L.load_datasource(root, include_rejected=True)
    sa = org.subject_areas[0]
    return {"metric": sa.metrics, "entity": sa.entities, "relationship": sa.relationships}[kind][0]


def _set_state(root: Path, kind: str, **fields) -> None:
    sa = root / "subject_areas" / "sales"
    if kind == "relationship":
        path = sa / "relationships.yaml"
        doc = yaml.safe_load(path.read_text())
        for k in ("signed_off_by", "signed_off_role", "signed_off_at"):
            doc["relationships"][0].pop(k, None)
        doc["relationships"][0].update(fields)
    else:
        path = sa / {"metric": "metrics/revenue.yaml", "entity": "entities/customer.yaml"}[kind]
        doc = yaml.safe_load(path.read_text())
        for k in ("signed_off_by", "signed_off_role", "signed_off_at"):
            doc.pop(k, None)
        doc.update(fields)
    path.write_text(yaml.safe_dump(doc))


# --- the rule ----------------------------------------------------------------------------------


@pytest.mark.parametrize("op, kind", [
    (BINDING, "metric"),
    (_edit("metric", "revenue", "calculation", "order amount net of discounts"), "metric"),
    (_edit("metric", "revenue", "unit", "USD"), "metric"),
    (_edit("metric", "revenue", "source_tables", ["orders", "customers"]), "metric"),
    (MAPS, "entity"),
    (_edit("entity", "customer", "value_pattern", "^[0-9]+$"), "entity"),
    (_edit("entity", "customer", "value_format_hint", "digits only"), "entity"),
    (CARDINALITY, "relationship"),
    (_edit("relationship", "orders->customers", "from_column", "buyer_id"), "relationship"),
    (_edit("relationship", "orders->customers", "join_type", "INNER"), "relationship"),
], ids=lambda v: v if isinstance(v, str) else v["field"])
def test_an_edit_of_what_an_approved_item_computes_or_connects_makes_it_stale(root, op, kind):
    res = curate.apply(root, [op], signer="editor@example.com", role="analyst")
    assert res.validated, res.errors
    item = _state(root, kind)
    assert item.review_state == "stale"
    # the stamp stays as the record of the approval the edit took away
    assert (item.signed_off_by, item.signed_off_at) == ("you@example.com", "2026-01-01T00:00:00Z")
    assert len(res.stale) == 1 and res.as_dict()["stale"] == res.stale
    queue = curate.review_queue(L.load_datasource(root))
    assert any(it["kind"] == kind and it["review_state"] == "stale"
               for it in queue["rule_1"] + queue["rule_2"])


@pytest.mark.parametrize("op, kind", [
    (_edit("metric", "revenue", "description", "gross, before refunds"), "metric"),
    (_edit("metric", "revenue", "other_names", ["sales", "gmv"]), "metric"),
    (_edit("entity", "customer", "description", "someone who bought"), "entity"),
    (_edit("entity", "customer", "other_names", ["client"]), "entity"),
    (_edit("entity", "customer", "caveats", ["test accounts included"]), "entity"),
    (_edit("relationship", "orders->customers", "description", "who placed the order"), "relationship"),
    (_edit("metric", "revenue", "business_question", "How much did we sell?"), "metric"),
    (_edit("entity", "customer", "plural", "clients"), "entity"),
    (_edit("relationship", "orders->customers", "for_questions_about", ["who ordered"]), "relationship"),
], ids=lambda v: v if isinstance(v, str) else f"{v['kind']}-{v['field']}")
def test_a_cosmetic_edit_keeps_the_approval(root, op, kind):
    res = curate.apply(root, [op])
    assert res.validated, res.errors
    item = _state(root, kind)
    assert (item.review_state, item.signed_off_by) == ("approved", "you@example.com")
    assert "stale" not in res.as_dict()


@pytest.mark.parametrize("op, kind", [
    (_edit("metric", "revenue", "bindings", {"SQLite": "SUM(orders.amount)"}), "metric"),
    (_edit("relationship", "orders->customers", "relationship", "many_to_one"), "relationship"),
    # the same mapping, with the default `primary` spelled differently
    (_edit("entity", "customer", "maps_to", [{"table": "customers", "column": "id", "primary": True}]),
     "entity"),
])
def test_an_edit_to_the_current_value_changes_nothing(root, op, kind):
    assert curate.apply(root, [op]).validated
    assert _state(root, kind).review_state == "approved"


@pytest.mark.parametrize("state", ["unreviewed", "rejected"])
@pytest.mark.parametrize("op, kind", [(BINDING, "metric"), (MAPS, "entity"), (CARDINALITY, "relationship")],
                         ids=["metric", "entity", "relationship"])
def test_an_edit_of_an_item_not_approved_keeps_its_state(root, op, kind, state):
    _set_state(root, kind, review_state=state)
    res = curate.apply(root, [op])
    assert res.validated, res.errors
    assert _state(root, kind).review_state == state
    assert not res.stale


def test_a_second_edit_of_a_stale_item_keeps_it_stale(root):
    assert curate.apply(root, [BINDING]).validated
    res = curate.apply(root, [_edit("metric", "revenue", "unit", "USD")])
    assert res.validated and not res.stale
    assert _state(root, "metric").review_state == "stale"


# --- batch order and re-approval ---------------------------------------------------------------


@pytest.mark.parametrize("op, kind, approve", [
    (BINDING, "metric", _approve("metric", "revenue")),
    (MAPS, "entity", _approve("entity", "customer")),
    (CARDINALITY, "relationship", _approve("relationship", "orders->customers")),
], ids=["metric", "entity", "relationship"])
def test_an_approve_after_the_edit_in_one_batch_signs_off_the_edited_version(root, op, kind, approve):
    res = curate.apply(root, [op, approve], signer="another@example.com", role="owner")
    assert res.validated, res.errors
    item = _state(root, kind)
    assert (item.review_state, item.signed_off_by, item.signed_off_role, item.signed_off_at) == \
        ("approved", "another@example.com", "owner", "2026-10-27T10:00:00Z")
    assert not res.stale


def test_an_edit_after_the_approve_in_one_batch_leaves_the_item_stale(root):
    res = curate.apply(root, [_approve("metric", "revenue"), BINDING],
                       signer="another@example.com", role="owner")
    assert res.validated, res.errors
    assert _state(root, "metric").review_state == "stale"
    assert res.stale == ["metric sales/revenue"]


def test_re_approving_a_stale_item_records_the_new_signer_and_time(root):
    assert curate.apply(root, [CARDINALITY]).validated
    res = curate.apply(root, [_approve("relationship", "orders->customers", "2026-10-28T09:00:00Z")],
                       signer="another@example.com", role="owner")
    assert res.validated, res.errors
    rel = _state(root, "relationship")
    assert (rel.review_state, rel.signed_off_by, rel.signed_off_role, rel.signed_off_at) == \
        ("approved", "another@example.com", "owner", "2026-10-28T09:00:00Z")
    assert rel.relationship == "one_to_one"


def test_an_approve_with_no_signer_cannot_reuse_the_lost_approval(root):
    assert curate.apply(root, [BINDING]).validated
    res = curate.apply(root, [_approve("metric", "revenue")])
    assert not res.applied and "needs a signer" in res.skipped[0]["reason"]
    mm = _state(root, "metric")
    assert (mm.review_state, mm.signed_off_by) == ("stale", "you@example.com")


def test_approve_queue_re_approves_a_stale_item_as_the_new_signer(root, capsys):
    assert curate.apply(root, [BINDING], signer="editor@example.com", role="analyst").validated
    assert cli.main(["approve-queue", str(root), "--signer", "another@example.com",
                     "--role", "owner"]) == 0
    out = json.loads(capsys.readouterr().out)
    (changed,) = out["changed_since_approval"]
    assert changed["name"] == "revenue" and changed["signed_off_by"] == "you@example.com"
    assert changed["changes"] == [
        "binding (SQLite) changed from SUM(orders.amount) to SUM(orders.amount) - SUM(orders.discount)"]
    mm = _state(root, "metric")
    assert (mm.review_state, mm.signed_off_by) == ("approved", "another@example.com")


# --- what changed since the approval -----------------------------------------------------------


def test_the_review_queue_shows_what_changed_since_the_approval(root):
    assert curate.apply(root, [_approve("metric", "revenue"), BINDING,
                               _edit("metric", "revenue", "description", "net")],
                        signer="editor@example.com", role="analyst").validated
    assert curate.apply(root, [_edit("metric", "revenue", "unit", "USD")],
                        signer="other@example.com", role="analyst").validated
    (item,) = curate.review_queue(L.load_datasource(root), root=root)["rule_1"]
    since = item["changes_since_approval"]
    assert since["recorded"] is True
    assert [c["text"] for c in since["changes"]] == [
        "binding (SQLite) changed from SUM(orders.amount) to SUM(orders.amount) - SUM(orders.discount)",
        "unit changed from (none) to USD"]
    assert [c["by"] for c in since["changes"]] == ["editor@example.com", "other@example.com"]
    assert all(c["at"] for c in since["changes"])
    assert "; ".join(c["text"] for c in since["changes"]) == since["summary"]


def test_a_join_whose_columns_changed_is_followed_back_through_the_log(root):
    ops = [_edit("relationship", "orders->customers", "from_column", "buyer_id",
                 from_column="customer_id", to_column="id"),
           _edit("relationship", "orders->customers", "relationship", "one_to_one",
                 from_column="buyer_id", to_column="id")]
    assert curate.apply(root, ops).validated
    (item,) = curate.review_queue(L.load_datasource(root), root=root)["rule_2"][:1]
    assert item["kind"] == "relationship"
    assert [c["text"] for c in item["changes_since_approval"]["changes"]] == [
        "from column changed from customer_id to buyer_id",
        "cardinality changed from many_to_one to one_to_one"]


def test_all_items_shows_the_changes_too(root):
    assert curate.apply(root, [MAPS]).validated
    items = curate.all_items(L.load_datasource(root), root=root)
    (ent,) = [it for it in items if it["kind"] == "entity"]
    assert ent["tab"] == "review"
    assert ent["changes_since_approval"]["changes"][0]["text"] == \
        "maps to changed from customers.id to customers.email"


@pytest.mark.parametrize("damage", ["missing", "trimmed", "garbled"])
def test_a_log_that_does_not_reach_the_approval_says_changes_not_recorded(root, damage):
    assert curate.apply(root, [CARDINALITY]).validated
    log = root / curate.CURATION_LOG
    if damage == "missing":
        log.unlink()
    elif damage == "trimmed":
        log.write_text("")
    else:
        log.write_bytes(b"\xff not json\n")
    (item,) = curate.review_queue(L.load_datasource(root), root=root)["rule_2"]
    assert item["review_state"] == "stale"
    assert item["changes_since_approval"] == {"recorded": False, "changes": [],
                                              "summary": "changes not recorded"}


def test_a_hand_edited_stale_item_says_changes_not_recorded(root):
    _set_state(root, "metric", review_state="stale")
    (item,) = curate.review_queue(L.load_datasource(root), root=root)["rule_1"]
    assert item["changes_since_approval"]["summary"] == "changes not recorded"


def test_a_re_approval_ends_the_changes_shown(root):
    assert curate.apply(root, [BINDING]).validated
    assert curate.apply(root, [_approve("metric", "revenue")], signer="a@example.com", role="owner").validated
    assert curate.apply(root, [_edit("metric", "revenue", "unit", "USD")]).validated
    (item,) = curate.review_queue(L.load_datasource(root), root=root)["rule_1"]
    assert [c["text"] for c in item["changes_since_approval"]["changes"]] == ["unit changed from (none) to USD"]


def test_without_a_root_the_queue_is_as_before(root):
    assert curate.apply(root, [BINDING]).validated
    (item,) = curate.review_queue(L.load_datasource(root))["rule_1"]
    assert "changes_since_approval" not in item


# --- receipts ----------------------------------------------------------------------------------


def test_a_receipt_reports_a_stale_metric_and_join_as_not_signed_off(root):
    sql = ("SELECT SUM(orders.amount) - SUM(orders.discount) AS revenue FROM orders "
           "JOIN customers ON orders.customer_id = customers.id")
    edit_binding = _edit("metric", "revenue", "bindings",
                         {"SQLite": "SUM(orders.amount) - SUM(orders.discount)"})
    assert curate.apply(root, [edit_binding, CARDINALITY]).validated

    def trust(receipt: dict) -> tuple:
        (join,) = receipt["joins"]["items"]
        metrics = [c for c in receipt["columns"]["items"] if c["kind"] == "output" and c.get("name")]
        return join["review_state"], [m["review_state"] for m in metrics]

    assert trust(rt.assemble_receipt(L.load_datasource(root), sql)) == ("stale", ["stale"])
    assert curate.apply(root, [_approve("metric", "revenue"), _approve("relationship", "orders->customers")],
                        signer="another@example.com", role="owner").validated
    assert trust(rt.assemble_receipt(L.load_datasource(root), sql)) == ("approved", ["approved"])


# --- the explorer ------------------------------------------------------------------------------


def _page(root: Path, js: str) -> dict:
    from render_model_explorer import build_manifest, render
    html = render(title="t", profile="demo", manifest=build_manifest(root, "demo"))
    script = html.rsplit("<script>", 1)[1].split("</script>", 1)[0]
    proc = subprocess.run([NODE, "-"], input=DOM + script + "\n" + js, capture_output=True, text=True,
                          timeout=60)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_the_explorer_manifest_carries_the_changes(root):
    from render_model_explorer import build_manifest
    assert curate.apply(root, [BINDING, CARDINALITY]).validated
    man = build_manifest(root, "demo")
    assert man["metrics"][0]["changes_since_approval"]["changes"][0]["text"].startswith(
        "binding (SQLite) changed from SUM(orders.amount)")
    assert man["relationships"][0]["changes_since_approval"]["summary"] == \
        "cardinality changed from many_to_one to one_to_one"
    assert "changes_since_approval" not in man["entities"][0]


@needs_node
def test_the_review_tab_lists_a_stale_item_with_what_changed(root):
    assert curate.apply(root, [BINDING], signer="editor@example.com", role="analyst").validated
    (root / curate.CURATION_LOG).rename(root / "kept.jsonl")
    assert curate.apply(root, [CARDINALITY]).validated
    (root / curate.CURATION_LOG).unlink()
    (root / "kept.jsonl").rename(root / curate.CURATION_LOG)
    out = _page(root, """
      activeTab = 'review'; reviewSubTab = 'eyes'; renderAll();
      console.log(JSON.stringify(document.getElementById('tab-content').textContent));""")
    assert "Changed since it was approved (approved by you@example.com)" in out
    assert "binding (SQLite) changed from SUM(orders.amount) to SUM(orders.amount) - SUM(orders.discount)" \
        " — editor@example.com" in out
    assert "changes not recorded" in out   # the join, whose edit the log no longer holds


@needs_node
def test_the_explorer_sends_approvals_after_edits(root):
    _set_state(root, "metric", review_state="unreviewed")
    out = _page(root, """
      activeTab = 'review'; reviewSubTab = 'eyes'; renderAll(); signOff.email = 'you@example.com';
      const m = manifest.metrics[0];
      toggleApprove('metric', m);
      edits[m.qname] = { kind: 'metric', area: m.area, name: m.name, fields: { unit: 'USD' } };
      console.log(JSON.stringify({ block: feedback() }));""")
    block = out["block"]
    ops = json.loads(block.split("curate-ops:\n", 1)[1].split("\n", 1)[0])
    assert [o["op"] for o in ops] == ["edit", "approve"]
