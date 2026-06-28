"""The admin/auth design polish — self-hosted Inter (no font-CDN call) + the browser-local time script."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytest.importorskip("starlette")
pytest.importorskip("mcp")

PKG_SRC = Path(__file__).resolve().parent.parent / "packages" / "agami-core" / "src"
if str(PKG_SRC) not in sys.path:
    sys.path.insert(0, str(PKG_SRC))

import mcp_http  # noqa: E402
import ui  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402


def test_css_self_hosts_inter_and_calls_no_font_cdn():
    css = ui._CSS
    assert "/static/fonts/inter-400.woff2" in css and "font-family:Inter" in css
    # the admin's browser must never reach out to a font CDN
    assert "fonts.googleapis.com" not in css and "fonts.gstatic.com" not in css


def test_pages_include_the_browser_local_time_script():
    page = ui.auth_page("t", '<time data-utc="2026-01-01T00:00:00Z">2026-01-01T00:00:00Z</time>')
    assert "time[data-utc]" in page and "toLocaleString" in page


def test_console_is_full_width():
    assert ".main{max-width:none" in ui._CSS.replace(" ", "")


def test_inter_font_files_are_served(monkeypatch):
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://demo.example.com")
    c = TestClient(mcp_http.build_app())
    r = c.get("/static/fonts/inter-400.woff2")
    assert r.status_code == 200 and int(r.headers["content-length"]) > 1000


def test_password_fields_get_a_reveal_eye_toggle():
    assert ".pw-eye" in ui._CSS and ".pw-field" in ui._CSS  # the eye-toggle styles ship
    # the toggle script is emitted on every page; it no-ops where there's no password field.
    page = ui.auth_page("t", '<input type="password" name="password">')
    assert "input[type=password]" in page and "Show password" in page


def test_drawer_uses_adjacent_sibling_not_general_sibling():
    # One drawer per row (Sessions / Tool calls): the general-sibling `~` revealed EVERY later drawer
    # at once — any row opened the same (last) one, and the backdrop toggled the wrong checkbox so it
    # wouldn't close. The adjacent-sibling `+` scopes each toggle to its own drawer.
    css = ui._CSS
    assert ".drawer-toggle:checked + .drawer-wrap" in css
    assert ".drawer-toggle:checked ~ .drawer-wrap" not in css


def test_drawer_backdrop_resets_the_inherited_label_margin():
    # The backdrop is a <label>; the global `label{margin:16px 0 6px}` would push it 16px down,
    # leaving an uncovered strip (the topbar showing through) at the top of the overlay. margin:0 fixes it.
    import re

    m = re.search(r"\.drawer-backdrop\{[^}]*\}", ui._CSS)
    assert m is not None and "margin:0" in m.group(0)
