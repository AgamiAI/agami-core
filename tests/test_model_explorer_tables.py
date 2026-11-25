"""The model explorer's spreadsheet tables: Metrics, Joins, Cross datasource and Review as one row per
item, with the full card under its row, an approval tag saying who signed it off and when, ticks with
"Approve selected / Reject selected", and the organization's cross-datasource joins.

The page tests run the real rendered script under Node with the small DOM the bulk-reject tests use."""

from __future__ import annotations

import datetime
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

import render_model_explorer as R  # noqa: E402
from _read_only_view import CHANGE_CONTROLS  # noqa: E402
from render_model_explorer import build_manifest, render, signoff_date  # noqa: E402

from test_model_explorer_bulk_reject import DOM, NODE, _write_model, needs_node  # noqa: E402

SIGNER = "you@example.com"


def _bridge(from_ds: str, to_ds: str, **extra) -> dict:
    return {
        "from_datasource": from_ds,
        "from_dataset": f"public.{from_ds}_keys",
        "from_columns": ["id"],
        "to_datasource": to_ds,
        "to_dataset": f"public.{to_ds}_keys",
        "to_columns": ["key_id"],
        "relationship": "many_to_one",
        "description": f"{from_ds} keys are {to_ds} keys.",
        "confidence": "confirmed",
        **extra,
    }


@pytest.fixture
def model(tmp_path: Path) -> Path:
    """Area `sales` (three unreviewed metrics, one approved, one rejected; one join agami signed off
    itself), and an organization.yaml one level up holding two links that touch `demo` and one that
    does not."""
    root = tmp_path / "demo"
    _write_model(root)
    (root / "subject_areas" / "sales" / "relationships.yaml").write_text(
        yaml.safe_dump(
            {
                "relationships": [
                    {
                        "from_table": "orders",
                        "from_column": "id",
                        "to_table": "customers",
                        "to_column": "id",
                        "relationship": "many_to_one",
                        "review_state": "approved",
                        "confidence": "confirmed",
                        "signed_off_by": "agami_introspect",
                        "signed_off_role": "system",
                        "signed_off_at": "2026-01-05T00:00:00Z",
                    }
                ]
            }
        )
    )
    (tmp_path / "organization.yaml").write_text(
        yaml.safe_dump(
            {
                "org_id": "acme",
                "datasources": ["demo", "crm", "erp"],
                "cross_datasource_relationships": [
                    _bridge(
                        "demo",
                        "crm",
                        review_state="approved",
                        signed_off_by=SIGNER,
                        signed_off_role="data_lead",
                        signed_off_at="2026-02-03T00:00:00Z",
                    ),
                    _bridge("erp", "demo"),
                    _bridge(
                        "crm",
                        "erp",
                        review_state="approved",
                        signed_off_by=SIGNER,
                        signed_off_role="data_lead",
                        signed_off_at="2026-03-09T00:00:00Z",
                    ),
                ],
            }
        )
    )
    return root


def _run_page(model: Path, js: str, read_only: bool = False) -> dict:
    html = render(title="t", profile="demo", manifest=build_manifest(model, "demo"))
    if read_only:  # what make_read_only marks; the page reads it at load
        js_dom = (
            DOM + "document.body = { classList: { contains: c => c === 'agami-read-only' } };\n"
        )
    else:
        js_dom = DOM
    script = html.rsplit("<script>", 1)[1].split("</script>", 1)[0]
    proc = subprocess.run(
        [NODE, "-"], input=js_dom + script + "\n" + js, capture_output=True, text=True, timeout=60
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


# Helpers the page tests run inside the page: the drawn table's header and rows, a tab's count.
GRID = r"""
function walkAll(id) { return document.getElementById(id).walk([]); }
function grid() { return walkAll('tab-content').find(n => n.tagName === 'TABLE'); }
function heads() { return grid().walk([]).filter(n => n.tagName === 'TH').map(n => n.textContent); }
function rows() { return grid().walk([]).filter(n => n.tagName === 'TR' && (n.attrs.class || '').split(' ').includes('row')); }
function rowOf(q) { return rows().find(r => r.attrs['data-qname'] === q); }
function ticks() { return grid().walk([]).filter(n => n.tagName === 'INPUT' && (n.attrs.class || '') === 'row-select'); }
function tabCountNode(label) {
  const b = walkAll('tab-bar').find(n => n.tagName === 'BUTTON' && n._text === '' && n.children[0] && n.children[0]._text === label);
  return b.children.find(c => (c.attrs.class || '').includes('tab-count'));
}
function selectBarButtons() { return walkAll('select-bar').filter(n => n.tagName === 'BUTTON'); }
"""


# --- the manifest --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw, shown",
    [
        ("2026-11-12T00:00:00Z", "12 Nov 2026"),
        ("2026-11-12T09:30:00+05:30", "12 Nov 2026"),
        ("2026-02-03", "3 Feb 2026"),
        (datetime.date(2026, 4, 1), "1 Apr 2026"),  # YAML loads an unquoted date as a date
        ("t", ""),
        ("sometime last week", ""),
        ("", ""),
        (None, ""),
    ],
)
def test_signoff_date_formats_or_falls_back_to_nothing(raw, shown):
    assert signoff_date(raw) == shown


