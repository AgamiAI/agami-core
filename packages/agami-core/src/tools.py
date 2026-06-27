#!/usr/bin/env python3
"""
The shared MCP tool registry + implementations — one impl, both transports.

`TOOLS` (name → {handler, description, inputSchema}) is the single source both the stdio
entrypoint (`mcp_harness`) and the HTTP entrypoint (`mcp_http`) advertise, so a client sees the
same surface and behavior whether it connects over stdio (Claude Desktop) or HTTP (claude.ai).

The surface is the **5 product tools**: `list_datasources`, `get_datasource_schema` (adaptive),
`get_prompt_examples`, `execute_sql`, `log_feedback`.

Design constraints (match the rest of agami):
  - The execute_sql + log_feedback tools are pure-stdlib. The model-backed tools import the
    `semantic_model` package (Pydantic) lazily and surface a clear "install the model deps" error
    if it's absent — so execution still works on a bare install.
  - **No data leaves the machine.** SQL is executed locally by shelling out to `execute_sql` (the
    same executor the skills use), which runs the fan/chasm pre-flight + default_filters safety
    pass; the semantic model is read from `<artifacts_dir>/<profile>/`.
"""

from __future__ import annotations

import csv
import io
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Paths & config resolution (mirrors execute_sql.py / file-layout.md exactly)
# ---------------------------------------------------------------------------
import agami_paths

# Secrets + per-user state live under <artifacts_dir>/local/. Re-resolved after bootstrap() in main().
AGAMI_LOCAL = agami_paths.local_dir()
CREDENTIALS_PATH = agami_paths.credentials_path()
CONFIG_PATH = agami_paths.config_path()
QUERY_LOG = agami_paths.query_log_path()
FEEDBACK_LOG = AGAMI_LOCAL / "feedback.jsonl"
TOOL_CALL_LOG = AGAMI_LOCAL / "tool_calls.jsonl"

SERVER_NAME = "agami"


def server_version() -> str:
    """Best-effort version: the AGAMI_VERSION env override, else the installed package metadata.
    Shared by both transports' serverInfo."""
    env_v = os.environ.get("AGAMI_VERSION")
    if env_v:
        return env_v
    try:
        from importlib.metadata import PackageNotFoundError, version

        return version("agami-core")
    except PackageNotFoundError:
        return "0.0.0"


# Client-facing usage guidance, surfaced by both transports. Describes the 5-tool flow;
# no save_correction (that's a skill operation, not on the MCP surface).
SERVER_INSTRUCTIONS = (
    "agami local datasource agent. The NL→SQL intelligence runs on your side; these tools provide "
    "the local semantic model + curated examples and execute SQL locally. All execution is local.\n"
    "Flow: (1) list_datasources, then get_datasource_schema for the datasource the question "
    "touches (it sizes itself — pass a `query` to focus metrics, `dataset_names` for full table "
    "detail). (2) Examples-first — call get_prompt_examples and mirror the closest match; use "
    "metric `calculation`/`bindings` verbatim. (3) execute_sql (safety + default_filters run "
    "inside it). (4) Read the returned `receipt`: SHOW the user `receipt.warnings` and any "
    "`receipt.metrics` whose review_state != 'approved' — joins/metrics they haven't signed off; "
    "never hide them. Don't refuse on an unreviewed metric — answer and warn.\n"
    "PII: a column marked `sensitive: true` restricts OUTPUT, not the query — you MAY "
    "COUNT/COUNT(DISTINCT)/filter/GROUP BY/JOIN on it, but never SELECT its raw per-row values. "
    "'unique emails' → COUNT(DISTINCT email). To disambiguate identical labels, project the "
    "non-sensitive id. (execute_sql enforces this and errors on a raw sensitive projection.)\n"
    "Activity log: on execute_sql, pass `user_question` (the user's verbatim question) and a "
    "`thread_id` you generate once per conversation and reuse on every call — so a deployment admin "
    "can see what was asked and group a conversation's queries. Best-effort; omit if unknown."
)


def bootstrap_paths() -> None:
    """Re-resolve the module-level paths at startup. agami_paths.bootstrap() also runs a one-time
    migration of any *legacy* ~/.agami install into the current <artifacts_dir>/local layout — new
    installs never create ~/.agami, so the migration is a no-op once there's nothing to move (it's
    a backward-compat shim, safe to drop in a later cleanup). Every entrypoint calls this so the
    paths reflect the resolved (possibly migrated) artifacts dir."""
    global AGAMI_LOCAL, CREDENTIALS_PATH, CONFIG_PATH, QUERY_LOG, FEEDBACK_LOG, TOOL_CALL_LOG
    agami_paths.bootstrap()
    AGAMI_LOCAL = agami_paths.local_dir()
    CREDENTIALS_PATH = agami_paths.credentials_path()
    CONFIG_PATH = agami_paths.config_path()
    QUERY_LOG = agami_paths.query_log_path()
    FEEDBACK_LOG = AGAMI_LOCAL / "feedback.jsonl"
    TOOL_CALL_LOG = AGAMI_LOCAL / "tool_calls.jsonl"


def _load_config() -> dict[str, Any]:
    if CONFIG_PATH.exists():
        try:
            return json.loads(CONFIG_PATH.read_text())
        except (OSError, ValueError):
            pass
    return {}


