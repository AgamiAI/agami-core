#!/usr/bin/env python3
"""
agami serve — a local, single-player MCP server (stdio transport).

Exposes the *local* semantic model + local SQL execution over the Model
Context Protocol so a developer can use agami from any MCP-capable client
that launches a local stdio server as a child process — chiefly **Claude
Code** (`claude mcp add`) and **Claude Desktop** (`claude_desktop_config.json`).

This is the local mirror of the hosted "Ask Agami" connector: the same tool
surface (list_datasources / get_datasource_schema / get_prompt_examples /
execute_sql / log_feedback), but backed by the user's local .agami files and
local DB execution instead of a cloud registry. Going from local → team is a
backend swap, not a new product.

Design constraints (match the rest of agami):
  - **Zero third-party dependencies.** Pure stdlib: the MCP stdio protocol is
    newline-delimited JSON-RPC 2.0, which we speak by hand. Grep the source —
    there is no network call here, no auth, no telemetry.
  - **No data leaves the machine.** SQL is executed locally by shelling out to
    the sibling `execute_sql.py` (the same Tier-3 executor the skills use); the
    semantic model is read from `<artifacts_dir>/<profile>/` and returned as-is.
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
EXECUTE_SQL = SCRIPT_DIR / "execute_sql.py"
AGAMI_HOME = Path.home() / ".agami"
CREDENTIALS_PATH = AGAMI_HOME / "credentials"
CONFIG_PATH = AGAMI_HOME / ".config"
QUERY_LOG = AGAMI_HOME / "query_log.jsonl"
FEEDBACK_LOG = AGAMI_HOME / "feedback.jsonl"

SERVER_NAME = "agami"
# The protocol version we fall back to if the client doesn't pin one. We echo
# the client's requested version when present (see _handle_initialize).
DEFAULT_PROTOCOL_VERSION = "2024-11-05"


def _server_version() -> str:
    """Best-effort plugin version.

    Prefer the AGAMI_VERSION env var (set by setup_desktop_mcp.py when the server
    is copied to a standalone ~/.agami/serve dir, where the marketplace.json
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
    """Resolution order: AGAMI_ARTIFACTS_DIR → .config.artifacts_dir → $HOME/agami-artifacts."""
    env = os.environ.get("AGAMI_ARTIFACTS_DIR")
    if env:
        return Path(env).expanduser()
    cfg = _load_config().get("artifacts_dir")
    if isinstance(cfg, str) and cfg:
        return Path(cfg).expanduser()
    return Path.home() / "agami-artifacts"


def _credentials_sections() -> dict[str, dict[str, str]]:
    """Parse ~/.agami/credentials (INI) into {profile: {field: value}}. Empty on any error."""
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


def tool_list_datasources(_args: dict[str, Any]) -> str:
    """Local analog of Ask Agami `list_organizations`: enumerate local profiles."""
    creds = _credentials_sections()
    artifacts = resolve_artifacts_dir()
    active = resolve_profile()
    out = []
    for profile in sorted(creds.keys()):
        pdir = artifacts / profile
        dataset_count = 0
        if pdir.is_dir():
            dataset_count = sum(
                1
                for f in pdir.rglob("*.yaml")
                if f.name not in ("index.yaml", "_schema.yaml")
            )
        out.append({
            "datasource": profile,
            "database_type": _db_type_for(profile, creds),
            "dataset_count": dataset_count,
            "model_present": (pdir / "index.yaml").exists(),
            "is_active": profile == active,
        })
    if not out:
        return json.dumps({
            "datasources": [],
            "note": "No profiles found in ~/.agami/credentials. Run the agami-connect skill first.",
        }, indent=2)
    return json.dumps({"datasources": out, "active_datasource": active}, indent=2)


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text()
    except OSError:
        return None


def tool_get_datasource_schema(args: dict[str, Any]) -> str:
    """Local analog of Ask Agami `get_datasource_schema`.

    Returns the curated semantic-model context as text — exactly the files the
    agami-query-database skill reads: index.yaml + every <schema>/_schema.yaml
    (the Pass-1 slim TOC), plus the full per-table yaml for any `dataset_names`
    passed (Pass-2 lazy-load), plus ORGANIZATION.md and USER_MEMORY.md context.
    """
    profile = resolve_profile(args.get("datasource"))
    artifacts = resolve_artifacts_dir()
    pdir = artifacts / profile
    if not (pdir / "index.yaml").exists():
        return json.dumps({
            "error": {
                "kind": "not_found",
                "remediation": f"No semantic model at {pdir}/index.yaml. "
                               f"Run the agami-connect skill to introspect this database.",
            }
        }, indent=2)

    parts: list[str] = [f"# Semantic model for datasource '{profile}'  (source: {pdir})\n"]

    idx = _read_text(pdir / "index.yaml")
    if idx is not None:
        parts.append(f"## index.yaml\n```yaml\n{idx}\n```\n")

    # Pass 1: every <schema>/_schema.yaml (slim TOC + relationships)
    for schema_file in sorted(pdir.glob("*/_schema.yaml")):
        text = _read_text(schema_file)
        if text is not None:
            rel = schema_file.relative_to(pdir)
            parts.append(f"## {rel}\n```yaml\n{text}\n```\n")

    # Pass 2: full per-table yaml for explicitly requested datasets.
    requested = args.get("dataset_names") or []
    if requested:
        wanted = {str(n).split(".")[-1].lower() for n in requested}
        for table_file in sorted(pdir.glob("*/*.yaml")):
            if table_file.name in ("index.yaml", "_schema.yaml"):
                continue
            if table_file.stem.lower() in wanted:
                text = _read_text(table_file)
                if text is not None:
                    rel = table_file.relative_to(pdir)
                    parts.append(f"## {rel}  (full table model)\n```yaml\n{text}\n```\n")

    org = _read_text(pdir / "ORGANIZATION.md")
    if org and org.strip():
        parts.append(f"## ORGANIZATION.md (domain context)\n{org}\n")
    user_mem = _read_text(artifacts / "USER_MEMORY.md")
    if user_mem and user_mem.strip():
        parts.append(f"## USER_MEMORY.md (cross-database preferences)\n{user_mem}\n")

    if not requested:
        parts.append(
            "\n> Note: per-table field detail is lazy-loaded. Call get_datasource_schema "
            "again with `dataset_names: [...]` to pull the full model for the tables you need."
        )
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
    examples_path = artifacts / profile / "examples.yaml"
    text = _read_text(examples_path)
    if text is None:
        # v1.0 fallback layout
        text = _read_text(AGAMI_HOME / f"{profile}-examples.yaml")
    if text is None:
        return json.dumps({
            "examples": [],
            "note": f"No examples library at {examples_path}. "
                    f"Corrections saved via agami-save-correction will appear here.",
        }, indent=2)
    header = (
        f"# Few-shot NL→SQL examples for datasource '{profile}'  (source: {examples_path})\n"
        f"# Use these to ground SQL dialect and house style.\n"
    )
    return header + "\n```yaml\n" + text + "\n```\n"


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

    started = time.monotonic()
    try:
        proc = subprocess.run(
            [sys.executable, str(EXECUTE_SQL), "--profile", profile, "--sql", sql],
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

    result = {
        "columns": columns,
        "rows": data_rows,
        "row_count": len(data_rows),
        "truncated": truncated,
        "sql": sql,
        "execution_ms": execution_ms,
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
    """Local analog of Ask Agami `log_feedback`: append to ~/.agami/feedback.jsonl."""
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
            "Fetch the local semantic model for a datasource: index + per-schema TOCs (Pass 1), "
            "plus the full per-table model for any `dataset_names` you pass (Pass 2 lazy-load), "
            "plus ORGANIZATION.md / USER_MEMORY.md context. Use the metric/measure `calculation` "
            "fields VERBATIM when generating SQL."
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
            "Fetch the curated few-shot NL→SQL examples (examples.yaml) for a datasource. "
            "Use before generating SQL to ground dialect and house style. Local analog of the "
            "hosted get_prompt_examples; returns the full curated library (no cosine ranking locally)."
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
            "Record thumbs-up/down feedback for a question to the local ~/.agami/feedback.jsonl. "
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
            "Discover datasources with list_datasources, fetch the model + examples for the one "
            "the question touches, generate SQL using metric `calculation` fields verbatim, then "
            "execute_sql. All execution is local; nothing leaves the machine."
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
