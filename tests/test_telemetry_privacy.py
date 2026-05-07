"""
Privacy invariant tests for the telemetry payload.

Asserts that no matter what data sits in the user's environment (questions,
schemas, hostnames, paths, PII), the constructed payload contains ONLY the
allowlisted fields documented in plugins/agami/shared/telemetry-payload.md.

These tests are the source of truth for "did we accidentally leak something
into telemetry?". Any change to the allowlist requires updating both this
file and shared/telemetry-payload.md, and ideally a code-review with someone
other than the author.

Run: pytest tests/test_telemetry_privacy.py -v
"""

from __future__ import annotations

import json
import os
import sys
import uuid
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agami" / "scripts"))

import sample_send_telemetry as t  # noqa: E402


# --- Allowlist invariants ---

EXPECTED_ALLOWED_FIELDS = frozenset({
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

EXPECTED_ALLOWED_DB_TYPES = frozenset({
    "postgres", "redshift", "mysql", "snowflake", "sqlite",
})


def test_allowed_fields_match_doc():
    """
    The Python ALLOWED_FIELDS set must match the table in
    plugins/agami/shared/telemetry-payload.md exactly. If you change one,
    change both — and update this list.
    """
    assert t.ALLOWED_FIELDS == EXPECTED_ALLOWED_FIELDS


def test_event_only_contains_allowlisted_fields():
    install_id = str(uuid.uuid4())
    event = t.build_event(
        event_type="query",
        tier="cli",
        db_type="postgres",
        install_id=install_id,
        host="claude-code-cli",
        latency_p50_ms=250,
        latency_p95_ms=1100,
    )
    extras = set(event.keys()) - EXPECTED_ALLOWED_FIELDS
    assert not extras, f"event leaked disallowed fields: {sorted(extras)}"


def test_event_with_error_kind_is_allowed():
    event = t.build_event(
        event_type="error",
        tier="cli",
        db_type="postgres",
        install_id=str(uuid.uuid4()),
        host="claude-code-cli",
        error_kind="column_not_found",
    )
    assert event["error_kind"] == "column_not_found"
    extras = set(event.keys()) - EXPECTED_ALLOWED_FIELDS
    assert not extras


# --- Reject-on-bad-input invariants ---

def test_rejects_bad_event_type():
    with pytest.raises(ValueError, match="event_type"):
        t.build_event(
            event_type="user_query",  # not in the enum
            tier="cli", db_type="postgres",
            install_id=str(uuid.uuid4()), host="claude-code-cli",
        )


def test_rejects_bad_tier():
    with pytest.raises(ValueError, match="tier"):
        t.build_event(
            event_type="query",
            tier="hosted",  # not in the enum
            db_type="postgres",
            install_id=str(uuid.uuid4()), host="claude-code-cli",
        )


def test_rejects_bad_db_type():
    with pytest.raises(ValueError, match="db_type"):
        t.build_event(
            event_type="query",
            tier="cli",
            db_type="oracle",  # not in v1 allowlist
            install_id=str(uuid.uuid4()), host="claude-code-cli",
        )


def test_rejects_bad_host():
    with pytest.raises(ValueError, match="host"):
        t.build_event(
            event_type="query", tier="cli", db_type="postgres",
            install_id=str(uuid.uuid4()),
            host="claude-desktop",  # not a v1 supported host
        )


def test_rejects_bad_error_kind():
    with pytest.raises(ValueError, match="error_kind"):
        t.build_event(
            event_type="error", tier="cli", db_type="postgres",
            install_id=str(uuid.uuid4()), host="claude-code-cli",
            error_kind="oom",  # not in the enum
        )


# --- The big one: planted-PII test ---

PLANTED_VALUES = {
    "user_email": "alice@example.com",
    "user_name": "Alice Anderson",
    "company_name": "ExampleCorp Inc",
    "hostname": "alice-laptop.local",
    "machine_id": "1234567890ABCDEF",
    "query_text": "SELECT email FROM users WHERE id = 42",
    "table_name": "secret_internal_table",
    "schema_content": "CREATE TABLE customers (email TEXT, ssn TEXT)",
    "result_rows": "alice@example.com,Alice,42,SSN-redacted",
    "filesystem_path": "/Users/alice/work/secret-project/db.yaml",
    "stacktrace": "Traceback: psycopg2.errors.UndefinedColumn at line 47",
    "ip_address": "10.0.42.17",
}


def test_planted_pii_does_not_leak(monkeypatch, tmp_path):
    """
    Plant 12 categories of sensitive data into the environment and a fake
    ~/.agami/.config, then build a normal event payload. The payload must
    contain none of the planted strings.
    """
    fake_home = tmp_path / "home"
    fake_agami = fake_home / ".agami"
    fake_agami.mkdir(parents=True)

    install_id = str(uuid.uuid4())
    config = {
        "schema_version": 1,
        "analytics_consent": True,
        "install_id": install_id,
        "tier": "cli",
        "host": "claude-code-cli",
        "consent_ts": "2026-05-06T12:00:00Z",
    }
    (fake_agami / ".config").write_text(json.dumps(config))

    # Plant the values into env + filesystem so anything iterating
    # naively over `os.environ` or the home dir would scoop them up.
    for k, v in PLANTED_VALUES.items():
        monkeypatch.setenv(f"AGAMI_TEST_{k.upper()}", v)
    monkeypatch.setenv("HOME", str(fake_home))

    # Also write planted values into a fake credentials file (not chmod-checked
    # here — we're testing the payload builder, not the credentials reader).
    (fake_agami / "credentials").write_text(
        f"[default]\ntype=postgres\nhost={PLANTED_VALUES['hostname']}\n"
        f"user={PLANTED_VALUES['user_email']}\npassword=hunter2\n"
        f"database={PLANTED_VALUES['table_name']}\n"
    )

    # Build a normal event using the public API. None of the planted values
    # should make it in — the API doesn't have parameters for them.
    event = t.build_event(
        event_type="query",
        tier="cli",
        db_type="postgres",
        install_id=install_id,
        host="claude-code-cli",
        latency_p50_ms=250,
        latency_p95_ms=1100,
    )
    payload = t.build_payload([event])

    serialized = json.dumps(payload)
    for label, value in PLANTED_VALUES.items():
        assert value not in serialized, (
            f"PRIVACY VIOLATION: planted {label} value '{value}' "
            f"appeared in telemetry payload"
        )


def test_payload_batch_size_limit():
    """The server enforces 100 events per batch; client must too."""
    install_id = str(uuid.uuid4())
    events = [
        t.build_event(
            event_type="query", tier="cli", db_type="postgres",
            install_id=install_id, host="claude-code-cli",
        )
        for _ in range(101)
    ]
    with pytest.raises(ValueError, match="<= 100"):
        t.build_payload(events)


# --- Defense in depth: client-built events must pass server schema ---

def test_event_keys_match_doc_table_exactly():
    """
    No extras, no missing-required. If you change ALLOWED_FIELDS,
    you must also: (1) update shared/telemetry-payload.md, (2) update
    services/telemetry-endpoint/src/worker.ts, and (3) bump schema_version.
    """
    event = t.build_event(
        event_type="query", tier="cli", db_type="postgres",
        install_id=str(uuid.uuid4()), host="claude-code-cli",
    )
    # Required fields:
    for required in ("event_type", "install_id", "db_type", "os", "host",
                     "tier", "client_version", "timestamp"):
        assert required in event, f"required field {required} missing"
    # No extras:
    assert set(event.keys()) <= EXPECTED_ALLOWED_FIELDS