def resolve_profile(explicit: str | None = None) -> str:
    """Resolution order: explicit arg → AGAMI_PROFILE → .config.active_profile → 'default'."""
    if explicit:
        return explicit
    env = os.environ.get("AGAMI_PROFILE")
    if env:
        return env
    active = _load_config().get("active_profile")
    if isinstance(active, str) and active:
        return active
    return "default"


def resolve_artifacts_dir() -> Path:
    """Resolution order: AGAMI_ARTIFACTS_DIR → ~/.config/agami/path pointer → default
    ~/agami-artifacts. (The pointer, not .config, holds the location now — so there's no
    chicken-and-egg: .config itself lives under <artifacts_dir>/local/.)"""
    return agami_paths.artifacts_dir()


def _credentials_sections() -> dict[str, dict[str, str]]:
    """Parse <artifacts_dir>/local/credentials (INI) into {profile: {field: value}}. Empty on any error."""
    if not CREDENTIALS_PATH.exists():
        return {}
    import configparser

    cfg = configparser.ConfigParser(inline_comment_prefixes=("#", ";"))
    try:
        cfg.read(CREDENTIALS_PATH)
    except configparser.Error:
        return {}
    out: dict[str, dict[str, str]] = {}
    for section in cfg.sections():
        out[section] = {
            k: (v.strip() if isinstance(v, str) else v) for k, v in cfg[section].items()
        }
    return out


def _db_type_for(profile: str, creds: dict[str, dict[str, str]]) -> str:
    sect = creds.get(profile, {})
    t = sect.get("type", "")
    if not t and sect.get("url"):
        # Map a DSN scheme → the datasource `type` label (display only; execution is execute_sql's
        # job). Covers the DBs agami advertises; an unknown scheme passes through verbatim.
        scheme = sect["url"].split("://", 1)[0].split("+", 1)[0].lower()
        t = {
            "postgresql": "postgres",
            "postgres": "postgres",
            "mysql": "mysql",
            "mariadb": "mysql",
            "redshift": "redshift",
            "snowflake": "snowflake",
            "bigquery": "bigquery",
            "bq": "bigquery",
            "sqlite": "sqlite",
            "mssql": "sqlserver",
            "sqlserver": "sqlserver",
            "oracle": "oracle",
            "databricks": "databricks",
            "trino": "trino",
            "presto": "trino",
            "duckdb": "duckdb",
        }.get(scheme, scheme)
    return t


# ---------------------------------------------------------------------------
# Read-only SQL guard (mirrors shared/sql-generation-rules.md → Safety Rules)
# ---------------------------------------------------------------------------

_COMMENT_LINE = re.compile(r"--[^\n]*")
_COMMENT_BLOCK = re.compile(r"/\*.*?\*/", re.DOTALL)
# Data-modifying statements that could ride after a leading WITH (Postgres
# allows `WITH x AS (...) DELETE ...`). DDL (DROP/CREATE/ALTER/TRUNCATE) cannot
# follow WITH, and a single statement that begins with SELECT/WITH otherwise
# cannot mutate — so guarding these four keywords + the leading-token + the
# single-statement rule is sufficient without the false positives of scanning
# every DDL keyword (which collide with identifiers like create_date).
_MUTATION = re.compile(r"\b(INSERT|UPDATE|DELETE|MERGE)\b", re.IGNORECASE)


def _strip_comments(sql: str) -> str:
    return _COMMENT_BLOCK.sub(" ", _COMMENT_LINE.sub(" ", sql))


def check_read_only(sql: str) -> str | None:
    """Return None if the SQL is a safe single read-only statement, else a reason string."""
    stripped = _strip_comments(sql).strip()
    # Tolerate a single trailing semicolon; reject any interior one (multi-statement).
    if stripped.endswith(";"):
        stripped = stripped[:-1].strip()
    if not stripped:
        return "empty statement"
    if ";" in stripped:
        return "multiple statements are not allowed — send one SELECT"
    head = stripped.lstrip("(").split(None, 1)[0].upper() if stripped else ""
    if head not in ("SELECT", "WITH"):
        return f"only SELECT / WITH...SELECT is allowed (statement starts with {head or '?'})"
    if _MUTATION.search(stripped):
        return "statement contains a data-modifying keyword (INSERT/UPDATE/DELETE/MERGE)"
    return None


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------


def _load_org(profile: str):
    """Lazily load the semantic model for a profile, producing an `Organization`. Two backends
    behind one seam: when AGAMI_DB_URL is set the hosted server reads it from the DB; otherwise the
    local skill reads the YAML files (unchanged). Raises a clear error if the model deps (pydantic)
    aren't importable or there's no model for the profile."""
    from store import Store  # stdlib-light; psycopg2/sqlite imported lazily inside

    store = Store.from_env()
    if store is not None:
        from model_store import load_organization as _load_db

        try:
            org = _load_db(store, profile)
        finally:
            store.close()
        if org is None:
            raise FileNotFoundError(
                f"No semantic model in the database for datasource {profile!r}. Load it from YAML "
                f"with the deploy's model loader."
            )
        return org

    from semantic_model import loader as L  # may raise ImportError (pydantic)

    root = resolve_artifacts_dir() / profile
    if not (root / "org.yaml").exists():
        raise FileNotFoundError(
            f"No semantic model at {root}/org.yaml. Run the agami-connect skill to "
            f"introspect this database."
        )
    return L.load_organization(root)


