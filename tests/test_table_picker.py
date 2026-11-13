"""The table picker page and the answers it and its fallback list give (#654).

A 46-table schema left the connect card with "keep everything" or "type the names". The picker page
lists every table to tick; it is served by `serve_page.py --decisions` and its save is checked
against the inventory by `inventory_summary.py --keep page:` before the kept-tables file is written.
Where a page can't open, `--numbered` lists the tables in the chat and `--keep pick:` reads numbers,
ranges and patterns typed in Other.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "plugins" / "agami" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import inventory_summary as IS  # noqa: E402
import render_table_picker as RTP  # noqa: E402
import serve_page  # noqa: E402

# Names a catalog could hold that are markup, a script end, a template placeholder or an entity.
HOSTILE = [
    "<img src=x onerror=alert(1)>",
    "</script><script>alert(2)</script>",
    "{{DATA}}",
    "a&amp;b",
    "<!--x",
    "q\"uote's",
]


def _inventory(profile: str | None = "demo") -> dict:
    spec = {
        "sales": ["orders", "customers", "orders_bak"],
        "staging": ["customers"],
        "odd": HOSTILE,
    }
    tables = [
        {"schema": s, "table": t, "columns": [{"name": "id", "type": "int"}] * (i + 1)}
        for s, ts in spec.items()
        for i, t in enumerate(ts)
    ]
    tables[0]["rows"] = 1200
    return {"profile": profile, "schemas": list(spec), "tables": tables}


def _names(inv: dict) -> list[str]:
    return [IS._qualified(t) for t in inv["tables"]]


def _block(html: str) -> str:
    return re.search(
        r'<script type="application/json" id="agami-tables">(.*?)</script>', html, re.S
    ).group(1)


def _page_script(html: str) -> str:
    return re.findall(r"<script>(.*?)</script>", html, re.S)[-1]


# --- the page as rendered ------------------------------------------------------------------------


def test_the_page_carries_every_table_with_its_size_and_flag():
    inv = _inventory()
    data = json.loads(_block(RTP.render(inv)))
    assert data["profile"] == "demo"
    assert [t["name"] for t in data["tables"]] == _names(inv)
    orders = data["tables"][0]
    assert (orders["rows"], orders["columns"], orders["why"]) == (1200, 1, None)
    assert data["tables"][1]["rows"] is None  # no estimate: shown as unknown, never as 0
    flagged = {t["name"]: t["copy_of"] for t in data["tables"] if t["why"]}
    assert flagged == {"sales.orders_bak": "sales.orders", "staging.customers": "sales.customers"}
    assert data["noise_all_copies"] is True  # so the page starts with them unticked


def test_no_table_name_can_leave_the_data_block():
    html = RTP.render(_inventory())
    # the page's two scripts and nothing else: no name closed the data block or opened a script
    assert html.count("<script") == 2 and html.lower().count("</script") == 2
    assert len(serve_page.script_hashes(html)) == 2
    assert "<img" not in html
    assert not set("<>&") & set(_block(html))  # the names are in it, every bracket escaped
    outside = html.replace(_block(html), "")
    assert (
        "{{" not in outside
    )  # every placeholder filled, once: a name holding one was not filled again
    assert not re.search(
        r"<[^>]+\son[a-z]+\s*=", html, re.I
    )  # no inline handler (a hash can't cover it)
    assert [t["table"] for t in json.loads(_block(html))["tables"][4:]] == HOSTILE


def test_the_profile_is_escaped_where_it_is_markup():
    html = RTP.render(_inventory(profile="<b>x</b>"))
    assert "<b>x</b>" not in html and "&lt;b&gt;x&lt;/b&gt;" in html
    assert json.loads(_block(html))["profile"] == "<b>x</b>"


def test_the_page_loads_nothing_and_writes_names_only_as_text():
    html = RTP.render(_inventory())
    outside = html.replace(_block(html), "")  # the names are data, checked above
    assert not re.search(r"<(?:link|img|iframe)\b|\ssrc=|@import|url\(http", outside, re.I)
    assert set(re.findall(r"https?://[^\s\"'<>)]+", html)) <= {"http://www.w3.org/2000/svg"}
    script = _page_script(html)
    for sink in (
        "innerHTML",
        "outerHTML",
        "insertAdjacentHTML",
        "document.write",
        "eval(",
        "Function(",
    ):
        assert sink not in script, sink


def test_render_cli_writes_the_page_and_counts(tmp_path, capsys):
    inv = tmp_path / "inventory.json"
    inv.write_text(json.dumps(_inventory()))
    out = tmp_path / "p" / "tables.html"
    assert RTP.main(["--inventory", str(inv), "--save-to-helper", "--out", str(out)]) == 0
    said = json.loads(capsys.readouterr().out)
    assert said == {"page": str(out), "tables": 10, "flagged": 2, "unticked": 2}
    assert out.read_text().startswith("<!doctype html>")
    inv.write_text(json.dumps({"tables": []}))
    assert RTP.main(["--inventory", str(inv), "--save-to-helper", "--out", str(out)]) == 1


# --- the page's own script, under node with a small DOM ------------------------------------------

_DOM = r"""
const created = [];
function El(tag) { this.tagName = tag.toUpperCase(); this.children = []; this.listeners = {}; this.hidden = false;
  this.checked = false; this.disabled = false; this.className = ''; this._text = ''; this.attrs = {}; created.push(this); }
