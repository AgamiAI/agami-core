#!/usr/bin/env python3
"""
agami serve — a local, single-player MCP server (stdio transport).

Exposes the *local* semantic model + local SQL execution over the Model
Context Protocol so a developer can use agami from any MCP-capable client
that launches a local stdio server as a child process — chiefly **Claude
Code** (`claude mcp add`) and **Claude Desktop** (`claude_desktop_config.json`).

This is the local mirror of the hosted "Ask Agami" connector: the same core tool
surface (list_datasources / get_datasource_schema / get_prompt_examples /
execute_sql / log_feedback) plus the semantic-model traversal tools
(list_subject_areas / get_subject_area_bundle / get_table_context /
identify_entity / pre_flight_check), backed by the user's local .agami files and
local DB execution instead of a cloud registry. Going from local → team is a
backend swap, not a new product.

Design constraints (match the rest of agami):
  - **No network call, no auth, no telemetry.** The MCP stdio protocol is
    newline-delimited JSON-RPC 2.0, spoken by hand. Grep the source.
  - The execute_sql + log_feedback tools are pure-stdlib. The model-backed tools
    (schema / traversal) import `scripts/semantic_model` (Pydantic) lazily and
    surface a clear "install the model deps" error if it's absent — so execution
    still works on a bare install.
  - **No data leaves the machine.** SQL is executed locally by shelling out to
    the sibling `execute_sql.py` (the same Tier-3 executor the skills use), which
    runs the fan/chasm pre-flight + default_filters safety pass; the semantic
    model is read from `<artifacts_dir>/<profile>/` (org.yaml + subject_areas/…).
  - **Security model = the OS user boundary.** A stdio server is a child process
    of the client, running as you, reading the creds you already have. There is
    deliberately NO authentication (the MCP spec defines auth for the HTTP
    transport only). NEVER make this bind a network port — that would create an
    unauthenticated listener. If you need networked/multi-user serving with
    auth + RBAC + audit, that is the hosted product, by design.

Execution path: this server routes SQL through `execute_sql.py` (the Python
driver tier), so the relevant driver must be importable for non-SQLite DBs
(`psycopg2-binary` / `pymysql` / `snowflake-connector-python` /
`google-cloud-bigquery`). SQLite needs nothing (stdlib).

Wire it up:
    # Claude Code
    claude mcp add agami -- python3 /ABS/PATH/plugins/agami/scripts/mcp_server.py

    # Claude Desktop — claude_desktop_config.json
    {
      "mcpServers": {
        "agami": {
          "command": "python3",
          "args": ["/ABS/PATH/plugins/agami/scripts/mcp_server.py"],
          "env": { "AGAMI_PROFILE": "main" }
        }
      }
    }
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
from typing import Any, Callable

# ---------------------------------------------------------------------------
# Paths & config resolution (mirrors execute_sql.py / file-layout.md exactly)
# ---------------------------------------------------------------------------

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
import agami_paths  # noqa: E402  (copied alongside this server into local/serve/ by setup_desktop_mcp)

EXECUTE_SQL = SCRIPT_DIR / "execute_sql.py"
# Secrets + per-user state live under <artifacts_dir>/local/ (the consolidated,
# gitignored replacement for ~/.agami). Re-resolved after bootstrap() in main().
AGAMI_LOCAL = agami_paths.local_dir()
CREDENTIALS_PATH = agami_paths.credentials_path()
CONFIG_PATH = agami_paths.config_path()
QUERY_LOG = agami_paths.query_log_path()
FEEDBACK_LOG = AGAMI_LOCAL / "feedback.jsonl"

SERVER_NAME = "agami"
# The protocol version we fall back to if the client doesn't pin one. We echo
# the client's requested version when present (see _handle_initialize).
DEFAULT_PROTOCOL_VERSION = "2024-11-05"


def _server_version() -> str:
    """Best-effort plugin version.

    Prefer the AGAMI_VERSION env var (set by setup_desktop_mcp.py when the server
    is copied to a standalone <artifacts_dir>/local/serve dir, where the marketplace.json
    isn't reachable), then fall back to reading the shipped manifest.
    """
    env_v = os.environ.get("AGAMI_VERSION")
    if env_v:
        return env_v
    for rel in ("../../.claude-plugin/marketplace.json", "../.claude-plugin/plugin.json"):
        p = (SCRIPT_DIR / rel).resolve()
        try:
            text = p.read_text()
        except OSError:
            continue
        m = re.search(r'"version"\s*:\s*"([^"]+)"', text)
        if m:
            return m.group(1)
    return "0.0.0"


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
    if str(SCRIPT_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPT_DIR))
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
    the skill reads. None if there's no .snapshots/ (legacy model)."""
    try:
        snaps = resolve_artifacts_dir() / profile / ".snapshots"
        dirs = sorted((p for p in snaps.iterdir() if p.is_dir()),
                      key=lambda p: p.stat().st_mtime, reverse=True)
        return dirs[0].name if dirs else None
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
    cmd = [sys.executable, str(EXECUTE_SQL), "--profile", profile, "--sql", sql]
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


def _op_summary(op: dict[str, Any]) -> str:
    """One-line human preview of a curate op, for the confirmation gate."""
    parts: list[str] = [str(op.get("op", "?")), str(op.get("kind", ""))]
    if op.get("area"):
        parts.append(f"in {op['area']}")
    name = op.get("name")
    if not name and op.get("items"):
        name = "+".join(str(i.get("name", "?")) for i in op["items"])
    if name:
        parts.append(str(name))
    if op.get("column"):
        parts.append(f"· column {op['column']}")
    if op.get("field"):
        parts.append(f"· set {op['field']}")
    return " ".join(p for p in parts if p)


def tool_save_correction(args: dict[str, Any]) -> str:
    """Local analog of the agami-save-correction skill: apply a correction through the
    SAME validated, git-committed engine (`curate`), so a fix made in Claude Desktop lands
    exactly like one made in Claude Code. The CLIENT does the routing (which note becomes a
    caveat / unit / new metric / relationship edit — same decision tree as the skill); this
    tool APPLIES the resulting ops + saves the corrected example.

    **Shared-model edits are gated — ENFORCED, not advisory.** Any `ops` (which mutate the
    shared model) apply ONLY when `confirmed: true`. Without it the tool does NOT write — it
    returns `requires_confirmation` + a preview, so the client must show the user the exact
    change and get an explicit OK, then re-call with `confirmed: true`. Saving a corrected
    `example` is the safe floor (only shapes this question's few-shot) and is NOT gated.

    args:
      datasource: profile (optional; defaults to active)
      ops:        curate ops, each {op, kind, area, name, [column], [field, value]} for
                  approve/reject/edit/exclude/include, or {op:"add", kind, area, items:[...]}
                  to add a metric/entity.
      confirmed:  bool — REQUIRED to apply `ops` (the user's explicit OK to edit the model).
      example:    optional corrected NL->SQL to save as a prompt example —
                  {area, question, sql, [tables], [columns], [metric], [source], [status]}.
      signer/role: stamped on approvals (a user approving here IS their sign-off).
    """
    profile = resolve_profile(args.get("datasource"))
    root = resolve_artifacts_dir() / profile
    if not (root / "org.yaml").exists():
        return json.dumps({"error": {"kind": "other",
                           "remediation": f"No model at {root}. Run the agami-connect skill first."}})
    try:
        from semantic_model import curate
    except Exception as e:
        return json.dumps({"error": {"kind": "other", "remediation": f"Model deps unavailable: {e}"}})

    signer, role = args.get("signer"), args.get("role")
    confirmed = bool(args.get("confirmed"))
    out: dict[str, Any] = {"profile": profile}

    ops = args.get("ops") or []
    if ops and not confirmed:
        # Enforced gate: never mutate the shared model without an explicit confirm.
        preview = [_op_summary(o) for o in ops]
        out["requires_confirmation"] = True
        out["pending_ops"] = preview
        out["message"] = (
            "These edits change your shared semantic model and were NOT applied: "
            + "; ".join(preview) + ". Show the user exactly what will change, get their OK, then "
            "call save_correction again with the same `ops` and `confirmed: true`. "
            "(A corrected `example` saves without confirmation — it only affects this question.)")
    elif ops:
        results = []
        # {op:"add"} routes to write_items (add a metric/entity); the rest through apply().
        for ao in [o for o in ops if o.get("op") == "add"]:
            results.append(curate.write_items(root, ao.get("area"), ao.get("kind"),
                                              ao.get("items") or [], signer=signer, role=role).as_dict())
        plain = [o for o in ops if o.get("op") != "add"]
        if plain:
            results.append(curate.apply(root, plain, signer=signer, role=role).as_dict())
        out["ops"] = results

    ex = args.get("example")
    if isinstance(ex, dict) and ex.get("area") and ex.get("question") and ex.get("sql"):
        area = {k: v for k, v in ex.items() if k != "area"}
        out["example"] = curate.add_examples(root, ex["area"], [area],
                                              signer=signer, role=role).as_dict()

    if not any(k in out for k in ("ops", "example", "requires_confirmation")):
        return json.dumps({"error": {"kind": "other",
                           "remediation": "Pass `ops` (curate ops) and/or a complete `example` "
                                          "(area+question+sql)."}})
    return json.dumps(out, indent=2, default=str)


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
# Semantic-model traversal tools (examples-first loop). These need the model
# package (pydantic); they surface a clear error if it isn't installed, while the
# execute_sql / log_feedback tools stay pure-stdlib.
# ---------------------------------------------------------------------------


def _model_error_json(exc: Exception) -> str:
    if isinstance(exc, FileNotFoundError):
        return json.dumps({"error": {"kind": "not_found", "remediation": str(exc)}}, indent=2)
    if isinstance(exc, ImportError):
        return json.dumps({"error": {"kind": "driver_missing", "remediation":
            "semantic model deps not installed. Run: pip install -r "
            "plugins/agami/scripts/semantic_model/requirements.txt"}}, indent=2)
    return json.dumps({"error": {"kind": "other", "remediation": str(exc)}}, indent=2)


def tool_list_subject_areas(args: dict[str, Any]) -> str:
    try:
        org = _load_org(resolve_profile(args.get("datasource")))
        from semantic_model import runtime as RT
        return json.dumps(RT.list_subject_areas(org), indent=2)
    except Exception as e:
        return _model_error_json(e)


def tool_get_subject_area_bundle(args: dict[str, Any]) -> str:
    try:
        org = _load_org(resolve_profile(args.get("datasource")))
        from semantic_model import loader as L
        return json.dumps(L.get_subject_area_bundle(org, args["area"]), indent=2, default=str)
    except Exception as e:
        return _model_error_json(e)


def tool_get_table_context(args: dict[str, Any]) -> str:
    try:
        org = _load_org(resolve_profile(args.get("datasource")))
        from semantic_model import loader as L
        return json.dumps(L.get_table_context(
            org, args["tables"], area=args.get("area"),
            columns=args.get("columns"), include=args.get("include"),
        ), indent=2, default=str)
    except Exception as e:
        return _model_error_json(e)


def tool_identify_entity(args: dict[str, Any]) -> str:
    try:
        org = _load_org(resolve_profile(args.get("datasource")))
        from semantic_model import runtime as RT
        res = RT.identify_entity(args["literal"], org, area=args.get("area"),
                                 query_context=args.get("query_context", ""))
        return json.dumps({"status": res.status, "candidates": res.candidates,
                           "question_template": res.question_template}, indent=2, default=str)
    except Exception as e:
        return _model_error_json(e)


def tool_pre_flight_check(args: dict[str, Any]) -> str:
    try:
        org = _load_org(resolve_profile(args.get("datasource")))
        from semantic_model import runtime as RT
        return json.dumps(RT.pre_flight_check(args["sql"], org).as_dict(), indent=2, default=str)
    except Exception as e:
        return _model_error_json(e)


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
    "save_correction": {
        "handler": tool_save_correction,
        "description": (
            "Apply a correction to the local semantic model AND/OR save a corrected NL→SQL "
            "example, through the same validated, git-committed engine the agami-save-correction "
            "skill uses — so a fix made in Claude Desktop lands identically to one in Claude Code. "
            "Use after execute_sql when the answer was wrong or used something unapproved (see "
            "receipt.metrics with review_state != 'approved', or receipt.warnings). YOU decide the "
            "routing (a column's unit/meaning → an edit op; a reusable aggregation → add a metric; "
            "an approval → an approve op); this tool applies it. Approving here IS the user's sign-off."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "datasource": {"type": "string", "description": "Profile name; defaults to the active profile."},
                "ops": {
                    "type": "array",
                    "description": "Curate ops. Each: {op, kind, area, name, [column], [field], [value]} "
                                   "for approve/reject/edit/exclude/include; or {op:'add', kind:'metric'|'entity', "
                                   "area, items:[...]} to add one. Applied ONLY with confirmed:true.",
                    "items": {"type": "object"},
                },
                "confirmed": {
                    "type": "boolean",
                    "description": "Set true ONLY after showing the user the exact model change and "
                                   "getting their OK. Required to apply `ops`; ignored for `example`.",
                },
                "example": {
                    "type": "object",
                    "description": "Optional corrected NL→SQL to save as a prompt example: "
                                   "{area, question, sql, [tables], [columns], [metric], [source], [status]}.",
                },
                "signer": {"type": "string", "description": "Email stamped on approvals (the user's sign-off)."},
                "role": {"type": "string", "description": "Signer role (e.g. cfo, data_lead)."},
            },
            "additionalProperties": False,
        },
    },
    "list_subject_areas": {
        "handler": tool_list_subject_areas,
        "description": (
            "List the subject areas (the primary semantic unit) for a datasource. "
            "Examples-first traversal step 1: pick the area whose description matches the "
            "question's intent, then fetch its tables."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"datasource": {"type": "string", "description": "Profile; defaults to active."}},
            "additionalProperties": False,
        },
    },
    "get_subject_area_bundle": {
        "handler": tool_get_subject_area_bundle,
        "description": (
            "One-shot bundle for a small subject area: tables, columns (scoped by the area's "
            "expose_column_groups), default_filters, relationships, entities, metrics. Use for "
            "small areas to avoid multiple round-trips."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "datasource": {"type": "string", "description": "Profile; defaults to active."},
                "area": {"type": "string", "description": "Subject area name."},
            },
            "required": ["area"],
            "additionalProperties": False,
        },
    },
    "get_table_context": {
        "handler": tool_get_table_context,
        "description": (
            "Compound context fetch — columns (+ default_filters, relationships, caveats, "
            "value_transforms, metrics) for a set of tables in one round-trip. Honors the area's "
            "expose_column_groups so wide tables disclose only the scoped columns. Use metric "
            "`calculation`/`bindings` VERBATIM and apply any column `value_transform` when generating SQL."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "datasource": {"type": "string", "description": "Profile; defaults to active."},
                "area": {"type": "string", "description": "Subject area (scopes column-group visibility)."},
                "tables": {"type": "array", "items": {"type": "string"}, "description": "Tables to fetch."},
                "columns": {"type": "array", "items": {"type": "string"}, "description": "Optional column subset."},
                "include": {"type": "array", "items": {"type": "string"}, "description": "Optional include list."},
            },
            "required": ["tables"],
            "additionalProperties": False,
        },
    },
    "identify_entity": {
        "handler": tool_identify_entity,
        "description": (
            "Identify what kind of entity an opaque literal is, via the entity value_pattern regexes. "
            "Returns resolved / clarify (ranked candidates + question) / unrecognized."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "datasource": {"type": "string", "description": "Profile; defaults to active."},
                "area": {"type": "string", "description": "Optional subject area to scope to."},
                "literal": {"type": "string", "description": "The opaque value to identify."},
                "query_context": {"type": "string", "description": "Surrounding query text (disambiguation)."},
            },
            "required": ["literal"],
            "additionalProperties": False,
        },
    },
    "pre_flight_check": {
        "handler": tool_pre_flight_check,
        "description": (
            "Fan-trap / chasm-trap pre-flight on a proposed SELECT using join cardinality. Returns "
            "{risk, action: auto_rewrite|refuse|allow, rewritten_sql, reason, suggestion}. execute_sql "
            "runs this automatically; call it directly only to check SQL before committing to it."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "datasource": {"type": "string", "description": "Profile; defaults to active."},
                "sql": {"type": "string", "description": "The SELECT to check."},
            },
            "required": ["sql"],
            "additionalProperties": False,
        },
    },
}


