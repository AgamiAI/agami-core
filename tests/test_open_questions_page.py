"""Review the open questions on a page that saves (#676).

"Ask me as you go" asks what the data can't settle on cards, four at a time. A build with many
open questions needs the whole list on one page, the way the other sign-offs have one: one row per
item with a stable id, ordered by how much it changes answers, saved once to the helper that serves
it (`serve_page.py --decisions`, #651), then checked by its parser into one `sm curate` batch signed
by the saved identity. Blank is undecided, never a default.

A synthetic store built by real introspection over SQLite. Generic names only.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

pytest.importorskip("pydantic")
pytest.importorskip("sqlglot")
pytest.importorskip("yaml")

REPO_ROOT = Path(__file__).resolve().parent.parent
PKG_SRC = REPO_ROOT / "packages" / "agami-core" / "src"
SCRIPTS = REPO_ROOT / "plugins" / "agami" / "scripts"
for p in (PKG_SRC, SCRIPTS):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import parse_open_questions_decisions as POQ  # noqa: E402
import render_open_questions_page as ROQ  # noqa: E402
import serve_page  # noqa: E402
from semantic_model import curate  # noqa: E402
from semantic_model import dialects as D  # noqa: E402
from semantic_model import introspect as I  # noqa: E402
from semantic_model import loader as L  # noqa: E402
from semantic_model import open_questions as OQ  # noqa: E402

import test_open_questions as TOQ  # noqa: E402

TODAY, _ask, _sqlite_runner = TOQ.TODAY, TOQ._ask, TOQ._sqlite_runner
store = TOQ.store  # the fixture: the synthetic store #486's tests build
EMAIL, ROLE = "you@example.com", "Analyst"


def _edit(root: Path, table: str, column: str, field: str, value) -> None:
    res = curate.apply(
        root,
        [
            {
                "op": "edit",
                "kind": "table",
                "name": table,
                "column": column,
                "field": field,
                "value": value,
            }
        ],
    )
    assert res.validated, res.skipped


def _codes_store(tmp_path: Path) -> tuple[Path, Path]:
    """A small store whose columns are the page's three row kinds and the lists it leaves out."""
    db = tmp_path / "codes.db"
    con = sqlite3.connect(db)
    rows = ", ".join(
        f"({i + 1}, 'S{i % 3 + 1}', '{'Y' if i % 2 else 'N'}', '{('kilo', 'lima', 'mike', 'nova')[i % 4]}', 'R{i % 2}', "
        f"'{('east', 'west')[i % 2]}', '{i % 3 + 1}')"
        for i in range(30)
    )
    con.executescript(
        "CREATE TABLE items(id INTEGER PRIMARY KEY, st_cd TEXT, active_flag TEXT, v_1 TEXT,"
        " rk TEXT, region TEXT, qty TEXT);"
        f"INSERT INTO items VALUES {rows};"
    )
    con.commit()
    con.close()
    I.introspect("codes", "sqlite", runner=_sqlite_runner(db), artifacts_dir=tmp_path)
    root = tmp_path / "codes"
    # the build's guesses: a code list it decoded, a yes/no flag, a code list that says itself,
    # a column it could not read and an opaque one it guessed at
    _edit(root, "items", "st_cd", "choice_field", {"S1": "Open", "S2": "Closed", "S3": ""})
    _edit(root, "items", "active_flag", "choice_field", {"Y": "", "N": ""})
    _edit(root, "items", "region", "choice_field", {"east": "East", "west": "West"})
    _edit(root, "items", "rk", "choice_field", {"R0": "Retail", "R1": "Wholesale"})
    curate.apply(
        root,
        [
            {
                "op": "edit",
                "kind": "table",
                "name": "items",
                "column": "rk",
                "field": "description",
                "source": "ai",
                "value": "The kind of customer.",
            }
        ],
    )
    _edit(root, "items", "v_1", "description_source", "ai_unknown")
    curate.apply(
        root,
        [
            {
                "op": "edit",
                "kind": "table",
                "name": "items",
                "column": "qty",
                "field": "description",
                "source": "ai",
                "value": "How many were ordered.",
            }
        ],
    )
    (tmp_path / "local").mkdir(exist_ok=True)
    (tmp_path / "local" / ".config").write_text(
        json.dumps({"reviewer_email": EMAIL, "reviewer_role": ROLE})
    )
    return root, db