def _resolve_units(profile: str, sql: str) -> dict[str, str]:
    """Best-effort {result-column -> unit} by **tracing the SQL** (so `SUM(amount) AS
    total` inherits amount's currency, not just bare-name matches). Returns {} if the
    model deps (pydantic/sqlglot) aren't installed — execute_sql stays pure-stdlib;
    numbers still format exactly via units.py, just without a currency symbol."""
    try:
        org = _load_org(profile)
        from semantic_model import runtime as RT

        return RT.resolve_result_units(org, sql)
    except Exception:
        return {}


def _model_version(profile: str) -> str | None:
    """The model-version pin the receipt records — the newest model version. Served from the DB
    when AGAMI_DB_URL is set (no file read), else the newest snapshot dir name (a content hash, what
    the local skill reads). None if absent/unavailable (execute_sql stays usable)."""
    from store import Store

    store = Store.from_env()
    if store is not None:
        from model_store import newest_model_version

        try:
            return newest_model_version(store, profile)
        except Exception:
            return None
        finally:
            store.close()
    try:
        from semantic_model import snapshot as SN

        return SN.newest_version(resolve_artifacts_dir() / profile)
    except Exception:
        return None


def _domain_memory(profile: str) -> tuple[str, str | None]:
    """(ORGANIZATION.md text, USER_MEMORY.md text) for the domain-context block — from the DB when
    AGAMI_DB_URL is set (no file read at runtime), else from disk."""
    from store import Store

    store = Store.from_env()
    if store is not None:
        from model_store import load_memory

        try:
            mem = load_memory(store, profile)
        finally:
            store.close()
        return mem.get("organization") or "", mem.get("user")
    artifacts = resolve_artifacts_dir()
    return (_read_text(artifacts / profile / "ORGANIZATION.md") or ""), _read_text(
        artifacts / "USER_MEMORY.md"
    )


def _resolve_receipt(profile: str, sql: str) -> dict | None:
    """The FULL trust receipt (tables / relationships / metrics+review_state / assumptions
    / warnings) for this query — the SAME assembler the skill uses, so the 'what did this
    touch / what's unapproved' panel is identical in Claude Code and Claude Desktop.
    Returns None only if the model deps aren't importable (execute_sql stays usable)."""
    try:
        org = _load_org(profile)
        from semantic_model import runtime as RT

        return RT.assemble_receipt(org, sql, model_version=_model_version(profile))
    except Exception:
        return None


def tool_list_datasources(_args: dict[str, Any]) -> str:
    """Local analog of Ask Agami `list_organizations`: enumerate local profiles."""
    creds = _credentials_sections()
    artifacts = resolve_artifacts_dir()
    active = resolve_profile()
    out = []
    for profile in sorted(creds.keys()):
        pdir = artifacts / profile
        table_count = 0
        if pdir.is_dir():
            table_count = (
                sum(1 for _ in (pdir / "subject_areas").glob("*/tables/*.yaml"))
                if (pdir / "subject_areas").is_dir()
                else 0
            )
        out.append(
            {
                "datasource": profile,
                "database_type": _db_type_for(profile, creds),
                "table_count": table_count,
                "model_present": (pdir / "org.yaml").exists(),
                "is_active": profile == active,
            }
        )
    if not out:
        return json.dumps(
            {
                "datasources": [],
                "note": "No profiles found in your credentials file. Run the agami-connect skill first.",
            },
            indent=2,
        )
    return json.dumps({"datasources": out, "active_datasource": active}, indent=2)


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text()
    except OSError:
        return None


def _distill_for_llm(text: str | None) -> str:
    """Strip the human-only scaffolding from a context doc (ORGANIZATION.md / USER_MEMORY.md)
    before it goes into the model's prompt. These files serve two readers: a human editing
    them (who wants the `<!-- edit freely … -->` prompts) and the LLM reading them as query
    context (for whom those prompts are noise — or worse, a "this was auto-generated" aside it
    might distrust). The skill strips comments on its read path; the MCP must match, or Claude
    Desktop sees the raw scaffolding on every query. Drops HTML comments + collapses the blank
    lines they leave behind."""
    if not text:
        return ""
    out = re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out.strip()


# --- get_datasource_schema adaptive sizing ---------------------------------
# A full semantic model can be enormous and overwhelm the client's context. `mode="auto"` picks
# an initial verbosity by **subject-area count** (agami-core's primary unit); the char budget is
# the hard backstop that downgrades one rung at a time (full→summary→index) even for an explicit
# `mode="full"`, so a single tool result can't blow the context window.
_AUTO_FULL_MAX_AREAS = 12  # <= this -> full
_AUTO_SUMMARY_MAX_AREAS = 50  # <= this -> summary; above -> index
_SCHEMA_CHAR_BUDGET = 60_000  # decoded len(json.dumps(...)) ceiling (~15K tokens)
_SCHEMA_MODE_DOWNGRADE = {"full": "summary", "summary": "index", "index": None}
_LARGE_TABLE_ROWS = 1_000_000  # tables at/above this surface in `large_tables` in every mode

# Metric ranking (lexical, no embeddings): exact/substring hits ("strong") are always kept; the
# weak token-overlap tail needs >= this coverage and is capped at top-K. This only decides which
# metrics get FULL detail inline — `get_datasource_schema` ALWAYS returns `metric_index` (every
# metric's name + one-liner), so a metric that matches nothing is never hidden: the client sees it
# exists and can pull it by name via `metric_names`. The stopwords carry no metric-identity signal,
# so they're dropped from the weak token-overlap path only (exact/substring still match them).
_METRIC_MATCH_TOP_K = 10
_METRIC_MATCH_MIN_COVERAGE = 0.6
_METRIC_MATCH_STOPWORDS = frozenset(
    {
        "per",
        "to",
        "by",
        "of",
        "the",
        "a",
        "an",
        "and",
        "in",
        "for",
        "on",
        "vs",
        "average",
        "avg",
        "mean",
        "rate",
        "ratio",
        "total",
        "number",
        "num",
        "count",
        "percentage",
        "percent",
        "pct",
    }
)
_METRIC_WORD_RE = re.compile(r"[a-z0-9]+")