def test_manifest_carries_when_each_item_was_signed_off(model):
    m = build_manifest(model, "demo")
    signed = next(x for x in m["metrics"] if x["name"] == "signed_orders")
    assert (
        signed["signed_off_at"] == "2026-01-01T00:00:00Z"
        and signed["signed_off_date"] == "1 Jan 2026"
    )
    assert signed["signed_off_by"] == SIGNER and signed["signed_off_role"] == "data_lead"
    unsigned = next(x for x in m["metrics"] if x["name"] == "order_rows")
    assert unsigned["signed_off_at"] is None and unsigned["signed_off_date"] == ""
    (join,) = m["relationships"]
    assert join["signed_off_role"] == "system" and join["signed_off_date"] == "5 Jan 2026"


def test_manifest_lists_only_the_cross_datasource_joins_touching_this_datasource(model):
    m = build_manifest(model, "demo")
    links = m["cross_datasource_relationships"]
    assert sorted((x["from_datasource"], x["to_datasource"]) for x in links) == [
        ("demo", "crm"),
        ("erp", "demo"),
    ]
    assert m["totals"]["cross_datasource_relationships"] == 2
    out = next(x for x in links if x["from_datasource"] == "demo")
    assert out["from_dataset"] == "public.demo_keys" and out["to_columns"] == ["key_id"]
    assert out["cardinality"] == "many_to_one" and out["executable"] == "split"
    assert out["signed_off_date"] == "3 Feb 2026" and out["review_state"] == "approved"
    assert len({x["qname"] for x in links}) == 2


def test_no_organization_record_means_no_cross_datasource_joins(tmp_path):
    root = tmp_path / "demo"
    _write_model(root)
    assert build_manifest(root, "demo")["cross_datasource_relationships"] == []


def test_an_organization_record_that_does_not_load_does_not_take_the_page_down(tmp_path, capsys):
    root = tmp_path / "demo"
    _write_model(root)
    (tmp_path / "organization.yaml").write_text(
        yaml.safe_dump({"cross_datasource_relationships": "nope"})
    )
    assert build_manifest(root, "demo")["cross_datasource_relationships"] == []
    assert "organization.yaml did not load" in capsys.readouterr().err


def test_approval_summary_counts_what_is_still_in_the_model(model):
    a = build_manifest(model, "demo")["approvals"]
    assert a["metrics"] == {"approved": 1, "total": 4}  # the rejected metric is out of the model
    assert a["joins"] == {"approved": 1, "total": 1}
    assert a["cross_datasource"] == {
        "approved": 1,
        "total": 2,
    }  # the crm-erp link is not this datasource's
    # the latest approval a person made; agami's own sign-off of a declared key is not one
    assert a["last"] == {"by": SIGNER, "date": "3 Feb 2026"}


# --- the opening tab -----------------------------------------------------------------------------


def _render_main(monkeypatch, root: Path, out: Path, *extra: str) -> dict:
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "render_model_explorer.py",
            "--profile",
            root.name,
            "--artifacts-dir",
            str(root.parent),
            "--out",
            str(out),
            "--manifest-out",
            str(out) + ".json",
            *extra,
        ],
    )
    assert R.main() == 0
    return json.loads(Path(str(out) + ".json").read_text())


def test_auto_opens_on_review_when_anything_waits(model, tmp_path, monkeypatch):
    assert _render_main(monkeypatch, model, tmp_path / "a.html")["initial_tab"] == "review"


