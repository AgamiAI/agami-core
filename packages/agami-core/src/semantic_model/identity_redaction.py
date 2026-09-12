"""Self-reference detection and identity-literal redaction — stdlib-only, on purpose (ACE-118).

A self-referential question ("my", "me", "I", "mine") must never be answered by copying a matched
example's SQL verbatim — the example's own identity literal belongs to whoever asked IT, not to
this caller. Two callers need this: `semantic_model.runtime.is_high_confidence` (the local `sm
examples` path) and `tools.py`'s hosted + local-file `get_prompt_examples` serving.

**This module imports nothing but `re`, deliberately.** It used to live inside `runtime.py`, which
needs Pydantic (`semantic_model.models`) for other things — so `tools.py`'s local file-serving
branch (no database, meant to work on a bare `agami-core` install with no `[model]` extra) had to
import it inside a `try/except ImportError`, and on failure it treated the question as NOT
self-referential and skipped redaction entirely. That is backwards for a security check: an
unavailable dependency silently turned OFF the guarantee instead of leaving it on (ACE-118 review).
Moving this logic somewhere it can never fail to import removes the need for that fallback, and
the guarantee, rather than degrading.
"""

from __future__ import annotations

import re

# Word-boundary + case-insensitive so "minecraft" or "IMPORTANT" don't match. `myself` added after
# review: "tickets assigned to myself" is exactly the self-referential shape this exists to catch,
# and without it such a question would be scored high-confidence and its SQL left unredacted
# (Copilot review).
_SELF_REFERENCE_MARKERS = re.compile(r"\b(my|myself|me|i|mine)\b", re.IGNORECASE)
# A bare "me" preceded by a request verb ("give me", "show me", "tell me", "email me", ...) names
# the ASKER as the recipient of the answer, not the person the answer should be scoped to — "Give
# me all issues assigned to spanda" must not be flagged self-referential on "me" alone when the
# actual filter target ("spanda") is named explicitly and is not the caller. "my"/"mine"/"myself"
# carry no such ambiguity (there is no idiom where they name the recipient rather than the
# subject) and are never excluded; only a bare "me" needs this check.
_REQUEST_VERB_BEFORE_ME = re.compile(
    r"\b(?:give|show|tell|send|email|get|find|fetch|pull)\s+me\b$", re.IGNORECASE
)


def _is_self_referential(question: str) -> bool:
    question = question or ""
    for m in _SELF_REFERENCE_MARKERS.finditer(question):
        if m.group(1).lower() == "me" and _REQUEST_VERB_BEFORE_ME.search(question[: m.end()]):
            continue
        return True
    return False


