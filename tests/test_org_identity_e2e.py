"""F14 / ACE-056–057 end-to-end: the deployment mints a portable org identity, keeps it immutable,
and the deploy-stamp + serve-resolve agree on it — the invariant the whole feature rests on.

No network is touched (the id is a local uuid4 + a file write); `tests/test_privacy_no_network.py`
is the separate static gate that proves no egress primitive was introduced.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytest.importorskip("pydantic")
pytest.importorskip("yaml")

PKG_SRC = Path(__file__).resolve().parent.parent / "packages" / "agami-core" / "src"
if str(PKG_SRC) not in sys.path:
    sys.path.insert(0, str(PKG_SRC))

import tools  # noqa: E402
from semantic_model import build, loader  # noqa: E402
from semantic_model.models import Organization  # noqa: E402

_HEX = set("0123456789abcdef")


def _minimal_org(name: str = "acme") -> Organization:
    return Organization(organization=name)


def test_write_tree_mints_uuid4_and_is_immutable(tmp_path):
    prof = tmp_path / "acme"
    build.write_tree(_minimal_org(), prof)

    minted = loader.load_org_id(prof)
    assert minted and len(minted) == 32 and set(minted) <= _HEX  # a uuid4 hex, minted locally

    # load -> mutate -> rewrite: the id is preserved, never re-minted (immutability guarantee).
    org = loader.load_organization(prof)
    assert org.org_id == minted
    org.description = "edited"
    build.write_tree(org, prof)
    assert loader.load_org_id(prof) == minted


def test_deployment_scoped_second_profile_adopts_the_id(tmp_path):
    # A company with several datasources is ONE tenant: a new profile adopts the deployment's id
    # rather than minting a second one.
    build.write_tree(_minimal_org("acme"), tmp_path / "sales")
    deployment_id = loader.load_org_id(tmp_path / "sales")

    build.write_tree(_minimal_org("acme"), tmp_path / "support")
    assert loader.load_org_id(tmp_path / "support") == deployment_id
    assert loader.deployment_org_id(tmp_path) == deployment_id


def test_deploy_and_serve_resolve_the_same_id(tmp_path, monkeypatch):
    # The load-bearing invariant: the deploy-time stamp (_default_org) and the serve-time resolver
    # (resolved_org_id) call the SAME function over the SAME artifacts dir, so they can't disagree —
    # even with AGAMI_PROFILE unset and the model under a named profile (not 'default').
    import model_deploy

    build.write_tree(_minimal_org("acme"), tmp_path / "northpeak_salesforce")
    minted = loader.load_org_id(tmp_path / "northpeak_salesforce")

    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(tmp_path))
    monkeypatch.delenv("AGAMI_ORG_ID", raising=False)
    monkeypatch.delenv("AGAMI_PROFILE", raising=False)
    tools.resolved_org_id.cache_clear()

    assert model_deploy._default_org() == minted          # deploy stamps this
    assert tools.resolved_org_id() == minted              # serve resolves this
    assert tools._current_org_id() == minted              # ...and the per-request path agrees


def test_ensure_org_id_cli_mints_into_copied_model(tmp_path, capsys):
    # The sample-copy path (agami-connect 6A) drops a prebuilt org.yaml with no id; `sm ensure-org-id`
    # mints one into it. Idempotent: a second run prints the SAME id and doesn't rewrite.
    from semantic_model import cli

    prof = tmp_path / "agami-example"
    prof.mkdir()
    (prof / "org.yaml").write_text("organization: Acme Store\nversion: 1\n")

    assert cli.main(["ensure-org-id", str(prof)]) == 0
    minted = capsys.readouterr().out.strip()
    assert minted and len(minted) == 32 and set(minted) <= _HEX
    assert loader.load_org_id(prof) == minted  # persisted into org.yaml

    assert cli.main(["ensure-org-id", str(prof)]) == 0
    assert capsys.readouterr().out.strip() == minted  # idempotent — same id, no re-mint


def test_legacy_profile_without_org_id_resolves_local(tmp_path, monkeypatch):
    # A pre-F14 org.yaml (no org_id key) still loads and resolves to the 'local' sentinel — no crash,
    # no forced mint at serve time (serve is read-only; minting happens only at connect/build).
    (tmp_path / "old").mkdir()
    (tmp_path / "old" / "org.yaml").write_text("organization: legacy\nversion: 1\n")
    assert loader.load_org_id(tmp_path / "old") is None
    assert loader.load_organization(tmp_path / "old").org_id is None

    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(tmp_path))
    monkeypatch.delenv("AGAMI_ORG_ID", raising=False)
    tools.resolved_org_id.cache_clear()
    assert tools.resolved_org_id() == "local"
