"""
Tests for plugins/agami/scripts/prepare_deploy.py — the `/agami-deploy` bundle scaffolder.

It copies the carried bundle templates, stages the model artifacts, and writes a `.env` with the
NON-SECRET values only. The contract these tests pin: a complete bundle is produced, the right
COMPOSE_PROFILES land per toggle, no password ever passes through the helper, an existing `.env` is
preserved, and the generated `.env` passes deploy_preflight.
"""

from __future__ import annotations

import argparse
import stat
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agami" / "scripts"))

import prepare_deploy  # noqa: E402


def _artifacts(tmp_path: Path) -> Path:
    """A minimal local artifacts dir: a `local/credentials` (chmod 600) + one model profile."""
    art = tmp_path / "agami-artifacts"
    (art / "local").mkdir(parents=True)
    creds = art / "local" / "credentials"
    creds.write_text("[demo]\nhost = db.example\n", encoding="utf-8")
    creds.chmod(0o600)
    (art / "demo").mkdir()
    (art / "demo" / "org.yaml").write_text("name: demo\n", encoding="utf-8")
    return art


def _args(target: Path, artifacts: Path, **over) -> argparse.Namespace:
    base = dict(
        target=str(target),
        artifacts_dir=str(artifacts),
        public_base_url="https://agami.acme.example",
        admin_email="you@example.com",
        admin_first="Alex",
        admin_last="Kim",
        profiles="bundled-db,edge",
        app_database_url="",
        image_tag="latest",
    )
    base.update(over)
    return argparse.Namespace(**base)


def test_prepares_a_complete_bundle(tmp_path):
    target = tmp_path / "bundle"
    status, code = prepare_deploy.prepare(_args(target, _artifacts(tmp_path)))
    assert code == 0 and status.startswith("PREPARED ")

    # The carried templates are all present + the image-based compose pulls (no build context).
    for name in ("docker-compose.yml", "Caddyfile", "deploy.sh", "README.md", ".env"):
        assert (target / name).exists(), name
    compose = (target / "docker-compose.yml").read_text()
    assert "image: ghcr.io/agamiai/agami-core:" in compose
    assert "build:" not in compose

    # The model + credentials are staged so the bundle is self-contained + shippable.
    assert (target / "artifacts" / "demo" / "org.yaml").exists()
    assert (target / "artifacts" / "local" / "credentials").exists()


def test_env_has_non_secrets_and_a_blank_password(tmp_path):
    target = tmp_path / "bundle"
    prepare_deploy.prepare(_args(target, _artifacts(tmp_path)))
    env = (target / ".env").read_text()
    assert "PUBLIC_BASE_URL=https://agami.acme.example" in env
    assert "AGAMI_ADMIN_USERNAME=you@example.com" in env
    assert "AGAMI_ADMIN_FIRST_NAME=Alex" in env
    assert "AGAMI_ADMIN_LAST_NAME=Kim" in env
    # The password is left for the user to type into the file — never filled by the helper.
    assert "AGAMI_ADMIN_PASSWORD=\n" in env or env.rstrip().endswith("AGAMI_ADMIN_PASSWORD=")
    # And no signing secret yet — deploy_preflight generates that.
    assert "AGAMI_SIGNING_SECRET=" not in env


def test_env_is_chmod_600(tmp_path):
    target = tmp_path / "bundle"
    prepare_deploy.prepare(_args(target, _artifacts(tmp_path)))
    mode = stat.S_IMODE((target / ".env").stat().st_mode)
    assert mode == 0o600, oct(mode)


@pytest.mark.parametrize(
    "profiles,app_db,expect",
    [
        ("bundled-db,edge", "", "COMPOSE_PROFILES=bundled-db,edge"),
        ("bundled-db,tunnel", "", "COMPOSE_PROFILES=bundled-db,tunnel"),
        ("edge", "postgresql://u@mgd.example:5432/agami?sslmode=require", "COMPOSE_PROFILES=edge"),
    ],
)
def test_tier2_toggles_set_profiles(tmp_path, profiles, app_db, expect):
    target = tmp_path / "bundle"
    prepare_deploy.prepare(_args(target, _artifacts(tmp_path), profiles=profiles, app_database_url=app_db))
    env = (target / ".env").read_text()
    assert expect in env
    if app_db:
        assert f"APP_DATABASE_URL={app_db}" in env


def test_helper_takes_no_password_argument(tmp_path):
    """Criterion: a secret never travels on the command line. Passing --password must be rejected."""
    with pytest.raises(SystemExit):
        prepare_deploy.main(
            [
                "--target", str(tmp_path / "b"),
                "--artifacts-dir", str(_artifacts(tmp_path)),
                "--public-base-url", "https://h.example",
                "--admin-email", "you@example.com",
                "--admin-first", "A", "--admin-last", "K",
                "--password", "hunter2",  # not a real option — argparse must error out
            ]
        )


def test_existing_env_is_preserved(tmp_path):
    target = tmp_path / "bundle"
    art = _artifacts(tmp_path)
    prepare_deploy.prepare(_args(target, art))
    # Simulate the user typing a password + a generated secret, then a re-run.
    env_path = target / ".env"
    env_path.write_text("AGAMI_ADMIN_PASSWORD=typed-by-user\nAGAMI_SIGNING_SECRET=deadbeef\n", encoding="utf-8")
    status, code = prepare_deploy.prepare(_args(target, art))
    assert code == 0 and status.startswith("PREPARED_KEPT_ENV ")
    # The user's filled .env is untouched (no clobbered password / wiped secret).
    kept = env_path.read_text()
    assert "AGAMI_ADMIN_PASSWORD=typed-by-user" in kept
    assert "AGAMI_SIGNING_SECRET=deadbeef" in kept


def test_main_prints_status_and_returns_zero(tmp_path, capsys):
    code = prepare_deploy.main(
        [
            "--target", str(tmp_path / "b"),
            "--artifacts-dir", str(_artifacts(tmp_path)),
            "--public-base-url", "https://h.example",
            "--admin-email", "you@example.com",
            "--admin-first", "A", "--admin-last", "K",
        ]
    )
    assert code == 0
    assert capsys.readouterr().out.startswith("PREPARED ")


def test_no_artifacts_is_a_clean_error(tmp_path):
    target = tmp_path / "bundle"
    empty = tmp_path / "empty-artifacts"
    empty.mkdir()
    status, code = prepare_deploy.prepare(_args(target, empty))
    assert code == 1 and status.startswith("ERROR ") and "artifacts" in status


def test_generated_env_passes_deploy_preflight(tmp_path):
    """The .env (once a password is set) satisfies deploy_preflight: secret generated, host derived."""
    deploy_preflight = pytest.importorskip("deploy_preflight")
    target = tmp_path / "bundle"
    prepare_deploy.prepare(_args(target, _artifacts(tmp_path)))
    env_path = target / ".env"
    # The user supplies the password by editing the file (here, simulated).
    env_path.write_text(
        env_path.read_text().replace("AGAMI_ADMIN_PASSWORD=", "AGAMI_ADMIN_PASSWORD=a-strong-pw"),
        encoding="utf-8",
    )
    errors = deploy_preflight.prepare_env(env_path)
    assert errors == [], errors
    after = env_path.read_text()
    assert "AGAMI_SIGNING_SECRET=" in after
    assert "AGAMI_PUBLIC_HOST=agami.acme.example" in after
