"""Host-side deploy preflight — validate the `.env` and fill in what the operator shouldn't have to.

Run before `docker compose up` (the `deploy.sh` wrapper / the `/agami-deploy` skill call it). It checks the
hard-floor inputs are present, **generates and persists** `AGAMI_SIGNING_SECRET` once (an ephemeral secret
would break every connected Claude on the next restart), and derives `AGAMI_PUBLIC_HOST` (the bare hostname
Caddy needs for its TLS site) from `PUBLIC_BASE_URL`. Idempotent — a re-run reuses the persisted values.

    python -m deploy_preflight [path/to/.env]   # default: ./.env
"""

from __future__ import annotations

import secrets
import sys
from pathlib import Path
from urllib.parse import urlparse

# The hard-floor inputs (see the ACE-009 `.env` contract). Auth needs at least one of the two methods.
_REQUIRED = ("PUBLIC_BASE_URL", "AGAMI_ADMIN_USERNAME", "DATASOURCE_URL")
_AUTH_ANY = ("AGAMI_ADMIN_PASSWORD", "AGAMI_ADMIN_PROVIDER")


def _parse_env(text: str) -> dict[str, str]:
    """Parse a `.env` into {KEY: VALUE} — `KEY=VALUE` per line, `#` comments and blanks skipped. A value's
    surrounding quotes are stripped; everything after the first `=` is the value (so URLs with `=` survive)."""
    out: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        out[key.strip()] = val.strip().strip("'\"")
    return out


def _set_env(env_path: Path, key: str, value: str) -> None:
    """Set `KEY=VALUE` in the `.env` — **replacing** an existing (even present-but-empty) line in place, else
    appending. Replace-not-append avoids a confusing duplicate key when e.g. `AGAMI_SIGNING_SECRET=` is blank.
    chmod 600 because the file holds the signing secret + DB creds."""
    lines = env_path.read_text().splitlines()
    new = f"{key}={value}"
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped.startswith("#") and stripped.split("=", 1)[0].strip() == key:
            lines[i] = new
            break
    else:
        lines.append(new)
    env_path.write_text("\n".join(lines) + "\n")
    env_path.chmod(0o600)


def prepare_env(env_path: Path) -> list[str]:
    """Validate + complete the `.env` in place. Returns a list of human-readable errors (empty = ready).
    Generates `AGAMI_SIGNING_SECRET` and derives `AGAMI_PUBLIC_HOST` when absent — both idempotent."""
    if not env_path.exists():
        return [f"{env_path} not found — copy .env.example to .env and fill it in"]
    env = _parse_env(env_path.read_text())

    errors = [f"{k} is required" for k in _REQUIRED if not env.get(k)]
    if not any(env.get(k) for k in _AUTH_ANY):
        errors.append("set AGAMI_ADMIN_PASSWORD and/or AGAMI_ADMIN_PROVIDER (at least one)")

    # Generate-once-and-persist the signing secret — never regenerate (it would invalidate live tokens).
    if not env.get("AGAMI_SIGNING_SECRET"):
        _set_env(env_path, "AGAMI_SIGNING_SECRET", secrets.token_hex(32))

    # Caddy's TLS site needs the bare hostname, not the full URL — derive it from PUBLIC_BASE_URL.
    if not env.get("AGAMI_PUBLIC_HOST") and env.get("PUBLIC_BASE_URL"):
        host = urlparse(env["PUBLIC_BASE_URL"]).hostname
        if host:
            _set_env(env_path, "AGAMI_PUBLIC_HOST", host)
        else:
            errors.append("PUBLIC_BASE_URL must be a URL like https://host (could not derive a hostname)")

    return errors


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    env_path = Path(args[0]) if args else Path(".env")
    errors = prepare_env(env_path)
    if errors:
        print("deploy_preflight: .env is not ready:", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        return 1
    print(f"deploy_preflight: {env_path} is ready.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
