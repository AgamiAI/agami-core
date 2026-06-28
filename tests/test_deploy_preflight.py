"""ACE-009 — the host-side deploy preflight: validate the `.env`, persist the signing secret, derive the host."""

from __future__ import annotations

import sys
from pathlib import Path

PKG_SRC = Path(__file__).resolve().parent.parent / "packages" / "agami-core" / "src"
if str(PKG_SRC) not in sys.path:
    sys.path.insert(0, str(PKG_SRC))

import deploy_preflight  # noqa: E402

_COMPLETE = (
    "PUBLIC_BASE_URL=https://agami.example.com\n"
    "AGAMI_ADMIN_USERNAME=you@example.com\n"
    "AGAMI_ADMIN_PASSWORD=choose-a-strong-one\n"
    "DATASOURCE_URL=postgresql://you@warehouse/db\n"
)


def _env(tmp_path: Path, text: str) -> Path:
    p = tmp_path / ".env"
    p.write_text(text)
    return p


def test_missing_public_base_url_is_an_error(tmp_path):
    p = _env(tmp_path, _COMPLETE.replace("PUBLIC_BASE_URL=https://agami.example.com\n", ""))
    errors = deploy_preflight.prepare_env(p)
    assert any("PUBLIC_BASE_URL" in e for e in errors)
    assert deploy_preflight.main([str(p)]) == 1  # fails fast, non-zero


def test_missing_auth_method_is_an_error(tmp_path):
    p = _env(tmp_path, _COMPLETE.replace("AGAMI_ADMIN_PASSWORD=choose-a-strong-one\n", ""))
    errors = deploy_preflight.prepare_env(p)
    assert any("AGAMI_ADMIN_PASSWORD" in e for e in errors)  # neither password nor provider set


def test_signing_secret_is_generated_then_stable(tmp_path):
    p = _env(tmp_path, _COMPLETE)
    assert deploy_preflight.prepare_env(p) == []  # complete .env → ready
    first = deploy_preflight._parse_env(p.read_text())["AGAMI_SIGNING_SECRET"]
    assert len(first) >= 32  # a real generated secret
    deploy_preflight.prepare_env(p)  # re-run (a redeploy)
    second = deploy_preflight._parse_env(p.read_text())["AGAMI_SIGNING_SECRET"]
    assert first == second  # generated ONCE — never regenerated (would break live tokens)


def test_public_host_is_derived_from_the_url(tmp_path):
    p = _env(tmp_path, _COMPLETE)
    deploy_preflight.prepare_env(p)
    assert deploy_preflight._parse_env(p.read_text())["AGAMI_PUBLIC_HOST"] == "agami.example.com"


def test_public_base_url_without_a_hostname_is_an_error(tmp_path):
    p = _env(tmp_path, _COMPLETE.replace("https://agami.example.com", "not-a-real-url"))
    errors = deploy_preflight.prepare_env(p)
    assert any("hostname" in e.lower() for e in errors)  # can't derive the Caddy host from a non-URL


def test_complete_env_passes(tmp_path):
    p = _env(tmp_path, _COMPLETE)
    assert deploy_preflight.main([str(p)]) == 0  # ready → exit 0


def test_provider_only_auth_is_accepted(tmp_path):
    p = _env(
        tmp_path,
        _COMPLETE.replace("AGAMI_ADMIN_PASSWORD=choose-a-strong-one\n", "AGAMI_ADMIN_PROVIDER=google\n"),
    )
    assert deploy_preflight.prepare_env(p) == []  # a pinned provider alone satisfies the auth floor


def test_missing_env_file_is_a_clear_error(tmp_path):
    assert deploy_preflight.main([str(tmp_path / "nope.env")]) == 1  # no .env → clean exit 1
