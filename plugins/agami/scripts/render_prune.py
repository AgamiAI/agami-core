#!/usr/bin/env python3
"""
Prune-view renderer — the first, lightweight step of agami-connect.

Renders the cheap discover-pass inventory (every table + its columns, NO
descriptions / grain / relationships — none of that is computed yet) into a
standalone HTML page where the user unchecks tables they don't need (and
optionally individual columns), then pastes a feedback block back so the full
introspection runs on ONLY the kept tables.

This is deliberately SEPARATE from render_model_explorer.py / the model-explorer
template — the prune page is small and self-contained so the (large, stateful)
explorer is never at risk from a pre-introspection change.

Input is the inventory JSON produced by `semantic_model.introspect.discover_inventory`
(written by `sm discover`):

    {"profile", "db_type", "schemas":[...], "table_count", "column_mode",
     "tables":[{"schema", "table", "columns":[{"name","type"}]}, ...]}

Usage:

    python3 render_prune.py --inventory <inventory.json> --out <prune.html>

Stdlib only — no PyYAML, no model deps (so it runs under any interpreter).
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import sys
from pathlib import Path

SHARED_DIR = Path(__file__).resolve().parent.parent / "shared"
TEMPLATE_PATH = SHARED_DIR / "prune-template.html"
LOGO_DARK_PATH = SHARED_DIR / "agami-logo-dark.svg"
LOGO_LIGHT_PATH = SHARED_DIR / "agami-logo-light.svg"


def build_manifest(inventory: dict) -> dict:
    """Group the flat inventory tables under their schema, preserving discovery
    order within each schema and schema order from the inventory."""
    schemas: list[str] = list(inventory.get("schemas") or [])
    by_schema: dict[str, list[dict]] = {}
    order: list[str] = []
    for t in inventory.get("tables") or []:
        sc = t.get("schema") or ""
        if sc not in by_schema:
            by_schema[sc] = []
            order.append(sc)
        by_schema[sc].append({
            "schema": t.get("schema") or "",
            "table": t["table"],
            "columns": [{"name": c.get("name", ""), "type": c.get("type", "")}
                        for c in (t.get("columns") or [])],
        })

    # Schema display order: the engine's schema list first (it's already sorted /
    # meaningful), then any leftover schema that only showed up in the table list.
    ordered = [s for s in schemas if s in by_schema] + [s for s in order if s not in schemas]
    out_schemas = [{"name": s, "tables": by_schema[s]} for s in ordered]

    total_tables = sum(len(g["tables"]) for g in out_schemas)
    total_cols = sum(len(t["columns"]) for g in out_schemas for t in g["tables"])
    return {
        "profile": inventory.get("profile", ""),
        "db_type": inventory.get("db_type", ""),
        "schemas": out_schemas,
        "totals": {"tables": total_tables, "columns": total_cols},
    }


def render(manifest: dict) -> str:
    template = TEMPLATE_PATH.read_text(encoding="utf-8")
    logo_dark = LOGO_DARK_PATH.read_text(encoding="utf-8") if LOGO_DARK_PATH.exists() else ""
    logo_light = LOGO_LIGHT_PATH.read_text(encoding="utf-8") if LOGO_LIGHT_PATH.exists() else ""
    theme_css = (SHARED_DIR / "theme.css").read_text(encoding="utf-8")
    # Escape `</` so a `</script>` inside any column/table name can't terminate
    # the <script> block holding `const MANIFEST = …`.
    manifest_json = json.dumps(manifest, separators=(",", ":")).replace("</", "<\\/")
    now = datetime.datetime.now(datetime.timezone.utc)
    generated_at = now.strftime("%-d %b %Y, %H:%M UTC")
    title = f"Prune tables · {manifest.get('profile', '')}"
    return (
        template
        .replace("{{REPORT_TITLE}}", title)
        .replace("{{PROFILE}}", manifest.get("profile", ""))
        .replace("{{DB_TYPE}}", manifest.get("db_type", ""))
        .replace("{{GENERATED_AT}}", generated_at)
        .replace("{{MANIFEST_JSON}}", manifest_json)
        .replace("{{AGAMI_LOGO_DARK_TEXT}}", logo_dark)
        .replace("{{AGAMI_LOGO_LIGHT_TEXT}}", logo_light)
        .replace("{{THEME_CSS}}", theme_css)
    )


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Render the agami prune view from a discover inventory.")
    p.add_argument("--inventory", required=True, help="Path to the discover inventory JSON")
    p.add_argument("--out", required=True, help="Output HTML path")
    args = p.parse_args(argv)

    inv_path = Path(os.path.expanduser(args.inventory)).resolve()
    if not inv_path.is_file():
        sys.stderr.write(f"agami: inventory not found at {inv_path}\n")
        return 1
    inventory = json.loads(inv_path.read_text(encoding="utf-8"))
    manifest = build_manifest(inventory)
    if not manifest["schemas"]:
        sys.stderr.write("agami: no tables in the inventory — nothing to prune.\n")
        return 1

    out_path = Path(os.path.expanduser(args.out)).resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(render(manifest), encoding="utf-8")

    t = manifest["totals"]
    sys.stderr.write(
        f"agami: prune view → {out_path} ({t['tables']} tables, {t['columns']} columns)\n"
    )
    print(str(out_path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
