"""The rendered chart page loads nothing from the network: Chart.js is inlined, not fetched."""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agami" / "scripts"))

from render_chart import render  # noqa: E402

SECTION = {
    "title": "Top customers",
    "chart_type": "bar",
    "labels": ["Acme", "Demo"],
    "datasets": [{"label": "Spend", "data": [3, 1]}],
    "table_headers": ["Customer", "Spend"],
    "table_rows": [["Acme", 3], ["Demo", 1]],
}


def test_rendered_chart_loads_nothing_remote():
    html = render(title="Top customers", summary="", sections=[SECTION])
    # Every script tag, with or without a src, and every link or img that could fetch.
    for tag in re.findall(r"<(?:script|link|img|iframe)\b[^>]*>", html, flags=re.I):
        assert not re.search(r"""(?:src|href)\s*=\s*["']?\s*(?:https?:)?//""", tag, flags=re.I), tag
    assert "cdn.jsdelivr.net" not in html


def test_chart_library_is_inlined_with_its_licence():
    html = render(title="t", summary="", sections=[SECTION])
    assert "Chart.js v4.4.1" in html
    assert "Released under the MIT License" in html
    assert "{{CHARTJS}}" not in html
