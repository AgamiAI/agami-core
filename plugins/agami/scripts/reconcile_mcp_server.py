#!/usr/bin/env python3
"""The agami MCP server a reconcile run serves to its cold client.

`mcp_harness` is the local stdio server a person actually uses. This module IS that server: it
imports it, serves the same four tools over the same transport, and adds exactly two things a
reconcile run needs and a person's session must never have.

**Why this is a separate entrypoint and not a flag on the product.** The two additions below are
facts about a measured run, not about agami. Putting them in `tools.py` would mean a refusal rule in
the product's guardrail vocabulary (and in the contract test that pins it) for a decision that can
never fire in a real deployment, and a `Failure` is definitionally "not a third thing we chose". So
they live here, wrapping the registry the server dispatches through, and the product is untouched.

**Why it is enforced here rather than asked for in the prompt.** An instruction can be partly
obeyed, and then nobody knows what the run measured. A client cannot talk past its own transport.
That is the whole reason this file exists rather than a paragraph in the system prompt.

  1. **A query that did not run ends the row.** Reconcile's finding IS the failure: a statement the
     warehouse rejected, or one the guardrail refused, says the semantic model or the tool fetching
     is broken. A query that ran and returned no rows is not this case: it ran, and it answered. A client left free to retry would paper over exactly that, so after a
     query comes back anything but `ok`, every later query is stopped here. Note that a REFUSAL
     counts: an out-of-scope table is the semantic model being wrong about the warehouse, which is
     the finding, not an obstacle to route around.

  2. **Every call is written to a trace.** One JSON object per line, in call order. This is the
     row's own record of how agami was asked, and it is taken at the transport boundary so it is
     what the client actually sent, not what the client later says it sent.

Successful queries are NOT capped. A client probing the data before it answers is a person probing
the data before they answer, and the count of those probes is the measurement: it reads how much the
semantic model failed to say up front. Only a runaway ceiling applies, and it sits far above what any
healthy row uses.

Launched by `run_golden_eval.py --via mcp` through the client's own `--mcp-config`, never by hand.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _agami_lib  # noqa: E402

_agami_lib.ensure_importable()

#: Where this row's call trace is written. Absent means the run wanted no trace, which is the
#: default so that importing this module never writes to a path nobody asked for.
TRACE_ENV = "AGAMI_RECONCILE_TRACE"

#: The runaway ceiling on SUCCESSFUL queries. Not a quality signal and not the budget the run cares
#: about: it stops a client that has stopped making progress, and nothing else.
DEFAULT_MAX_QUERIES = 10

#: The tool the two rules above are about. The other three are local, cheap, and the more a client
#: calls them the more the trace has to say, so they are never counted and never stopped.
QUERY_TOOL = "execute_sql"

# What the client reads when a rule above stops it. Deliberately NOT shaped like agami's own refusal
# envelope: the run harness stopped this call, agami did not, and a message that pretended otherwise
# would put words in the product's mouth on a page someone reads to decide whether to trust it.
STOPPED_AFTER_FAILURE = (
    "A query in this run did not run: it was blocked, the database rejected it, or the tool failed. "
    "This run allows no query after that one. "
    "Report what happened and stop. Do not rewrite the statement and run it again: the failure "
    "is what this run is measuring."
)
STOPPED_AT_CEILING = (
    "This run allows {ceiling} queries and that many have already run. Answer from what you have "
    "already read, or say that you cannot."
)


def _stopped(reason: str, message: str) -> str:
    """The body of a stopped call. `run_harness_stopped` is the key a reader greps for: it says the
    harness did this, so nothing here can be mistaken for agami refusing a statement."""
    return json.dumps({"run_harness_stopped": {"reason": reason, "message": message}})


def _status_of(text: str) -> str | None:
    """The envelope status a tool returned, or None when it returned something without one.

    Only `execute_sql` answers with an envelope, so None means "not a query outcome" and is treated
    as success: a schema call that returned anything at all did its job.
    """
    try:
        payload = json.loads(text)
    except (TypeError, ValueError):
        return None
    return payload.get("status") if isinstance(payload, dict) else None


def _detail_of(text: str) -> str | None:
    """Why a query did not answer, in the product's own words.

    This is the diagnostic the whole run exists to surface, so it is relayed verbatim rather than
    replaced with a fixed sentence. It is safe to relay where a client's stderr is not: a refusal's
    detail and a failure's message are the product's OWN value-free text, written to be read by
    whoever asked, while raw driver text is captured server-side and never reaches here.
    """
    try:
        payload = json.loads(text)
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    refusal = payload.get("refusal") or {}
    failure = payload.get("failure") or {}
    for part in (refusal.get("detail"), refusal.get("remediation"), failure.get("message")):
        if isinstance(part, str) and part.strip():
            return part.strip()
    return None


class RunBudget:
    """The two rules, and the trace, as one object so a test can drive them without a subprocess."""

    def __init__(self, *, trace_path: Path | None = None, ceiling: int = DEFAULT_MAX_QUERIES) -> None:
        self.trace_path = trace_path
        self.ceiling = ceiling
        self.queries_ok = 0
        self.stopped_reason: str | None = None

    # -- the trace ---------------------------------------------------------

    def record(self, entry: dict[str, Any]) -> None:
        if self.trace_path is None:
            return
        self.trace_path.parent.mkdir(parents=True, exist_ok=True)
        with self.trace_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry) + "\n")

    # -- the rules ---------------------------------------------------------

    def _stop_reason(self, name: str) -> str | None:
        """Why this call may not run, or None to let it through. Only queries are ever stopped."""
        if name != QUERY_TOOL:
            return None
        if self.stopped_reason is not None:
            return self.stopped_reason
        if self.queries_ok >= self.ceiling:
            return "ceiling"
        return None

    def wrap(self, name: str, handler: Callable[[dict[str, Any]], str]) -> Callable[[dict[str, Any]], str]:
        """`handler`, with the rules applied and the call written to the trace."""

        def wrapped(args: dict[str, Any]) -> str:
            reason = self._stop_reason(name)
            if reason is not None:
                message = (
                    STOPPED_AT_CEILING.format(ceiling=self.ceiling)
                    if reason == "ceiling"
                    else STOPPED_AFTER_FAILURE
                )
                self.record({"tool": name, "args": _trim(args), "stopped": reason})
                return _stopped(reason, message)

            started = time.monotonic()
            try:
                text = handler(args)
            except Exception as exc:
                # The server turns this into an isError result of its own. Record it first: a tool
                # that raised is a failed query for rule 1's purposes, and losing that would let the
                # next call through as if nothing had happened.
                self._observe(name, "raised")
                self.record(
                    {
                        "tool": name,
                        "args": _trim(args),
                        "status": "raised",
                        "detail": type(exc).__name__,
                        "ms": round((time.monotonic() - started) * 1000),
                    }
                )
                raise

            status = _status_of(text)
            self._observe(name, status)
            entry: dict[str, Any] = {
                "tool": name,
                "args": _trim(args),
                "status": status,
                "ms": round((time.monotonic() - started) * 1000),
            }
            if name == QUERY_TOOL and status not in (None, "ok"):
                entry["detail"] = _detail_of(text)
            self.record(entry)
            return text

        return wrapped

    def _observe(self, name: str, status: str | None) -> None:
        """Count a query that answered, or latch on the first one that did not."""
        if name != QUERY_TOOL:
            return
        if status is None or status == "ok":
            self.queries_ok += 1
            return
        # `refused` and `failed` both land here, and both are the finding. See the module docstring.
        self.stopped_reason = "query_failed"


#: Argument values longer than this are cut in the trace. The SQL is the point of the record and is
#: never near this; a pasted blob would be, and the trace is meant to be read.
_TRIM_AT = 4000


def _trim(args: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in args.items():
        if isinstance(value, str) and len(value) > _TRIM_AT:
            out[key] = value[:_TRIM_AT] + f"… [{len(value) - _TRIM_AT} more characters]"
        else:
            out[key] = value
    return out


def install(budget: RunBudget) -> None:
    """Replace every handler in the shared registry with its wrapped form.

    The server looks the handler up by name on each call (`mcp_harness._handle_tools_call`), so
    replacing the entry here is enough and the server itself needs no change. The registry OBJECT is
    mutated rather than rebound because `mcp_harness.TOOLS is tools.TOOLS` is pinned by a test, and
    that identity is what makes both servers advertise one registry.
    """
    import tools

    for name, meta in list(tools.TOOLS.items()):
        tools.TOOLS[name] = {**meta, "handler": budget.wrap(name, meta["handler"])}


def budget_from_env() -> RunBudget:
    trace = os.environ.get(TRACE_ENV)
    return RunBudget(trace_path=Path(trace) if trace else None)


def main() -> int:
    import mcp_harness

    install(budget_from_env())
    return mcp_harness.serve()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)