El.prototype.appendChild = function (c) { this.children.push(c); c.parent = this; return c; };
Object.defineProperty(El.prototype, 'textContent', {
  get() { return this._text + this.children.map(c => c.textContent).join(''); },
  set(v) { this._text = String(v); this.children = []; } });
Object.defineProperty(El.prototype, 'innerHTML', { set() { throw new Error('innerHTML'); }, get() { throw new Error('innerHTML'); } });
El.prototype.addEventListener = function (t, f) { (this.listeners[t] = this.listeners[t] || []).push(f); };
El.prototype.fire = function (t) { (this.listeners[t] || []).forEach(f => f({ target: this })); };
El.prototype.setAttribute = function (k, v) { this.attrs[k] = String(v); };
const ids = {};
['schemas', 'noise-note', 'search', 'all-shown', 'none-shown', 'no-match', 'total', 'save', 'status']
  .forEach(i => { ids[i] = new El(i === 'search' ? 'input' : 'button'); ids[i].value = ''; });
ids['agami-tables'] = { textContent: DATA };
const document = { getElementById: i => ids[i], createElement: t => new El(t),
  querySelectorAll: s => created.filter(e => e.tagName === 'BUTTON') };
const posted = [];
function fetch(url, opts) { posted.push({ url, body: JSON.parse(opts.body) });
  return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({ ok: true }) }); }
function boxes() { return created.filter(e => e.tagName === 'INPUT' && e.type === 'checkbox'); }
function nameOf(b) { return b.parent.children[1].children[0].textContent; }
function ticked() { return boxes().filter(b => b.checked).map(nameOf); }
function sectionButton(i, label) { return created.filter(e => e.tagName === 'SECTION')[i].children[0].children
  .find(c => c.tagName === 'BUTTON' && c.textContent === label); }
