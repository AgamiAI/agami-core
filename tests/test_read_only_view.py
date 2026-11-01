"""The read-only full view: a page shown from a question card hides its change controls.

A live run showed the chart page ending in "Paste this back to Claude", a second way to say what
the answer card already asks. `--read-only` hides every control that changes something and says to
decide on the cards; tabs keep working. Every renderer a skill calls must be given the flag.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PLUGIN = REPO_ROOT / "plugins" / "agami"
SCRIPTS = PLUGIN / "scripts"
sys.path.insert(0, str(SCRIPTS))

from _read_only_view import BANNER_TEXT, make_read_only  # noqa: E402

TEMPLATES = sorted((PLUGIN / "shared").glob("*-template.html"))


def test_every_template_can_be_made_read_only() -> None:
    assert TEMPLATES
    for path in TEMPLATES:
        html = make_read_only(path.read_text())
        assert '<body class="agami-read-only">' in html, path.name
        assert BANNER_TEXT in html, path.name
        assert html.index('id="agami-read-only"') < html.index("</head>"), path.name


def test_tab_buttons_stay_and_change_buttons_go() -> None:
    style = make_read_only("<html><head></head><body></body></html>")
    assert "button:not(.tab-bar button):not(.subtab-bar button)" in style
    for hidden in (".sticky-footer", ".feedback-bar", ".modal-backdrop", ".signoff-card"):
        assert hidden in style


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
        + "".join(f', "{s}": {{"items": [], "undetermined": null}}' for s in ("columns", "tables", "joins", "aggregates", "assumptions"))
        + "}"
    )
    out = tmp_path / "chart.html"
    import render_chart

    args = ["render_chart.py", "--title", "Orders", "--sections-file", str(sections), "--receipt-file", str(receipt)]
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
