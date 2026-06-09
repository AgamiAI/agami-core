#!/usr/bin/env python3
"""
Model-explorer renderer for the agami-model skill.

Walks every YAML under <artifacts_dir>/<profile>/ exactly once, builds a
compact manifest of schemas → tables → fields (with current excluded /
review_state per entity), and writes a self-contained HTML file that lets
the user search the model and queue exclude / include actions for tables
and columns. Stdlib only (yaml is from PyYAML, the same dep used elsewhere
in the plugin).

This script does the YAML reading so the LLM doesn't have to. Total cost
per render: zero LLM tokens for the file walk, plus the one-shot write
cost of the static HTML template.

Usage:

    python3 render_model_explorer.py \\
        --profile finbud \\
        --artifacts-dir ~/agami-artifacts \\
        --out ~/.agami/model/20260512-101500.html

The HTML's MANIFEST_JSON schema is documented at the top of
shared/model-explorer-template.html.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import sys
from pathlib import Path
from typing import Any

import yaml


SHARED_DIR = Path(__file__).resolve().parent.parent / "shared"
TEMPLATE_PATH = SHARED_DIR / "model-explorer-template.html"
LOGO_DARK_PATH = SHARED_DIR / "agami-logo-dark.svg"
LOGO_LIGHT_PATH = SHARED_DIR / "agami-logo-light.svg"


def build_manifest(profile_dir: Path, profile: str) -> dict:
    """Build the explorer manifest from the semantic model. Subject areas map to
    the explorer's top-level groups ("schemas"), tables → tables, columns → fields.
    Loads with include_rejected=True so excluded entries show (toggleable in the UI).
    """
    import sys as _sys
    _scripts = str(Path(__file__).resolve().parent)
    if _scripts not in _sys.path:
        _sys.path.insert(0, _scripts)
    from semantic_model import loader as _L

    org = _L.load_organization(profile_dir, include_rejected=True)
    out_schemas: list[dict] = []
    total_tables = total_fields = total_excluded_tables = total_excluded_fields = 0
    metrics_out: list[dict] = []

    for sa in org.subject_areas:
        out_tables: list[dict] = []
        for t in sa.tables_defined:
            total_tables += 1
            t_excluded = t.review_state == "rejected"
            if t_excluded:
                total_excluded_tables += 1
            qname = f"{sa.name}.{t.name}"
            fields_out: list[dict] = []
            for c in t.columns:
                total_fields += 1
                f_excluded = c.review_state == "rejected"
                if f_excluded:
                    total_excluded_fields += 1
                fields_out.append({
                    "name": c.name, "qname": f"{qname}.{c.name}", "type": c.type,
                    "description": c.description, "review_state": c.review_state,
                    "origin": "", "confidence": c.confidence, "excluded": f_excluded,
                    "sensitive": c.sensitive,
                })
            out_tables.append({
                "name": t.name, "qname": qname, "description": t.description,
                "row_count": (t.performance_hints.estimated_row_count
                              if t.performance_hints else None),
                "review_state": t.review_state, "origin": "", "excluded": t_excluded,
                "yaml_path": f"subject_areas/{sa.name}/tables/{t.name}.yaml",
                "synonyms": [], "area": sa.name, "fields": fields_out,
            })
        for mm in sa.metrics:
            metrics_out.append({"name": mm.name, "qname": f"{sa.name}.{mm.name}",
                                "definition": mm.calculation, "review_state": mm.review_state,
                                "area": sa.name})
        if out_tables:
            out_schemas.append({"name": sa.name, "description": sa.description,
                                "tables": out_tables})

    return {
        "profile": profile,
        "totals": {
            "schemas": len(out_schemas), "tables": total_tables, "fields": total_fields,
            "excluded_tables": total_excluded_tables, "excluded_fields": total_excluded_fields,
            "metrics": len(metrics_out), "named_filters": 0,
        },
        "schemas": out_schemas, "metrics": metrics_out, "named_filters": [],
    }


def render(*, title: str, profile: str, manifest: dict) -> str:
    template = TEMPLATE_PATH.read_text()
    logo_dark = LOGO_DARK_PATH.read_text() if LOGO_DARK_PATH.exists() else ""
    logo_light = LOGO_LIGHT_PATH.read_text() if LOGO_LIGHT_PATH.exists() else ""

    out = (
        template
        .replace("{{REPORT_TITLE}}", title)
        .replace("{{PROFILE}}", profile)
        .replace("{{GENERATED_AT}}",
                 datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"))
        .replace("{{MANIFEST_JSON}}", json.dumps(manifest, separators=(",", ":")))
        .replace("{{AGAMI_LOGO_DARK_TEXT}}", logo_dark)
        .replace("{{AGAMI_LOGO_LIGHT_TEXT}}", logo_light)
    )
    return out


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--profile", required=True,
                   help="Profile name (e.g., 'finbud', 'main', 'staging')")
    p.add_argument("--artifacts-dir", required=True,
                   help="Root artifacts directory (typically ~/agami-artifacts)")
    p.add_argument("--title",
                   help="Dashboard title (default: 'Model explorer · <profile>')")
    p.add_argument("--out", required=True,
                   help="Output HTML path")
    p.add_argument("--manifest-out",
                   help="Optional: also dump the raw manifest JSON to this path")
    args = p.parse_args()

    artifacts_dir = Path(os.path.expanduser(args.artifacts_dir)).resolve()
    profile_dir = artifacts_dir / args.profile
    if not profile_dir.is_dir():
        sys.stderr.write(f"agami: profile dir not found at {profile_dir}\n")
        return 1

    manifest = build_manifest(profile_dir, args.profile)
    if not manifest["schemas"]:
        sys.stderr.write(
            f"agami: no schemas found in {profile_dir}. Run /agami-connect first.\n"
        )
        return 1

    title = args.title or f"Model explorer · {args.profile}"
    out_path = Path(os.path.expanduser(args.out)).resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(render(title=title, profile=args.profile, manifest=manifest))

    if args.manifest_out:
        mpath = Path(os.path.expanduser(args.manifest_out)).resolve()
        mpath.parent.mkdir(parents=True, exist_ok=True)
        mpath.write_text(json.dumps(manifest, indent=2))

    t = manifest["totals"]
    print(
        f"Wrote {out_path} "
        f"({t['schemas']} schemas · {t['tables']} tables · {t['fields']} fields; "
        f"{t['excluded_tables']} tables + {t['excluded_fields']} columns currently excluded)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
