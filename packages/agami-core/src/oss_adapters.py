"""OSS default adapters for the four ports.

These defaults make the local product run out of the box — the single-tenant resolver, the
file/jsonl activity sink, presence-only auth, and warn-only governance. They live in agami-core
(not the ``agami-oss-adapters`` placeholder) so ``pip install agami-core`` is enough to run
locally; richer adapters (a Postgres sink, real auth providers, enforcement) are supplied by
their own consumers.

Each adapter satisfies its ``ports`` Protocol structurally — no inheritance needed.
"""

from __future__ import annotations

import json
from pathlib import Path

import agami_paths
from contracts import FeedbackRecord, QueryExecutionRecord
from ports import GovernanceVerdict, Org, Principal


def _append_jsonl(path: Path, record: dict) -> bool:
    """Append one JSON line. Mirrors mcp_harness._append_jsonl — best-effort: a logging failure
    must never break a query (the local skill's contract), so OSError is swallowed, not raised."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, default=str) + "\n")
        return True
    except OSError:
        return False


class FileActivitySink:
    """Default ``ActivitySink`` — appends query/feedback records to the same jsonl files the local
    skill uses (``<artifacts_dir>/local/...``). A Postgres adapter can replace this."""

    def __init__(self, query_log: Path | None = None, feedback_log: Path | None = None) -> None:
        # Resolve lazily by default (the artifacts dir may not be bootstrapped at construction);
        # paths can be injected for testing.
        self._query_log = query_log
        self._feedback_log = feedback_log

    def _query_log_path(self) -> Path:
        return self._query_log or agami_paths.query_log_path()

    def _feedback_log_path(self) -> Path:
        return self._feedback_log or (agami_paths.local_dir() / "feedback.jsonl")

    def record_query_execution(self, record: QueryExecutionRecord) -> None:
        _append_jsonl(self._query_log_path(), record.model_dump())

    def record_feedback(self, record: FeedbackRecord) -> None:
        _append_jsonl(self._feedback_log_path(), record.model_dump())


class SingleTenantOrgResolver:
    """Default ``OrgResolver`` — the N=1 deployment resolves every context to the one configured
    org (tenancy is a config flag, not a schema fork). Multi-tenant resolvers come later."""

    def __init__(self, org: Org | None = None) -> None:
        self._org = org or Org(id="local")

    def resolve_org(self, ctx: object | None = None) -> Org:
        return self._org


class PresenceAuthProvider:
    """Default ``AuthProvider`` — a non-empty token is accepted as the single local user; empty/
    missing is rejected. Enough for a token-gated server; real identity providers come later."""

    def __init__(self, subject: str = "local") -> None:
        self._subject = subject

    def validate_token(self, token: str) -> Principal | None:
        return Principal(subject=self._subject) if (token or "").strip() else None


class WarnOnlyGovernancePolicy:
    """Default ``GovernancePolicy`` — never blocks (``allowed=True``); any warnings are advisory
    ("basic governance warning"). Enforcement is a paid tier."""

    def evaluate(self, ctx: object | None = None) -> GovernanceVerdict:
        return GovernanceVerdict(allowed=True, warnings=[])