def _auto_mode_for(area_count: int) -> str:
    """Pick the initial verbosity for mode='auto' by subject-area count."""
    if area_count <= _AUTO_FULL_MAX_AREAS:
        return "full"
    if area_count <= _AUTO_SUMMARY_MAX_AREAS:
        return "summary"
    return "index"


def _norm_phrase(s: str | None) -> str:
    """Lowercase + word-tokenize + single-space join (case/underscore/punct collapse away)."""
    return " ".join(_METRIC_WORD_RE.findall((s or "").lower()))


def _content_tokens(s: str | None) -> set[str]:
    """Non-stopword tokens with naive plural folding — the token-overlap path only."""
    out: set[str] = set()
    for t in _METRIC_WORD_RE.findall((s or "").lower()):
        if t in _METRIC_MATCH_STOPWORDS:
            continue
        out.add(t[:-1] if len(t) > 3 and t.endswith("s") else t)
    return out


def _all_metrics(org) -> dict[str, tuple[Any, str | None]]:
    """Map a unique key -> (metric, area) for every metric (subject-area + cross-area). The key is
    the metric name, disambiguated by area on a collision so two areas sharing a metric name are
    BOTH kept (the never-hide contract: every metric must appear in metric_index)."""
    out: dict[str, tuple[Any, str | None]] = {}

    def _add(m, area: str | None) -> None:
        key = m.name
        if key in out:  # name collision across areas — disambiguate, keep both
            key = f"{m.name} ({area})" if area else f"{m.name} (cross-area)"
        out[key] = (m, area)

    for sa in org.subject_areas:
        for m in sa.metrics:
            _add(m, sa.name)
    for m in getattr(org, "cross_subject_area_metrics", []):
        _add(m, None)
    return out


def _match_metrics(query: str | None, metrics: dict[str, tuple[Any, str | None]]) -> list[str]:
    """Lexically rank metrics against `query` -> matched names. Strong (exact/substring) hits are
    never dropped by the cap; the cap bounds only the weak token-overlap tail. [] if no match."""
    q_norm = _norm_phrase(query)
    if not q_norm:
        return []
    q_tokens = _content_tokens(query)
    scored: list[tuple[float, bool, str]] = []
    for name, (m, _area) in metrics.items():
        # Match on the metric's real name (not the possibly area-disambiguated dict key).
        cand_phrases = [m.name.replace("_", " "), m.description or ""] + list(m.other_names or [])
        cand_norms = [c for c in (_norm_phrase(p) for p in cand_phrases) if c]
        score, strong = 0.0, False
        for cn in cand_norms:
            if cn == q_norm:
                score, strong = max(score, 100.0), True
            elif cn in q_norm or q_norm in cn:
                score, strong = max(score, 60.0), True
        if q_tokens:
            cand_tokens: set[str] = set()
            for cn in cand_norms:
                cand_tokens |= _content_tokens(cn)
            if cand_tokens:
                coverage = len(q_tokens & cand_tokens) / len(q_tokens)
                if coverage >= _METRIC_MATCH_MIN_COVERAGE:
                    score = max(score, 20.0 * coverage)
        if score > 0:
            scored.append((score, strong, name))
    if not scored:
        return []
    scored.sort(key=lambda t: (-t[0], t[2]))
    strong_hits = [n for _, st, n in scored if st]
    result = list(dict.fromkeys(strong_hits + [n for _, _, n in scored]))
    return result[: max(_METRIC_MATCH_TOP_K, len(strong_hits))]


def _metric_full(m, area: str | None) -> dict[str, Any]:
    return {
        "name": m.name,
        "area": area,
        "description": m.description,
        "calculation": m.calculation,
        "other_names": list(m.other_names or []),
        "review_state": m.review_state,
    }


def _large_tables(org) -> dict[str, int]:
    out: dict[str, int] = {}
    for sa in org.subject_areas:
        for t in sa.tables_defined:
            ph = t.performance_hints
            rc = ph.estimated_row_count if ph else None
            if rc and rc >= _LARGE_TABLE_ROWS:
                out[t.name] = rc
    return out


def _table_contexts(org, table_names: list[str], L) -> dict[str, Any]:
    """Full get_table_context for the named tables, grouped back into a {name: ctx} map."""
    area_of = {t.name: sa.name for sa in org.subject_areas for t in sa.tables_defined}
    by_area: dict[str | None, list[str]] = {}
    for t in table_names:
        by_area.setdefault(area_of.get(t), []).append(t)
    contexts: dict[str, Any] = {}
    for area, tbls in by_area.items():
        ctx = L.get_table_context(
            org,
            tbls,
            area=area,
            include=["default_filters", "relationships", "caveats", "value_transforms", "metrics"],
        )
        contexts.update(ctx.get("tables", {}))
    return contexts


