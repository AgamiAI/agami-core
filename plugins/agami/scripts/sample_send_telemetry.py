#!/usr/bin/env python3
"""
Sample telemetry sender.

Builds a single event payload, validates it against the allowlist documented
in plugins/agami/shared/telemetry-payload.md, and POSTs it to the configured
endpoint. Stdlib only.

The agami skill itself sends telemetry via curl directly (no script needed).
This file exists so the privacy invariant test in tests/test_telemetry_privacy.py
has a Python-callable surface to assert against, and so users automating their
own workflows have a reference implementation.

Usage:
    python sample_send_telemetry.py \\
        --event-type query \\
        --tier cli \\
        --db-type postgres \\
        --latency-p50 250 \\
        --latency-p95 1100
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import platform
import sys
import urllib.request
import urllib.error
import uuid
from pathlib import Path
from typing import Any


# --- Authoritative allowlist — matches plugins/agami/shared/telemetry-payload.md ---

ALLOWED_FIELDS: frozenset[str] = frozenset({
    "schema_version",
    "event_type",
    "install_id",
    "db_type",
    "os",
    "host",
    "error_kind",
    "latency_p50_ms",
    "latency_p95_ms",
    "tier",
    "client_version",
    "timestamp",
})

ALLOWED_EVENT_TYPES: frozenset[str] = frozenset({
    "install", "connect", "query", "correction", "chart", "error", "update_check",
})

ALLOWED_DB_TYPES: frozenset[str] = frozenset({"postgres", "mysql", "sqlite"})

ALLOWED_OS: frozenset[str] = frozenset({"darwin", "linux", "windows"})

ALLOWED_HOSTS: frozenset[str] = frozenset({
    "claude-code-cli", "claude-code-vscode", "claude-code-cursor", "claude-cowork",
})

ALLOWED_ERROR_KINDS: frozenset[str] = frozenset({
    "auth", "dsn", "network", "permission", "column_not_found",
    "table_not_found", "syntax", "timeout", "driver_missing", "other",
})

ALLOWED_TIERS: frozenset[str] = frozenset({"cli", "duckdb", "python"})

CLIENT_VERSION = "1.0.0"
ENDPOINT = "https://analytics.agami.ai/v1/events"


# --- Build + validate ---

def detect_os() -> str:
    p = platform.system().lower()
    if "darwin" in p:
        return "darwin"
    if "linux" in p:
        return "linux"
    if "windows" in p:
        return "windows"
    raise ValueError(f"unsupported os: {p!r}")


def detect_host() -> str:
    # Best-effort. The skill knows its host more precisely than this script does;
    # callers should pass --host explicitly if they care.
    explicit = os.environ.get("AGAMI_HOST")
    if explicit and explicit in ALLOWED_HOSTS:
        return explicit
    return "claude-code-cli"


def load_install_id() -> str | None:
    """Read install_id from ~/.agami/.config; return None if not opted in."""
    cfg_path = Path(os.path.expanduser("~/.agami/.config"))
    if not cfg_path.exists():
        return None
    try:
        cfg = json.loads(cfg_path.read_text())
    except json.JSONDecodeError:
        return None
    if not cfg.get("analytics_consent"):
        return None
    iid = cfg.get("install_id")
    if not iid:
        return None
    # Validate it parses as UUID
    try:
        uuid.UUID(iid)
    except ValueError:
        return None
    return iid


def build_event(*,
                event_type: str,
                tier: str,
                db_type: str,
                install_id: str,
                host: str | None = None,
                error_kind: str | None = None,
                latency_p50_ms: int | None = None,
                latency_p95_ms: int | None = None) -> dict[str, Any]:
    """Build an event using ONLY allowlisted fields. Validates each field's value."""
    if event_type not in ALLOWED_EVENT_TYPES:
        raise ValueError(f"event_type must be one of {sorted(ALLOWED_EVENT_TYPES)}")
    if db_type not in ALLOWED_DB_TYPES:
        raise ValueError(f"db_type must be one of {sorted(ALLOWED_DB_TYPES)}")
    if tier not in ALLOWED_TIERS:
        raise ValueError(f"tier must be one of {sorted(ALLOWED_TIERS)}")
    if error_kind is not None and error_kind not in ALLOWED_ERROR_KINDS:
        raise ValueError(f"error_kind must be one of {sorted(ALLOWED_ERROR_KINDS)}")

    host_val = host or detect_host()
    if host_val not in ALLOWED_HOSTS:
        raise ValueError(f"host must be one of {sorted(ALLOWED_HOSTS)}")

    os_val = detect_os()

    event: dict[str, Any] = {
        "event_type": event_type,
        "install_id": install_id,
        "db_type": db_type,
        "os": os_val,
        "host": host_val,
        "tier": tier,
        "client_version": CLIENT_VERSION,
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
    }
    if error_kind is not None:
        event["error_kind"] = error_kind
    if latency_p50_ms is not None:
        event["latency_p50_ms"] = int(latency_p50_ms)
    if latency_p95_ms is not None:
        event["latency_p95_ms"] = int(latency_p95_ms)

    # Defense in depth: re-check there are no extras
    extras = set(event) - ALLOWED_FIELDS
    if extras:
        raise ValueError(f"event has disallowed fields: {sorted(extras)}")

    return event


def build_payload(events: list[dict[str, Any]]) -> dict[str, Any]:
    if len(events) > 100:
        raise ValueError(f"batch must be <= 100 events (got {len(events)})")
    return {"schema_version": 1, "events": events}


def post(payload: dict[str, Any], endpoint: str = ENDPOINT, timeout: float = 5.0) -> int | None:
    """POST the payload. Returns the HTTP status code, or None on network error."""
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        endpoint, data=body, method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status
    except urllib.error.HTTPError as e:
        sys.stderr.write(f"telemetry server returned {e.code}\n")
        return e.code
    except (urllib.error.URLError, TimeoutError):
        # Silent on network errors — telemetry must never block real work.
        return None


# --- CLI ---

def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--event-type", required=True, choices=sorted(ALLOWED_EVENT_TYPES))
    p.add_argument("--tier", required=True, choices=sorted(ALLOWED_TIERS))
    p.add_argument("--db-type", required=True, choices=sorted(ALLOWED_DB_TYPES))
    p.add_argument("--host", choices=sorted(ALLOWED_HOSTS))
    p.add_argument("--error-kind", choices=sorted(ALLOWED_ERROR_KINDS))
    p.add_argument("--latency-p50", type=int)
    p.add_argument("--latency-p95", type=int)
    p.add_argument("--endpoint", default=ENDPOINT)
    p.add_argument("--dry-run", action="store_true", help="print payload, don't send")
    args = p.parse_args()

    install_id = load_install_id()
    if install_id is None:
        sys.stderr.write("Not opted in (no analytics_consent in ~/.agami/.config). Doing nothing.\n")
        return 0

    event = build_event(
        event_type=args.event_type,
        tier=args.tier,
        db_type=args.db_type,
        install_id=install_id,
        host=args.host,
        error_kind=args.error_kind,
        latency_p50_ms=args.latency_p50,
        latency_p95_ms=args.latency_p95,
    )
    payload = build_payload([event])

    if args.dry_run:
        print(json.dumps(payload, indent=2))
        return 0

    status = post(payload, endpoint=args.endpoint)
    if status and 200 <= status < 300:
        return 0
    return 0  # never block on telemetry


if __name__ == "__main__":
    sys.exit(main())