def test_auto_opens_on_datasource_when_nothing_waits(model, tmp_path, monkeypatch):
    for p in (model / "subject_areas" / "sales" / "metrics").glob("*.yaml"):
        doc = yaml.safe_load(p.read_text())
        if doc.get("review_state", "unreviewed") == "unreviewed":
            doc.update(
                review_state="approved",
                signed_off_by=SIGNER,
                signed_off_role="data_lead",
                signed_off_at="2026-01-01T00:00:00Z",
            )
            p.write_text(yaml.safe_dump(doc))
    assert _render_main(monkeypatch, model, tmp_path / "b.html")["initial_tab"] == "datasource"


@pytest.mark.parametrize("change", ["cross_area_join", "unreadable_table", "unreadable_column"])
def test_review_waits_on_whatever_the_page_counts(model, change):
    m = build_manifest(model, "demo")
    for x in m["metrics"]:
        x["review_state"] = "approved" if not x["excluded"] else x["review_state"]
    assert not R.has_pending_review(m)
    table = m["schemas"][0]["tables"][0]
    if change == "cross_area_join":
        m["cross_relationships"].append({"review_state": "unreviewed", "excluded": False})
    elif change == "unreadable_table":
        table["description_source"] = "ai_unknown"
    else:
        table["fields"][0]["description_source"] = "ai_unknown"
    assert R.has_pending_review(m)


def test_the_cross_datasource_tab_can_be_opened_by_name(model, tmp_path, monkeypatch):
    out = _render_main(monkeypatch, model, tmp_path / "c.html", "--initial-tab", "cross_datasource")
    assert out["initial_tab"] == "cross_datasource"


# --- the page ------------------------------------------------------------------------------------


@needs_node
def test_tabs_run_review_first_then_datasource_and_cross_datasource_after_joins(model):
    out = _run_page(
        model,
        "renderAll(); console.log(JSON.stringify({ ids: TABS.map(t => t.id), active: activeTab }));",
    )
    assert out["ids"] == [
        "review",
        "datasource",
        "tables",
        "pii",
        "metrics",
        "entities",
        "joins",
        "cross_datasource",
        "examples",
        "queued",
    ]
    assert out["active"] == "datasource"  # a manifest naming no opening tab


@needs_node
def test_review_count_is_red_while_anything_waits_and_grey_at_zero(model):
    out = _run_page(
        model,
        GRID
        + """
      renderAll(); const waiting = tabCountNode('Review');
      const before = { text: waiting.textContent, cls: waiting.attrs.class };
      manifest.metrics.forEach(m => { if (m.review_state === 'unreviewed') ops[m.qname] = { op: 'approve', kind: 'metric', area: m.area, name: m.name }; });
      renderAll(); const done = tabCountNode('Review');
      console.log(JSON.stringify({ before, after: { text: done.textContent, cls: done.attrs.class } }));""",
    )
    assert out["before"]["text"] == "3" and "alert" in out["before"]["cls"].split()
    assert out["after"]["text"] == "0" and "alert" not in out["after"]["cls"].split()


@needs_node
def test_metrics_render_as_a_table_with_the_approval_tag(model):
    out = _run_page(
        model,
        GRID
        + """
      activeTab = 'metrics'; renderAll();
      const signed = rowOf('sales.signed_orders');
      console.log(JSON.stringify({ heads: heads(), n: rows().length, signed: signed.textContent,
        unsigned: rowOf('sales.order_rows').textContent }));""",
    )
    assert out["heads"][1:] == [
        "Metric",
        "Area",
        "Unit",
        "Definition",
        "Calculation",
        "Status",
        "Actions",
    ]
    assert out["n"] == 5
    assert "✓ Approved" in out["signed"] and f"{SIGNER} · 1 Jan 2026" in out["signed"]
    assert "unreviewed" in out["unsigned"] and "Approve" in out["unsigned"]


@needs_node
def test_a_join_agami_signed_off_itself_reads_approved_automatically(model):
    out = _run_page(
        model,
        GRID
        + """
      activeTab = 'joins'; renderAll();
      console.log(JSON.stringify({ heads: heads(), row: rows()[0].textContent }));""",
    )
    assert out["heads"][1:] == [
        "From",
        "To",
        "Cardinality",
        "Description",
        "Confidence",
        "Status",
        "Actions",
    ]
    assert "Approved automatically" in out["row"] and "5 Jan 2026" in out["row"]
    assert "agami_introspect" not in out["row"]