def _schema_payload(
    org, profile: str, mode: str, matched: list[str], metrics: dict[str, tuple[Any, str | None]], L
) -> dict[str, Any]:
    """Build the structured schema payload at the given verbosity. `metric_index` + `large_tables`
    are always present (the never-hide net); `metrics` carries FULL detail for the matched set, or
    every metric in `full` with no query."""
    result: dict[str, Any] = {
        "datasource": profile,
        "organization": org.description or None,
        "mode": mode,
        "cross_area_relationships": [
            {
                "from": r.from_subject_area,
                "to": r.to_subject_area,
                "for_questions_about": r.for_questions_about,
            }
            for r in org.cross_subject_area_relationships
        ],
        "metric_index": {n: (m.description or n) for n, (m, _a) in metrics.items()},
        "large_tables": _large_tables(org),
    }
    if mode == "index":
        result["subject_areas"] = [
            {"name": sa.name, "description": sa.description, "table_count": len(sa.tables)}
            for sa in org.subject_areas
        ]
    else:  # summary or full — areas carry their table list (name + one-line description)
        result["subject_areas"] = [
            {
                "name": sa.name,
                "description": sa.description,
                "default_time_window": sa.default_time_window,
                "tables": [
                    {"name": t.name, "description": t.description} for t in sa.tables_defined
                ],
            }
            for sa in org.subject_areas
        ]
    if mode == "full":
        result["tables"] = _table_contexts(
            org, [t.name for sa in org.subject_areas for t in sa.tables_defined], L
        )
    # metrics in full: the matched set (a query/metric_names limits them); else every metric in
    # full mode (back-compat); else none (rely on metric_index).
    selected = matched if matched else (list(metrics) if mode == "full" else [])
    result["metrics"] = [
        _metric_full(metrics[n][0], metrics[n][1]) for n in selected if n in metrics
    ]
    return result


def tool_get_datasource_schema(args: dict[str, Any]) -> str:
    """Return the semantic model for a datasource, **sized to fit the client's context**.

    `mode="auto"` (default) picks verbosity by subject-area count (full <=12, summary <=50, index
    51+); a hard ~60K-char budget then downgrades one rung at a time (full→summary→index) even for
    an explicit `mode="full"`, setting `truncated=true`. `dataset_names=[...]` returns full
    `get_table_context` for the named tables (an explicit scope is respected — no downgrade).
    `query="<question>"` lexically ranks metrics so the client never ingests the whole catalog;
    `metric_index` (name->description for EVERY metric) + `large_tables` are always present.
    Plus ORGANIZATION.md / USER_MEMORY.md domain context.
    """
    profile = resolve_profile(args.get("datasource"))
    try:
        org = _load_org(profile)
    except FileNotFoundError as e:
        return json.dumps({"error": {"kind": "not_found", "remediation": str(e)}}, indent=2)
    except ImportError:
        return json.dumps(
            {
                "error": {
                    "kind": "driver_missing",
                    "remediation": "semantic model deps not installed. Run: pip install -r "
                    "plugins/agami/scripts/semantic_model/requirements.txt",
                }
            },
            indent=2,
        )

    from semantic_model import loader as L

    requested = args.get("dataset_names") or []
    requested_mode = (args.get("mode") or "auto").lower()
    metrics = _all_metrics(org)

    if requested:
        # Explicit table scope — full detail for the named tables, no budget downgrade.
        wanted = [str(n).split(".")[-1] for n in requested]
        result: dict[str, Any] = {
            "datasource": profile,
            "organization": org.description or None,
            "mode": "full",
            "requested_mode": requested_mode,
            "tables": _table_contexts(org, wanted, L),
            "metric_index": {n: (m.description or n) for n, (m, _a) in metrics.items()},
            "large_tables": _large_tables(org),
        }
    else:
        explicit = [n for n in (args.get("metric_names") or []) if n in metrics]
        matched = list(dict.fromkeys(explicit + _match_metrics(args.get("query"), metrics)))
        mode = (
            _auto_mode_for(len(org.subject_areas)) if requested_mode == "auto" else requested_mode
        )
        if mode not in _SCHEMA_MODE_DOWNGRADE:
            mode = "summary"
        truncated = False
        while True:
            result = _schema_payload(org, profile, mode, matched, metrics, L)
            if len(json.dumps(result, default=str)) <= _SCHEMA_CHAR_BUDGET:
                break
            nxt = _SCHEMA_MODE_DOWNGRADE[mode]
            if nxt is None:
                # At the floor (index) and STILL over budget — the inline `metrics` (full detail
                # for matched/all metrics) is the remaining bulk. Shed it; `metric_index` still
                # lists every metric by name, so nothing is hidden — the client requests specifics
                # via `metric_names`. Flag truncated so the overflow is never silent (C1/C3).
                truncated = True
                if result.get("metrics"):
                    result["metrics"] = []
                break
            mode, truncated = nxt, True
        result["requested_mode"] = requested_mode
        if truncated:
            result["truncated"] = True
            result["next_action"] = (
                "Response was downgraded to fit the context budget. Request "
                "specific tables via `dataset_names` or focus metrics with `query`."
            )

    parts = [json.dumps(result, indent=2, default=str)]
    # Domain context = the human's ORGANIZATION.md narrative + the model-DERIVED summary
    # (subject areas, conventions, decoded glossary) assembled fresh from the structured model.
    # Source (ORGANIZATION.md / USER_MEMORY.md text) comes from the DB under the DB backend, files
    # otherwise — so a DB-only deploy reads no files at runtime.
    from semantic_model import org_draft as _OD

    org_md_raw, user_md_raw = _domain_memory(profile)
    domain_context = _OD.compose_context(org_md_raw, org)
    if domain_context:
        parts.append(f"\n## Domain context\n{domain_context}")
    user_mem = _distill_for_llm(user_md_raw)
    if user_mem:
        parts.append(f"\n## USER_MEMORY.md (cross-database preferences)\n{user_mem}")
    return "\n".join(parts)


