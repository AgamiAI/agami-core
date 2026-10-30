"""What the server extra installs (ACE-152).

MCP SDK 2 instruments itself with OpenTelemetry. It depends on `opentelemetry-api` only, which is a
no-op until an SDK and an exporter are installed and configured. The day either arrives through our
dependency closure, the trace of every tool call — its arguments, the SQL, the caller — has
somewhere to go, and nobody chose that. So the closure is held to the API alone.

The closure is walked from the installed metadata rather than read off `pip list`: a developer who
installed an exporter by hand for their own debugging has not changed what `agami-core[server]`
pulls in, and must not fail this test.
"""

from __future__ import annotations

import importlib.metadata as metadata

import pytest

pytest.importorskip("mcp")

from packaging.requirements import Requirement  # noqa: E402
from packaging.specifiers import SpecifierSet  # noqa: E402
from packaging.utils import canonicalize_name  # noqa: E402
from packaging.version import Version  # noqa: E402

SDK_LINE = SpecifierSet(">=2.2,<3")


def _closure(root: str, extras: set[str]) -> set[str]:
    """Every distribution `root[extras]` requires, transitively, on this interpreter and platform.

    A requirement counts when its marker holds for one of the extras it was reached through (or for
    none, when it names no extra), so an optional extra of a dependency is followed only when
    something asked for it.
    """
    seen: set[str] = set()
    pending = [(canonicalize_name(root), extras)]
    while pending:
        name, wanted = pending.pop()
        key = name if not wanted else f"{name}[{','.join(sorted(wanted))}]"
        if key in seen:
            continue
        seen.add(key)
        for line in metadata.distribution(name).requires or []:
            req = Requirement(line)
            envs = [{"extra": e} for e in wanted] or [{"extra": ""}]
            if req.marker is None or any(req.marker.evaluate(env) for env in envs):
                pending.append((canonicalize_name(req.name), set(req.extras)))
    return {key.split("[")[0] for key in seen}


def test_server_extra_pulls_no_otel_sdk():
    closure = _closure("agami-core", {"server"})

    # Not vacuous: the walk reaches past our own declarations into the SDK's.
    assert "starlette" in closure
    leaked = sorted(
        name
        for name in closure
        if name == "opentelemetry-sdk" or name.startswith("opentelemetry-exporter-")
    )
    assert leaked == []


def test_the_sdk_is_on_the_2_line():
    declared = [
        Requirement(line)
        for line in metadata.distribution("agami-core").requires or []
        if Requirement(line).name == "mcp"
    ]

    assert [req.specifier for req in declared] == [SDK_LINE]
    assert Version(metadata.version("mcp")) in SDK_LINE
