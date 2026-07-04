"""Host-side deploy preflight — validate `agami.env` and fill in what the operator shouldn't have to.

Run before `docker compose up` (the `deploy.sh` wrapper / the `/agami-deploy` skill call it). It checks the
hard-floor inputs are present, **generates and persists** `AGAMI_SIGNING_SECRET` once (an ephemeral secret
would break every connected Claude on the next restart), and derives `AGAMI_PUBLIC_HOST` (the bare hostname
Caddy needs for its TLS site) from `PUBLIC_BASE_URL`. Idempotent — a re-run reuses the persisted values.

    python -m deploy_preflight [path/to/agami.env]   # default: ./agami.env
"""

from __future__ import annotations

import secrets
import sys
from pathlib import Path
from urllib.parse import urlparse

# The hard-floor inputs (see the `.env` contract). The warehouse credentials are NOT an env var —
# they travel in the mounted artifacts (`<artifacts>/local/credentials`), so there's no DATASOURCE_URL here.
_REQUIRED = ("PUBLIC_BASE_URL", "AGAMI_ADMIN_USERNAME")
_OIDC_PROVIDERS = {"google": "GOOGLE", "microsoft": "MICROSOFT"}  # provider → AGAMI_OIDC_<PREFIX>_CLIENT_*


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
    """Validate + complete `agami.env` in place. Returns a list of human-readable errors (empty = ready).
    Generates `AGAMI_SIGNING_SECRET` and derives `AGAMI_PUBLIC_HOST` when absent — both idempotent."""
    if not env_path.exists():
        return [f"{env_path} not found — copy agami.env.example to agami.env and fill it in"]
    env = _parse_env(env_path.read_text())

    errors = [f"{k} is required" for k in _REQUIRED if not env.get(k)]

    # TLS is mandatory — the server hard-fails on a non-https PUBLIC_BASE_URL, so catch it here with a clear
    # message rather than a cryptic boot crash.
    pub = env.get("PUBLIC_BASE_URL", "")
    if pub and not pub.startswith("https://"):
        errors.append("PUBLIC_BASE_URL must start with https:// (TLS is required for OAuth + the admin cookie)")

    # Auth floor: a password and/or a pinned social provider — and a provider needs its OIDC client, or it
    # would seed an admin who can't actually sign in.
    provider = env.get("AGAMI_ADMIN_PROVIDER", "").lower()
    if not env.get("AGAMI_ADMIN_PASSWORD") and not provider:
        errors.append("set AGAMI_ADMIN_PASSWORD and/or AGAMI_ADMIN_PROVIDER (at least one)")
    if provider:
        prefix = _OIDC_PROVIDERS.get(provider)
        if prefix is None:
            errors.append(f"AGAMI_ADMIN_PROVIDER must be one of: {', '.join(_OIDC_PROVIDERS)}")
        elif not env.get(f"AGAMI_OIDC_{prefix}_CLIENT_ID") or not env.get(f"AGAMI_OIDC_{prefix}_CLIENT_SECRET"):
            errors.append(
                f"AGAMI_ADMIN_PROVIDER={provider} needs AGAMI_OIDC_{prefix}_CLIENT_ID + _CLIENT_SECRET"
            )

    # Generate/derive the values the operator shouldn't hand-set. Wrap the file writes so a permission error
    # is a clean message, not a traceback.
    try:
        # Generate-once-and-persist the signing secret — never regenerate (it would invalidate live tokens).
        if not env.get("AGAMI_SIGNING_SECRET"):
            _set_env(env_path, "AGAMI_SIGNING_SECRET", secrets.token_hex(32))
        # Caddy's TLS site needs the bare hostname — derive it from a valid https PUBLIC_BASE_URL.
        if not env.get("AGAMI_PUBLIC_HOST") and pub.startswith("https://"):
            host = urlparse(pub).hostname
            if host:
                _set_env(env_path, "AGAMI_PUBLIC_HOST", host)
            else:
                errors.append("PUBLIC_BASE_URL must be https://<host> (could not derive a hostname)")
    except OSError as e:
        errors.append(f"could not write {env_path}: {e}")

    return errors


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    env_path = Path(args[0]) if args else Path("agami.env")
    errors = prepare_env(env_path)
    if errors:
        print("deploy_preflight: agami.env is not ready:", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        return 1
    print(f"deploy_preflight: {env_path} is ready.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