# ---------------------------------------------------------------------------
# JSON-RPC 2.0 / MCP stdio transport (newline-delimited messages)
# ---------------------------------------------------------------------------


def _log(msg: str) -> None:
    """Diagnostics go to stderr — stdout is reserved for protocol messages."""
    sys.stderr.write(f"[agami-mcp] {msg}\n")
    sys.stderr.flush()


def _send(message: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()


def _result(req_id: Any, result: Any) -> None:
    _send({"jsonrpc": "2.0", "id": req_id, "result": result})


def _error(req_id: Any, code: int, message: str) -> None:
    _send({"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}})


def _handle_initialize(req_id: Any, params: dict[str, Any]) -> None:
    requested = params.get("protocolVersion")
    protocol = requested if isinstance(requested, str) and requested else DEFAULT_PROTOCOL_VERSION
    _result(req_id, {
        "protocolVersion": protocol,
        "capabilities": {"tools": {"listChanged": False}},
        "serverInfo": {"name": SERVER_NAME, "version": _server_version()},
        "instructions": (
            "agami local datasource agent. The NL→SQL intelligence runs on your side; these "
            "tools provide the local semantic model + curated examples and execute SQL locally. "
            "All execution is local; nothing leaves the machine.\n"
            "Flow: (1) list_datasources, then fetch the model + examples for the datasource the "
            "question touches. (2) Examples-first — call get_prompt_examples and mirror the closest "
            "match before composing new SQL; use metric `calculation`/`bindings` verbatim. "
            "(3) execute_sql (safety + default_filters run inside it). (4) Read the returned "
            "`receipt`: SHOW the user `receipt.warnings` and any `receipt.metrics` whose "
            "review_state != 'approved' — these are joins/metrics they haven't signed off; never "
            "hide them. Don't refuse on an unreviewed metric — answer and warn. (5) If the answer "
            "was wrong or the user approves an item, call save_correction. Model edits (its `ops`) "
            "apply only with confirmed:true — preview the exact change, get the user's OK, then "
            "re-call confirmed. Saving a corrected example needs no confirmation."
        ),
    })


def _handle_tools_list(req_id: Any) -> None:
    _result(req_id, {
        "tools": [
            {"name": name, "description": meta["description"], "inputSchema": meta["inputSchema"]}
            for name, meta in TOOLS.items()
        ]
    })


def _handle_tools_call(req_id: Any, params: dict[str, Any]) -> None:
    name = params.get("name")
    arguments = params.get("arguments") or {}
    meta = TOOLS.get(name)
    if meta is None:
        _error(req_id, -32602, f"Unknown tool: {name}")
        return
    handler: Callable[[dict[str, Any]], str] = meta["handler"]
    try:
        text = handler(arguments)
        _result(req_id, {"content": [{"type": "text", "text": text}], "isError": False})
    except Exception as exc:  # never let one tool call kill the server
        _log(f"tool {name} raised: {exc!r}")
        _result(req_id, {
            "content": [{"type": "text", "text": json.dumps({"error": {"kind": "other", "remediation": str(exc)}})}],
            "isError": True,
        })


def serve() -> int:
    # One-shot migration of a legacy ~/.agami into <artifacts_dir>/local/, then re-resolve
    # paths (migration may set the artifacts-dir pointer to a custom location).
    global AGAMI_LOCAL, CREDENTIALS_PATH, CONFIG_PATH, QUERY_LOG, FEEDBACK_LOG
    agami_paths.bootstrap()
    AGAMI_LOCAL = agami_paths.local_dir()
    CREDENTIALS_PATH = agami_paths.credentials_path()
    CONFIG_PATH = agami_paths.config_path()
    QUERY_LOG = agami_paths.query_log_path()
    FEEDBACK_LOG = AGAMI_LOCAL / "feedback.jsonl"
    _log(f"starting (version {_server_version()}); reading stdio JSON-RPC")
    for raw in sys.stdin:
        line = raw.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            _log(f"skipping non-JSON line: {line[:80]!r}")
            continue

        method = msg.get("method")
        req_id = msg.get("id")
        params = msg.get("params") or {}

        # Notifications (no id) — acknowledge by doing nothing that needs a reply.
        if req_id is None:
            if method == "notifications/initialized":
                _log("client initialized")
            # notifications/cancelled, etc. are safely ignored
            continue

        if method == "initialize":
            _handle_initialize(req_id, params)
        elif method == "ping":
            _result(req_id, {})
        elif method == "tools/list":
            _handle_tools_list(req_id)
        elif method == "tools/call":
            _handle_tools_call(req_id, params)
        else:
            _error(req_id, -32601, f"Method not found: {method}")
    _log("stdin closed; exiting")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(serve())
    except KeyboardInterrupt:
        sys.exit(0)