def tool_get_prompt_examples(args: dict[str, Any]) -> str:
    """Ask Agami `get_prompt_examples`: the few-shot library.

    DB serving (hosted, AGAMI_DB_URL set): scope to the datasource, rank by word-overlap on
    `query`, and cap to `top_k` within a char budget — so a large library (e.g. accumulated
    corrections) never floods the context. Local serving (files): returns the curated examples.yaml
    verbatim (small; the client reads YAML directly), `query`/`top_k` accepted for parity.
    """
    profile = resolve_profile(args.get("datasource"))

    from store import Store

    store = Store.from_env()
    if store is not None:
        from model_store import select_examples

        # honour an explicit top_k=0 (caller wants none); only default when absent/None
        top_k = args.get("top_k")
        top_k = 10 if top_k is None else int(top_k)
        try:
            examples = select_examples(
                store, profile, query=args.get("query"), area=args.get("area"), top_k=top_k
            )
        finally:
            store.close()
        return json.dumps(
            {"datasource": profile, "examples": examples, "count": len(examples)},
            indent=2,
            default=str,
        )

    artifacts = resolve_artifacts_dir()
    ex_dir = artifacts / profile / "prompt_examples"
    blocks: list[str] = []
    if ex_dir.is_dir():
        for ex_file in sorted(ex_dir.glob("*/examples.yaml")):
            text = _read_text(ex_file)
            if text and text.strip():
                area = ex_file.parent.name
                blocks.append(f"## subject area: {area}\n```yaml\n{text}\n```")
    if not blocks:
        return json.dumps(
            {
                "examples": [],
                "note": f"No examples under {ex_dir}/<area>/examples.yaml. "
                f"Corrections saved via agami-save-correction will appear here.",
            },
            indent=2,
        )
    header = (
        f"# Few-shot NL→SQL examples for datasource '{profile}'  (source: {ex_dir})\n"
        f"# Each block is one subject area's curated library. Match on the question, "
        f"then reuse the tagged tables/columns/SQL shape.\n"
    )
    return header + "\n" + "\n\n".join(blocks) + "\n"


def _classify_exit(code: int) -> str:
    return {
        2: "dsn",  # config / missing credentials / bad profile
        3: "driver_missing",
        4: "auth",  # connect / auth failed (also network)
        5: "syntax",  # SQL execution error
    }.get(code, "other")


def tool_execute_sql(args: dict[str, Any]) -> str:
    """Local analog of Ask Agami `execute_sql`: run a read-only SELECT locally.

    Routes through the sibling execute_sql.py (Tier-3 Python executor) so all
    DB types are handled identically and nothing but the rows leaves the
    process. Enforces the same read-only guarantee as the hosted connector.
    """
    sql = args.get("sql")
    if not isinstance(sql, str) or not sql.strip():
        return json.dumps(
            {"error": {"kind": "other", "remediation": "Pass a non-empty `sql` string."}}
        )

    reason = check_read_only(sql)
    if reason is not None:
        return json.dumps(
            {
                "error": {"kind": "permission", "remediation": reason},
                "sql": sql,
            },
            indent=2,
        )

    profile = resolve_profile(args.get("datasource"))
    max_rows = args.get("max_rows")
    try:
        max_rows = int(max_rows) if max_rows is not None else None
    except (TypeError, ValueError):
        max_rows = None
    if max_rows is not None:
        max_rows = max(1, min(max_rows, 10_000))

    # The model safety pass (fan/chasm pre-flight + default_filters) runs inside
    # execute_sql.py; pass the subject area so default_filters scope correctly.
    # Route through the unified executor as a module (the package is installed alongside
    # this harness), so the read-only safety pass + default_filters + logging run once.
    cmd = [sys.executable, "-m", "execute_sql", "--profile", profile, "--sql", sql]
    if args.get("area"):
        cmd += ["--area", str(args["area"])]

    started = time.monotonic()
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=240,
        )
    except subprocess.TimeoutExpired:
        return json.dumps(
            {"error": {"kind": "timeout", "remediation": "Query exceeded 240s."}, "sql": sql}
        )
    execution_ms = int((time.monotonic() - started) * 1000)

    if proc.returncode != 0:
        return json.dumps(
            {
                "error": {
                    "kind": _classify_exit(proc.returncode),
                    "remediation": (proc.stderr or "").strip() or "execute_sql.py failed",
                },
                "sql": sql,
                "execution_ms": execution_ms,
            },
            indent=2,
        )

    # Parse the RFC-4180 CSV emitted on stdout.
    reader = csv.reader(io.StringIO(proc.stdout))
    rows_all = list(reader)
    columns = rows_all[0] if rows_all else []
    data_rows = rows_all[1:] if len(rows_all) > 1 else []
    truncated = False
    if max_rows is not None and len(data_rows) > max_rows:
        data_rows = data_rows[:max_rows]
        truncated = True

    # Deterministic, exact rendering — so the numbers a user verifies don't depend on
    # how the host LLM chooses to format them. `markdown` is the table to display
    # verbatim; `rows` stays raw (exact CSV values) for charting / programmatic use.
    unit_map = _resolve_units(profile, sql)
    try:
        from semantic_model import units  # stdlib-only; safe even without model deps

        markdown = units.format_table(columns, data_rows, unit_map)
    except Exception:
        markdown = None

    result = {
        "columns": columns,
        "rows": data_rows,
        "row_count": len(data_rows),
        "truncated": truncated,
        "units": unit_map,
        "markdown": markdown,  # exact, full numbers (currency symbol + grouping) — render as-is
        "sql": sql,
        "execution_ms": execution_ms,
        # Trust receipt — provenance + anything unapproved this answer used. Same assembler
        # the agami-query skill renders, so Desktop gets the same trust panel. Clients should
        # surface receipt.warnings and any receipt.metrics whose review_state != "approved"
        # (offer to approve/correct via the save_correction tool).
        "receipt": _resolve_receipt(profile, sql),
    }

    # Log the execution through the single chokepoint: the DB sink when AGAMI_DB_URL is set (one
    # query_executions row), else the local jsonl the skills use. Best-effort either way.
    _record_query(
        {
            "ts": _now_iso(),
            "profile": profile,
            "question": args.get("raw_query"),
            "sql": sql,
            "row_count": len(data_rows),
            "source": "mcp_server",
        }
    )
    return json.dumps(result, indent=2, default=str)


