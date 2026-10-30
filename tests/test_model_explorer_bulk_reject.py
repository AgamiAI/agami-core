"""The model explorer's bulk reject (#531): a "✕ Reject" control beside every bulk approve, acting
on exactly the shown items, with one required reason that every queued reject op carries through
the feedback block, the parser and curate.apply into curation_log.jsonl.

The page tests run the real rendered script under Node with a small working DOM, click the
buttons and read the generated feedback block. Skipped where Node is not installed."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("pydantic")
pytest.importorskip("sqlglot")
yaml = pytest.importorskip("yaml")

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agami" / "scripts"))

import parse_model_feedback as F  # noqa: E402
from render_model_explorer import build_manifest, render  # noqa: E402
from semantic_model import curate  # noqa: E402

NODE = shutil.which("node")
if NODE is None and os.environ.get("CI"):
    raise RuntimeError("node is required on CI for the page tests; add a setup-node step")
needs_node = pytest.mark.skipif(NODE is None, reason="node is not installed")

REASON = "not a real metric"

# Just enough DOM for the explorer to render and its buttons to be clicked: a tree of nodes with
# text, attributes, listeners and an id registry. confirm / prompt answer from JS-set queues.
DOM = r"""
const ids = {};
class N {
  constructor(tag, text) { this.tagName = (tag || '').toUpperCase(); this.attrs = {}; this.children = []; this.parentNode = null;
    this.listeners = {}; this.style = {}; this._text = text || ''; this.value = ''; this.hidden = false; this.disabled = false;
    const self = this;
    this.classList = { add(c) { const s = new Set((self.attrs.class || '').split(' ').filter(Boolean)); s.add(c); self.attrs.class = [...s].join(' '); },
      remove(c) { self.attrs.class = (self.attrs.class || '').split(' ').filter(x => x && x !== c).join(' '); },
      contains(c) { return (self.attrs.class || '').split(' ').includes(c); }, toggle() {} }; }
  get className() { return this.attrs.class || ''; } set className(v) { this.attrs.class = v; }
  setAttribute(k, v) { this.attrs[k] = String(v); if (k === 'id') ids[v] = this; }
  getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; }
  appendChild(c) { c.parentNode = this; this.children.push(c); return c; }
  insertBefore(c, ref) { c.parentNode = this; const i = this.children.indexOf(ref); this.children.splice(i < 0 ? 0 : i, 0, c); return c; }
  get textContent() { return this._text + this.children.map(c => c.textContent).join(''); }
  set textContent(v) { this.children = []; this._text = String(v); }
  get innerHTML() { return this._html || ''; } set innerHTML(v) { this.children = []; this._text = ''; this._html = v; }
  addEventListener(t, f) { (this.listeners[t] = this.listeners[t] || []).push(f); }
  click() { (this.listeners.click || []).forEach(f => f({ stopPropagation() {}, preventDefault() {}, target: this })); }
  closest() { return null; } querySelectorAll() { return []; } focus() {} select() {} remove() {}
  walk(out) { out.push(this); this.children.forEach(c => c.walk(out)); return out; }
}
const document = { getElementById(id) { return ids[id] || (ids[id] = new N('div')); },
  createElement(t) { return new N(t); }, createTextNode(t) { return new N('#text', String(t)); },
  querySelectorAll() { return []; }, querySelector() { return null; }, addEventListener() {}, execCommand() {} };