"""


def _node(inv: dict, steps: str) -> dict:
    node = shutil.which("node")
    if node is None:
        if os.environ.get("CI"):
            raise RuntimeError("node is required on CI for the page tests")
        pytest.skip("node not installed")
    html = RTP.render(inv)
    js = f"const DATA = {json.dumps(_block(html))};\n{_DOM}\n{_page_script(html)}\n{steps}"
    out = subprocess.run([node, "-e", js], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


_REPORT = (
    "setTimeout(() => console.log(JSON.stringify({ticked: ticked(), total: ids.total.textContent, "
    "posted, status: ids.status.textContent, note: ids['noise-note'].textContent, "
    "save: ids.save.disabled, shown: boxes().filter(b => !b.parent.parent.parent.hidden).map(nameOf)})), 20);"
)


def test_copies_start_unticked_when_each_has_an_original():
    got = _node(_inventory(), _REPORT)
    assert "orders_bak" not in got["ticked"] and len(got["ticked"]) == 8
    assert got["total"] == "Keeping 8 of 10 tables"
    assert "Each copies another table, so they start unticked." in got["note"]
    # every name, the hostile ones too, reached the page as text
    assert set(HOSTILE) <= set(got["ticked"])


def test_a_flagged_table_without_an_original_starts_ticked():
    inv = _inventory()
    inv["tables"].append({"schema": "sales", "table": "invoices_2023", "columns": []})
    got = _node(inv, _REPORT)
    assert len(got["ticked"]) == 11 and "they start ticked" in got["note"]


def test_search_then_select_none_changes_only_the_tables_shown():
    steps = (
        "ids.search.value = 'cust'; ids.search.fire('input');\n"
        "sectionButton(0, 'Select none').fire('click');\n" + _REPORT
    )
    got = _node(_inventory(), steps)
    assert got["shown"] == ["customers", "customers"]  # sales.customers and staging.customers
    # sales' "Select none" unticked the one shown there; orders, hidden by the search, stays ticked
    assert "customers" not in got["ticked"] and "orders" in got["ticked"]
    assert got["total"] == "Keeping 7 of 10 tables"


def test_select_all_shown_then_save_posts_the_names_once_in_inventory_order():
    inv = _inventory()
    steps = (
        "ids['all-shown'].fire('click'); ids.save.fire('click'); ids.save.fire('click');\n"
        + _REPORT
    )
    got = _node(inv, steps)
    assert len(got["posted"]) == 1
    assert got["posted"][0] == {
        "url": "save",
        "body": {"page": "tables", "profile": "demo", "keep": _names(inv)},
    }
    assert got["status"].startswith(
        "Saved: keeping 10 of 10 tables. Go back to the chat and press I"
    )
    assert got["save"] is True


@pytest.mark.parametrize("schema", ["constructor", "toString", "__proto__", "hasOwnProperty"])
def test_a_schema_named_like_an_object_property_still_renders_and_saves(schema):
    """Copilot on #657: a schema named `constructor` found an inherited property, skipped building
    its section and threw, so the page neither rendered nor saved."""
    inv = {
        "profile": "demo",
        "tables": [
            {"schema": schema, "table": "orders", "columns": []},
            {"schema": "sales", "table": "orders", "columns": []},
            {"schema": schema, "table": "items", "columns": []},
        ],
    }
    steps = "ids.save.fire('click');\n" + _REPORT
    got = _node(inv, steps)
    assert got["ticked"] == ["orders", "items", "orders"]  # one section per schema, in list order
    assert got["posted"][0]["body"]["keep"] == [
        f"{schema}.orders",
        f"{schema}.items",
        "sales.orders",
    ]


def test_nothing_ticked_cannot_be_saved():
    steps = "ids['none-shown'].fire('click'); ids.save.fire('click');\n" + _REPORT
    got = _node(_inventory(), steps)
    assert got["posted"] == [] and got["save"] is True
    assert got["status"] == "Pick at least one table to save."


# --- what the page saved, checked before anything is written -------------------------------------


def _keep(tmp_path: Path, inv: dict, choice: str) -> tuple[int, dict, Path]:
    path = tmp_path / "inventory.json"
    path.write_text(json.dumps(inv), encoding="utf-8")
    out = tmp_path / "agami-keep.txt"
    # In process, so the coverage gate sees the checks (the CLI is the same `main`).
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = IS.main(["--inventory", str(path), "--keep", choice, "--tables-out", str(out)])
    return code, json.loads(buf.getvalue()), out


def _saved(tmp_path: Path, doc: object) -> str:
    f = tmp_path / "agami-page.json"
    f.write_text(doc if isinstance(doc, str) else json.dumps(doc), encoding="utf-8")
    return f"page:{f}"


def test_the_page_writes_the_same_file_as_naming_the_tables(tmp_path):
    inv = _inventory()
    pick = ["odd." + HOSTILE[1], "sales.orders", "staging.customers"]
    code, out, kept = _keep(
        tmp_path, inv, _saved(tmp_path, {"page": "tables", "profile": "demo", "keep": pick})
    )
    assert code == 0 and out["kept"] == 3
    from_page = kept.read_text()
    names = tmp_path / "names.txt"
    names.write_text("sales.orders\nstaging.customers\n")
    _keep(tmp_path, inv, f"names:{names}")
    assert (
        kept.read_text().splitlines()[:2] == from_page.splitlines()[:2]
    )  # same order, same spelling
    assert from_page.splitlines() == ["sales.orders", "staging.customers", "odd." + HOSTILE[1]]


@pytest.mark.parametrize(
    ("doc", "said"),
    [
        ("{not json", "can't be read as JSON"),
        ([1, 2], "not a JSON object"),
        (
            {"page": "tables", "profile": "demo", "keep": [], "extra": 1},
            "unexpected or missing keys",
        ),
        ({"page": "tables", "keep": ["sales.orders"]}, "unexpected or missing keys"),
        ({"page": "examples", "profile": "demo", "keep": ["sales.orders"]}, "not the table picker"),
        ({"page": "tables", "profile": "other", "keep": ["sales.orders"]}, "another database"),
        ({"page": "tables", "profile": "demo", "keep": "sales.orders"}, "not a list"),
        ({"page": "tables", "profile": "demo", "keep": ["sales.orders", 3]}, "not a list"),
    ],
)
def test_a_decisions_file_that_is_not_the_pickers_is_refused_and_nothing_written(
    tmp_path, doc, said
):
    code, out, kept = _keep(tmp_path, _inventory(), _saved(tmp_path, doc))
    assert code == 1 and said in out["error"] and not kept.exists()


def test_a_name_the_inventory_does_not_hold_is_refused(tmp_path):
    doc = {
        "page": "tables",
        "profile": "demo",
        "keep": ["sales.orders", "<svg onload=x>", "SALES.ORDERS"],
    }
    code, out, kept = _keep(tmp_path, _inventory(), _saved(tmp_path, doc))
    # exact names only: the page sends the inventory's own spelling, so a case change is not one
    assert (
        code == 1 and out == {"unmatched": ["<svg onload=x>", "SALES.ORDERS"]} and not kept.exists()
    )


def test_an_empty_pick_keeps_nothing_and_writes_nothing(tmp_path):
    code, out, kept = _keep(
        tmp_path, _inventory(), _saved(tmp_path, {"page": "tables", "profile": "demo", "keep": []})
    )
    assert code == 1 and out == {"error": "no tables kept"} and not kept.exists()


def test_the_page_saved_through_the_helper_lands_in_the_kept_tables_file(tmp_path):
    """The whole path in one process: rendered page, helper, the page's own post, then --keep page:."""
    inv = _inventory()
    page = tmp_path / "tables.html"
    page.write_text(RTP.render(inv), encoding="utf-8")
    decisions = tmp_path / "agami-page-1.json"
    server = serve_page._Server(page.resolve(), decisions)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{server.port}{server.base}"
        with urllib.request.urlopen(url, timeout=5) as r:
            served = r.read().decode()
            csp = r.headers["Content-Security-Policy"]
        for h in serve_page.script_hashes(served):
            assert h in csp
        body = json.dumps(
            {"page": "tables", "profile": "demo", "keep": ["sales.customers"]}
        ).encode()

        def post(origin: str) -> int:
            req = urllib.request.Request(
                url + "save",
                data=body,
                method="POST",
                headers={"Content-Type": "application/json", "Origin": origin},
            )
            try:
                with urllib.request.urlopen(req, timeout=5) as r:
                    return r.status
            except urllib.error.HTTPError as e:
                return e.code

        assert post("http://evil.example") == 403 and not decisions.exists()
        assert post(f"http://127.0.0.1:{server.port}") == 200
        assert post(f"http://127.0.0.1:{server.port}") == 409  # one save
    finally:
        server.shutdown()
        server.server_close()
    code, out, kept = _keep(tmp_path, inv, f"page:{decisions}")
    assert code == 0 and kept.read_text() == "sales.customers\n"


