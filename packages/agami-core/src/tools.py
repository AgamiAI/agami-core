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
    "non-sensitive id. (execute_sql enforces this and errors on a raw sensitive projection.)"
)


def bootstrap_paths() -> None:
    """Run the one-shot ~/.agami migration, then re-resolve the module-level paths (the migration
    may point the artifacts dir at a custom location). Every entrypoint calls this at startup."""
    global AGAMI_LOCAL, CREDENTIALS_PATH, CONFIG_PATH, QUERY_LOG, FEEDBACK_LOG
    agami_paths.bootstrap()
    AGAMI_LOCAL = agami_paths.local_dir()
    CREDENTIALS_PATH = agami_paths.credentials_path()
    CONFIG_PATH = agami_paths.config_path()
    QUERY_LOG = agami_paths.query_log_path()
    FEEDBACK_LOG = AGAMI_LOCAL / "feedback.jsonl"


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
        out[section] = {k: (v.strip() if isinstance(v, str) else v) for k, v in cfg[section].items()}
    return out


def _db_type_for(profile: str, creds: dict[str, dict[str, str]]) -> str:
    sect = creds.get(profile, {})
    t = sect.get("type", "")
    if not t and sect.get("url"):
        scheme = sect["url"].split("://", 1)[0].split("+", 1)[0].lower()
        t = {"postgresql": "postgres", "postgres": "postgres", "mysql": "mysql",
              "mariadb": "mysql", "redshift": "redshift", "snowflake": "snowflake",
              "bigquery": "bigquery", "bq": "bigquery", "sqlite": "sqlite"}.get(scheme, scheme)
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
    """Lazily load the semantic model for a profile. Raises a clear error if the
    model package (pydantic) isn't importable or there's no model on disk.

    The schema/traversal tools need the model; the execute_sql + log_feedback tools
    stay pure-stdlib so the server runs for execution even without the model deps.
    """
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
    """The model-version pin = the newest snapshot dir name (a content hash), same as
    the skill reads. None if there's no .snapshots/ (legacy model) or model deps are
    unavailable (execute_sql stays usable)."""
    try:
        from semantic_model import snapshot as SN
        return SN.newest_version(resolve_artifacts_dir() / profile)
    except Exception:
        return None


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
            table_count = sum(1 for _ in (pdir / "subject_areas").glob("*/tables/*.yaml")) \
                if (pdir / "subject_areas").is_dir() else 0
        out.append({
            "datasource": profile,
            "database_type": _db_type_for(profile, creds),
            "table_count": table_count,
            "model_present": (pdir / "org.yaml").exists(),
            "is_active": profile == active,
        })
    if not out:
        return json.dumps({
            "datasources": [],
            "note": "No profiles found in your credentials file. Run the agami-connect skill first.",
        }, indent=2)
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