def _page_items(root: Path, db: Path, **kw) -> dict:
    org = L.load_datasource(root)
    return OQ.open_questions(
        org, _sqlite_runner(db), D.get_dialect("sqlite"), today=TODAY, page=True, **kw
    )


def _by_id(out: dict) -> dict:
    return {q["id"]: q for q in out["questions"]}


# --- the engine's page mode ----------------------------------------------------------------------


def test_every_item_has_a_stable_id_the_same_on_every_run(store):
    root, db = store
    first = [q["id"] for q in _ask(root, db)["questions"]]
    assert len(first) == len(set(first)) and all(first)
    assert first == [q["id"] for q in _ask(root, db)["questions"]]
    assert "codes:tickets.priority" in first
    assert any(i.startswith("question:orders_") for i in first)


def test_the_page_gets_every_item_not_the_cards_twelve(store, monkeypatch):
    root, db = store
    monkeypatch.setattr(OQ, "MAX_QUESTIONS", 2)
    cards = _ask(root, db)
    org = L.load_datasource(root)
    page = OQ.open_questions(
        org, _sqlite_runner(db), D.get_dialect("sqlite"), today=TODAY, page=True
    )
    assert len(cards["questions"]) == 2 and cards["more"] > 0
    assert len(page["questions"]) == 2 + cards["more"] and page["more"] == 0
    # ambiguities first, the widest apart first: how much the choice changes answers
    spreads = [q["spread"] for q in page["questions"] if q["kind"] == "ambiguity"]
    assert spreads == sorted(spreads, reverse=True)
    kinds = [q["kind"] for q in page["questions"]]
    assert kinds.index("coded_values") > max(i for i, k in enumerate(kinds) if k == "ambiguity")


def test_self_explanatory_code_lists_are_left_off(tmp_path):
    root, db = _codes_store(tmp_path)
    items = _by_id(_page_items(root, db))
    # yes/no, plain numbers and codes that already carry their label are not asked
    assert "codes:items.active_flag" not in items
    assert "codes:items.region" not in items
    assert "codes:items.qty" not in items
    # a list with a blank label is asked, with the build's guesses for the others
    st = items["codes:items.st_cd"]
    assert st["guess"] == {"S1": "Open", "S2": "Closed"}
    assert st["labels"] == {"S1": "Open", "S2": "Closed", "S3": ""}
    # a list the build filled in on its own is asked too, its guess pre-filled
    assert items["codes:items.rk"]["guess"] == {"R0": "Retail", "R1": "Wholesale"}


def test_yes_no_flags_are_not_asked_on_the_cards_either(tmp_path):
    root, db = _codes_store(tmp_path)
    org = L.load_datasource(root)
    out = OQ.open_questions(org, _sqlite_runner(db), D.get_dialect("sqlite"), today=TODAY)
    ids = [q["id"] for q in out["questions"]]
    assert "codes:items.active_flag" not in ids and "codes:items.st_cd" in ids
    # the cards keep asking only what they asked before: no guessed list, no opaque column
    assert "codes:items.rk" not in ids and "column:items.qty" not in ids


def test_unclear_columns_carry_samples_and_the_builds_guess(tmp_path):
    root, db = _codes_store(tmp_path)
    items = _by_id(_page_items(root, db))
    unknown = items["column:items.v_1"]
    assert unknown["guess"] == "" and sorted(unknown["samples"]) == ["kilo", "lima", "mike", "nova"]
    guessed = items["column:items.qty"]
    assert guessed["guess"] == "How many were ordered."
    assert sorted(guessed["samples"]) == ["1", "2", "3"]


def test_samples_are_never_read_from_a_personal_column(tmp_path):
    root, db = _codes_store(tmp_path)
    _edit(root, "items", "v_1", "sensitive", True)
    seen: list[str] = []
    org = L.load_datasource(root)
    out = OQ.open_questions(
        org, _sqlite_runner(db, seen), D.get_dialect("sqlite"), today=TODAY, page=True
    )
    assert "column:items.v_1" not in _by_id(out)
    assert not any("v_1" in s for s in seen)


