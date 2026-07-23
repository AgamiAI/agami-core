"""F15 / ACE-067: the deployment-level organization record + the relocated org_id.

The record (`<artifacts_dir>/organization.yaml`) is the new home of F14's `org_id`: minted once,
immutable, deployment-scoped — but stored ABOVE the profiles so a multi-datasource company writes its
identity (and, later, its company context) once instead of per profile. These tests pin the mint-once
rule, the "second profile shares the id via the record (no sibling scan)" behaviour, the idempotent
legacy lift of a pre-record id, and lossless round-tripping of the record's structured content.

No network is touched (uuid4 + file I/O only); `tests/test_privacy_no_network.py` is the separate
static gate proving no egress primitive was introduced.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytest.importorskip("pydantic")
pytest.importorskip("yaml")

import yaml  # noqa: E402

PKG_SRC = Path(__file__).resolve().parent.parent / "packages" / "agami-core" / "src"
if str(PKG_SRC) not in sys.path:
    sys.path.insert(0, str(PKG_SRC))

import tools  # noqa: E402
from semantic_model import build, loader  # noqa: E402
from semantic_model import org_record as OR  # noqa: E402
from semantic_model.models import DisplayConventions, Organization, OrgRecord  # noqa: E402

_HEX = set("0123456789abcdef")


def _minimal_org(name: str = "acme") -> Organization:
    return Organization(organization=name)


def test_ensure_org_record_mints_once_into_organization_yaml(tmp_path):
    assert OR.load_org_record(tmp_path) is None  # nothing yet
    rec = OR.ensure_org_record(tmp_path)

    assert OR.record_path(tmp_path).exists()  # persisted at the artifacts-dir ROOT
    assert len(rec.org_id) == 32 and set(rec.org_id) <= _HEX  # a locally-minted uuid4 hex
    # Idempotent: a second call returns the SAME id, never a re-mint.
    assert OR.ensure_org_record(tmp_path).org_id == rec.org_id
    assert OR.load_org_record(tmp_path).org_id == rec.org_id


def test_second_profile_shares_the_record_id_without_a_sibling_scan(tmp_path, monkeypatch):
    # A company with several datasources stays ONE tenant — but F15 gets the shared id from the root
    # record, not by scanning sibling profiles. Assert the deletion: the sibling-scan resolver is never
    # consulted while writing the second profile.
    build.write_tree(_minimal_org("acme"), tmp_path / "sales")
    deployment_id = OR.load_org_record(tmp_path).org_id
    assert loader.load_org_id(tmp_path / "sales") == deployment_id  # profile stamp agrees

    calls = {"n": 0}
    real = loader.deployment_org_id

    def _counting(art):
        calls["n"] += 1
        return real(art)

    monkeypatch.setattr(loader, "deployment_org_id", _counting)
    build.write_tree(_minimal_org("acme"), tmp_path / "support")

    assert loader.load_org_id(tmp_path / "support") == deployment_id  # same id, from the record
    assert calls["n"] == 0  # adopt-sibling is gone: the record answered without a scan


def test_legacy_lift_preserves_a_pre_record_org_id_idempotently(tmp_path):
    # A post-F14 / pre-F15 deployment keeps its id in a profile's org.yaml and has no record yet.
    # ensure_org_record LIFTS that id up (never re-mints), and is idempotent on re-run.
    prof = tmp_path / "northpeak_salesforce"
    prof.mkdir()
    (prof / "org.yaml").write_text(
        yaml.safe_dump({"org_id": "legacyid0000", "organization": "acme"}), encoding="utf-8"
    )
    assert OR.load_org_record(tmp_path) is None

    rec = OR.ensure_org_record(tmp_path)
    assert rec.org_id == "legacyid0000"  # lifted, not a fresh uuid4
    assert OR.ensure_org_record(tmp_path).org_id == "legacyid0000"  # idempotent


def test_resolver_reads_the_record_then_falls_back_to_legacy_then_local(tmp_path, monkeypatch):
    monkeypatch.setenv("AGAMI_ARTIFACTS_DIR", str(tmp_path))
    monkeypatch.delenv("AGAMI_ORG_ID", raising=False)

    # (a) no record, no profile id -> the single-tenant "local" sentinel (degradation, no crash).
    tools.resolved_org_id.cache_clear()
    assert tools.resolved_org_id() == "local"

    # (b) a legacy per-profile id, still no record -> the legacy scan resolves it.
    prof = tmp_path / "old"
    prof.mkdir()
    (prof / "org.yaml").write_text(
        yaml.safe_dump({"org_id": "legacyid0000", "organization": "x"}), encoding="utf-8"
    )
    tools.resolved_org_id.cache_clear()
    assert tools.resolved_org_id() == "legacyid0000"

    # (c) once a record exists, IT is the source of truth (precedence over the per-profile scan).
    rec = OR.ensure_org_record(tmp_path)  # lifts legacyid0000 up into the record
    tools.resolved_org_id.cache_clear()
    assert tools.resolved_org_id() == rec.org_id == "legacyid0000"


def test_org_record_roundtrips_structured_content_losslessly(tmp_path):
    rec = OrgRecord(
        org_id="abc123",
        name="Acme",
        description="a demo company",
        fiscal_year_start_month=4,
        display_conventions=DisplayConventions(
            currency="USD", rounding=2, week_start="monday", notes=["fiscal weeks"]
        ),
        glossary={"ARR": "annual recurring revenue"},
    )
    OR.write_org_record(tmp_path, rec)
    assert (
        OR.load_org_record(tmp_path) == rec
    )  # every structured field survives the YAML round-trip


def test_bare_record_is_valid_and_fiscal_year_bounds_enforced():
    assert OrgRecord(org_id="x").fiscal_year_start_month is None  # id-only record validates
    with pytest.raises(ValueError):
        OrgRecord(org_id="x", fiscal_year_start_month=13)  # same 1..12 bound as Organization