const confirms = [], prompts = [], asked = [];
const window = { confirm(m) { asked.push(m); return confirms.shift(); }, prompt(m) { asked.push(m); return prompts.shift(); } };
const localStorage = { getItem() { return null; }, setItem() {} };
const navigator = { clipboard: { writeText() { return Promise.resolve(); } } };
function buttons() { return document.getElementById('tab-content').walk([]).filter(n => n.tagName === 'BUTTON'); }
function btn(prefix) { return buttons().find(b => b.textContent.startsWith(prefix)); }
function labels() { return buttons().map(b => b.textContent); }
function feedback() { generateFeedback(); return document.getElementById('modal-text').value; }
"""


def _metric(name: str, table: str, **extra) -> dict:
    return {"name": name, "calculation": f"count of {table}", "bindings": {"PostgreSQL": "COUNT(*)"},
            "source_tables": [table], "primary_table": table, "confidence": "proposed",
            "review_state": "unreviewed", **extra}


def _write_model(root: Path) -> None:
    """Area `sales`: orders + customers, three unreviewed metrics, one approved, one rejected."""
    sa = root / "subject_areas" / "sales"
    (root / "datasources" / "c").mkdir(parents=True)
    (sa / "tables").mkdir(parents=True)
    (sa / "metrics").mkdir(parents=True)
    (root / "datasource.yaml").write_text(yaml.safe_dump({
        "datasource": "demo", "version": 1,
        "storage_connections": [{"name": "c", "ref": "datasources/c/storage.yaml"}],
        "subject_areas": ["subject_areas/sales"]}))
    (root / "datasources" / "c" / "storage.yaml").write_text(
        yaml.safe_dump({"name": "c", "storage_type": "PostgreSQL"}))
    (sa / "subject_area.yaml").write_text(yaml.safe_dump({
        "name": "sales",
        "tables": [{"storage_connection": "c", "schema": "public", "table": "orders"},
                   {"storage_connection": "c", "schema": "public", "table": "customers"}]}))
    for t in ("orders", "customers"):
        (sa / "tables" / f"{t}.yaml").write_text(yaml.safe_dump({
            "name": t, "schema": "public", "storage_connection": "c", "grain": ["id"], "description": t,
            "columns": [{"name": "id", "type": "integer", "primary_key": True}]}))
    metrics = [_metric("order_rows", "orders"), _metric("order_ids", "orders"),
               _metric("customer_rows", "customers"),
               _metric("signed_orders", "orders", review_state="approved", signed_off_by="you@example.com",
                       signed_off_role="data_lead", signed_off_at="2026-01-01T00:00:00Z"),
               _metric("dropped_orders", "orders", review_state="rejected")]
    for m in metrics:
        (sa / "metrics" / f"{m['name']}.yaml").write_text(yaml.safe_dump(m))


@pytest.fixture
def model(tmp_path: Path) -> Path:
    root = tmp_path / "demo"
    _write_model(root)
    return root


def _run_page(model: Path, js: str) -> dict:
    html = render(title="t", profile="demo", manifest=build_manifest(model, "demo"))
    script = html.rsplit("<script>", 1)[1].split("</script>", 1)[0]
    proc = subprocess.run([NODE, "-"], input=DOM + script + "\n" + js, capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


def _ops(block: str) -> list[dict]:
    data, anomalies, needs = F.parse(block)
    assert not anomalies and needs is None
    return data["ops"]


@needs_node
def test_each_review_group_has_a_reject_beside_its_approve(model):
    out = _run_page(model, "activeTab = 'review'; reviewSubTab = 'eyes'; renderAll();"
                           "console.log(JSON.stringify(labels()));")
    assert "✓ Approve 3" in out and "✕ Reject 3" in out
    assert out.index("✕ Reject 3") == out.index("✓ Approve 3") + 1


@needs_node
def test_the_metrics_tab_has_a_reject_all_beside_approve_all(model):
    out = _run_page(model, "activeTab = 'metrics'; renderAll(); console.log(JSON.stringify(labels()));")
    # the approved and the rejected metric are not counted
    assert "✓ Approve all 3 proposed" in out and "✕ Reject all 3 proposed" in out
    assert out.index("✕ Reject all 3 proposed") == out.index("✓ Approve all 3 proposed") + 1
    assert "✓ Approve 2" in out and "✕ Reject 2" in out    # the orders group
    assert "✓ Approve 1" in out and "✕ Reject 1" in out    # the customers group


@needs_node
def test_reject_all_queues_only_the_shown_items_with_one_reason(model):
    out = _run_page(model, f"""
      activeTab = 'review'; reviewSubTab = 'eyes'; searchQ = 'order'; renderAll();
      confirms.push(true); prompts.push('  {REASON}  ');
      btn('✕ Reject').click();
      console.log(JSON.stringify({{ asked, block: feedback(), queue: reviewQueue().length }}));""")
    assert out["asked"][0] == "Reject 2 metrics?"
    ops = _ops(out["block"])
    assert sorted(o["name"] for o in ops) == ["order_ids", "order_rows"]   # customer_rows not shown
    assert all(o == {"op": "exclude", "kind": "metric", "area": "sales", "name": o["name"], "reason": REASON}
               for o in ops)
    assert out["queue"] == 1


@needs_node
def test_metrics_tab_reject_all_names_the_kind_on_each_op(model):
    out = _run_page(model, f"""
      activeTab = 'metrics'; renderAll();
      confirms.push(true); prompts.push('{REASON}');
      btn('✕ Reject all').click();
      console.log(JSON.stringify({{ asked, block: feedback() }}));""")
    assert out["asked"][0] == "Reject 3 metrics?"
    ops = _ops(out["block"])
    assert sorted(o["name"] for o in ops) == ["customer_rows", "order_ids", "order_rows"]
    assert all(o["kind"] == "metric" and o["reason"] == REASON for o in ops)


@needs_node
def test_metrics_tab_approve_all_names_the_kind_on_each_op(model):
    out = _run_page(model, """
      activeTab = 'metrics'; renderAll(); signOff.email = 'you@example.com';
      btn('✓ Approve all').click();
      console.log(JSON.stringify({ block: feedback() }));""")
    assert {o["kind"] for o in _ops(out["block"])} == {"metric"}


@needs_node
def test_a_cancel_or_a_blank_reason_queues_nothing(model):
    out = _run_page(model, """
      activeTab = 'review'; reviewSubTab = 'eyes'; renderAll();
      confirms.push(false); btn('✕ Reject').click();
      const afterNo = Object.keys(ops).length;
      confirms.push(true); prompts.push('   ', null); btn('✕ Reject').click();
      console.log(JSON.stringify({ afterNo, afterBlank: Object.keys(ops).length, asked }));""")
    assert out["afterNo"] == 0 and out["afterBlank"] == 0
    assert out["asked"][-1].startswith("A reason is required")


@needs_node
def test_a_single_reject_carries_no_reason(model):
    out = _run_page(model, """
      activeTab = 'review'; reviewSubTab = 'eyes'; renderAll();
      buttons().find(b => b.textContent === 'Reject').click();
      console.log(JSON.stringify({ block: feedback() }));""")
    (op,) = _ops(out["block"])
    assert op["op"] == "exclude" and "reason" not in op


def test_parsed_rejects_each_carry_the_reason_and_the_log_records_it(model):
    names = ["order_rows", "order_ids", "customer_rows"]
    ops_json = json.dumps([{"op": "exclude", "kind": "metric", "area": "sales", "name": n, "reason": REASON}
                           for n in names])
    before = {p: p.read_text() for p in model.rglob("*.yaml")}
    ops = _ops(f"profile: demo\ncurate-ops:\n{ops_json}\ndone\n")
    assert [o["reason"] for o in ops] == [REASON] * 3
    # parsing applies nothing: the model and the log are untouched until curate runs
    assert {p: p.read_text() for p in model.rglob("*.yaml")} == before
    assert not (model / "curation_log.jsonl").exists()

    res = curate.apply(model, ops)
    assert res.validated and len(res.applied) == 3
    for n in names:
        assert yaml.safe_load((model / "subject_areas" / "sales" / "metrics" / f"{n}.yaml").read_text())[
            "review_state"] == "rejected"
    log = [json.loads(line) for line in (model / "curation_log.jsonl").read_text().splitlines()]
    assert sorted(e["name"] for e in log) == sorted(names)
    assert all(e["reason"] == REASON for e in log)
    assert not any(REASON in p.read_text() for p in model.rglob("*.yaml"))   # the log only


def test_a_reject_without_a_reason_logs_none(model):
    res = curate.apply(model, [{"op": "exclude", "kind": "metric", "area": "sales", "name": "order_rows"}])
    assert res.validated
    (entry,) = [json.loads(line) for line in (model / "curation_log.jsonl").read_text().splitlines()]
    assert "reason" not in entry
