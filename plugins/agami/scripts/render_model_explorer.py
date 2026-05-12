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


def _read_yaml(p: Path) -> dict | None:
    if not p.exists():
        return None
    try:
        return yaml.safe_load(p.read_text())
    except yaml.YAMLError as e:
        sys.stderr.write(f"warning: failed to parse {p}: {e}\n")
        return None


def _extract_agami(custom_extensions: list | None) -> dict:
    """Find the agami extension payload inside a list of custom_extensions[]."""
    if not custom_extensions:
        return {}
    for ext in custom_extensions:
        if not isinstance(ext, dict):
            continue
        if ext.get("vendor_name") != "COMMON":
            continue
        data = ext.get("data")
        if not isinstance(data, str):
            continue
        try:
            payload = json.loads(data)
        except json.JSONDecodeError:
            continue
        agami = payload.get("agami")
        if isinstance(agami, dict):
            return agami
    return {}


def _is_excluded(agami: dict) -> bool:
    """An entity is treated as 'excluded' from the runtime model when its
    agami extension carries review_state=rejected. The trust-spine rule
    that gates dependent queries (in agami-query-database Phase 1c) reads
    this same field."""
    return (agami.get("review_state") or "") == "rejected"


def build_manifest(profile_dir: Path, profile: str) -> dict:
    index = _read_yaml(profile_dir / "index.yaml") or {}
    schemas_meta = index.get("schemas") or []

    out_schemas: list[dict] = []
    total_tables = 0
    total_fields = 0
    total_excluded_tables = 0
    total_excluded_fields = 0

    for sm in schemas_meta:
        schema_name = sm.get("name")
        if not schema_name:
            continue
        schema_dir = profile_dir / schema_name
        if not schema_dir.is_dir():
            continue
        _schema_yaml = _read_yaml(schema_dir / "_schema.yaml") or {}
        tables_meta = _schema_yaml.get("tables") or []

        # Some _schema.yaml layouts list tables under the top-level key
        # `tables`, others put them inline under datasets. Support both.
        if not tables_meta and isinstance(_schema_yaml.get("semantic_model"), list):
            for entry in _schema_yaml["semantic_model"]:
                for ds in (entry.get("datasets") or []):
                    tables_meta.append({"name": ds.get("name"), "file": None})

        out_tables: list[dict] = []
        for tm in tables_meta:
            table_name = tm.get("name")
            if not table_name:
                continue
            file_name = tm.get("file") or f"{table_name}.yaml"
            tyaml_path = schema_dir / file_name
            tyaml = _read_yaml(tyaml_path)
            if tyaml is None:
                continue

            # Walk semantic_model[0].datasets[0] for the actual dataset block.
            ds_block = None
            for sm_entry in (tyaml.get("semantic_model") or []):
                for ds in (sm_entry.get("datasets") or []):
                    if ds.get("name") == table_name:
                        ds_block = ds
                        break
                if ds_block:
                    break
            if ds_block is None:
                continue

            ds_agami = _extract_agami(ds_block.get("custom_extensions"))
            row_count = None
            ph = ds_agami.get("performance_hints") or {}
            if isinstance(ph, dict) and isinstance(ph.get("estimated_row_count"), int):
                row_count = ph["estimated_row_count"]

            qname = f"{schema_name}.{table_name}"
            t_excluded = _is_excluded(ds_agami)
            if t_excluded:
                total_excluded_tables += 1
            total_tables += 1

            fields_out: list[dict] = []
            for f in (ds_block.get("fields") or []):
                f_name = f.get("name")
                if not f_name:
                    continue
                f_agami = _extract_agami(f.get("custom_extensions"))
                f_excluded = _is_excluded(f_agami)
                if f_excluded:
                    total_excluded_fields += 1
                total_fields += 1

                desc = (f.get("description") or "").strip()
                fields_out.append({
                    "name":         f_name,
                    "qname":        f"{qname}.{f_name}",
                    "type":         f_agami.get("type") or "",
                    "description":  desc,
                    "review_state": f_agami.get("review_state") or "unreviewed",
                    "origin":       f_agami.get("origin") or "",
                    "confidence":   f_agami.get("confidence"),
                    "excluded":     f_excluded,
                })

            # Synonyms (ai_context) — helpful for search
            ai_ctx = ds_block.get("ai_context") or {}
            synonyms = ai_ctx.get("synonyms") if isinstance(ai_ctx, dict) else []
            if not isinstance(synonyms, list):
                synonyms = []

            out_tables.append({
                "name":         table_name,
                "qname":        qname,
                "description":  (ds_block.get("description") or "").strip(),
                "row_count":    row_count,
                "review_state": ds_agami.get("review_state") or "unreviewed",
                "origin":       ds_agami.get("origin") or "",
                "excluded":     t_excluded,
                "yaml_path":    f"{schema_name}/{file_name}",
                "synonyms":     synonyms,
                "fields":       fields_out,
            })

        if out_tables:
            out_schemas.append({
                "name":        schema_name,
                "description": sm.get("description") or "",
                "tables":      out_tables,
            })

    return {
        "profile": profile,
        "totals": {
            "schemas": len(out_schemas),
            "tables":  total_tables,
            "fields":  total_fields,
            "excluded_tables": total_excluded_tables,
            "excluded_fields": total_excluded_fields,
        },
        "schemas": out_schemas,
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