def test_a_failed_sample_read_shows_none_and_says_no_more(tmp_path):
    root, db = _codes_store(tmp_path)
    run = _sqlite_runner(db)

    def runner(sql: str):
        if "DISTINCT" in sql:
            raise RuntimeError("secret value in the message")
        return run(sql)

    org = L.load_datasource(root)
    out = OQ.open_questions(org, runner, D.get_dialect("sqlite"), today=TODAY, page=True)
    assert _by_id(out)["column:items.v_1"]["samples"] is None
    assert "secret" not in json.dumps(out)


def test_a_code_list_a_person_signed_is_not_asked_again(tmp_path):
    root, db = _codes_store(tmp_path)
    q = _by_id(_page_items(root, db))["codes:items.rk"]
    curate.apply(root, [{**q["op"], "value": q["labels"]}], signer=EMAIL, role=ROLE)
    confirmed = OQ.confirmed_code_lists(curate.read_log(root))
    assert ("items", "rk") in confirmed
    assert "codes:items.rk" not in _by_id(_page_items(root, db, confirmed=confirmed))


def test_the_command_prints_the_page_list_with_all(tmp_path, monkeypatch, capsys):
    from semantic_model import cli

    root, db = _codes_store(tmp_path)
    monkeypatch.setattr(I, "make_execute_sql_runner", lambda profile, **_: _sqlite_runner(db))
    q = _by_id(_page_items(root, db))["codes:items.rk"]
    curate.apply(root, [q["op"]], signer=EMAIL, role=ROLE)
    assert cli.main(["open-questions", str(root), "--profile", "codes", "--all"]) == 0
    out = json.loads(capsys.readouterr().out)
    ids = [q["id"] for q in out["questions"]]
    # every item and no card; the list the person signed is read from the log and left off
    assert "column:items.qty" in ids and "codes:items.rk" not in ids
    assert all("card" not in q for q in out["questions"]) and out["more"] == 0


# --- the page ------------------------------------------------------------------------------------


HOSTILE = "</script><img src=x onerror=alert(1)>{{DATA}}"


def _items_file(tmp_path: Path, out: dict) -> Path:
    path = tmp_path / "agami-open-questions.json"
    path.write_text(json.dumps(out), encoding="utf-8")
    return path


def _block(html: str) -> str:
    return re.search(
        r'<script type="application/json" id="agami-questions">(.*?)</script>', html, re.S
    ).group(1)


def _page_script(html: str) -> str:
    return re.findall(r"<script>(.*?)</script>", html, re.S)[-1]


def _sample_out() -> dict:
    return {
        "questions": [
            {
                "id": "question:orders_date",
                "kind": "ambiguity",
                "pattern": "date",
                "table": "orders",
                "question": "When someone says 'orders last month', which date?",
                "evidence": "Orders last month: 31 by placed, 12 by shipped",
                "spread": 0.6,
                "options": [
                    {"label": "Placed date", "how": "by placed_at", "value": 31, "shown": "31"},
                    {"label": "Shipped date", "how": "by shipped_at", "value": 12, "shown": "12"},
                ],
                "op": {
                    "op": "add",
                    "kind": "ambiguity",
                    "name": "orders_date",
                    "when": "orders in a period",
                    "applies_to": ["orders"],
                    "options": [
                        {"label": "Placed date", "how": "by placed_at"},
                        {"label": "Shipped date", "how": "by shipped_at"},
                    ],
                },
            },
            {
                "id": "same:orders|orders_old",
                "kind": "look_alike",
                "question": "Orders and Orders old look alike. Which one holds the current data?",
                "tables": [
                    {
                        "table": "orders",
                        "label": "Orders",
                        "rows": 200,
                        "ops": [
                            {
                                "op": "edit",
                                "kind": "table",
                                "area": "main",
                                "name": "orders_old",
                                "field": "caveats",
                                "value": [
                                    "Looks like a copy of Orders; the current data is in Orders."
                                ],
                            }
                        ],
                    },
                    {
                        "table": "orders_old",
                        "label": "Orders old",
                        "rows": 50,
                        "ops": [
                            {
                                "op": "edit",
                                "kind": "table",
                                "area": "main",
                                "name": "orders",
                                "field": "caveats",
                                "value": [
                                    "Looks like a copy of Orders old; the current data is in Orders old."
                                ],
                            }
                        ],
                    },
                ],
            },
            {
                "id": "codes:items.st_cd",
                "kind": "coded_values",
                "table": "items",
                "column": "st_cd",
                "values": ["S3"],
                "evidence": "Codes: S3",
                "labels": {"S1": "Open", "S2": "Closed", "S3": ""},
                "guess": {"S1": "Open", "S2": "Closed"},
                "question": "What do the values of St cd on Items mean?",
                "op": {
                    "op": "edit",
                    "kind": "table",
                    "area": "main",
                    "name": "items",
                    "column": "st_cd",
                    "field": "choice_field",
                    "value": {"S1": "Open", "S2": "Closed", "S3": ""},
                },
            },
            {
                "id": "column:items.v_1",
                "kind": "unknown_column",
                "table": "items",
                "column": "v_1",
                "guess": "",
                "samples": ["k0", HOSTILE],
                "question": "What does V 1 on Items hold?",
                "op": {
                    "op": "edit",
                    "kind": "table",
                    "area": "main",
                    "name": "items",
                    "column": "v_1",
                    "field": "description",
                    "source": "human",
                    "value": "",
                },
            },
            {
                "id": "column:items.qty",
                "kind": "unknown_column",
                "table": "items",
                "column": "qty",
                "guess": "How many were ordered.",
                "samples": ["1", "2"],
                "question": "What does Qty on Items hold?",
                "op": {
                    "op": "edit",
                    "kind": "table",
                    "area": "main",
                    "name": "items",
                    "column": "qty",
                    "field": "description",
                    "source": "human",
                    "value": "",
                },
            },
        ],
        "more": 0,
    }