@needs_node
def test_the_row_opens_the_full_card_under_it(model):
    out = _run_page(
        model,
        GRID
        + """
      activeTab = 'metrics'; renderAll();
      rowOf('sales.order_rows').walk([]).find(n => (n.attrs.class || '').startsWith('row-exp')).click();
      const detail = grid().walk([]).find(n => n.tagName === 'TR' && n.attrs.class === 'detail-row');
      console.log(JSON.stringify({ detail: detail ? detail.textContent : null }));""",
    )
    assert out["detail"] is not None
    assert "What it measures" in out["detail"] and "COUNT(*)" in out["detail"]  # the card, in full


@needs_node
def test_sorting_a_column_lists_the_rows_flat_and_a_third_click_restores_the_groups(model):
    out = _run_page(
        model,
        GRID
        + """
      activeTab = 'metrics'; renderAll();
      const names = () => rows().map(r => r.attrs['data-qname'].split('.')[1]);
      const groupRows = () => grid().walk([]).filter(n => n.tagName === 'TR' && n.attrs.class === 'group-row').length;
      const grouped = groupRows();
      cycleSort('metrics', 'name'); const asc = names(); const flat = groupRows();
      cycleSort('metrics', 'name'); const desc = names();
      cycleSort('metrics', 'name');
      console.log(JSON.stringify({ grouped, flat, asc, desc, back: groupRows() }));""",
    )
    assert out["grouped"] == 2 and out["flat"] == 0 and out["back"] == 2
    assert out["asc"] == sorted(out["asc"]) and out["desc"] == sorted(out["asc"], reverse=True)


def _tick(q: str) -> str:
    return f"rowOf('{q}').walk([]).find(n => n.attrs.class === 'row-select').click();"


def _press(label: str) -> str:
    return (
        f"walkAll('tab-content').concat(walkAll('select-bar')).filter(n => n.tagName === 'BUTTON')"
        f".find(b => b.textContent.startsWith('{label}')).click();"
    )


@needs_node
def test_one_ticked_row_queues_exactly_what_its_own_buttons_do(model):
    out = _run_page(
        model,
        GRID
        + f"""
      activeTab = 'metrics'; renderAll();
      {_tick("sales.order_rows")} {_press("Approve selected")}
      const approve = JSON.parse(JSON.stringify(ops)); Object.keys(ops).forEach(k => delete ops[k]); renderAll();
      {_tick("sales.order_rows")} {_press("Reject selected")}
      const reject = JSON.parse(JSON.stringify(ops)); Object.keys(ops).forEach(k => delete ops[k]); renderAll();
      const byRow = {{}};
      rowOf('sales.order_rows').walk([]).find(n => n.tagName === 'BUTTON' && n.textContent === 'Approve').click();
      byRow.approve = JSON.parse(JSON.stringify(ops)); Object.keys(ops).forEach(k => delete ops[k]); renderAll();
      rowOf('sales.order_rows').walk([]).find(n => n.tagName === 'BUTTON' && n.textContent === 'Reject').click();
      byRow.reject = JSON.parse(JSON.stringify(ops));
      console.log(JSON.stringify({{ approve, reject, byRow, asked, left: selected.size }}));""",
    )
    assert (
        out["approve"]
        == out["byRow"]["approve"]
        == {
            "sales.order_rows": {
                "op": "approve",
                "kind": "metric",
                "area": "sales",
                "name": "order_rows",
            }
        }
    )
    assert out["reject"] == out["byRow"]["reject"]
    assert out["asked"] == [] and out["left"] == 0  # one row: no confirm, no reason


@needs_node
def test_approve_selected_on_several_rows_is_a_bulk_sign_off(model):
    out = _run_page(
        model,
        GRID
        + f"""
      activeTab = 'metrics'; renderAll();
      ['sales.order_rows', 'sales.customer_rows', 'sales.signed_orders'].forEach(q =>
        rowOf(q).walk([]).find(n => n.attrs.class === 'row-select').click());
      {_press("Approve selected")}
      console.log(JSON.stringify({{ ops }}));""",
    )
    # the approved metric is ticked but not approved again; each op is the row's own, marked bulk (#651)
    assert out["ops"] == {
        q: {
            "op": "approve",
            "kind": "metric",
            "area": "sales",
            "name": q.split(".")[1],
            "bulk": True,
        }
        for q in ("sales.order_rows", "sales.customer_rows")
    }