def tool_get_datasource_schema(args: dict[str, Any]) -> str:
    """Local analog of Ask Agami `get_datasource_schema`, backed by the semantic model.

    Pass 1 (no `dataset_names`): the subject-area index — each area's name +
    description + table list (compact). Pass 2 (`dataset_names: [...]`): the full
    `get_table_context` for those tables (columns scoped by the area's
    expose_column_groups, default_filters, relationships, caveats, value_transforms).
    Plus ORGANIZATION.md / USER_MEMORY.md domain context.
    """
    profile = resolve_profile(args.get("datasource"))
    artifacts = resolve_artifacts_dir()
    try:
        org = _load_org(profile)
    except FileNotFoundError as e:
        return json.dumps({"error": {"kind": "not_found", "remediation": str(e)}}, indent=2)
    except ImportError:
        return json.dumps({"error": {"kind": "driver_missing", "remediation":
            "semantic model deps not installed. Run: pip install -r "
            "plugins/agami/scripts/semantic_model/requirements.txt"}}, indent=2)

    from semantic_model import loader as L

    requested = args.get("dataset_names") or []
    result: dict[str, Any] = {"datasource": profile, "organization": org.description or None}

    if not requested:
        result["subject_areas"] = [{
            "name": sa.name,
            "description": sa.description,
            "default_time_window": sa.default_time_window,
            "tables": [tr.table for tr in sa.tables],
        } for sa in org.subject_areas]
        result["cross_area_relationships"] = [
            {"from": r.from_subject_area, "to": r.to_subject_area,
             "for_questions_about": r.for_questions_about}
            for r in org.cross_subject_area_relationships
        ]
        result["note"] = ("Per-table detail is lazy-loaded. Call again with "
                          "`dataset_names: [...]` for full columns + relationships.")
    else:
        wanted = [str(n).split(".")[-1] for n in requested]
        # find the area each table belongs to (for expose_column_groups scoping)
        area_of = {t.name: sa.name for sa in org.subject_areas for t in sa.tables_defined}
        by_area: dict[str, list[str]] = {}
        for t in wanted:
            by_area.setdefault(area_of.get(t), []).append(t)
        contexts = {}
        for area, tbls in by_area.items():
            ctx = L.get_table_context(org, tbls, area=area,
                                      include=["default_filters", "relationships",
                                               "caveats", "value_transforms", "metrics"])
            contexts.update(ctx.get("tables", {}))
            if ctx.get("relationships"):
                result.setdefault("relationships", []).extend(ctx["relationships"])
            if ctx.get("metrics"):
                result.setdefault("metrics", []).extend(ctx["metrics"])
        result["tables"] = contexts

    parts = [json.dumps(result, indent=2, default=str)]
    # Domain context = the human's ORGANIZATION.md narrative + the model-DERIVED summary
    # (subject areas, conventions, decoded glossary) assembled fresh from the structured
    # model. The glossary thus always reaches the LLM — it no longer depends on a file
    # having been re-rendered, and the human's prose is never mixed with auto content.
    from semantic_model import org_draft as _OD
    org_md_raw = _read_text(artifacts / profile / "ORGANIZATION.md") or ""
    domain_context = _OD.compose_context(org_md_raw, org)
    if domain_context:
        parts.append(f"\n## Domain context\n{domain_context}")
    user_mem = _distill_for_llm(_read_text(artifacts / "USER_MEMORY.md"))
    if user_mem:
        parts.append(f"\n## USER_MEMORY.md (cross-database preferences)\n{user_mem}")
    return "\n".join(parts)


def tool_get_prompt_examples(args: dict[str, Any]) -> str:
    """Local analog of Ask Agami `get_prompt_examples`: the few-shot library.

    Returns the curated examples.yaml verbatim as text. Local serving has no
    embedding store, so `query`/`top_k` are accepted for interface-parity with
    the hosted connector but do not rank or cap — the full curated library is
    returned (it is small; the client reads YAML directly).
    """
    profile = resolve_profile(args.get("datasource"))
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
        return json.dumps({
            "examples": [],
            "note": f"No examples under {ex_dir}/<area>/examples.yaml. "
                    f"Corrections saved via agami-save-correction will appear here.",
        }, indent=2)
    header = (
        f"# Few-shot NL→SQL examples for datasource '{profile}'  (source: {ex_dir})\n"
        f"# Each block is one subject area's curated library. Match on the question, "
        f"then reuse the tagged tables/columns/SQL shape.\n"
    )
    return header + "\n" + "\n\n".join(blocks) + "\n"


def _classify_exit(code: int) -> str:
    return {
        2: "dsn",            # config / missing credentials / bad profile
        3: "driver_missing",
        4: "auth",           # connect / auth failed (also network)
        5: "syntax",         # SQL execution error
    }.get(code, "other")


