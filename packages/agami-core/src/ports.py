"""The four port Protocols — the seams adapters plug into.

agami-core keeps one MCP implementation across deployments; deployment-specific behavior is
swapped at the composition root through these ports, never by forking a tool:

  - ``ActivitySink``     — where query-execution records go (file by default)
  - ``OrgResolver``      — single vs multi tenancy as a config flag, not a schema fork
  - ``AuthProvider``     — bearer token → principal (presence by default)
  - ``GovernancePolicy`` — warn-only by default; enforcement is a paid concern

These are **interfaces only** — `typing.Protocol`, so an adapter satisfies a port by shape, with
no import coupling back to core. The OSS default adapters live in ``oss_adapters`` (so the local
product runs out of the box); a downstream consumer supplies its own.

The seam value types (``Org`` / ``Principal`` / ``GovernanceVerdict``) are stdlib dataclasses, not
pydantic models, so this module imports with **zero dependencies** — a consumer can depend on the
seams without pulling the model deps. The wire shapes that need validation (the 4-tool I/O) live
in ``contracts`` (pydantic). Each type is kept minimal — only what a default adapter or a
consumer needs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    # Only for type-checkers — kept out of the runtime import graph so the Protocols (and a
    # consumer that needs only the seams) import without the pydantic model deps. With
    # `from __future__ import annotations` the method annotations are lazy strings, and
    # @runtime_checkable only checks method *names*, so isinstance() works without these.
    from contracts import QueryExecutionRecord

# ---------------------------------------------------------------------------
# Seam value types (minimal — a consumer extends them when it needs more)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Org:
    """A resolved organization. The model hierarchy is org → datasource → subject-area; the
    default deployment is single-tenant (one Org)."""

    id: str
    name: str | None = None


@dataclass(frozen=True)
class Principal:
    """An authenticated caller. By default this is just token presence (one user); real
    providers populate identity/claims."""

    subject: str


@dataclass(frozen=True)
class GovernanceVerdict:
    """The outcome of a governance check. The default is **warn-only** — ``allowed`` is always
    True and ``warnings`` is advisory; only a paid enforcement tier may set allowed=False."""

    allowed: bool = True
    warnings: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# The four ports
# ---------------------------------------------------------------------------


@runtime_checkable
class ActivitySink(Protocol):
    """Sink for runtime activity, written through the single ``execute_sql`` chokepoint.

    OSS default = the file/jsonl writer (keeps the local skill working). Record shape is the
    local log record (``contracts.QueryExecutionRecord``)."""

    def record_query_execution(self, record: QueryExecutionRecord) -> None: ...


@runtime_checkable
class OrgResolver(Protocol):
    """Resolve the calling context to an ``Org`` — the single/multi-tenant seam.

    OSS default = single-tenant (returns the one configured org)."""

    def resolve_org(self, ctx: object | None = None) -> Org: ...


@runtime_checkable
class AuthProvider(Protocol):
    """Validate a bearer token → ``Principal`` (or ``None`` if invalid).

    OSS default = presence only (enough for a token-gated server); real providers come later."""

    def validate_token(self, token: str) -> Principal | None: ...


@runtime_checkable
class GovernancePolicy(Protocol):
    """Evaluate a request and return a ``GovernanceVerdict`` (warnings; never blocks by default).

    OSS default = warn-only ("basic governance warning"); enforcement is a paid tier."""

    def evaluate(self, ctx: object | None = None) -> GovernanceVerdict: ...


# ---------------------------------------------------------------------------
# Composition-root container — the four adapters passed as one argument
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Adapters:
    """The four port adapters, bundled so ``mcp_http.create_app`` takes them as one argument.

    A consumer builds this with its own implementations of the four ports (its own ``OrgResolver``,
    ``AuthProvider``, ``ActivitySink``, and ``GovernancePolicy``); passing ``adapters=None`` to
    ``create_app`` uses the OSS defaults (``mcp_http.default_adapters``)."""

    activity_sink: ActivitySink
    org_resolver: OrgResolver
    auth_provider: AuthProvider
    governance: GovernancePolicy
