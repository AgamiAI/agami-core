"""The admin read-only Model explorer.

A pure projection of the served model (`load_organization`) + domain docs (`load_memory`): the
overview / area landing / table page render the served tree; it is admin-gated, read-only (a GET
only), never leaks `storage_config`, and escapes operator-authored text.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytest.importorskip("starlette")
pytest.importorskip("mcp")
pytest.importorskip("argon2")
pytest.importorskip("pydantic")

PKG_SRC = Path(__file__).resolve().parent.parent / "packages" / "agami-core" / "src"
if str(PKG_SRC) not in sys.path:
    sys.path.insert(0, str(PKG_SRC))

import mcp_http  # noqa: E402
import model_store  # noqa: E402
import user_store  # noqa: E402
from semantic_model.models import Organization  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402
from store import Store  # noqa: E402

BASE = "https://your-host.example.com"
SECRET = "x" * 40
ADMIN_USER = "admin@example.com"
ADMIN_PW = "admin-password-localtest"

# A neutral served model — `storage_config` carries a sentinel that must NEVER reach the rendered page.
ORG = {
    "organization": "acme",
    "version": 1,
    "description": "Acme Commerce — the deployed model.",
    "fiscal_year_start_month": 1,
    "key_terminology": {"AOV": "average order value"},
    "storage_connections": [
        {
            "name": "warehouse",
            "storage_type": "PostgreSQL",
            "storage_config": {"host": "host-should-not-render"},
        }
    ],
    "subject_areas": [
        {
            "name": "Sales",
            "description": "Orders and revenue.",
            "default_time_window": "last_90_days",
            "tables": [{"storage_connection": "warehouse", "schema": "public", "table": "orders"}],
            "tables_defined": [
                {
                    "name": "orders",
                    "schema": "public",
                    "storage_connection": "warehouse",
                    "grain": ["id"],
                    "description": "One row per order.",
                    "columns": [
                        {"name": "id", "type": "integer", "primary_key": True},
                        {"name": "amount", "type": "decimal", "unit": "USD"},
                    ],
                    "performance_hints": {"estimated_row_count": 184000},
                }
            ],
            "metrics": [
                {
                    "name": "revenue",
                    "calculation": "sum of amount",
                    "other_names": ["sales"],
                    "unit": "USD",
                }
            ],
            "entities": [{"name": "customer", "value_pattern": "^C[0-9]+$"}],
            "relationships": [],
        }
    ],
}


def _seed(url, datasource="SALES_DATA", org=ORG):
    s = Store.connect(url)
    s.run_migrations()
    model_store.write_organization(s, datasource, Organization.model_validate(org))
    s.close()


@pytest.fixture
def env(tmp_path, monkeypatch):
    url = "sqlite://" + str(tmp_path / "model.db")
    monkeypatch.setenv("PUBLIC_BASE_URL", BASE)
    monkeypatch.setenv("AGAMI_DB_URL", url)
    monkeypatch.setenv("AGAMI_SIGNING_SECRET", SECRET)
    monkeypatch.setenv("AGAMI_ADMIN_USERNAME", ADMIN_USER)
    monkeypatch.setenv("AGAMI_ADMIN_PASSWORD", ADMIN_PW)
    for v in ("AGAMI_OIDC_GOOGLE_CLIENT_ID", "AGAMI_OIDC_GOOGLE_CLIENT_SECRET"):
        monkeypatch.delenv(v, raising=False)
    s = Store.connect(url)
    s.run_migrations()
    user_store.seed_admin_from_env(s)
    s.close()
    return url


@pytest.fixture
def client(env):
    return TestClient(mcp_http.build_app(), base_url=BASE)


def _login(c):
    c.post("/admin/login", data={"username": ADMIN_USER, "password": ADMIN_PW})


# --- read helper -------------------------------------------------------------


def test_list_datasources_is_sorted(env):
    _seed(env, "ZED")
    _seed(env, "ACME")
    s = Store.connect(env)
    assert model_store.list_datasources(s) == ["ACME", "ZED"]
    s.close()


# --- gating + empty state ----------------------------------------------------


def test_model_requires_a_session(client):
    assert client.get("/admin/model", follow_redirects=False).status_code == 302


def test_model_route_is_get_only(client, env):
    # Read-only by construction: no write verb is mounted at /admin/model.
    _login(client)
    assert client.post("/admin/model", follow_redirects=False).status_code == 405


def test_empty_state_when_no_model(client, env):
    _login(client)
    html = client.get("/admin/model").text
    assert "No model deployed yet" in html


# --- overview ----------------------------------------------------------------


def test_overview_renders_org_and_areas(client, env):
    _seed(env)
    _login(client)
    html = client.get("/admin/model").text
    assert "acme" in html and "Sales" in html
    assert "AOV" in html and "average order value" in html  # glossary
    assert "warehouse" in html and "PostgreSQL" in html  # storage names/types
    assert "Model" in html  # the tab is present in the shell


def test_overview_never_leaks_storage_config(client, env):
    _seed(env)
    _login(client)
    assert "host-should-not-render" not in client.get("/admin/model").text


def test_picker_only_when_multiple_datasources(client, env):
    _seed(env, "SALES_DATA")
    _login(client)
    assert 'name="datasource"' not in client.get("/admin/model").text  # one → no picker
    _seed(env, "MARKETING")
    assert 'name="datasource"' in client.get("/admin/model").text  # two → picker


# --- area landing ------------------------------------------------------------


def test_area_landing_renders_tables_metrics_entities(client, env):
    _seed(env)
    _login(client)
    html = client.get("/admin/model?datasource=SALES_DATA&area=Sales").text
    assert "orders" in html  # its table
    assert "revenue" in html and "sum of amount" in html  # a metric + its calculation
    assert "customer" in html  # an entity


def test_unknown_area_falls_back_to_overview(client, env):
    _seed(env)
    _login(client)
    # A stale/unknown area param must not 500 — it degrades to the datasource overview.
    html = client.get("/admin/model?datasource=SALES_DATA&area=Nope").text
    assert "acme" in html and "Subject areas" in html
