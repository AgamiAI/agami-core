#!/usr/bin/env python3
"""Scaffold an `/agami-deploy` bundle on the user's machine: copy the carried templates, stage the
model artifacts, and write `agami.env` with the NON-SECRET values the skill gathered.

This helper never touches a secret: the admin password is typed by the user into the file afterwards
(the agami-connect hand-off pattern), `deploy_preflight` generates the signing secret, and an external
`APP_DATABASE_URL` (itself a credential) is likewise edited into `agami.env` by the user — never passed
here on the command line. So that a re-run can't wipe a password the user already typed or a secret already
generated, an EXISTING `agami.env` is preserved untouched — only the other bundle files (and the artifacts
copy) are refreshed.

Stdout is a single status line (first token machine-readable); the skill reads it and acts. Stdlib only.

  PREPARED <target>           fresh bundle written; user must set AGAMI_ADMIN_PASSWORD next        [0]
  PREPARED_KEPT_ENV <target>  bundle refreshed; an existing agami.env was preserved (not overwritten) [0]
  ERROR <message>             templates missing / artifacts missing / write failed                  [1]
"""
from __future__ import annotations

import argparse
import os
import shutil
import stat
import sys
from pathlib import Path

# The files copied verbatim into the bundle. `agami.env` is generated from `agami.env.example` (below),
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
    # External/managed Postgres (APP_DATABASE_URL) is a credential, so it is NOT set here — the template
    # ships it commented and the user edits it into .env by hand (the same hand-off as the password).
    return text


def _widen_one(path: Path) -> None:
    """`a+rX` on a single path: add read for all, plus execute/traverse for a directory (or a file that
    already has an execute bit). Add-only — never removes a bit, so a read-only snapshot stays readable
    (perms only widen, never narrow). A symlink is skipped so a link's target (possibly outside the bundle) is untouched."""
    st = path.lstat()
    if stat.S_ISLNK(st.st_mode):
        return
    mode = stat.S_IMODE(st.st_mode)
    add = 0o444  # a+r
    if stat.S_ISDIR(st.st_mode) or (mode & 0o111):  # a+X: dirs, or files already carrying an execute bit
        add |= 0o111
    path.chmod(mode | add)


def _grant_world_read(root: Path) -> None:
    """`chmod -R a+rX` over the staged model. The deployed container runs as uid 10001, not the operator
    who owns these files, and mounts them read-only — without this the boot-time model load fails
    "Permission denied" on ORGANIZATION.md and the container crash-loops. Only non-secret model files
    reach here (`local/` is excluded from staging).

    Uses `os.walk(followlinks=False)` — NOT `rglob("**")`, which follows directory symlinks on Python
    ≤3.12 and could chmod files *outside* the bundle — and it streams rather than materializing every
    path, so a large model doesn't build a giant list."""
    _widen_one(root)
    for dirpath, dirnames, filenames in os.walk(root):  # followlinks=False (default): never enters a symlinked dir
        base = Path(dirpath)
        for name in (*dirnames, *filenames):
            _widen_one(base / name)


def prepare(args: argparse.Namespace) -> tuple[str, int]:
    """Returns (status_line, exit_code). The status line's first token is machine-readable."""
    target = Path(args.target).expanduser().resolve()
    artifacts = Path(args.artifacts_dir).expanduser().resolve()

    if not _BUNDLE_SRC.is_dir():
        return f"ERROR carried bundle templates not found at {_BUNDLE_SRC}", 1
    if not (artifacts / "local").is_dir():
        # `local/` marks a real agami-artifacts dir (i.e. /agami-connect has run) — it is the precondition,
        # NOT something we stage (creds now travel in .env via DATASOURCE_URL; the model is staged below).
        return f"ERROR no agami-artifacts at {artifacts} (run /agami-connect first)", 1
    if target == artifacts or target.is_relative_to(artifacts) or artifacts.is_relative_to(target):
        # Else the copytree(artifacts -> target/artifacts) would recurse into the bundle it just created.
        return f"ERROR --target must not be inside --artifacts-dir (or vice versa): {target}", 1

    try:
        target.mkdir(parents=True, exist_ok=True)
        for name in _VERBATIM:
            shutil.copy2(_BUNDLE_SRC / name, target / name)
        (target / "deploy.sh").chmod(0o755)

        # Stage the MODEL only — never a secret. `local/` (credentials + .pgpass) is excluded: the
        # deployed server reads warehouse creds from DATASOURCE_URL in .env, so no 600-mode file is
        # copied into a shippable bundle or mounted into the container (the uid-mismatch crash this
        # replaces). symlinks=True keeps links as links; dirs_exist_ok so a re-run refreshes in place.
        staged = target / "artifacts"
        # `dirs_exist_ok=True` merges into an existing bundle, and `ignore` only skips COPYING `local/` —
        # it does NOT delete a `local/` a PRIOR (older) prepare_deploy staged. Purge it explicitly so a
        # re-run over an old bundle can't keep a stale credentials file in the mounted volume. Fail-CLOSED:
        # NOT `ignore_errors` — a failed purge (a perms/read-only issue) must surface as an ERROR, never
        # silently leave the secret behind. A missing `local/` is fine (nothing to purge).
        stale_local = staged / "local"
        if stale_local.exists():
            shutil.rmtree(stale_local)
        shutil.copytree(
            artifacts, staged, symlinks=True, dirs_exist_ok=True,
            ignore=shutil.ignore_patterns("local"),
        )
        # The model is non-secret, but the container runs as a different uid than the file owner — widen
        # the staged copy to world-readable/traversable so the read-only mount is readable regardless.
        _grant_world_read(staged)

        # The operator-editable config is `agami.env` — a visible name (a dot-file like `.env` is hidden in
        # Finder, and this is the one file the user must open). docker-compose reads it via `--env-file` in
        # deploy.sh + the `env_file:` directive, since it no longer auto-loads by the `.env` name.
        env_path = target / "agami.env"
        if env_path.exists():
            # Never clobber an agami.env that may already hold a typed password / a generated signing secret —
            # but do reassert chmod 600 in case an editor/umask loosened it (the file holds secrets).
            env_path.chmod(0o600)
            return f"PREPARED_KEPT_ENV {target}", 0
        example = (_BUNDLE_SRC / "agami.env.example").read_text(encoding="utf-8")
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
    p.add_argument("--image-tag", default="latest", help="ghcr.io/agamiai/agami-core tag to pull")
    # Deliberately NO --password / --app-database-url / secret args: a credential never travels on the
    # command line (it would leak into chat logs / shell history). The user edits those into .env.
    args = p.parse_args(argv)

    status, code = prepare(args)
    print(status)
    return code


if __name__ == "__main__":
    sys.exit(main())
