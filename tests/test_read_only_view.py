"""The read-only full view: a page shown from a question card hides its change controls.

A live run showed the chart page ending in "Paste this back to Claude", a second way to say what
the answer card already asks. `--read-only` hides every control that changes something and says to
decide on the cards; tabs, filters and search keep working. Every renderer a skill calls must be given the flag.
"""

from __future__ import annotations

import re
import sys
from html.parser import HTMLParser
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PLUGIN = REPO_ROOT / "plugins" / "agami"
SCRIPTS = PLUGIN / "scripts"
sys.path.insert(0, str(SCRIPTS))

from _read_only_view import BANNER_TEXT, CHANGE_CONTROLS, make_read_only  # noqa: E402

TEMPLATES = sorted((PLUGIN / "shared").glob("*-template.html"))


def test_every_template_can_be_made_read_only() -> None:
    assert TEMPLATES
    for path in TEMPLATES:
        html = make_read_only(path.read_text())
        assert '<body class="agami-read-only">' in html, path.name
        assert BANNER_TEXT in html, path.name
        assert html.index('id="agami-read-only"') < html.index("</head>"), path.name


def test_change_controls_are_listed_not_every_button() -> None:
    # A blanket rule on every button hid the filter chips and "Show all" / "Clear filters" too.
    style = make_read_only("<html><head></head><body></body></html>")
    assert "button:not(" not in style
    for hidden in (
        ".sticky-footer",
        ".feedback-bar",
        ".modal-backdrop",
        ".signoff-card",
        ".card-actions",
        ".field-actions",
        ".table-actions",
        ".actions-row",
        ".grade.opts",
        ".map-actions",
    ):
        assert f"body.agami-read-only {hidden}" in style, hidden


class _Tree(HTMLParser):
    """The static elements of a template, each with its ancestors' (tag, attrs) chain."""

    _VOID = {"input", "br", "img", "meta", "link", "hr", "source", "wbr"}

    def __init__(self) -> None:
        super().__init__()
        self.stack: list[tuple[str, dict]] = []
        self.elements: list[list[tuple[str, dict]]] = []

    def handle_starttag(self, tag, attrs):
        node = (tag, {k: v or "" for k, v in attrs})
        self.elements.append([*self.stack, node])
        if tag not in self._VOID:
            self.stack.append(node)

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                del self.stack[i:]
                return


def _matches(simple: str, node: tuple[str, dict]) -> bool:
    """A compound selector of classes, an optional tag and `[attr="v"]` against one element."""
    tag, attrs = node
    m = re.fullmatch(r'([a-z]*)((?:\.[\w-]+)*)(?:\[([\w-]+)="([^"]*)"\])?', simple)
    assert m, simple
    classes = set(attrs.get("class", "").split())
    want = [c for c in m.group(2).split(".") if c]
    return (
        (not m.group(1) or m.group(1) == tag)
        and set(want) <= classes
        and (not m.group(3) or attrs.get(m.group(3)) == m.group(4))
    )


def _hidden(chain: list[tuple[str, dict]]) -> list[str]:
    hits = []
    for sel in CHANGE_CONTROLS:
        if "+" in sel:
            continue  # sibling rules hide one named element, never a filter
        parts = sel.split()
        if (
            _matches(parts[-1], chain[-1])
            if len(parts) == 1
            else (_matches(parts[-1], chain[-1]) and any(_matches(parts[0], a) for a in chain[:-1]))
        ):
            hits.append(sel)
        elif len(parts) == 1 and any(_matches(sel, a) for a in chain[:-1]):
            hits.append(sel)
    return hits


@pytest.mark.parametrize("path", TEMPLATES, ids=lambda p: p.name)
def test_filter_controls_stay_visible(path: Path) -> None:
    tree = _Tree()
    tree.feed(path.read_text())
    filters = [
        chain
        for chain in tree.elements
        if chain[-1][0] in ("button", "input")
        and ("data-filter" in chain[-1][1] or chain[-1][1].get("id") in ("clear", "q", "search"))
    ]
    offenders = [f"{chain[-1][1]}: {_hidden(chain)}" for chain in filters if _hidden(chain)]
    assert not offenders, offenders
    # Chips, pills and filter buttons the page builds in script are never on the hide-list.
    for cls in (
        "chip",
        "pill",
        "filter",
        "filter-chips",
        "tab-bar",
        "subtab-bar",
        "btn-prev",
        "btn-next",
    ):
        assert not any(re.search(rf"\.{cls}(?![\w-])", sel) for sel in CHANGE_CONTROLS), cls


def test_the_filter_check_sees_a_hidden_filter() -> None:
    chain = [("div", {"class": "sticky-footer"}), ("button", {"data-filter": "all"})]
    assert _hidden(chain) == [".sticky-footer"]


def test_a_page_without_a_plain_body_tag_is_refused() -> None:
    try:
        make_read_only('<html><head></head><body class="x"></body></html>')
    except ValueError:
        return
    raise AssertionError("expected ValueError")


def test_render_chart_writes_a_read_only_page(tmp_path: Path, monkeypatch) -> None:
    sections = tmp_path / "sections.json"
    sections.write_text(
        '[{"title": "Orders by status", "insights": "", "chart_type": "bar", "labels": ["paid"],'
        ' "datasets": [{"label": "orders", "data": [3]}], "table_headers": ["status", "orders"],'
        ' "table_rows": [["paid", "3"]], "sql": "SELECT 1"}]'
    )
    receipt = tmp_path / "receipt.json"
    receipt.write_text(
        '{"model_version": null'
        + "".join(
            f', "{s}": {{"items": [], "undetermined": null}}'
            for s in ("columns", "tables", "joins", "aggregates", "assumptions")
        )
        + "}"
    )
    out = tmp_path / "chart.html"
    import render_chart

    args = [
        "render_chart.py",
        "--title",
        "Orders",
        "--sections-file",
        str(sections),
        "--receipt-file",
        str(receipt),
    ]
    monkeypatch.setattr(sys, "argv", [*args, "--out", str(out), "--read-only"])
    assert render_chart.main() == 0
    assert BANNER_TEXT in out.read_text()


def test_every_renderer_a_skill_calls_is_given_read_only() -> None:
    offenders = []
    for skill in sorted((PLUGIN / "skills").glob("*/SKILL.md")):
        for block in re.findall(r"```bash\n(.*?)```", skill.read_text(), re.S):
            for call in re.findall(r"render_[a-z_]+\.py.*?--out", block, re.S):
                if "--read-only" not in call:
                    offenders.append(f"{skill.parent.name}: {call.splitlines()[0]}")
    assert not offenders, offenders