def test_the_page_carries_every_row_in_order_and_who_signs(tmp_path):
    html = ROQ.render(_sample_out(), profile="demo", signoff={"email": EMAIL, "role": ROLE})
    data = json.loads(_block(html))
    assert [r["id"] for r in data["rows"]] == [q["id"] for q in _sample_out()["questions"]]
    assert data["profile"] == "demo" and data["signer"] == {"email": EMAIL, "role": ROLE}
    assert EMAIL in html  # the banner names who the answers are recorded for
    ask = data["rows"][0]
    # the readings, then ask each time and don't know; the evidence numbers beside them
    assert [c["value"] for c in ask["choices"]] == [
        "reading:Placed date",
        "reading:Shipped date",
        "ask",
        "dont_know",
    ]
    assert ask["evidence"].startswith("Orders last month")
    assert [c["value"] for c in data["rows"][1]["choices"]] == [
        "table:orders",
        "table:orders_old",
        "both",
        "dont_know",
    ]
    assert [c["value"] for c in data["rows"][2]["choices"]] == ["confirm", "fix", "dont_know"]
    # an unclear column with no guess has nothing to confirm
    assert [c["value"] for c in data["rows"][3]["choices"]] == ["fix", "hide", "dont_know"]
    assert [c["value"] for c in data["rows"][4]["choices"]] == [
        "confirm",
        "fix",
        "hide",
        "dont_know",
    ]


def test_no_value_can_leave_the_data_block_or_run():
    html = ROQ.render(_sample_out(), profile="<b>x</b>", signoff={})
    assert html.count("<script") == 2 and html.lower().count("</script") == 2
    assert not set("<>&") & set(_block(html))
    assert "<b>x</b>" not in html and "<img" not in html
    assert "{{" not in html.replace(_block(html), "")
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
    assert not re.search(r"<[^>]+\son[a-z]+\s*=", html.replace(_block(html), ""), re.I)
    assert not re.search(
        r"<(?:link|img|iframe)\b|\ssrc=|@import|url\(http", html.replace(_block(html), ""), re.I
    )


def test_render_cli_writes_the_page_and_counts(tmp_path, capsys):
    items = _items_file(tmp_path, _sample_out())
    (tmp_path / "local").mkdir()
    (tmp_path / "local" / ".config").write_text(
        json.dumps({"reviewer_email": EMAIL, "reviewer_role": ROLE})
    )
    out = tmp_path / "p" / "questions.html"
    args = [
        "--items-file",
        str(items),
        "--profile",
        "demo",
        "--artifacts-dir",
        str(tmp_path),
        "--save-to-helper",
        "--out",
        str(out),
    ]
    assert ROQ.main(args) == 0
    said = json.loads(capsys.readouterr().out)
    assert said == {
        "page": str(out),
        "rows": 5,
        "questions": 2,
        "code_lists": 1,
        "columns": 2,
        "signer": EMAIL,
    }
    assert out.read_text().startswith("<!doctype html>")