# A quoted string literal in an equality or IN test against an identity-shaped column, either
# operand order for equality (`email = 'x'` or `'x' = email`) — the shape a stored example's SQL
# uses to name WHOEVER asked it. Conservative and regex-based rather than a real parse, matching
# the heuristic style sql_guard.py already leans on for read-only/deny-list checks elsewhere in
# this codebase: it only needs to catch these shapes, not understand the statement (ACE-118 review).
#
# The list is grounded in `metadata_sources.py`'s own ServiceNow reference-graph fields
# (`opened_by`, `closed_by`, `resolved_by`, `opened_for`, `requested_by`, `watch_list`) and the
# generic person-reference columns this codebase's own tests use as worked examples (`created_by`,
# `approved_by`) — not invented (ACE-118 review; `assignment_group`/`group` are excluded, since
# they name a team, not a person). `sys_id` was removed after review: it is a generic record
# primary key, not a person-reference column — `test_metadata_sources.py` models it as the grain
# of BOTH `incident` and `sys_user` tables, so a filter on a ticket's own `sys_id` alongside a real
# identity column (e.g. `caller_id`) would have its ticket reference corrupted too, changing the
# query's meaning rather than protecting an identity (Copilot review). `caller_id` alone already
# covers the person-identity case this column exists for. `manager_id`/`mentor_id` added after
# review: they're the repository's own worked example of an employee self-join key
# (`model_store.py`'s `_relationship_key` docstring, `test_model_store_roundtrip.py:131-150`) —
# exactly the shape of a person-reference column this list exists to cover.
_IDENTITY_COLUMN_NAMES = (
    r"email|user(?:name)?|owner|assign(?:ed_to|ee)|caller_id|manager_id|mentor_id|"
    r"requested_(?:by|for)|opened_(?:by|for)|closed_by|resolved_by|watch_list|"
    r"created_by|approved_by"
)
# A column reference, optionally table-qualified and optionally quoted in any of the three styles
# this codebase's supported dialects use — double quotes (Postgres/Redshift/Snowflake), backticks
# (MySQL), square brackets (SQL Server); see `dialects.py`. The bare-`\w` version missed every
# quoted form, e.g. `"assigned_to" = '...'`, and a dialect-specific stored example could still
# return its identity literal verbatim (ACE-118 review).
_IDENTITY_COLUMN_REF = (
    rf'(?:"(?:\w+\.)?(?:{_IDENTITY_COLUMN_NAMES})"'
    rf"|`(?:\w+\.)?(?:{_IDENTITY_COLUMN_NAMES})`"
    rf"|\[(?:\w+\.)?(?:{_IDENTITY_COLUMN_NAMES})\]"
    rf"|\b(?:\w+\.)?(?:{_IDENTITY_COLUMN_NAMES})\b)"
)
# A single quoted literal, allowing THREE escape conventions: a backslash escape (`\\.`,
# MySQL-style) and a doubled single quote (`''`) — the standard SQL escaping `dialects.py` itself
# emits for an apostrophe in a value (e.g. `o''reilly`). Missing the doubled-quote form (ACE-118
# review) matched only up to the first `'`, leaving the remainder of the identity unredacted and
# the resulting SQL malformed.
_QUOTED_LITERAL = r"'(?:[^'\\]|\\.|'')*'"
# `!=`/`<>` matched alongside `=` after review: `assignee != 'previous@example.com'` still names
# the previous caller's identity — a self-referential example rejecting everyone but a name is
# just as much a leak of that name as one selecting it (Copilot review).
_EQ_OP = r"(?:=|!=|<>)"
_IDENTITY_LITERAL_RE = re.compile(
    rf"(?P<pre>{_IDENTITY_COLUMN_REF}\s*{_EQ_OP}\s*)(?P<lit>{_QUOTED_LITERAL})"
    rf"|(?P<lit2>{_QUOTED_LITERAL})(?P<post>\s*{_EQ_OP}\s*{_IDENTITY_COLUMN_REF})",
    re.IGNORECASE,
)
# `IN (...)` / `NOT IN (...)` are a second, equally common shape an identity predicate takes and
# the equality pattern above never matches (ACE-118 review) — the whole parenthesized list is
# replaced with one placeholder rather than redacting each member individually, since once any
# member names an identity, the model has no business seeing which values were in the list at all.
# `NOT IN` added alongside `IN` for the same reason `!=` joined `=` above: excluding a caller's
# past identity from a result set still names it in the query text (Copilot review).
_IDENTITY_IN_RE = re.compile(
    rf"(?P<col>{_IDENTITY_COLUMN_REF})\s+(?P<neg>NOT\s+)?IN\s*\(\s*{_QUOTED_LITERAL}"
    rf"(?:\s*,\s*{_QUOTED_LITERAL})*\s*\)",
    re.IGNORECASE,
)
_IDENTITY_REDACTION_PLACEHOLDER = "'<RESOLVE_FROM_CALLER_IDENTITY>'"


def _redact_identity_literals(sql: str) -> str:
    """Replace every identity-shaped equality/inequality or `[NOT] IN (...)` literal in `sql` with
    a placeholder the model cannot mistake for a real value — see `_IDENTITY_LITERAL_RE`/
    `_IDENTITY_IN_RE` for the shapes matched.

    Not an exhaustive SQL grammar — `LIKE` and a value reached through a function call are still
    unredacted. Equality/inequality and `[NOT] IN` are the shapes actually seen in review so far;
    if another shape surfaces, that is the signal to reconsider a real parse (`sqlglot`, already a
    dependency elsewhere in this codebase) rather than extend this pattern again.
    """

    def _sub_eq(m: "re.Match[str]") -> str:
        if m.group("pre") is not None:
            return f"{m.group('pre')}{_IDENTITY_REDACTION_PLACEHOLDER}"
        return f"{_IDENTITY_REDACTION_PLACEHOLDER}{m.group('post')}"

    def _sub_in(m: "re.Match[str]") -> str:
        neg = m.group("neg") or ""
        return f"{m.group('col')} {neg}IN ({_IDENTITY_REDACTION_PLACEHOLDER})"

    sql = _IDENTITY_LITERAL_RE.sub(_sub_eq, sql or "")
    return _IDENTITY_IN_RE.sub(_sub_in, sql)
