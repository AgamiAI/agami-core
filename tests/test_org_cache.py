"""ACE-045 Slice 2: get_cached_org serves the semantic model warm across queries (one load,
then cache hits), reloads when the model version bumps, and is ORG-SCOPED — a multi-tenant
server never serves one org's model to another, even on the same datasource name."""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agami" / "scripts"))

import tools  # noqa: E402

# Cache isolation between tests is handled once in tests/conftest.py::_reset_org_cache (autouse) —
# no per-file duplicate here.


def test_loads_once_then_serves_warm(monkeypatch):
    calls = {"load": 0}
    monkeypatch.setattr(tools, "_model_version", lambda profile: "v1")

    def fake_load(profile):
        calls["load"] += 1
        return {"org": profile}

    monkeypatch.setattr(tools, "_load_org", fake_load)

    first = tools.get_cached_org("sales")
    second = tools.get_cached_org("sales")
    assert calls["load"] == 1  # query #2 served warm from the cache
    assert first is second  # the same cached Organization object


def test_reloads_after_version_bump(monkeypatch):
    calls = {"load": 0}
    versions = iter(["v1", "v1", "v2"])
    monkeypatch.setattr(tools, "_model_version", lambda profile: next(versions))

    def fake_load(profile):
        calls["load"] += 1
        return object()

    monkeypatch.setattr(tools, "_load_org", fake_load)

    tools.get_cached_org("sales")  # v1 -> load
    tools.get_cached_org("sales")  # v1 -> warm
    tools.get_cached_org("sales")  # v2 -> reload
    assert calls["load"] == 2
    assert len(tools._ORG_CACHE) == 1  # stale v1 entry evicted, only v2 remains


def test_org_scoped_no_cross_tenant(monkeypatch):
    monkeypatch.setattr(tools, "_model_version", lambda profile: "v1")

    def fake_load(profile):
        return {"org_id": tools._current_org_id(), "profile": profile}

    monkeypatch.setattr(tools, "_load_org", fake_load)

    tools._current_org_ctx.set("orgA")
    a = tools.get_cached_org("sales")
    tools._current_org_ctx.set("orgB")
    b = tools.get_cached_org("sales")  # SAME datasource name, different org

    assert a is not b
    assert a["org_id"] == "orgA" and b["org_id"] == "orgB"
    # org A never receives org B's cached model
    tools._current_org_ctx.set("orgA")
    assert tools.get_cached_org("sales") is a


def test_default_org_id_falls_back_to_local(monkeypatch):
    monkeypatch.delenv("AGAMI_ORG_ID", raising=False)
    tools._current_org_ctx.set(None)
    assert tools._current_org_id() == "local"