@needs_node
def test_reject_selected_on_several_rows_asks_once_for_the_reason(model):
    out = _run_page(
        model,
        GRID
        + f"""
      activeTab = 'review'; reviewSubTab = 'eyes'; renderAll();
      ticks()[1].click(); ticks()[0].click();   // tick one row, then the header's "every row shown"
      const shown = rows().map(r => r.attrs['data-qname']);
      confirms.push(false); {_press("Reject selected")}
      const afterNo = Object.keys(ops).length;
      confirms.push(true); prompts.push('  not a real metric  '); {_press("Reject selected")}
      console.log(JSON.stringify({{ ops, shown, afterNo, asked }}));""",
    )
    assert len(out["shown"]) == 3 and out["afterNo"] == 0
    assert out["asked"][-2:] == [
        "Reject 3 metrics?",
        "Why are you rejecting these 3 metrics? The reason is recorded on each one.",
    ]
    assert out["ops"] == {
        q: {
            "op": "exclude",
            "kind": "metric",
            "area": "sales",
            "name": q.split(".")[1],
            "reason": "not a real metric",
        }
        for q in out["shown"]
    }


@needs_node
def test_rows_in_a_folded_group_are_never_selected(model):
    out = _run_page(
        model,
        GRID
        + f"""
      activeTab = 'metrics'; metricGroupsClosed.add('sales.orders'); renderAll();
      ticks()[0].click(); {_press("Approve selected")}
      console.log(JSON.stringify({{ drawn: rows().map(r => r.attrs['data-qname']), queued: Object.keys(ops) }}));""",
    )
    assert out["drawn"] == ["sales.customer_rows"]
    assert out["queued"] == ["sales.customer_rows"]


@needs_node
def test_review_selects_every_row_only_while_one_kind_shows_and_never_on_the_approved_pile(model):
    rel = model / "subject_areas" / "sales" / "relationships.yaml"
    doc = yaml.safe_load(rel.read_text())
    doc["relationships"].append(
        {
            "from_table": "customers",
            "from_column": "id",
            "to_table": "orders",
            "to_column": "id",
            "relationship": "one_to_many",
            "confidence": "proposed",
            "review_state": "unreviewed",
        }
    )
    rel.write_text(yaml.safe_dump(doc))
    out = _run_page(
        model,
        GRID
        + """
      const pick = () => ({ header: grid().walk([]).filter(n => n.tagName === 'TH')[0].children.length, ticks: ticks().length,
                            bar: walkAll('tab-content').some(n => (n.attrs.class || '') === 'select-bar') });
      activeTab = 'review'; reviewSubTab = 'eyes'; reviewSubTabChosen = true; renderAll(); const mixed = pick();
      reviewKind = 'Metrics'; renderAll(); const metrics = pick();
      reviewSubTab = 'approved'; reviewKind = 'all'; renderAll(); const approved = pick();
      console.log(JSON.stringify({ mixed, metrics, approved }));""",
    )
    assert out["mixed"] == {
        "header": 0,
        "ticks": 4,
        "bar": True,
    }  # metrics + a join: row ticks, no select-all
    assert out["metrics"] == {"header": 1, "ticks": 4, "bar": True}  # one kind: select-all + 3 rows
    assert out["approved"] == {"header": 0, "ticks": 0, "bar": False}


@needs_node
def test_review_lists_its_items_in_one_table_and_keeps_the_kind_bulk_buttons(model):
    out = _run_page(
        model,
        GRID
        + """
      activeTab = 'review'; reviewSubTab = 'eyes'; renderAll();
      console.log(JSON.stringify({ heads: heads(), kinds: rows().map(r => r.children[1].textContent), labels: labels() }));""",
    )
    assert out["heads"][1:] == [
        "Kind",
        "Item",
        "Area",
        "Why it's here",
        "Confidence",
        "Status",
        "Actions",
    ]
    assert out["kinds"] == ["Metric"] * 3
    assert "✓ Approve 3" in out["labels"] and "✕ Reject 3" in out["labels"]