def test_render_refuses_without_a_saved_identity(tmp_path, capsys):
    items = _items_file(tmp_path, _sample_out())
    out = tmp_path / "q.html"
    assert (
        ROQ.main(
            [
                "--items-file",
                str(items),
                "--profile",
                "demo",
                "--artifacts-dir",
                str(tmp_path),
                "--save-to-helper",
                "--out",
                str(out),
            ]
        )
        == 1
    )
    assert "identity" in json.loads(capsys.readouterr().out)["error"] and not out.exists()


# --- the page's own script, under node with a small DOM ------------------------------------------

_DOM = r"""
const created = [];
function El(tag) { this.tagName = tag.toUpperCase(); this.children = []; this.listeners = {}; this.hidden = false;
  this.checked = false; this.disabled = false; this.className = ''; this._text = ''; this.attrs = {}; this.value = '';
  created.push(this); }
El.prototype.appendChild = function (c) { this.children.push(c); c.parent = this; return c; };
Object.defineProperty(El.prototype, 'textContent', {
  get() { return this._text + this.children.map(c => c.textContent).join(''); },
  set(v) { this._text = String(v); this.children = []; } });
Object.defineProperty(El.prototype, 'innerHTML', { set() { throw new Error('innerHTML'); }, get() { throw new Error('innerHTML'); } });
El.prototype.addEventListener = function (t, f) { (this.listeners[t] = this.listeners[t] || []).push(f); };
El.prototype.fire = function (t) { (this.listeners[t] || []).forEach(f => f({ target: this })); };
El.prototype.setAttribute = function (k, v) { this.attrs[k] = String(v); };
const ids = {};
['rows', 'total', 'save', 'status'].forEach(i => { ids[i] = new El(i === 'rows' ? 'div' : 'button'); });
ids['agami-questions'] = { textContent: DATA };
const document = { getElementById: i => ids[i], createElement: t => new El(t),
  querySelectorAll: s => created.filter(e => e.tagName === 'BUTTON' || e.tagName === 'SELECT' || e.tagName === 'INPUT' || e.tagName === 'TEXTAREA') };
const posted = [];
let answer = { ok: true, status: 200, body: { ok: true } };
function fetch(url, opts) { posted.push({ url, body: JSON.parse(opts.body) });
  return Promise.resolve({ ok: answer.ok, status: answer.status, json: () => Promise.resolve(answer.body) }); }
function rowEl(id) { return created.find(e => e.attrs['data-id'] === id); }
function find(root, tag, cls) { const out = []; (function walk(n) { if (n.tagName === tag && (!cls || n.className.split(' ').includes(cls))) out.push(n); n.children.forEach(walk); })(root); return out; }
function pick(id, value) { const s = find(rowEl(id), 'SELECT')[0]; s.value = value; s.fire('change'); }
function type(id, cls, i, text) { const t = find(rowEl(id), cls === 'text' ? 'TEXTAREA' : 'INPUT', cls)[i]; t.value = text; t.fire('input'); }
"""

_REPORT = (
    "setTimeout(() => console.log(JSON.stringify({posted, total: ids.total.textContent, "
    "status: ids.status.textContent, save: ids.save.disabled, "
    "samples: find(rowEl('column:items.v_1'), 'LI').map(l => l.textContent), "
    "prefill: find(rowEl('codes:items.st_cd'), 'INPUT', 'label').map(i => [i.value, i.disabled])})), 20);"
)