def tool_execute_sql(args: dict[str, Any]) -> str:
    """Local analog of Ask Agami `execute_sql`: run a read-only SELECT locally.

    Routes through the sibling execute_sql.py (Tier-3 Python executor) so all
    DB types are handled identically and nothing but the rows leaves the
    process. Enforces the same read-only guarantee as the hosted connector.
    """
    sql = args.get("sql")
    if not isinstance(sql, str) or not sql.strip():
        return json.dumps({"error": {"kind": "other", "remediation": "Pass a non-empty `sql` string."}})

    reason = check_read_only(sql)
    if reason is not None:
        return json.dumps({
            "error": {"kind": "permission", "remediation": reason},
            "sql": sql,
        }, indent=2)

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
        return json.dumps({"error": {"kind": "timeout", "remediation": "Query exceeded 240s."}, "sql": sql})
    execution_ms = int((time.monotonic() - started) * 1000)

    if proc.returncode != 0:
        return json.dumps({
            "error": {
                "kind": _classify_exit(proc.returncode),
                "remediation": (proc.stderr or "").strip() or "execute_sql.py failed",
            },
            "sql": sql,
            "execution_ms": execution_ms,
        }, indent=2)

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

    # Append to the personal query log (same file the skills use), best-effort.
    _append_jsonl(QUERY_LOG, {
        "ts": _now_iso(),
        "profile": profile,
        "question": args.get("raw_query"),
        "sql": sql,
        "row_count": len(data_rows),
        "source": "mcp_server",
    })
    return json.dumps(result, indent=2, default=str)


def tool_log_feedback(args: dict[str, Any]) -> str:
    """Local analog of Ask Agami `log_feedback`: append to <artifacts_dir>/local/feedback.jsonl."""
    raw_query = args.get("raw_query")
    rating = args.get("rating")
    if not raw_query or not rating:
        return json.dumps({"error": {"kind": "other", "remediation": "raw_query and rating are required."}})
    norm = str(rating).strip().lower()
    good = {"good", "positive", "thumbs_up", "👍", "up", "yes"}
    bad = {"bad", "negative", "thumbs_down", "👎", "down", "no"}
    rating_value = "Good" if norm in good else "Bad" if norm in bad else str(rating)
    ok = _append_jsonl(FEEDBACK_LOG, {
        "ts": _now_iso(),
        "profile": resolve_profile(args.get("datasource")),
        "question": raw_query,
        "rating": rating_value,
        "notes": args.get("notes"),
        "source": "mcp_server",
    })
    if not ok:
        return json.dumps({"error": {"kind": "other", "remediation": f"Could not write {FEEDBACK_LOG}."}})
    return json.dumps({"ok": True, "rating": rating_value, "logged_to": str(FEEDBACK_LOG)})


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
            "Fetch the local semantic model for a datasource: the subject-area index (Pass 1 — "
            "each area's name + description + table list), plus full get_table_context (columns "
            "scoped by expose_column_groups, default_filters, relationships, caveats, "
            "value_transforms, metrics) for any `dataset_names` you pass (Pass 2 lazy-load), plus "
            "ORGANIZATION.md / USER_MEMORY.md context. Use metric `calculation`/`bindings` VERBATIM."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "datasource": {"type": "string", "description": "Profile name; defaults to the active profile."},
                "dataset_names": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Tables to pull full field-level detail for (Pass 2).",
                },
                "query": {"type": "string", "description": "The user's NL question (context only; not used for ranking locally)."},
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
                "datasource": {"type": "string", "description": "Profile name; defaults to the active profile."},
                "query": {"type": "string", "description": "The user's NL question (context only)."},
                "top_k": {"type": "integer", "description": "Accepted for hosted-parity; not applied locally."},
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
                "datasource": {"type": "string", "description": "Profile name; defaults to the active profile."},
                "area": {"type": "string", "description": "Subject area — scopes the fan/chasm pre-flight + default_filters safety pass."},
                "raw_query": {"type": "string", "description": "The user's NL question (recorded in the query log)."},
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
                "rating": {"type": "string", "description": "good/bad (also accepts positive/negative/👍/👎)."},
                "notes": {"type": "string", "description": "Optional free-text comment."},
                "datasource": {"type": "string", "description": "Profile name; defaults to the active profile."},
            },
            "required": ["raw_query", "rating"],
            "additionalProperties": False,
        },
    },
}