# --- the fallback list ---------------------------------------------------------------------------


def _many() -> dict:
    spec = {
        "crm": ["crm_accounts", "crm_leads", "notes"],
        "ops": ["audit_log", "audit_trail", "jobs", "notes"],
    }
    return {
        "profile": "demo",
        "tables": [{"schema": s, "table": t, "columns": []} for s, ts in spec.items() for t in ts],
    }


@pytest.mark.parametrize(
    ("typed", "kept"),
    [
        ("1-2, 5", ["crm.crm_accounts", "crm.crm_leads", "ops.audit_trail"]),
        ("1 to 2", ["crm.crm_accounts", "crm.crm_leads"]),
        ("all crm_*", ["crm.crm_accounts", "crm.crm_leads"]),
        ("ops.*", ["ops.audit_log", "ops.audit_trail", "ops.jobs", "ops.notes"]),
        ("all crm", ["crm.crm_accounts", "crm.crm_leads", "crm.notes"]),
        (
            "everything except audit_*",
            ["crm.crm_accounts", "crm.crm_leads", "crm.notes", "ops.jobs", "ops.notes"],
        ),
        ("all but audit_*, 3", ["crm.crm_accounts", "crm.crm_leads", "ops.jobs", "ops.notes"]),
        ("Jobs; `crm.notes`", ["crm.notes", "ops.jobs"]),
        ("everything", None),
        ("AUDIT_LOG\n6", ["ops.audit_log", "ops.jobs"]),
        ("1,, 2,", ["crm.crm_accounts", "crm.crm_leads"]),  # empty pieces are skipped
    ],
)
def test_numbers_ranges_and_patterns_pick_tables(typed, kept):
    inv = _many()
    got, problem = IS.pick(inv, typed, shown=7)
    assert problem is None
    assert got == (kept if kept is not None else _names(inv))