def _node(steps: str, signoff: dict | None = None) -> dict:
    node = shutil.which("node")
    if node is None:
        if os.environ.get("CI"):
            raise RuntimeError("node is required on CI for the page tests")
        pytest.skip("node not installed")
    signoff = {"email": EMAIL, "role": ROLE} if signoff is None else signoff
    html = ROQ.render(_sample_out(), profile="demo", signoff=signoff)
    js = f"const DATA = {json.dumps(_block(html))};\n{_DOM}\n{_page_script(html)}\n{steps}"
    out = subprocess.run([node, "-e", js], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_blank_rows_are_undecided_and_nothing_is_saved_until_one_is_decided():
    got = _node("ids.save.fire('click');" + _REPORT)
    assert got["posted"] == [] and got["save"] is True
    assert got["total"] == "0 of 5 decided"
    # the guess is shown, read-only until fix is picked; samples are text
    assert got["prefill"] == [["Open", True], ["Closed", True], ["", True]]
    assert got["samples"] == ["k0", HOSTILE]


def test_save_posts_only_the_decided_rows_once():
    steps = (
        "pick('question:orders_date', 'reading:Shipped date');"
        "pick('codes:items.st_cd', 'fix'); type('codes:items.st_cd', 'label', 2, 'Pending');"
        "pick('column:items.qty', 'hide');"
        "pick('column:items.v_1', 'dont_know');"
        "ids.save.fire('click'); ids.save.fire('click');"
    )
    got = _node(steps + _REPORT)
    assert len(got["posted"]) == 1
    body = got["posted"][0]["body"]
    assert body["page"] == "open-questions" and body["profile"] == "demo"
    assert body["decisions"] == [
        {"id": "question:orders_date", "choice": "reading", "label": "Shipped date"},
        {
            "id": "codes:items.st_cd",
            "choice": "fix",
            "labels": {"S1": "Open", "S2": "Closed", "S3": "Pending"},
        },
        {"id": "column:items.v_1", "choice": "dont_know"},
        {"id": "column:items.qty", "choice": "hide"},
    ]
    assert got["total"] == "4 of 5 decided"
    assert "press I’ve saved it" in got["status"]


def test_a_fix_with_no_words_cannot_be_saved():
    got = _node("pick('column:items.v_1', 'fix'); ids.save.fire('click');" + _REPORT)
    assert got["posted"] == [] and got["save"] is True
    assert "Type" in got["status"]


def test_without_a_saved_identity_the_page_cannot_save():
    got = _node(
        "pick('column:items.v_1', 'dont_know'); ids.save.fire('click');" + _REPORT, signoff={}
    )
    assert got["posted"] == [] and got["save"] is True


def test_a_refused_save_that_cannot_change_keeps_the_page_locked():
    steps = (
        "answer = { ok: false, status: 409, body: { ok: false, message: 'These decisions were already saved; this page saves once.' } };"
        "pick('column:items.v_1', 'dont_know'); ids.save.fire('click');"
        "setTimeout(() => ids.save.fire('click'), 5);"
    )
    got = _node(steps + _REPORT)
    assert len(got["posted"]) == 1 and got["save"] is True
    assert got["status"] == "These decisions were already saved; this page saves once."


# --- what the page saved, checked and turned into one curate batch -------------------------------


def _decisions(tmp_path: Path, rows: list, **top) -> Path:
    doc = {"page": "open-questions", "profile": "demo", "decisions": rows, **top}
    path = tmp_path / "agami-page-1.json"
    path.write_text(json.dumps(doc), encoding="utf-8")
    return path


def _parse(
    tmp_path: Path, rows: list | Path, items: dict | None = None, config: bool = True, **top
):
    if config:
        (tmp_path / "local").mkdir(exist_ok=True)
        (tmp_path / "local" / ".config").write_text(
            json.dumps({"reviewer_email": EMAIL, "reviewer_role": ROLE})
        )
    path = rows if isinstance(rows, Path) else _decisions(tmp_path, rows, **top)
    items_path = _items_file(tmp_path, items or _sample_out())
    ops = tmp_path / "ops.json"
    code = POQ.main(
        [
            "--decisions-file",
            str(path),
            "--items-file",
            str(items_path),
            "--expect-profile",
            "demo",
            "--artifacts-dir",
            str(tmp_path),
            "--ops-out",
            str(ops),
        ]
    )
    return code, ops


def _run(tmp_path, capsys, *a, **kw):
    code, ops = _parse(tmp_path, *a, **kw)
    out = json.loads(capsys.readouterr().out)
    return code, out, (json.loads(ops.read_text()) if ops.exists() else None)


def test_each_choice_becomes_the_op_its_card_answer_would(tmp_path, capsys):
    code, out, ops = _run(
        tmp_path,
        capsys,
        [
            {"id": "question:orders_date", "choice": "reading", "label": "Shipped date"},
            {"id": "same:orders|orders_old", "choice": "table", "table": "orders"},
            {"id": "codes:items.st_cd", "choice": "confirm"},
            {"id": "column:items.qty", "choice": "confirm"},
            {"id": "column:items.v_1", "choice": "fix", "text": "  The batch key.  "},
        ],
    )
    assert code == 0 and out["refused"] == []
    q = _sample_out()["questions"]
    assert ops == [
        {**q[0]["op"], "default": "Shipped date"},
        *q[1]["tables"][0]["ops"],
        {**q[2]["op"], "value": {"S1": "Open", "S2": "Closed", "S3": ""}},
        {
            "op": "edit",
            "kind": "table",
            "area": "main",
            "name": "items",
            "column": "qty",
            "field": "description_source",
            "value": "ai_validated",
        },
        {**q[3]["op"], "value": "The batch key."},
    ]
    assert out["data"]["signer"] == {"email": EMAIL, "role": ROLE}
    assert out["data"]["counts"] == {
        "questions": 2,
        "code_lists": 1,
        "columns_confirmed": 1,
        "columns_described": 1,
        "columns_hidden": 0,
        "left_as_is": 0,
        "undecided": 0,
    }


def test_ask_each_time_declares_no_default(tmp_path, capsys):
    _, out, ops = _run(tmp_path, capsys, [{"id": "question:orders_date", "choice": "ask"}])
    assert ops == [{**_sample_out()["questions"][0]["op"], "behaviour": "ask"}]
    assert "default" not in ops[0]


def test_blank_and_dont_know_write_nothing(tmp_path, capsys):
    code, out, ops = _run(
        tmp_path,
        capsys,
        [
            {"id": "question:orders_date", "choice": "dont_know"},
            {"id": "same:orders|orders_old", "choice": "both"},
        ],
    )
    assert code == 0 and ops == []
    assert out["data"]["counts"]["left_as_is"] == 2 and out["data"]["counts"]["undecided"] == 3


def test_hide_this_column_writes_an_exclusion(tmp_path, capsys):
    _, out, ops = _run(tmp_path, capsys, [{"id": "column:items.v_1", "choice": "hide"}])
    assert ops == [
        {"op": "exclude", "kind": "table", "area": "main", "name": "items", "column": "v_1"}
    ]
    assert out["data"]["counts"]["columns_hidden"] == 1


def test_a_code_list_fix_records_the_labels_the_person_typed(tmp_path, capsys):
    _, out, ops = _run(
        tmp_path,
        capsys,
        [
            {
                "id": "codes:items.st_cd",
                "choice": "fix",
                "labels": {"S1": "Open", "S2": "Shut", "S3": " Pending "},
            }
        ],
    )
    assert ops == [
        {
            **_sample_out()["questions"][2]["op"],
            "value": {"S1": "Open", "S2": "Shut", "S3": "Pending"},
        }
    ]


@pytest.mark.parametrize(
    "decision, reason",
    [
        ({"id": "nope", "choice": "dont_know"}, "no such item on the page"),
        (
            {"id": "question:orders_date", "choice": "reading", "label": "Made up"},
            "not one of its readings",
        ),
        ({"id": "question:orders_date", "choice": "hide"}, "not a choice for this item"),
        ({"id": "column:items.v_1", "choice": "confirm"}, "no guess to confirm"),
        ({"id": "column:items.v_1", "choice": "fix", "text": "  "}, "a fix needs its words"),
        ({"id": "column:items.v_1", "choice": "fix", "text": "two\nlines"}, "one short line"),
        (
            {"id": "codes:items.st_cd", "choice": "fix", "labels": {"ZZ": "x"}},
            "not one of its codes",
        ),
        (
            {"id": "same:orders|orders_old", "choice": "table", "table": "users"},
            "not one of its tables",
        ),
        ({"id": "column:items.v_1", "choice": "hide", "extra": 1}, "unknown keys"),
        ("not an object", "not an object"),
    ],
)
def test_a_malformed_decision_is_refused_and_not_applied(tmp_path, capsys, decision, reason):
    code, out, ops = _run(tmp_path, capsys, [decision])
    assert code == 0 and ops == []
    assert reason in out["refused"][0]["reason"]


def test_a_row_decided_twice_is_refused(tmp_path, capsys):
    _, out, ops = _run(
        tmp_path,
        capsys,
        [
            {"id": "column:items.v_1", "choice": "hide"},
            {"id": "column:items.v_1", "choice": "dont_know"},
        ],
    )
    assert len(ops) == 1 and out["refused"] == [
        {"id": "column:items.v_1", "reason": "decided twice"}
    ]


@pytest.mark.parametrize(
    "top, error",
    [
        ({"page": "examples"}, "not the open questions page"),
        ({"profile": "other"}, "another model"),
        ({"decisions": {}}, "not a list"),
        ({"extra": 1}, "unknown keys"),
    ],
)
def test_another_pages_file_is_refused_whole(tmp_path, capsys, top, error):
    code, out, ops = _run(tmp_path, capsys, [], **top)
    assert code == 2 and error in out["error"] and ops is None


def test_without_a_saved_identity_nothing_is_written(tmp_path, capsys):
    code, out, ops = _run(
        tmp_path, capsys, [{"id": "column:items.v_1", "choice": "hide"}], config=False
    )
    assert code == 2 and "identity" in out["error"] and ops is None


# --- the whole path: engine, page, helper, parser, curate ----------------------------------------


def test_the_saved_page_applies_under_the_persons_name_and_saves_once(tmp_path, capsys):
    root, db = _codes_store(tmp_path)
    out = _page_items(root, db)
    items = _items_file(tmp_path, out)
    page = tmp_path / "questions.html"
    assert (
        ROQ.main(
            [
                "--items-file",
                str(items),
                "--profile",
                "codes",
                "--artifacts-dir",
                str(tmp_path),
                "--save-to-helper",
                "--out",
                str(page),
            ]
        )
        == 0
    )
    capsys.readouterr()
    decisions = tmp_path / "local" / "tmp" / "agami-page-1.json"
    server = serve_page._Server(page.resolve(), decisions)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    body = json.dumps(
        {
            "page": "open-questions",
            "profile": "codes",
            "decisions": [
                {"id": "codes:items.rk", "choice": "confirm"},
                {"id": "codes:items.st_cd", "choice": "fix", "labels": {"S3": "Pending"}},
                {"id": "column:items.v_1", "choice": "hide"},
                {"id": "column:items.qty", "choice": "fix", "text": "Units on the line."},
            ],
        }
    ).encode()

    def post() -> int:
        req = urllib.request.Request(
            f"http://127.0.0.1:{server.port}{server.base}save",
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Origin": f"http://127.0.0.1:{server.port}",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status
        except urllib.error.HTTPError as e:
            return e.code

    try:
        assert post() == 200
        assert post() == 409  # a second save is refused, as on every saving page
    finally:
        server.shutdown()
        server.server_close()
    ops = tmp_path / "ops.json"
    assert (
        POQ.main(
            [
                "--decisions-file",
                str(decisions),
                "--items-file",
                str(items),
                "--expect-profile",
                "codes",
                "--artifacts-dir",
                str(tmp_path),
                "--ops-out",
                str(ops),
            ]
        )
        == 0
    )
    said = json.loads(capsys.readouterr().out)
    assert said["refused"] == []
    res = curate.apply(root, json.loads(ops.read_text()), signer=EMAIL, role=ROLE)
    assert res.validated and len(res.applied) == 4, res.skipped
    org = L.load_datasource(root, include_rejected=True)
    cols = {c.name: c for sa in org.subject_areas for t in sa.tables_defined for c in t.columns}
    assert cols["st_cd"].choice_field == {"S1": "Open", "S2": "Closed", "S3": "Pending"}
    assert cols["v_1"].review_state == "rejected"
    assert (cols["qty"].description, cols["qty"].description_source) == (
        "Units on the line.",
        "human",
    )
    log = [json.loads(x) for x in (root / curate.CURATION_LOG).read_text().splitlines()][-4:]
    assert {e["signer"] for e in log} == {EMAIL}
    # what was answered is not asked again
    again = _by_id(_page_items(root, db, confirmed=OQ.confirmed_code_lists(curate.read_log(root))))
    assert not {
        "codes:items.rk",
        "codes:items.st_cd",
        "column:items.v_1",
        "column:items.qty",
    } & set(again)