def tool_log_feedback(args: dict[str, Any]) -> str:
    """Local analog of Ask Agami `log_feedback`: append to <artifacts_dir>/local/feedback.jsonl."""
    raw_query = args.get("raw_query")
    rating = args.get("rating")
    if not raw_query or not rating:
        return json.dumps(
            {"error": {"kind": "other", "remediation": "raw_query and rating are required."}}
        )
    norm = str(rating).strip().lower()
    good = {"good", "positive", "thumbs_up", "👍", "up", "yes"}
    bad = {"bad", "negative", "thumbs_down", "👎", "down", "no"}
    rating_value = "Good" if norm in good else "Bad" if norm in bad else str(rating)
    logged = _record_feedback(
        {
            "ts": _now_iso(),
            "profile": resolve_profile(args.get("datasource")),
            "question": raw_query,
            "rating": rating_value,
            "notes": args.get("notes"),
            "source": "mcp_server",
        }
    )
    if logged is None:
        return json.dumps(
            {"error": {"kind": "other", "remediation": f"Could not write {FEEDBACK_LOG}."}}
        )
    return json.dumps({"ok": True, "rating": rating_value, "logged_to": logged})


def _now_iso() -> str:
    # Avoid Date.now-style nondeterminism concerns: use UTC wall clock here is fine
    # (this is a long-running server process, not a replayed workflow).
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _append_jsonl(path: Path, record: dict[str, Any]) -> bool:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, default=str) + "\n")
        return True
    except OSError:
        return False


def _record_query(rec: dict[str, Any]) -> None:
    """Log a query execution through the DB sink (AGAMI_DB_URL) or the local jsonl. **Best-effort:**
    a logging failure must never break an otherwise-successful query — so the DB path swallows
    errors exactly like the jsonl path, and always closes its connection."""
    from store import Store

    store = Store.from_env()
    if store is None:
        _append_jsonl(QUERY_LOG, rec)
        return
    try:
        from contracts import QueryExecutionRecord
        from model_store import DbActivitySink

        DbActivitySink(store).record_query_execution(QueryExecutionRecord(**rec))
    except Exception:
        pass  # best-effort: never fail the query because logging failed
    finally:
        store.close()


def _record_feedback(rec: dict[str, Any]) -> str | None:
    """Record feedback through the DB sink (AGAMI_DB_URL) or the local jsonl. Returns where it
    landed ('database' or the file path), or None if the write failed (feedback is the user's
    explicit action, so a failure surfaces — unlike incidental query logging). Closes the store."""
    from store import Store

    store = Store.from_env()
    if store is None:
        return str(FEEDBACK_LOG) if _append_jsonl(FEEDBACK_LOG, rec) else None
    try:
        from contracts import FeedbackRecord
        from model_store import DbActivitySink

        DbActivitySink(store).record_feedback(FeedbackRecord(**rec))
        return "database"
    except Exception:
        return None
    finally:
        store.close()


def record_tool_call(
    *,
    name: str,
    arguments: dict[str, Any] | None,
    result_text: str | None,
    execution_ms: int | None,
    actor: str | None,
    raised: bool = False,
) -> None:
    """Record one MCP tool call to the activity log (the transport calls this for **every** tool). The
    audit-grade fields are server-observed; `success`/`row_count`/`error_kind` are derived from the
    result (execute_sql returns an `{"error": ...}` body on a bad query without raising). The self-report
    fields (`user_question`/`agent_query`/`thread_id`) are whatever Claude supplied — may be None.
    **Best-effort and never raises** — a logging failure must not break the tool."""
    args = arguments or {}
    success, row_count, error_kind = True, None, None
    if raised:
        success, error_kind = False, "exception"
    else:
        try:
            parsed = json.loads(result_text) if result_text else None
            if isinstance(parsed, dict):
                if isinstance(parsed.get("error"), dict):
                    success = False
                    error_kind = parsed["error"].get("kind") or "error"
                row_count = parsed.get("row_count")
        except (ValueError, TypeError):
            pass
    _record_tool_call(
        {
            "ts": _now_iso(),
            "tool_name": name,
            "source": "mcp_server",
            "actor": actor,
            "datasource": args.get("datasource"),
            "sql": args.get("sql"),
            "row_count": row_count if isinstance(row_count, int) else None,
            "execution_ms": execution_ms,
            "success": success,
            "error_kind": error_kind,
            "user_question": args.get("user_question"),
            "agent_query": args.get("raw_query"),  # the existing arg is the agent's framing of the query
            "thread_id": args.get("thread_id"),
        }
    )