@pytest.mark.parametrize(
    ("typed", "problem"),
    [
        ("0, 8", {"unmatched": ["0", "8"], "ambiguous": {}}),
        ("5-2", {"unmatched": ["5-2"], "ambiguous": {}}),
        ("all sales_*", {"unmatched": ["all sales_*"], "ambiguous": {}}),
        ("notes", {"unmatched": [], "ambiguous": {"notes": ["crm.notes", "ops.notes"]}}),
        (
            "everything except nothing_like_this",
            {"unmatched": ["nothing_like_this"], "ambiguous": {}},
        ),
        # an except with nothing after it would keep everything without anyone choosing that
        ("everything except", {"unmatched": ["everything except"], "ambiguous": {}}),
        ("except ,", {"unmatched": ["except ,"], "ambiguous": {}}),
    ],
)
def test_what_matches_nothing_or_several_is_said_back_not_guessed(typed, problem):
    assert IS.pick(_many(), typed, shown=7) == ([], problem)


def test_a_pattern_is_a_wildcard_only_where_it_has_a_star():
    inv = {
        "profile": "p",
        "tables": [
            {"schema": "s", "table": "a.b", "columns": []},
            {"schema": "s", "table": "axb", "columns": []},
        ],
    }
    assert IS.pick(inv, "a.b")[0] == ["s.a.b"]  # the dot is a dot, not any character
    assert IS.pick(inv, "a?b")[0] == ["s.a.b", "s.axb"]


def test_pick_through_the_cli_writes_the_kept_file(tmp_path):
    typed = tmp_path / "agami-pick.txt"
    typed.write_text("everything except audit_*\n")
    code, out, kept = _keep(tmp_path, _many(), f"pick:{typed}")
    assert code == 0 and out["kept"] == 5 and "ops.audit_log" not in kept.read_text()


def test_the_numbered_list_numbers_what_pick_reads(tmp_path, capsys):
    inv = _inventory()
    path = tmp_path / "inventory.json"
    path.write_text(json.dumps(inv))
    assert IS.main(["--inventory", str(path), "--numbered"]) == 0
    got = json.loads(capsys.readouterr().out)
    lines = got["list"].splitlines()
    assert got["tables"] == 10
    assert got["shown"] == 10 and got["more"] == 0
    assert lines[0] == "sales: 3 tables"
    assert lines[1] == "   1. orders (1 column, about 1,200 rows)"
    assert lines[3] == "   3. orders_bak (3 columns)  [likely copy of sales.orders]"
    assert lines[4] == "staging: 1 table"
    # the number a line shows is the one pick reads
    n = int(re.search(r"(\d+)\. orders_bak", got["list"]).group(1))
    assert IS.pick(inv, str(n), shown=got["shown"])[0] == ["sales.orders_bak"]


