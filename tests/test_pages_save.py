"""Pages that save their own decisions (#651), the way the password page saves a password.

A review page rendered `--save-to-helper` and served by `serve_page.py --decisions` posts its queued
decisions to that helper; the helper writes them to a file and the skill applies them through the same
commands a card uses. The helper's own refusals are in test_serve_page.py; here: the page side (what
it hides, who it says approvals are signed as, and that nothing on the page can change that), the
renderers' flags, the parser reading the helper's file, and the curation log recording a bulk approval.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PLUGIN = REPO_ROOT / "plugins" / "agami"
SCRIPTS = PLUGIN / "scripts"
sys.path.insert(0, str(SCRIPTS))

import parse_model_feedback as F  # noqa: E402
from _read_only_view import (  # noqa: E402
    BANNER_TEXT,
    SAVE_LABEL,
    SAVING_HIDDEN,
    make_read_only,
    make_saving,
)

EXPLORER = PLUGIN / "shared" / "model-explorer-template.html"
EXAMPLES = PLUGIN / "shared" / "examples-validation-template.html"
ME = {"email": "you@example.com", "role": "Data Lead"}


def _head(html: str) -> str:
    return html[: html.index("</head>")]


@pytest.mark.parametrize("template", [EXPLORER, EXAMPLES], ids=lambda p: p.name)
def test_a_saving_page_hides_the_paste_back_and_names_the_signer(template: Path) -> None:
    html = make_saving(template.read_text(encoding="utf-8"), ME)
    assert '<body class="agami-saves">' in html
    for hidden in SAVING_HIDDEN:
        assert f"body.agami-saves {hidden}" in _head(html), hidden
    assert "#modal" in SAVING_HIDDEN  # the copy-and-paste dialog never shows
    assert "you@example.com</strong> (Data Lead), from your agami settings" in html
    assert "window.agamiSave" in _head(html) and "fetch('save'" in _head(html)
    # The page's own script hands its decisions to the helper instead of the paste-back dialog.
    assert "window.agamiSave(" in html[html.index("</head>") :]


def test_a_saving_page_without_a_saved_identity_says_approvals_cannot_be_saved() -> None:
    html = make_saving(EXPLORER.read_text(encoding="utf-8"), {})
    assert "approvals can't be saved from this page; set it in the chat" in html
    assert 'window.AGAMI_SAVE = {"signer": null}' in html
    # an email alone is not an identity: the role is stamped on every approval too
    assert '"signer": null' in make_saving(
        "<head></head><body></body>", {"email": "you@example.com"}
    )


def test_the_identity_is_escaped_in_both_the_banner_and_the_script() -> None:
    who = {"email": "x</script><b>@example.com", "role": "<i>lead</i>"}
    html = make_saving("<html><head></head><body></body></html>", who)
    assert "</script><b>" not in html and "<i>lead" not in html
    assert "x&lt;/script&gt;&lt;b&gt;@example.com" in html


def test_a_page_without_a_plain_body_tag_is_refused() -> None:
    with pytest.raises(ValueError):
        make_saving('<html><head></head><body class="x"></body></html>', ME)


def test_the_explorer_on_a_saving_page_never_takes_a_typed_or_remembered_identity() -> None:
    text = EXPLORER.read_text(encoding="utf-8")
    saves = text[text.index("const SAVES = ") : text.index("function saveSignOff()")]
    # In saving mode the identity comes from the saved settings only: not the browser's memory,
    # not a field on the page.
    assert "if (SAVES) {" in saves and "localStorage" in saves.split("} else {")[1]
    assert "localStorage" not in saves.split("} else {")[0]
    control = text[
        text.index("function renderSignOffControl()") : text.index("function reviewAll()")
    ]
    first = control[: control.index("return wrap;") + len("return wrap;")]
    assert "if (SAVES)" in first and "input" not in first  # no field to type in
    assert "function saveSignOff() { if (SAVES) return;" in text


def test_the_explorer_sends_the_same_block_it_would_have_shown() -> None:
    text = EXPLORER.read_text(encoding="utf-8")
    gen = text[text.index("function generateFeedback()") : text.index("// ---- Wire up ----")]
    send = gen.index("window.agamiSave({ page: 'model-explorer'")
    assert (
        gen.index("lines.push('done')")
        < send
        < gen.index("document.getElementById('modal').hidden = false")
    )
    assert "block: lines.join('\\n') + '\\n'" in gen


def test_a_bulk_approval_on_the_page_is_marked_as_one() -> None:
    text = EXPLORER.read_text(encoding="utf-8")
    bulk = text[text.index("function bulkApprove(") : text.index("const KIND_NOUN")]
    assert "bulk: true" in bulk
    single = text[text.index("function toggleApprove(") : text.index("function approveBtn(")]
    assert "bulk" not in single


def test_the_personal_data_tab_can_hide_columns() -> None:
    text = EXPLORER.read_text(encoding="utf-8")
    pii = text[text.index("function piiRowCard(") : text.index("// TAB: Subject areas")]
    assert "toggleOp('field', r.f)" in pii and "'Hide'" in pii
    assert "'Hide all ' + fl.length + ' shown'" in pii
    # the read-only view hides them with the other personal-data controls
    assert "btn-pii pii-hide" in pii and "btn-pii pii-hide-all" in pii


def test_the_examples_page_sends_each_decision_with_its_number_area_and_question() -> None:
    text = EXAMPLES.read_text(encoding="utf-8")
    save = text[text.index("const SAVE_ACTION") : text.index("function generateFeedback()")]
    assert "{ validate: 'keep', reject: 'drop', edit: 'fix', note: 'note' }" in save
    for key in ("n: Number(n)", "area: it.area", "question: it.question", "note_all: globalNote()"):
        assert key in save, key
    # an edit or a note still being typed is not sent half-done
    assert "Press Save edit or Save note" in save


def test_the_read_only_banner_says_nothing_on_the_page_is_saved() -> None:
    assert "nothing on it is saved" in BANNER_TEXT
    assert BANNER_TEXT in make_read_only("<html><head></head><body></body></html>")


# --- the renderers ------------------------------------------------------------------------------

yaml = pytest.importorskip("yaml")
pytest.importorskip("pydantic")


@pytest.fixture
def artifacts(tmp_path: Path) -> Path:
    """A one-table model with one unreviewed metric, under <artifacts>/demo, and a saved identity."""
    root = tmp_path / "demo"
    (root / "datasources" / "c").mkdir(parents=True)
    (root / "subject_areas" / "sales" / "tables").mkdir(parents=True)
    (root / "subject_areas" / "sales" / "metrics").mkdir(parents=True)
    (root / "datasource.yaml").write_text(
        yaml.safe_dump(
            {
                "datasource": "demo",
                "version": 1,
                "storage_connections": [{"name": "c", "ref": "datasources/c/storage.yaml"}],
                "subject_areas": ["subject_areas/sales"],
            }
        )
    )
    (root / "datasources" / "c" / "storage.yaml").write_text(
        yaml.safe_dump({"name": "c", "storage_type": "PostgreSQL"})
    )
    (root / "subject_areas" / "sales" / "subject_area.yaml").write_text(
        yaml.safe_dump(
            {
                "name": "sales",
                "tables": [{"storage_connection": "c", "schema": "public", "table": "orders"}],
            }
        )
    )
    (root / "subject_areas" / "sales" / "tables" / "orders.yaml").write_text(
        yaml.safe_dump(
            {
                "name": "orders",
                "schema": "public",
                "storage_connection": "c",
                "grain": ["id"],
                "description": "orders",
                "columns": [
                    {"name": "id", "type": "integer", "primary_key": True},
                    {"name": "amount", "type": "decimal", "description": "order amount"},
                ],
            }
        )
    )
    (root / "subject_areas" / "sales" / "metrics" / "revenue.yaml").write_text(
        yaml.safe_dump(
            {
                "name": "revenue",
                "calculation": "Total order amount",
                "bindings": {"postgres": "SUM(orders.amount)"},
                "source_tables": ["orders"],
                "description": "Total order amount",
            }
        )
    )
    (tmp_path / "local").mkdir()
    (tmp_path / "local" / ".config").write_text(
        json.dumps({"reviewer_email": "you@example.com", "reviewer_role": "Analyst"})
    )
    return tmp_path


def _render_explorer(monkeypatch, artifacts: Path, *extra: str) -> tuple[int, Path]:
    import render_model_explorer

    out = artifacts / "out.html"
    argv = [
        "render_model_explorer.py",
        "--profile",
        "demo",
        "--artifacts-dir",
        str(artifacts),
        "--out",
        str(out),
        *extra,
    ]
    monkeypatch.setattr(sys, "argv", argv)
    return render_model_explorer.main(), out


def _manifest(html: str) -> dict:
    return json.loads(
        re.search(r"const manifest = (\{.*?\});\n", html).group(1).replace("<\\/", "</")
    )


def test_the_explorer_renders_a_saving_page_deep_linked_to_a_section(
    monkeypatch, artifacts: Path
) -> None:
    rc, out = _render_explorer(
        monkeypatch,
        artifacts,
        "--initial-tab",
        "review",
        "--section",
        "metrics",
        "--save-to-helper",
    )
    assert rc == 0
    html = out.read_text(encoding="utf-8")
    assert (
        '<body class="agami-saves">' in html
        and "agami-read-only" not in html.split("<body", 1)[1][:40]
    )
    manifest = _manifest(html)
    assert manifest["initial_tab"] == "review" and manifest["initial_section"] == ["Metrics"]
    assert "you@example.com</strong> (Analyst)" in html


def test_the_explorer_opens_on_the_personal_data_tab(monkeypatch, artifacts: Path) -> None:
    rc, out = _render_explorer(monkeypatch, artifacts, "--initial-tab", "pii", "--read-only")
    assert rc == 0
    assert _manifest(out.read_text(encoding="utf-8"))["initial_tab"] == "pii"


def test_a_section_needs_the_review_tab(monkeypatch, artifacts: Path, capsys) -> None:
    rc, out = _render_explorer(
        monkeypatch, artifacts, "--initial-tab", "tables", "--section", "joins"
    )
    assert rc == 2 and not out.exists()
    assert "--section needs --initial-tab review" in capsys.readouterr().err


def test_read_only_and_saving_are_one_or_the_other(monkeypatch, artifacts: Path) -> None:
    with pytest.raises(SystemExit):
        _render_explorer(monkeypatch, artifacts, "--read-only", "--save-to-helper")


def test_the_section_names_match_the_pages_review_piles() -> None:
    import render_model_explorer

    text = EXPLORER.read_text(encoding="utf-8")
    for labels in render_model_explorer.REVIEW_SECTIONS.values():
        for label in labels:
            assert f"label: '{label}'" in text, label
    # the joins section reaches the cross-schema joins pile too
    assert render_model_explorer.REVIEW_SECTIONS["joins"] == ["Joins", "Cross-schema joins"]


def _render_examples(monkeypatch, tmp_path: Path, *extra: str) -> Path:
    import render_examples_validation

    items = tmp_path / "items.json"
    items.write_text(
        json.dumps(
            [
                {
                    "question": "How many orders?",
                    "sql": "SELECT COUNT(*) FROM orders",
                    "area": "sales",
                }
            ]
        )
    )
    out = tmp_path / "ex.html"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "render_examples_validation.py",
            "--title",
            "t",
            "--profile",
            "demo",
            "--items-file",
            str(items),
            "--out",
            str(out),
            *extra,
        ],
    )
    assert render_examples_validation.main() == 0
    return out


def test_the_examples_page_renders_as_a_saving_page(monkeypatch, artifacts: Path) -> None:
    out = _render_examples(
        monkeypatch, artifacts, "--save-to-helper", "--artifacts-dir", str(artifacts)
    )
    html = out.read_text(encoding="utf-8")
    assert '<body class="agami-saves">' in html
    assert "you@example.com</strong> (Analyst)" in html
    assert f"b.textContent = {json.dumps(SAVE_LABEL)}" in html
    assert "press I've marked them on the page" in html  # the examples card's own wording


def test_the_examples_page_renders_read_only_too(monkeypatch, tmp_path: Path) -> None:
    assert (
        '<body class="agami-read-only">'
        in _render_examples(monkeypatch, tmp_path, "--read-only").read_text()
    )


def test_the_examples_page_needs_to_know_who_signs(monkeypatch, tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        _render_examples(monkeypatch, tmp_path, "--save-to-helper")


# --- applying what the page saved -----------------------------------------------------------------

BLOCK = (
    "profile: demo\nsigned-off-by: you@example.com / Analyst\nexclude columns: sales.orders.amount\n"
    'curate-ops:\n[{"op":"approve","kind":"metric","area":"sales","name":"revenue","bulk":true}]\ndone\n'
)


def _saved(tmp_path: Path, **over) -> Path:
    saved = tmp_path / "decisions.json"
    saved.write_text(
        json.dumps({"page": "model-explorer", "profile": "demo", "block": BLOCK, **over})
    )
    return saved


def test_the_parser_reads_the_helpers_file_as_it_reads_a_pasted_block(
    tmp_path: Path, capsys
) -> None:
    assert F.main(["--block-file", str(_saved(tmp_path)), "--expect-profile", "demo"]) == 0
    from_file = json.loads(capsys.readouterr().out)
    pasted = tmp_path / "pasted.txt"
    pasted.write_text(BLOCK)
    assert F.main(["--block-file", str(pasted)]) == 0
    from_paste = json.loads(capsys.readouterr().out)
    # the same ops; only the signer differs: a page signs as .config's identity, never its own line
    assert from_paste["data"]["signer"] == "you@example.com"
    assert from_file["data"]["signer"] is None and from_file["data"]["role"] is None
    from_paste["data"]["signer"] = from_paste["data"]["role"] = None
    assert from_file == from_paste
    assert from_file["data"]["ops"][0] == {
        "op": "exclude",
        "kind": "table",
        "area": "sales",
        "name": "orders",
        "column": "amount",
    }


@pytest.mark.parametrize(
    ("over", "args", "why"),
    [
        ({}, [], "needs --expect-profile"),
        ({"page": "examples"}, ["--expect-profile", "demo"], "not the model explorer"),
        ({"profile": "other"}, ["--expect-profile", "demo"], "another model"),
        (
            {},
            ["--expect-profile", "other"],
            "another model",
        ),  # the block's own profile line says demo
        (
            {"profile": "other", "block": BLOCK.replace("profile: demo", "profile: other")},
            ["--expect-profile", "demo"],
            "another model",
        ),
    ],
)
def test_a_decisions_file_for_another_page_or_model_is_refused(
    tmp_path: Path, capsys, over, args, why
) -> None:
    assert F.main(["--block-file", str(_saved(tmp_path, **over)), *args]) == 2
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is False and why in out["error"]


@pytest.mark.parametrize("text", ["{not json", '{"block": 3}', "[1, 2]", "profile: demo\ndone\n"])
def test_text_that_is_not_the_helpers_file_is_parsed_as_a_block(text: str) -> None:
    assert F.block_text(text) == text


def test_a_bulk_approval_is_recorded_as_one_in_the_curation_log(artifacts: Path) -> None:
    pytest.importorskip("sqlglot")
    from semantic_model import curate

    root = artifacts / "demo"
    data, _, needs = F.parse(BLOCK)
    assert needs is None
    res = curate.apply(root, data["ops"], signer="you@example.com", role="Analyst")
    assert res.validated, res.errors
    log = [json.loads(line) for line in (root / "curation_log.jsonl").read_text().splitlines()]
    approve = next(e for e in log if e["op"] == "approve")
    assert approve["bulk"] is True and approve["signer"] == "you@example.com"
    assert next(e for e in log if e["op"] == "exclude").get("bulk") is None


# --- the pages' own save guards, executed under node (#651 review) ----------------------------


def _node(script: str) -> dict:
    import shutil
    import subprocess

    node = shutil.which("node")
    if node is None:
        pytest.skip("node not installed")
    out = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip().splitlines()[-1])


def _between(text: str, start: str, end: str) -> str:
    i = text.index(start)
    return text[i : text.index(end, i)]


_EXAMPLES_SAVE = _between(
    EXAMPLES.read_text(encoding="utf-8"), "const SAVE_ACTION", "function generateFeedback()"
)


def _examples(signer, pending: dict) -> dict:
    return _node(
        "let posted = null, said = null;\n"
        "const window = { AGAMI_SAVE: { signer: " + json.dumps(signer) + " } };\n"
        "window.agamiSave = (p) => { posted = p; return true; };\n"
        "window.agamiSave.say = (m) => { said = m; };\n"
        "const items = [{n: 1, area: 'sales', question: 'How many orders?'}, {n: 2, area: 'sales', question: 'Revenue?'}];\n"
        "const profile = 'demo'; const globalNote = () => '';\n"
        "const pendingActions = "
        + json.dumps(pending)
        + ";\n"
        + _EXAMPLES_SAVE
        + "\nsaveDecisions(); console.log(JSON.stringify({posted, said}));"
    )


def test_the_examples_page_refuses_a_keep_with_no_saved_identity() -> None:
    got = _examples(None, {"1": {"action": "validate"}, "2": {"action": "reject"}})
    assert got["posted"] is None and "no sign-off identity is saved" in got["said"]
    # drops alone need no identity, and with one a keep is sent
    assert _examples(None, {"2": {"action": "reject"}})["posted"]["decisions"] == [
        {"n": 2, "area": "sales", "question": "Revenue?", "action": "drop"}
    ]
    kept = _examples(ME, {"1": {"action": "validate"}})["posted"]["decisions"]
    assert kept == [{"n": 1, "area": "sales", "question": "How many orders?", "action": "keep"}]


def test_the_examples_page_refuses_to_save_a_half_done_edit() -> None:
    got = _examples(ME, {"1": {"action": "edit", "sql": "SELECT 1", "editSaved": False}})
    assert got["posted"] is None and "Press Save edit or Save note on #1" in got["said"]


_EXPLORER = EXPLORER.read_text(encoding="utf-8")
_BLOCKER = _between(_EXPLORER, "function saveBlocker()", "function generateFeedback()")
_IDENTITY = "\n".join(
    line
    for line in _EXPLORER.splitlines()
    if line.strip().startswith(("function validEmail(", "function hasQueuedApprovals("))
)


def _blocker(state: str) -> str | None:
    return _node(
        "let inlineEditKey = null, addMetricOpen = false, addJoinOpen = false, addExampleOpen = false;\n"
        "const editOpen = new Set(); const ops = {}; let signOff = { email: '' };\n"
        + _IDENTITY
        + "\n"
        + _BLOCKER
        + "\n"
        + state
        + "\nconsole.log(JSON.stringify({why: saveBlocker()}));"
    )["why"]


@pytest.mark.parametrize(
    "state",
    [
        "inlineEditKey = 'sales.orders|description';",
        "editOpen.add('sales.revenue');",
        "addMetricOpen = true;",
        "addJoinOpen = true;",
        "addExampleOpen = true;",
    ],
)
def test_the_explorer_will_not_save_while_an_edit_is_open(state: str) -> None:
    assert _blocker(state).startswith("⚠ Press Save on the open edit first, or cancel it")


def test_the_explorer_will_not_save_an_approval_without_a_saved_identity() -> None:
    approve = "ops['sales.revenue'] = { op: 'approve' };"
    assert "Approvals need a sign-off identity" in _blocker(approve)
    assert _blocker(approve + " signOff.email = 'you@example.com';") is None
    assert _blocker("ops['sales.orders'] = { op: 'exclude' };") is None  # a hide needs no identity


def test_the_explorer_checks_before_it_sends() -> None:
    gen = _between(_EXPLORER, "function generateFeedback()", "// ---- Wire up ----")
    assert gen.index("const why = saveBlocker()") < gen.index("window.agamiSave(")


def test_a_saved_page_says_to_press_the_cards_button() -> None:
    html = make_saving("<html><head></head><body></body></html>", ME)
    assert "Go back to the chat and press I've saved it" in html
    assert "You can close this page" not in html
    custom = make_saving(
        "<html><head></head><body></body></html>", ME, back_label="I've marked them on the page"
    )
    assert "press I've marked them on the page" in custom
    assert (
        "classList.add('agami-save-over')" in html and "body.agami-save-over #btn-generate" in html
    )
