#!/usr/bin/env python3
"""Scaffold an `/agami-deploy` bundle on the user's machine: copy the carried templates, stage the
model artifacts, and write a `.env` with the NON-SECRET values the skill gathered.

This helper never touches a secret. The admin password is typed by the user into the file afterwards
(the agami-connect hand-off pattern), and `deploy_preflight` generates the signing secret. So that a
re-run can't wipe a password the user already typed or a secret already generated, an EXISTING `.env`
is preserved untouched — only the other bundle files (and the artifacts copy) are refreshed.

Stdout is a single status line (first token machine-readable); the skill reads it and acts. Stdlib only.

  PREPARED <target>           fresh bundle written; user must set AGAMI_ADMIN_PASSWORD next      [0]
  PREPARED_KEPT_ENV <target>  bundle refreshed; an existing .env was preserved (not overwritten)  [0]
  ERROR <message>             templates missing / artifacts missing / write failed                [1]
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

# The non-.env files copied verbatim into the bundle. `.env` is generated from `.env.example` (below),
# never copied as-is, so the user gets a real config rather than a template full of placeholders.
_VERBATIM = ("docker-compose.yml", "Caddyfile", "deploy.sh", "README.md")

# The carried templates live next to the skill (scripts/ -> agami/ -> skills/agami-deploy/bundle/).
_BUNDLE_SRC = Path(__file__).resolve().parents[1] / "skills" / "agami-deploy" / "bundle"


def _set_key(text: str, key: str, value: str) -> str:
    """Set `KEY=value`, replacing the first matching line — whether it ships uncommented (`KEY=...`)
    or commented as a hint (`# KEY=...`) — so e.g. `--app-database-url` uncomments the template's hint
    rather than appending a confusing duplicate. Append only if the key is absent entirely. The `=`
    guard prevents a prefix key (`AGAMI_IMAGE_TAG`) from matching a longer one (`AGAMI_IMAGE_TAG_X`)."""
    out, replaced = [], False
    for line in text.splitlines():
        if not replaced and line.lstrip("#").lstrip().startswith(f"{key}="):
            out.append(f"{key}={value}")
            replaced = True
        else:
            out.append(line)
    if not replaced:
        out.append(f"{key}={value}")
    return "\n".join(out) + "\n"


def _build_env(example: str, args: argparse.Namespace) -> str:
    """The `.env` for a fresh bundle: the non-secret answers written onto the template. The admin
    password line is left blank (the user types it); the signing secret is left for deploy_preflight."""
    text = example if example.endswith("\n") else example + "\n"
    text = _set_key(text, "COMPOSE_PROFILES", args.profiles)
    text = _set_key(text, "AGAMI_IMAGE_TAG", args.image_tag)
    text = _set_key(text, "PUBLIC_BASE_URL", args.public_base_url)
    text = _set_key(text, "AGAMI_ADMIN_USERNAME", args.admin_email)
    text = _set_key(text, "AGAMI_ADMIN_FIRST_NAME", args.admin_first)
    text = _set_key(text, "AGAMI_ADMIN_LAST_NAME", args.admin_last)
    if args.app_database_url:
        # External/managed Postgres: the template ships this commented; set it as a real line.
        text = _set_key(text, "APP_DATABASE_URL", args.app_database_url)
    return text


def prepare(args: argparse.Namespace) -> tuple[str, int]:
    """Returns (status_line, exit_code). The status line's first token is machine-readable."""
    target = Path(args.target).expanduser().resolve()
    artifacts = Path(args.artifacts_dir).expanduser().resolve()

    if not _BUNDLE_SRC.is_dir():
        return f"ERROR carried bundle templates not found at {_BUNDLE_SRC}", 1
    if not (artifacts / "local").is_dir():
        # No staged model/creds to ship — the deploy would have nothing to serve.
        return f"ERROR no artifacts (model + credentials) found at {artifacts} — run /agami-connect first", 1

    try:
        target.mkdir(parents=True, exist_ok=True)
        for name in _VERBATIM:
            shutil.copy2(_BUNDLE_SRC / name, target / name)
        (target / "deploy.sh").chmod(0o755)

        # Stage the model + warehouse credentials so the bundle is self-contained + shippable. copy2
        # preserves the chmod-600 on local/credentials; symlinks=True keeps links as links rather than
        # materializing their targets into the bundle. dirs_exist_ok so a re-run refreshes in place.
        shutil.copytree(artifacts, target / "artifacts", symlinks=True, dirs_exist_ok=True)

        env_path = target / ".env"
        if env_path.exists():
            # Never clobber a .env that may already hold a typed password / a generated signing secret.
            return f"PREPARED_KEPT_ENV {target}", 0
        example = (_BUNDLE_SRC / ".env.example").read_text(encoding="utf-8")
        env_path.write_text(_build_env(example, args), encoding="utf-8")
        env_path.chmod(0o600)
    except OSError as e:
        return f"ERROR {e}", 1

    return f"PREPARED {target}", 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Scaffold an /agami-deploy bundle (non-secret values only).")
    p.add_argument("--target", required=True, help="bundle output directory")
    p.add_argument("--artifacts-dir", required=True, help="local agami-artifacts dir (model + credentials)")
    p.add_argument("--public-base-url", required=True, help="https://<hostname> teammates connect to")
    p.add_argument("--admin-email", required=True)
    p.add_argument("--admin-first", required=True)
    p.add_argument("--admin-last", required=True)
    p.add_argument("--profiles", default="bundled-db,edge", help="COMPOSE_PROFILES (default: single-server)")
    p.add_argument("--app-database-url", default="", help="external Postgres URL (omit for bundled)")
    p.add_argument("--image-tag", default="latest", help="ghcr.io/agamiai/agami-core tag to pull")
    # Deliberately NO --password / secret args: secrets never travel on the command line.
    args = p.parse_args(argv)

    status, code = prepare(args)
    print(status)
    return code


if __name__ == "__main__":
    sys.exit(main())