# --- in a real browser, where Playwright is installed --------------------------------------------

_CHROMIUM = r"""
const { chromium } = require('playwright');
(async () => {
  let b;
  try { b = await chromium.launch(); } catch (e) { console.log(JSON.stringify({ skip: String(e).slice(0, 200) })); return; }
  const p = await b.newPage(); const errs = []; let dialogs = 0;
  p.on('pageerror', e => errs.push(String(e))); p.on('dialog', d => { dialogs++; d.dismiss(); });
  p.on('console', m => { if (m.type() === 'error') errs.push(m.text()); });
  await p.goto(process.argv[1]);
  const names = await p.$$eval('tbody .name', ns => ns.map(n => n.textContent));
  const imgs = await p.$$eval('main img, main script', ns => ns.length);
  await p.fill('input[type=search]', 'onerror');
  const shown = await p.$$eval('tbody tr', ns => ns.filter(n => !n.hidden).length);
  await p.fill('input[type=search]', '');
  await p.click('#save');
  await p.waitForFunction(() => document.getElementById('status').className === 'ok', null, { timeout: 5000 });
  console.log(JSON.stringify({ names, imgs, shown, dialogs, errs, status: await p.textContent('#status') }));
  await b.close();
})();
"""


def test_hostile_names_show_as_text_in_a_real_browser(tmp_path):
    node = shutil.which("node")
    env = {**os.environ}
    # The suite runs with HOME in a temp folder; Playwright keeps its browsers under the real one.
    if "PLAYWRIGHT_BROWSERS_PATH" not in env and hasattr(os, "getuid"):
        import pwd

        home = Path(pwd.getpwuid(os.getuid()).pw_dir)
        for cache in (
            home / "Library" / "Caches" / "ms-playwright",
            home / ".cache" / "ms-playwright",
        ):
            if cache.is_dir():
                env["PLAYWRIGHT_BROWSERS_PATH"] = str(cache)
    if (
        node is None
        or subprocess.run(
            [node, "-e", "require.resolve('playwright')"], env=env, capture_output=True
        ).returncode
    ):
        pytest.skip(
            "node with playwright not available (set NODE_PATH to a node_modules that has it)"
        )
    inv = _inventory()
    page = tmp_path / "tables.html"
    page.write_text(RTP.render(inv), encoding="utf-8")
    decisions = tmp_path / "d.json"
    server = serve_page._Server(page.resolve(), decisions)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        proc = subprocess.run(
            [node, "-e", _CHROMIUM, f"http://127.0.0.1:{server.port}{server.base}"],
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
    finally:
        server.shutdown()
        server.server_close()
    got = json.loads(proc.stdout.strip().splitlines()[-1])
    if "skip" in got:
        pytest.skip(got["skip"])
    assert got["names"] == [t["table"] for t in inv["tables"]]
    assert got["imgs"] == 0 and got["dialogs"] == 0 and got["errs"] == []
    assert got["shown"] == 1
    assert got["status"].startswith("Saved: keeping 8 of 10 tables.")
    code, out, kept = _keep(tmp_path, inv, f"page:{decisions}")
    assert code == 0 and out["kept"] == 8 and HOSTILE[0] in kept.read_text()


# --- review fixes: a name that is a schema and a table, case, numbers never shown, empty except ----


def _same_name() -> dict:
    spec = {"sales": ["sales", "orders", "returns"], "crm": ["leads"]}
    return {
        "profile": "demo",
        "tables": [
            {"schema": sc, "table": t, "columns": []} for sc, ts in spec.items() for t in ts
        ],
    }


@pytest.mark.parametrize("typed", ["sales", "everything except sales", "SALES"])
def test_a_name_that_is_both_a_schema_and_a_table_is_asked_not_guessed(typed):
    kept, problem = IS.pick(_same_name(), typed)
    assert kept == [] and problem["unmatched"] == []
    (could_be,) = problem["ambiguous"].values()
    assert could_be == ["sales.sales", "sales.*"]


def test_all_before_a_name_means_the_schema():
    assert IS.pick(_same_name(), "all sales") == (
        ["sales.sales", "sales.orders", "sales.returns"],
        None,
    )
    assert IS.pick(_same_name(), "everything except all sales") == (["crm.leads"], None)
    # a qualified name is the table, never the schema
    assert IS.pick(_same_name(), "sales.sales") == (["sales.sales"], None)


def _cased() -> dict:
    return {
        "profile": "demo",
        "tables": [
            {"schema": "sales", "table": t, "columns": []} for t in ("Orders", "orders", "Items")
        ],
    }


def test_an_exact_case_match_wins_over_a_case_only_one():
    assert IS.pick(_cased(), "sales.orders") == (["sales.orders"], None)
    assert IS.pick(_cased(), "Orders") == (["sales.Orders"], None)
    assert IS.pick(_cased(), "items") == (["sales.Items"], None)  # no exact one: case is ignored


@pytest.mark.parametrize("typed", ["SALES.ORDERS", "ORDERS"])
def test_several_case_only_matches_are_asked_not_all_kept(typed):
    kept, problem = IS.pick(_cased(), typed)
    assert kept == [] and problem["ambiguous"] == {typed: ["sales.Orders", "sales.orders"]}


def _big(n: int = 250) -> dict:
    return {
        "profile": "demo",
        "tables": [{"schema": "s", "table": f"t{i:03d}", "columns": []} for i in range(1, n + 1)],
    }


def test_only_the_numbers_the_list_showed_are_read(tmp_path, capsys):
    inv = _big()
    path = tmp_path / "inventory.json"
    path.write_text(json.dumps(inv))
    assert IS.main(["--inventory", str(path), "--numbered"]) == 0
    got = json.loads(capsys.readouterr().out)
    assert (got["tables"], got["shown"], got["more"]) == (250, 200, 50)
    assert got["list"].splitlines()[-1].strip() == "200. t200 (0 columns)"
    assert IS.pick(inv, "199-200", shown=200) == (["s.t199", "s.t200"], None)
    assert IS.pick(inv, "1-250", shown=200) == ([], {"unmatched": ["1-250"], "ambiguous": {}})
    assert IS.pick(inv, "201", shown=200) == ([], {"unmatched": ["201"], "ambiguous": {}})
    assert IS.pick(inv, "t2*", shown=200)[0][-1] == "s.t250"  # a pattern still reaches every table


def test_numbers_mean_nothing_where_no_list_was_shown(tmp_path):
    # the main tables card shows no numbers, so a bare number there names nothing
    assert IS.pick(_many(), "3") == ([], {"unmatched": ["3"], "ambiguous": {}})
    typed = tmp_path / "agami-pick.txt"
    typed.write_text("1-2\n")
    path = tmp_path / "inventory.json"
    path.write_text(json.dumps(_many()))
    out = tmp_path / "agami-keep.txt"
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        assert (
            IS.main(
                [
                    "--inventory",
                    str(path),
                    "--keep",
                    f"pick:{typed}",
                    "--shown",
                    "2",
                    "--tables-out",
                    str(out),
                ]
            )
            == 0
        )
    assert out.read_text() == "crm.crm_accounts\ncrm.crm_leads\n"
    with contextlib.redirect_stdout(io.StringIO()):
        assert (
            IS.main(["--inventory", str(path), "--keep", f"pick:{typed}", "--tables-out", str(out)])
            == 1
        )


def test_the_list_numbers_schemas_together_even_when_the_inventory_interleaves_them():
    inv = {
        "profile": "demo",
        "tables": [
            {"schema": "a", "table": "x", "columns": []},
            {"schema": "b", "table": "y", "columns": []},
            {"schema": "a", "table": "z", "columns": []},
        ],
    }
    got = IS.numbered(inv)
    assert got["list"].splitlines() == [
        "a: 2 tables",
        "  1. x (0 columns)",
        "  2. z (0 columns)",
        "b: 1 table",
        "  3. y (0 columns)",
    ]
    assert IS.pick(inv, "2", shown=got["shown"])[0] == ["a.z"]
    # the kept file stays in inventory order, whatever the numbering
    assert IS.pick(inv, "1-3", shown=3)[0] == ["a.x", "b.y", "a.z"]