def _record_tool_call(rec: dict[str, Any]) -> None:
    """Write a tool-call record through the DB sink (AGAMI_DB_URL) or the local jsonl. Wrapped so the
    whole thing is best-effort — even opening the store can't surface an error to the caller."""
    try:
        from store import Store

        store = Store.from_env()
        if store is None:
            _append_jsonl(TOOL_CALL_LOG, rec)
            return
        try:
            from contracts import ToolCallRecord
            from model_store import DbActivitySink

            DbActivitySink(store).record_tool_call(ToolCallRecord(**rec))
        finally:
            store.close()
    except Exception:
        pass  # best-effort: never fail the tool because logging failed


# ---------------------------------------------------------------------------
# Tool registry (name → (handler, description, inputSchema))
# ---------------------------------------------------------------------------

TOOLS: dict[str, dict[str, Any]] = {
    "list_datasources": {
        "handler": tool_list_datasources,
        "description": (
            "List the local agami datasources (credential profiles) and whether each has a "
            "semantic model. Local analog of the hosted list_organizations. Call this first "
            "when the datasource is not yet known; the others accept an optional `datasource`."
        ),
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    "get_datasource_schema": {
        "handler": tool_get_datasource_schema,
        "description": (
            "Fetch the local semantic model for a datasource, sized to fit context. `mode=auto` "
            "(default) picks verbosity by subject-area count (full/summary/index) under a char "
            "budget; `dataset_names=[...]` returns full get_table_context (columns scoped by "
            "expose_column_groups, default_filters, relationships, caveats, value_transforms, "
            "metrics) for the named tables; `query` ranks metrics so you don't ingest the whole "
            "catalog (`metric_index` lists every metric regardless). Plus ORGANIZATION.md / "
            "USER_MEMORY.md context. Use metric `calculation`/`bindings` VERBATIM."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "datasource": {
                    "type": "string",
                    "description": "Profile name; defaults to the active profile.",
                },
                "mode": {
                    "type": "string",
                    "enum": ["auto", "full", "summary", "index"],
                    "description": "Verbosity; default auto (sized by subject-area count + char budget).",
                },
                "dataset_names": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Tables to pull full field-level detail for (an explicit scope, no downgrade).",
                },
                "query": {
                    "type": "string",
                    "description": "The user's NL question — lexically ranks metrics.",
                },
                "metric_names": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Return full detail for these named metrics.",
                },
            },
            "additionalProperties": False,
        },
    },
    "get_prompt_examples": {
        "handler": tool_get_prompt_examples,
        "description": (
            "Fetch the curated few-shot NL→SQL examples for a datasource (one block per subject "
            "area, from prompt_examples/<area>/examples.yaml). Use before generating SQL to ground "
            "dialect and house style; match on the question, then reuse the tagged tables/columns/SQL."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "datasource": {
                    "type": "string",
                    "description": "Profile name; defaults to the active profile.",
                },
                "query": {
                    "type": "string",
                    "description": "The user's NL question (context only).",
                },
                "top_k": {
                    "type": "integer",
                    "description": "Accepted for hosted-parity; not applied locally.",
                },
            },
            "additionalProperties": False,
        },
    },
    "execute_sql": {
        "handler": tool_execute_sql,
        "description": (
            "Execute a single read-only SELECT / WITH...SELECT against the local datasource and "
            "return {columns, rows, row_count, truncated, sql, execution_ms}. SELECT-only is "
            "enforced (DML/DDL/multi-statement rejected with kind='permission'). Runs entirely "
            "locally via execute_sql.py — no data leaves the machine."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "sql": {"type": "string", "description": "One SELECT or WITH...SELECT statement."},
                "datasource": {
                    "type": "string",
                    "description": "Profile name; defaults to the active profile.",
                },
                "area": {
                    "type": "string",
                    "description": "Subject area — scopes the fan/chasm pre-flight + default_filters safety pass.",
                },
                "raw_query": {
                    "type": "string",
                    "description": "Your (the agent's) framing of this query — recorded for the admin activity log.",
                },
                "user_question": {
                    "type": "string",
                    "description": "The user's ORIGINAL question, verbatim, that led to this query — "
                    "recorded so an admin can see what was asked. Pass it whenever you have it.",
                },
                "thread_id": {
                    "type": "string",
                    "description": "A short id you generate ONCE per conversation and reuse on every "
                    "tool call in it — lets the admin group a conversation's queries into one session.",
                },
                "max_rows": {"type": "integer", "description": "Row cap (clamped 1–10000)."},
            },
            "required": ["sql"],
            "additionalProperties": False,
        },
    },
    "log_feedback": {
        "handler": tool_log_feedback,
        "description": (
            "Record thumbs-up/down feedback for a question to the local feedback.jsonl. "
            "Local analog of the hosted log_feedback."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "raw_query": {"type": "string", "description": "The NL question the user asked."},
                "rating": {
                    "type": "string",
                    "description": "good/bad (also accepts positive/negative/👍/👎).",
                },
                "notes": {"type": "string", "description": "Optional free-text comment."},
                "datasource": {
                    "type": "string",
                    "description": "Profile name; defaults to the active profile.",
                },
            },
            "required": ["raw_query", "rating"],
            "additionalProperties": False,
        },
    },
}