@needs_node
def test_the_cross_datasource_tab_shows_the_links_and_changes_nothing(model):
    out = _run_page(
        model,
        GRID
        + """
      activeTab = 'cross_datasource'; renderAll();
      const text = walkAll('tab-content').map(n => n._text).join(' ');
      console.log(JSON.stringify({ heads: heads(), rows: rows().map(r => r.textContent), ticks: ticks().length,
        buttons: labels(), text, count: tabCountNode('Cross datasource').textContent }));""",
    )
    assert out["heads"][1:] == ["From", "To", "Cardinality", "Description", "Confidence", "Status"]
    assert out["count"] == "2" and len(out["rows"]) == 2
    assert any(r.startswith("▸DEMOdemo_keys.id") and "CRMcrm_keys.key_id" in r for r in out["rows"])
    # only the ▸ and "more", which open a card, and the sort headings: nothing that changes the link
    assert out["ticks"] == 0 and set(out["buttons"]) <= {
        "▸",
        "more",
        "From",
        "To",
        "Cardinality",
        "Description",
        "Confidence",
        "Status",
    }
    assert "rows are never joined across the two databases" in out["text"]


@needs_node
def test_the_approval_strip_says_how_much_is_approved_and_who_last_approved(model):
    out = _run_page(
        model,
        "renderAll(); console.log(JSON.stringify({ s: document.getElementById('approval-strip').textContent }));",
    )
    assert out["s"] == (
        "✓ 1 of 4 metrics approved·✓ 1 of 1 join approved·✓ 1 of 2 cross-datasource joins approved"
        f"·last approval {SIGNER}, 3 Feb 2026"
    )


@needs_node
def test_the_header_counts_the_same_joins_as_the_joins_tab(model):
    # A join between two subject areas is on the Joins tab and in the strip, so the header counts it too.
    out = _run_page(
        model,
        """
      const extra = { ...manifest.relationships[0], qname: 'sales~other.x', cross_area: true };
      manifest.cross_relationships.push(extra); manifest.totals.cross_relationships = 1;
      renderMeta(); renderAll();
      console.log(JSON.stringify({ meta: document.getElementById('totals-meta').textContent, tab: tabCount('joins') }));""",
    )
    assert "2 joins" in out["meta"] and out["tab"] == 2


@needs_node
def test_a_metric_and_an_entity_with_one_name_are_ticked_apart(model):
    ent = model / "subject_areas" / "sales" / "entities"
    ent.mkdir()
    (ent / "order_rows.yaml").write_text(
        yaml.safe_dump(
            {
                "name": "order_rows",
                "maps_to": [{"table": "orders", "column": "id", "primary": True}],
                "confidence": "proposed",
                "review_state": "unreviewed",
            }
        )
    )
    out = _run_page(
        model,
        GRID
        + """
      activeTab = 'review'; reviewSubTab = 'eyes'; reviewSubTabChosen = true; renderAll();
      const metric = rows().find(r => r.attrs['data-qname'] === 'sales.order_rows' && r.children[1].textContent === 'Metric');
      metric.walk([]).find(n => n.attrs.class === 'row-select').click();
      const ticked = rows().filter(r => r.walk([]).some(n => n.attrs.class === 'row-select' && n.attrs.checked))
                           .map(r => r.children[1].textContent + ' ' + r.attrs['data-qname']);
      walkAll('tab-content').filter(n => n.tagName === 'BUTTON').find(b => b.textContent.startsWith('Approve selected')).click();
      console.log(JSON.stringify({ ticked, ops }));""",
    )
    assert out["ticked"] == ["Metric sales.order_rows"]
    assert out["ops"] == {
        "sales.order_rows": {
            "op": "approve",
            "kind": "metric",
            "area": "sales",
            "name": "order_rows",
        }
    }


@needs_node
def test_sorting_is_a_button_and_the_heading_says_which_way(model):
    out = _run_page(
        model,
        GRID
        + """
      activeTab = 'metrics'; renderAll();
      const th = () => grid().walk([]).filter(n => n.tagName === 'TH').find(n => n.textContent.startsWith('Metric'));
      const before = th().attrs['aria-sort'];
      th().children.find(n => n.tagName === 'BUTTON').click();
      console.log(JSON.stringify({ before, after: th().attrs['aria-sort'], label: th().textContent }));""",
    )
    assert out == {"before": "none", "after": "ascending", "label": "Metric ▲"}


@needs_node
def test_a_read_only_page_offers_no_ticks(model):
    out = _run_page(
        model,
        GRID
        + """
      activeTab = 'metrics'; renderAll();
      console.log(JSON.stringify({ ticks: ticks().length, bar: selectBarButtons().length }));""",
        read_only=True,
    )
    assert out == {"ticks": 0, "bar": 0}


def test_read_only_hides_the_table_change_controls():
    for control in (".row-select", ".select-bar", ".col-actions", ".card-actions"):
        assert control in CHANGE_CONTROLS
