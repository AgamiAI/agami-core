#!/usr/bin/env python3
"""
agami semantic model validator — OSI v0.1.1 + Agami custom_extensions.

Two ways to use:

    # CLI (exit 0 on pass, 1 on fail)
    python validate_semantic_model.py path/to/model.yaml

    # As a library
    from validate_semantic_model import validate
    errors = validate(model_dict)   # list[str], empty if valid

The validator is the source of truth — agami-connect/SKILL.md and agami-save-correction/SKILL.md
both call it before writing the model file. A model that fails validation is never
persisted. This guarantees no OSI-breaking change ever reaches disk.

Layers, in order:

1. OSI JSON Schema (osi-schema.json, bundled at ../shared/osi-schema.json).
   Catches: missing required fields, wrong types, unknown top-level keys,
   bad enum values, structural breaches.

2. Agami invariants on top:
   - Unique names (datasets, fields-per-dataset, metrics, relationships)
   - Relationship from/to point at real datasets
   - from_columns / to_columns same length
   - All COMMON+agami custom_extensions use only documented keys
   - agami.type values are in the allowed simple-type set
   - choice_field keys/values are strings

3. Optional SQL parse via sqlglot. Warning, not error, on unparseable expressions.

Dependencies:
    pip install pyyaml jsonschema           # required
    pip install sqlglot                     # optional, for SQL validation

The script depends ONLY on these. No agami, no plugins. It can be vendored
into any tooling that needs to validate an OSI model.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

try:
    import yaml
    from jsonschema import Draft202012Validator
except ImportError:
    sys.stderr.write(
        "Missing dependencies. Install:\n"
        "  pip install pyyaml jsonschema\n"
    )
    sys.exit(2)

try:
    import sqlglot
    from sqlglot.errors import ParseError as SqlglotParseError
    HAS_SQLGLOT = True
except ImportError:
    HAS_SQLGLOT = False


# --- Agami extension allowlist ---------------------------------------------
#
# Mirrors plugins/agami/shared/agami-osi-extensions.md. Every key under the
# `agami` JSON object on a custom_extensions entry must appear here. Adding a
# new key requires:
#   1. documenting it in agami-osi-extensions.md
#   2. extending the allowlist below
#   3. adding a test in tests/test_semantic_model_validator.py

# Trust-layer keys — universal across field / dataset / relationship / metric
# / named_filter entries. Documented under the "Trust-layer extensions" section
# of agami-osi-extensions.md. Model-level extensions deliberately do NOT
# include these (the model itself isn't reviewable; only its members are).
TRUST_LAYER_KEYS: frozenset[str] = frozenset({
    "confidence", "signal_breakdown", "review_state",
    "signed_off_by", "signed_off_at", "signed_off_role", "origin",
})

ALLOWED_AGAMI_KEYS_FIELD: frozenset[str] = frozenset({
    "type", "choice_field", "unit", "original_type",
}) | TRUST_LAYER_KEYS

ALLOWED_AGAMI_KEYS_DATASET: frozenset[str] = frozenset({
    "performance_hints",
}) | TRUST_LAYER_KEYS

ALLOWED_AGAMI_KEYS_RELATIONSHIP: frozenset[str] = frozenset({
    "fk_validation",
}) | TRUST_LAYER_KEYS

# Metric-level extensions are new — OSI Metric supports custom_extensions but
# the validator did not previously walk them. The definitional layer
# (definition_prose / assumptions / excludes) lives here, alongside the
# universal trust-layer keys.
ALLOWED_AGAMI_KEYS_METRIC: frozenset[str] = frozenset({
    "definition_prose", "assumptions", "excludes",
}) | TRUST_LAYER_KEYS

ALLOWED_AGAMI_KEYS_MODEL: frozenset[str] = frozenset({
    "profile", "db_type", "schema", "table", "introspect_meta", "named_filters",
})

# Named-filter object keys (lives inside the model-level agami.named_filters
# array). Carries trust-layer fields because each named_filter is independently
# reviewed under Rule 1.
ALLOWED_NAMED_FILTER_KEYS: frozenset[str] = frozenset({
    "name", "expression", "definition_prose", "synonyms",
}) | TRUST_LAYER_KEYS

ALLOWED_AGAMI_TYPES: frozenset[str] = frozenset({
    "string", "integer", "decimal", "timestamp", "date", "boolean",
})

ALLOWED_PERFORMANCE_HINT_KEYS: frozenset[str] = frozenset({
    "estimated_row_count", "recommended_filters", "selective_filters", "indexes",
})

ALLOWED_FK_VALIDATION_KEYS: frozenset[str] = frozenset({
    "validated_at", "orphan_count", "total_rows", "orphan_ratio",
})

ALLOWED_INTROSPECT_META_KEYS: frozenset[str] = frozenset({
    "introspected_at", "tier", "source_db_version",
})

# Trust-layer enums (see agami-osi-extensions.md → Hard rules #7).
ALLOWED_REVIEW_STATES: frozenset[str] = frozenset({
    "unreviewed", "approved", "rejected", "stale",
})

ALLOWED_ORIGINS: frozenset[str] = frozenset({
    "fk", "introspect_heuristic", "column_comment", "llm_suggested", "human_authored",
})

ALLOWED_SIGNOFF_ROLES: frozenset[str] = frozenset({
    "cfo", "cto", "data_lead", "engineer", "analyst", "other", "system",
})


# --- Schema loader ----------------------------------------------------------

_SCHEMA_PATH = Path(__file__).resolve().parent.parent / "shared" / "osi-schema.json"


def load_osi_schema(path: Path = _SCHEMA_PATH) -> dict:
    if not path.exists():
        raise FileNotFoundError(
            f"Bundled OSI schema not found at {path}. "
            "Re-clone the repo or restore plugins/agami/shared/osi-schema.json."
        )
    with path.open() as f:
        return json.load(f)


# --- Layer 1: JSON Schema ---------------------------------------------------

def _osi_schema_errors(model: dict, schema: dict) -> list[str]:
    validator = Draft202012Validator(schema)
    out: list[str] = []
    for err in sorted(validator.iter_errors(model), key=lambda e: list(e.absolute_path)):
        path = ".".join(str(p) for p in err.absolute_path) or "(root)"
        out.append(f"[OSI Schema] {path}: {err.message}")
    return out


# --- Layer 2: Agami invariants ----------------------------------------------

def _find_duplicates(items: list[str]) -> list[str]:
    seen: set[str] = set()
    dups: list[str] = []
    for item in items:
        if item in seen and item not in dups:
            dups.append(item)
        seen.add(item)
    return dups


def _unique_name_errors(model: dict) -> list[str]:
    out: list[str] = []
    sm_list = model.get("semantic_model") if isinstance(model, dict) else None
    if not isinstance(sm_list, list):
        return out
    for sm in sm_list:
        if not isinstance(sm, dict):
            continue
        sm_name = sm.get("name", "<unnamed>")

        ds_names = [d.get("name") for d in sm.get("datasets", []) if d.get("name")]
        for dup in _find_duplicates(ds_names):
            out.append(f"[Unique] duplicate dataset name '{dup}' in model '{sm_name}'")

        for ds in sm.get("datasets", []):
            ds_name = ds.get("name", "<unnamed>")
            field_names = [f.get("name") for f in ds.get("fields", []) if f.get("name")]
            for dup in _find_duplicates(field_names):
                out.append(
                    f"[Unique] duplicate field name '{dup}' in dataset '{ds_name}'"
                )

        metric_names = [m.get("name") for m in sm.get("metrics", []) if m.get("name")]
        for dup in _find_duplicates(metric_names):
            out.append(f"[Unique] duplicate metric name '{dup}' in model '{sm_name}'")

        rel_names = [r.get("name") for r in sm.get("relationships", []) if r.get("name")]
        for dup in _find_duplicates(rel_names):
            out.append(
                f"[Unique] duplicate relationship name '{dup}' in model '{sm_name}'"
            )

    return out


def _relationship_ref_errors(model: dict) -> list[str]:
    out: list[str] = []
    sm_list = model.get("semantic_model") if isinstance(model, dict) else None
    if not isinstance(sm_list, list):
        return out
    for sm in sm_list:
        if not isinstance(sm, dict):
            continue
        ds_names = {d.get("name") for d in sm.get("datasets", []) if d.get("name")}
        for rel in sm.get("relationships", []):
            rel_name = rel.get("name", "<unnamed>")
            from_ds = rel.get("from")
            to_ds = rel.get("to")
            if from_ds and from_ds not in ds_names:
                out.append(
                    f"[Reference] relationship '{rel_name}' from '{from_ds}' "
                    f"does not match any dataset"
                )
            if to_ds and to_ds not in ds_names:
                out.append(
                    f"[Reference] relationship '{rel_name}' to '{to_ds}' "
                    f"does not match any dataset"
                )
            from_cols = rel.get("from_columns") or []
            to_cols = rel.get("to_columns") or []
            if from_cols and to_cols and len(from_cols) != len(to_cols):
                out.append(
                    f"[Reference] relationship '{rel_name}' from_columns "
                    f"({len(from_cols)}) and to_columns ({len(to_cols)}) "
                    f"differ in length"
                )
    return out


def _parse_agami_payload(extension: dict) -> tuple[dict | None, str | None]:
    """Return (agami_dict, error). If vendor_name != COMMON, returns (None, None)."""
    if extension.get("vendor_name") != "COMMON":
        return None, None
    raw = extension.get("data")
    if not isinstance(raw, str):
        return None, "data field must be a JSON string"
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as e:
        return None, f"data is not valid JSON: {e}"
    if not isinstance(parsed, dict):
        return None, "data JSON must be an object"
    agami = parsed.get("agami")
    if agami is None:
        # Other COMMON extensions (non-agami) are allowed to pass through.
        return None, None
    if not isinstance(agami, dict):
        return None, "agami payload must be an object"
    return agami, None


def _check_extension_keys(
    agami: dict, allowed: frozenset[str], context: str
) -> list[str]:
    out: list[str] = []
    extras = set(agami.keys()) - allowed
    if extras:
        out.append(
            f"[Extension] {context}: unknown agami key(s) {sorted(extras)}. "
            f"Allowed at this level: {sorted(allowed)}. "
            f"Document it in agami-osi-extensions.md and add to the allowlist "
            f"in validate_semantic_model.py before using."
        )
    return out


def _check_trust_layer(agami: dict, ctx: str, *, is_rule_1: bool) -> list[str]:
    """Validate the universal trust-layer fields (confidence, review_state,
    origin, signed_off_*). Enforces Hard Rules #7 / #8 / #9 / #10 from
    agami-osi-extensions.md.

    `is_rule_1=True` means this entry is a metric or a named_filter — high
    blast radius — and a `signed_off_role` is required when approved. Rule 1
    callers should additionally check `definition_prose` themselves.
    """
    out: list[str] = []

    # confidence: number in [0, 1]
    if "confidence" in agami:
        c = agami["confidence"]
        if isinstance(c, bool) or not isinstance(c, (int, float)):
            out.append(f"[Trust] {ctx}: confidence must be a number")
        elif not (0.0 <= float(c) <= 1.0):
            out.append(f"[Trust] {ctx}: confidence {c} outside [0, 1]")

    # signal_breakdown: free-form dict
    if "signal_breakdown" in agami:
        sb_dict = agami["signal_breakdown"]
        if not isinstance(sb_dict, dict):
            out.append(f"[Trust] {ctx}: signal_breakdown must be an object")

    # review_state enum
    rs = agami.get("review_state")
    if rs is not None and rs not in ALLOWED_REVIEW_STATES:
        out.append(
            f"[Trust] {ctx}: review_state '{rs}' invalid. "
            f"Must be one of {sorted(ALLOWED_REVIEW_STATES)}."
        )

    # origin enum
    origin = agami.get("origin")
    if origin is not None and origin not in ALLOWED_ORIGINS:
        out.append(
            f"[Trust] {ctx}: origin '{origin}' invalid. "
            f"Must be one of {sorted(ALLOWED_ORIGINS)}."
        )

    # signed_off_role enum
    role = agami.get("signed_off_role")
    if role is not None and role not in ALLOWED_SIGNOFF_ROLES:
        out.append(
            f"[Trust] {ctx}: signed_off_role '{role}' invalid. "
            f"Must be one of {sorted(ALLOWED_SIGNOFF_ROLES)}."
        )

    # signed_off_by, signed_off_at types
    sb = agami.get("signed_off_by")
    if sb is not None and not isinstance(sb, str):
        out.append(f"[Trust] {ctx}: signed_off_by must be a string or null")
    sa = agami.get("signed_off_at")
    if sa is not None and not isinstance(sa, str):
        out.append(f"[Trust] {ctx}: signed_off_at must be an ISO-8601 string or null")

    # Hard Rule #8 / #9 — sign-off completeness when approved.
    if rs == "approved":
        if not isinstance(sb, str) or not sb:
            out.append(
                f"[Trust] {ctx}: review_state=approved requires non-null signed_off_by"
            )
        if not isinstance(sa, str) or not sa:
            out.append(
                f"[Trust] {ctx}: review_state=approved requires non-null signed_off_at"
            )
        if is_rule_1 and role is None:
            out.append(
                f"[Trust] {ctx}: Rule 1 (metric / named_filter) approved requires "
                f"non-null signed_off_role"
            )

    # Hard Rule #10 — sign-off coherence: unreviewed/rejected entries must not
    # carry sign-off attribution. (Stale entries preserve their previous sign-off,
    # so they are exempt.)
    if rs in ("unreviewed", "rejected"):
        if sb is not None:
            out.append(
                f"[Trust] {ctx}: review_state={rs} requires signed_off_by=null"
            )
        if sa is not None:
            out.append(
                f"[Trust] {ctx}: review_state={rs} requires signed_off_at=null"
            )
        if role is not None:
            out.append(
                f"[Trust] {ctx}: review_state={rs} requires signed_off_role=null"
            )

    return out


def _check_field_extensions(field_agami: dict, context: str) -> list[str]:
    out = _check_extension_keys(field_agami, ALLOWED_AGAMI_KEYS_FIELD, context)

    if "type" in field_agami:
        t = field_agami["type"]
        if t not in ALLOWED_AGAMI_TYPES:
            out.append(
                f"[Extension] {context}: agami.type '{t}' invalid. "
                f"Must be one of {sorted(ALLOWED_AGAMI_TYPES)}."
            )

    if "choice_field" in field_agami:
        cf = field_agami["choice_field"]
        if not isinstance(cf, dict):
            out.append(f"[Extension] {context}: choice_field must be an object")
        else:
            for k, v in cf.items():
                if not isinstance(k, str):
                    out.append(
                        f"[Extension] {context}: choice_field key {k!r} must be a "
                        f"string (quote numeric values, e.g. \"1\": \"Critical\")"
                    )
                if not isinstance(v, str):
                    out.append(
                        f"[Extension] {context}: choice_field value {v!r} for key "
                        f"{k!r} must be a string"
                    )

    if "unit" in field_agami and not isinstance(field_agami["unit"], str):
        out.append(f"[Extension] {context}: unit must be a string")

    if "original_type" in field_agami and not isinstance(field_agami["original_type"], str):
        out.append(f"[Extension] {context}: original_type must be a string")

    out.extend(_check_trust_layer(field_agami, context, is_rule_1=False))
    return out


def _check_dataset_extensions(ds_agami: dict, context: str) -> list[str]:
    out = _check_extension_keys(ds_agami, ALLOWED_AGAMI_KEYS_DATASET, context)
    if "performance_hints" in ds_agami:
        ph = ds_agami["performance_hints"]
        if not isinstance(ph, dict):
            out.append(f"[Extension] {context}: performance_hints must be an object")
        else:
            extras = set(ph.keys()) - ALLOWED_PERFORMANCE_HINT_KEYS
            if extras:
                out.append(
                    f"[Extension] {context}.performance_hints: unknown key(s) "
                    f"{sorted(extras)}. Allowed: {sorted(ALLOWED_PERFORMANCE_HINT_KEYS)}."
                )
            if "estimated_row_count" in ph and not isinstance(ph["estimated_row_count"], int):
                out.append(
                    f"[Extension] {context}.performance_hints.estimated_row_count "
                    f"must be an integer"
                )
            for arr_key in ("recommended_filters", "selective_filters", "indexes"):
                if arr_key in ph and not isinstance(ph[arr_key], list):
                    out.append(
                        f"[Extension] {context}.performance_hints.{arr_key} "
                        f"must be an array"
                    )

    out.extend(_check_trust_layer(ds_agami, context, is_rule_1=False))
    return out


def _check_relationship_extensions(rel_agami: dict, context: str) -> list[str]:
    out = _check_extension_keys(rel_agami, ALLOWED_AGAMI_KEYS_RELATIONSHIP, context)
    if "fk_validation" in rel_agami:
        fkv = rel_agami["fk_validation"]
        if not isinstance(fkv, dict):
            out.append(f"[Extension] {context}: fk_validation must be an object")
        else:
            extras = set(fkv.keys()) - ALLOWED_FK_VALIDATION_KEYS
            if extras:
                out.append(
                    f"[Extension] {context}.fk_validation: unknown key(s) "
                    f"{sorted(extras)}. Allowed: {sorted(ALLOWED_FK_VALIDATION_KEYS)}."
                )

    out.extend(_check_trust_layer(rel_agami, context, is_rule_1=False))
    return out


def _check_metric_extensions(metric_agami: dict, context: str) -> list[str]:
    out = _check_extension_keys(metric_agami, ALLOWED_AGAMI_KEYS_METRIC, context)

    if "definition_prose" in metric_agami and not isinstance(metric_agami["definition_prose"], str):
        out.append(f"[Extension] {context}: definition_prose must be a string")
    if "assumptions" in metric_agami and not isinstance(metric_agami["assumptions"], list):
        out.append(f"[Extension] {context}: assumptions must be an array")
    if "excludes" in metric_agami and not isinstance(metric_agami["excludes"], list):
        out.append(f"[Extension] {context}: excludes must be an array")

    out.extend(_check_trust_layer(metric_agami, context, is_rule_1=True))

    # Rule 1 + Hard Rule #8 — approved metric requires non-empty definition_prose.
    if metric_agami.get("review_state") == "approved":
        dp = metric_agami.get("definition_prose")
        if not isinstance(dp, str) or not dp.strip():
            out.append(
                f"[Trust] {context}: Rule 1 (metric) approved requires "
                f"non-empty definition_prose"
            )

    return out


def _check_named_filter(nf: dict, ctx: str) -> list[str]:
    """Validate a single named_filter object inside agami.named_filters[]."""
    out: list[str] = []
    if not isinstance(nf, dict):
        return [f"[Extension] {ctx}: must be an object"]

    extras = set(nf.keys()) - ALLOWED_NAMED_FILTER_KEYS
    if extras:
        out.append(
            f"[Extension] {ctx}: unknown key(s) {sorted(extras)}. "
            f"Allowed: {sorted(ALLOWED_NAMED_FILTER_KEYS)}."
        )

    for required in ("name", "expression"):
        v = nf.get(required)
        if not isinstance(v, str) or not v.strip():
            out.append(
                f"[Extension] {ctx}: missing required key '{required}' (non-empty string)"
            )

    if "definition_prose" in nf and not isinstance(nf["definition_prose"], str):
        out.append(f"[Extension] {ctx}: definition_prose must be a string")
    if "synonyms" in nf and not isinstance(nf["synonyms"], list):
        out.append(f"[Extension] {ctx}: synonyms must be an array")

    out.extend(_check_trust_layer(nf, ctx, is_rule_1=True))

    if nf.get("review_state") == "approved":
        dp = nf.get("definition_prose")
        if not isinstance(dp, str) or not dp.strip():
            out.append(
                f"[Trust] {ctx}: Rule 1 (named_filter) approved requires "
                f"non-empty definition_prose"
            )

    return out


def _check_model_extensions(sm_agami: dict, context: str) -> list[str]:
    out = _check_extension_keys(sm_agami, ALLOWED_AGAMI_KEYS_MODEL, context)
    if "introspect_meta" in sm_agami:
        meta = sm_agami["introspect_meta"]
        if not isinstance(meta, dict):
            out.append(f"[Extension] {context}: introspect_meta must be an object")
        else:
            extras = set(meta.keys()) - ALLOWED_INTROSPECT_META_KEYS
            if extras:
                out.append(
                    f"[Extension] {context}.introspect_meta: unknown key(s) "
                    f"{sorted(extras)}. Allowed: {sorted(ALLOWED_INTROSPECT_META_KEYS)}."
                )

    if "named_filters" in sm_agami:
        nfs = sm_agami["named_filters"]
        if not isinstance(nfs, list):
            out.append(f"[Extension] {context}: named_filters must be an array")
        else:
            seen_names: set[str] = set()
            for i, nf in enumerate(nfs):
                nf_ctx = f"{context}.named_filters[{i}]"
                out.extend(_check_named_filter(nf, nf_ctx))
                if isinstance(nf, dict):
                    n = nf.get("name")
                    if isinstance(n, str):
                        if n in seen_names:
                            out.append(
                                f"[Extension] {context}.named_filters: "
                                f"duplicate name '{n}'"
                            )
                        seen_names.add(n)

    return out


def _walk_extensions(model: dict) -> list[str]:
    """Walk every custom_extensions list in the model and validate agami payloads."""
    out: list[str] = []

    sm_list = model.get("semantic_model") if isinstance(model, dict) else None
    if not isinstance(sm_list, list):
        return out
    for sm in sm_list:
        if not isinstance(sm, dict):
            continue
        sm_name = sm.get("name", "<unnamed>")

        # Model level
        for i, ext in enumerate(sm.get("custom_extensions", [])):
            agami, err = _parse_agami_payload(ext)
            ctx = f"semantic_model['{sm_name}'].custom_extensions[{i}].agami"
            if err:
                out.append(f"[Extension] {ctx}: {err}")
                continue
            if agami is not None:
                out.extend(_check_model_extensions(agami, ctx))

        # Dataset level
        for ds in sm.get("datasets", []):
            ds_name = ds.get("name", "<unnamed>")
            for i, ext in enumerate(ds.get("custom_extensions", [])):
                agami, err = _parse_agami_payload(ext)
                ctx = f"datasets['{ds_name}'].custom_extensions[{i}].agami"
                if err:
                    out.append(f"[Extension] {ctx}: {err}")
                    continue
                if agami is not None:
                    out.extend(_check_dataset_extensions(agami, ctx))

            # Field level
            for field in ds.get("fields", []):
                f_name = field.get("name", "<unnamed>")
                for i, ext in enumerate(field.get("custom_extensions", [])):
                    agami, err = _parse_agami_payload(ext)
                    ctx = f"datasets['{ds_name}'].fields['{f_name}'].custom_extensions[{i}].agami"
                    if err:
                        out.append(f"[Extension] {ctx}: {err}")
                        continue
                    if agami is not None:
                        out.extend(_check_field_extensions(agami, ctx))

        # Relationship level
        for rel in sm.get("relationships", []):
            rel_name = rel.get("name", "<unnamed>")
            for i, ext in enumerate(rel.get("custom_extensions", [])):
                agami, err = _parse_agami_payload(ext)
                ctx = f"relationships['{rel_name}'].custom_extensions[{i}].agami"
                if err:
                    out.append(f"[Extension] {ctx}: {err}")
                    continue
                if agami is not None:
                    out.extend(_check_relationship_extensions(agami, ctx))

        # Metric level — new in the trust-layer launch. OSI defines
        # metric.custom_extensions but this validator did not previously walk it.
        for metric in sm.get("metrics", []):
            m_name = metric.get("name", "<unnamed>")
            for i, ext in enumerate(metric.get("custom_extensions", [])):
                agami, err = _parse_agami_payload(ext)
                ctx = f"metrics['{m_name}'].custom_extensions[{i}].agami"
                if err:
                    out.append(f"[Extension] {ctx}: {err}")
                    continue
                if agami is not None:
                    out.extend(_check_metric_extensions(agami, ctx))

    return out


# --- Layer 3: optional SQL parse (warning) ----------------------------------

_DIALECT_TO_SQLGLOT: dict[str, str | None] = {
    "ANSI_SQL": None,
    "SNOWFLAKE": "snowflake",
    "DATABRICKS": "databricks",
    "MDX": None,        # not supported by sqlglot — skip
    "TABLEAU": None,    # not supported by sqlglot — skip
}
_SKIP_DIALECTS: frozenset[str] = frozenset({"MDX", "TABLEAU"})


def _sqlglot_warnings(model: dict) -> list[str]:
    if not HAS_SQLGLOT:
        return []
    out: list[str] = []

    def _check(expr: str, dialect: str, ctx: str) -> None:
        if dialect in _SKIP_DIALECTS:
            return
        sg_dialect = _DIALECT_TO_SQLGLOT.get(dialect)
        try:
            sqlglot.parse_one(expr, dialect=sg_dialect)
            return
        except SqlglotParseError:
            pass
        try:
            sqlglot.parse_one(f"SELECT {expr}", dialect=sg_dialect)
            return
        except SqlglotParseError as e:
            out.append(
                f"[SQL warning] {ctx}: {str(e).splitlines()[0]} "
                f"(dialect={dialect})"
            )

    sm_list = model.get("semantic_model") if isinstance(model, dict) else None
    if not isinstance(sm_list, list):
        return out
    for sm in sm_list:
        if not isinstance(sm, dict):
            continue
        for ds in sm.get("datasets", []):
            for field in ds.get("fields", []):
                for de in field.get("expression", {}).get("dialects", []):
                    expr = de.get("expression")
                    dialect = de.get("dialect", "ANSI_SQL")
                    if isinstance(expr, str):
                        _check(expr, dialect,
                               f"field '{ds.get('name')}.{field.get('name')}'")
        for metric in sm.get("metrics", []):
            for de in metric.get("expression", {}).get("dialects", []):
                expr = de.get("expression")
                dialect = de.get("dialect", "ANSI_SQL")
                if isinstance(expr, str):
                    _check(expr, dialect, f"metric '{metric.get('name')}'")
    return out


# --- Public API -------------------------------------------------------------

def validate(model: dict, *, schema: dict | None = None) -> list[str]:
    """
    Validate an OSI semantic-model dict. Returns a list of error strings.
    Empty list means valid.

    Warnings (e.g., SQL parse hints) appear with a "[... warning]" prefix and
    are NOT included in the returned errors — they go through validate_with_warnings.
    """
    errors, _warnings = validate_with_warnings(model, schema=schema)
    return errors


def validate_with_warnings(
    model: dict, *, schema: dict | None = None
) -> tuple[list[str], list[str]]:
    """Returns (errors, warnings). Errors block the write; warnings don't."""
    if schema is None:
        schema = load_osi_schema()

    errors: list[str] = []
    warnings: list[str] = []

    errors.extend(_osi_schema_errors(model, schema))
    errors.extend(_unique_name_errors(model))
    errors.extend(_relationship_ref_errors(model))
    errors.extend(_walk_extensions(model))

    warnings.extend(_sqlglot_warnings(model))

    return errors, warnings


# --- Directory mode (per-schema or per-table layout) ------------------------
#
# Two supported layouts:
#
# v1.2 (per-schema, single file per schema):
#     <profile>/
#         index.yaml                    # TOC, schemas[].file = "<name>.yaml"
#         <schema1>.yaml                # standalone OSI doc with all schema's datasets
#         <schema2>.yaml
#         examples.yaml
#         ORGANIZATION.md
#
# v1.3 (per-table, one file per table):
#     <profile>/
#         index.yaml                    # TOC, schemas[].file = "<name>/_schema.yaml"
#         <schema1>/
#             _schema.yaml              # agami-bespoke: tables[] + relationships[]
#             <table_a>.yaml            # standalone OSI doc with one dataset
#             <table_b>.yaml
#         <schema2>/...
#         examples.yaml
#         ORGANIZATION.md
#
# The validator detects which layout each schema uses from the `file` field
# in index.yaml.schemas[] and dispatches accordingly. A single profile dir
# CAN mix the two layouts (e.g., during incremental migration), but should
# converge to v1.3 within a single agami-connect run.
#
# In both layouts, index.yaml is agami-bespoke (NOT OSI) — a slim TOC plus
# cross-schema relationships and introspect metadata.

ALLOWED_INDEX_TOP_KEYS: frozenset[str] = frozenset({
    "version", "profile", "db_type", "schemas",
    "cross_schema_relationships", "introspect_meta",
})

ALLOWED_INDEX_SCHEMA_KEYS: frozenset[str] = frozenset({
    "name", "file", "table_count", "description",
})

ALLOWED_CROSS_REL_KEYS: frozenset[str] = frozenset({
    "name", "from", "to", "from_columns", "to_columns", "description",
})


def _index_errors(index: dict, *, ctx: str = "index.yaml") -> list[str]:
    out: list[str] = []
    if not isinstance(index, dict):
        return [f"[Index] {ctx}: top-level must be an object"]

    extras = set(index.keys()) - ALLOWED_INDEX_TOP_KEYS
    if extras:
        out.append(
            f"[Index] {ctx}: unknown top-level key(s) {sorted(extras)}. "
            f"Allowed: {sorted(ALLOWED_INDEX_TOP_KEYS)}."
        )

    for k in ("version", "profile", "db_type", "schemas"):
        if k not in index:
            out.append(f"[Index] {ctx}: missing required key '{k}'")

    schemas = index.get("schemas", [])
    if not isinstance(schemas, list) or not schemas:
        out.append(f"[Index] {ctx}: 'schemas' must be a non-empty array")
    else:
        names_seen: set[str] = set()
        for i, s in enumerate(schemas):
            if not isinstance(s, dict):
                out.append(f"[Index] {ctx}.schemas[{i}]: must be an object")
                continue
            extras_s = set(s.keys()) - ALLOWED_INDEX_SCHEMA_KEYS
            if extras_s:
                out.append(
                    f"[Index] {ctx}.schemas[{i}]: unknown key(s) {sorted(extras_s)}. "
                    f"Allowed: {sorted(ALLOWED_INDEX_SCHEMA_KEYS)}."
                )
            for k in ("name", "file"):
                if k not in s:
                    out.append(f"[Index] {ctx}.schemas[{i}]: missing '{k}'")
            n = s.get("name")
            if n in names_seen:
                out.append(f"[Index] {ctx}.schemas: duplicate schema name '{n}'")
            elif n:
                names_seen.add(n)

    rels = index.get("cross_schema_relationships", [])
    if rels and not isinstance(rels, list):
        out.append(f"[Index] {ctx}.cross_schema_relationships: must be an array")
    elif isinstance(rels, list):
        rel_names: set[str] = set()
        for i, r in enumerate(rels):
            if not isinstance(r, dict):
                out.append(
                    f"[Index] {ctx}.cross_schema_relationships[{i}]: must be an object"
                )
                continue
            extras_r = set(r.keys()) - ALLOWED_CROSS_REL_KEYS
            if extras_r:
                out.append(
                    f"[Index] {ctx}.cross_schema_relationships[{i}]: unknown key(s) "
                    f"{sorted(extras_r)}. Allowed: {sorted(ALLOWED_CROSS_REL_KEYS)}."
                )
            for k in ("name", "from", "to", "from_columns", "to_columns"):
                if k not in r:
                    out.append(
                        f"[Index] {ctx}.cross_schema_relationships[{i}]: missing '{k}'"
                    )
            rn = r.get("name")
            if rn in rel_names:
                out.append(
                    f"[Index] {ctx}.cross_schema_relationships: duplicate name '{rn}'"
                )
            elif rn:
                rel_names.add(rn)
            fc = r.get("from_columns") or []
            tc = r.get("to_columns") or []
            if isinstance(fc, list) and isinstance(tc, list) and len(fc) != len(tc):
                out.append(
                    f"[Index] {ctx}.cross_schema_relationships[{i}]: "
                    f"from_columns ({len(fc)}) and to_columns ({len(tc)}) differ in length"
                )
            for endpoint_key in ("from", "to"):
                ep = r.get(endpoint_key)
                if isinstance(ep, str) and "." not in ep:
                    out.append(
                        f"[Index] {ctx}.cross_schema_relationships[{i}].{endpoint_key}: "
                        f"'{ep}' must be qualified as '<schema>.<dataset>'"
                    )

    meta = index.get("introspect_meta")
    if meta is not None:
        if not isinstance(meta, dict):
            out.append(f"[Index] {ctx}.introspect_meta: must be an object")
        else:
            extras_m = set(meta.keys()) - ALLOWED_INTROSPECT_META_KEYS
            if extras_m:
                out.append(
                    f"[Index] {ctx}.introspect_meta: unknown key(s) {sorted(extras_m)}. "
                    f"Allowed: {sorted(ALLOWED_INTROSPECT_META_KEYS)}."
                )

    return out


# --- _schema.yaml allowlist (v1.3 layout) -----------------------------------

ALLOWED_SCHEMA_TOP_KEYS: frozenset[str] = frozenset({
    "version", "schema", "description", "tables",
    "relationships", "metrics",
})

ALLOWED_SCHEMA_TABLE_KEYS: frozenset[str] = frozenset({
    "name", "file", "description", "primary_key", "estimated_row_count",
})


def _schema_yaml_errors(s_doc: dict, *, ctx: str) -> list[str]:
    """Validate the agami-bespoke _schema.yaml (TOC + within-schema rels)."""
    out: list[str] = []
    if not isinstance(s_doc, dict):
        return [f"[Schema] {ctx}: top-level must be an object"]

    extras = set(s_doc.keys()) - ALLOWED_SCHEMA_TOP_KEYS
    if extras:
        out.append(
            f"[Schema] {ctx}: unknown top-level key(s) {sorted(extras)}. "
            f"Allowed: {sorted(ALLOWED_SCHEMA_TOP_KEYS)}."
        )

    for k in ("version", "schema", "tables"):
        if k not in s_doc:
            out.append(f"[Schema] {ctx}: missing required key '{k}'")

    tables = s_doc.get("tables", [])
    if not isinstance(tables, list) or not tables:
        out.append(f"[Schema] {ctx}: 'tables' must be a non-empty array")
    else:
        names_seen: set[str] = set()
        for i, t in enumerate(tables):
            if not isinstance(t, dict):
                out.append(f"[Schema] {ctx}.tables[{i}]: must be an object")
                continue
            extras_t = set(t.keys()) - ALLOWED_SCHEMA_TABLE_KEYS
            if extras_t:
                out.append(
                    f"[Schema] {ctx}.tables[{i}]: unknown key(s) {sorted(extras_t)}. "
                    f"Allowed: {sorted(ALLOWED_SCHEMA_TABLE_KEYS)}."
                )
            for k in ("name", "file"):
                if k not in t:
                    out.append(f"[Schema] {ctx}.tables[{i}]: missing '{k}'")
            n = t.get("name")
            if n in names_seen:
                out.append(f"[Schema] {ctx}.tables: duplicate table name '{n}'")
            elif n:
                names_seen.add(n)

    return out


def _merge_v13_schema(
    schema_dir: Path, schema_name: str, schema_doc_path: Path,
    schema: dict,
) -> tuple[dict, list[str], list[str]]:
    """Read a v1.3 schema directory (_schema.yaml + per-table yamls) and
    return a synthetic single-schema OSI dict suitable for `validate()`.

    Returns (merged_model, errors, warnings). On error, merged_model may
    be partial — the caller should not promote partial results to disk.
    """
    errors: list[str] = []
    warnings: list[str] = []

    try:
        with schema_doc_path.open() as f:
            s_doc = yaml.safe_load(f)
    except yaml.YAMLError as e:
        return ({}, [f"[Schema] {schema_doc_path.name}: invalid YAML: {e}"], warnings)

    errors.extend(_schema_yaml_errors(s_doc or {}, ctx=schema_doc_path.name))
    if errors:
        return ({}, errors, warnings)

    # Read each <table>.yaml as a standalone OSI doc, validate, extract dataset.
    merged_datasets: list[dict] = []
    declared_schema = (s_doc or {}).get("schema")
    if declared_schema and declared_schema != schema_name:
        errors.append(
            f"[Schema] {schema_doc_path.name}: 'schema' field '{declared_schema}' "
            f"doesn't match index.yaml schema name '{schema_name}'"
        )

    for i, t in enumerate(s_doc.get("tables", [])):
        if not isinstance(t, dict):
            continue
        t_file = t.get("file")
        t_name = t.get("name")
        if not t_file or not t_name:
            continue
        t_path = schema_dir / t_file
        if not t_path.exists():
            errors.append(
                f"[Schema] {schema_doc_path.name}.tables[{i}]: file '{t_file}' "
                f"is missing from {schema_dir}"
            )
            continue
        try:
            with t_path.open() as f:
                t_model = yaml.safe_load(f)
        except yaml.YAMLError as e:
            errors.append(f"[Table] {t_file}: invalid YAML: {e}")
            continue
        if not isinstance(t_model, dict):
            errors.append(f"[Table] {t_file}: top-level must be an object")
            continue

        e_list, w_list = validate_with_warnings(t_model, schema=schema)
        errors.extend(f"[{t_file}] {e}" for e in e_list)
        warnings.extend(f"[{t_file}] {w}" for w in w_list)

        sm_list = t_model.get("semantic_model")
        if not isinstance(sm_list, list) or not sm_list:
            errors.append(f"[Table] {t_file}: missing 'semantic_model'")
            continue
        sm = sm_list[0]
        if not isinstance(sm, dict):
            continue

        # Verify the table yaml's agami.schema matches and agami.table matches.
        for ext in sm.get("custom_extensions", []):
            agami, _ = _parse_agami_payload(ext)
            if agami:
                if agami.get("schema") not in (None, schema_name):
                    errors.append(
                        f"[Table] {t_file}: agami.schema='{agami.get('schema')}' "
                        f"doesn't match index.yaml schema name '{schema_name}'"
                    )
                if agami.get("table") not in (None, t_name):
                    errors.append(
                        f"[Table] {t_file}: agami.table='{agami.get('table')}' "
                        f"doesn't match _schema.yaml table name '{t_name}'"
                    )

        # Extract datasets — should be exactly 1 per file.
        ds_list = sm.get("datasets", [])
        if len(ds_list) != 1:
            errors.append(
                f"[Table] {t_file}: expected exactly 1 dataset (one file per "
                f"table), got {len(ds_list)}"
            )
        merged_datasets.extend(ds_list)

    # Build the synthetic merged model: borrow the first table's name + custom
    # extensions for the model-level fields, attach the schema's relationships
    # and metrics.
    merged_model = {
        "version": "0.1.1",
        "semantic_model": [
            {
                "name": "merged",  # synthetic — the per-table yamls each have the real profile name
                "datasets": merged_datasets,
                "relationships": s_doc.get("relationships") or [],
                "metrics": s_doc.get("metrics") or [],
            }
        ],
    }

    # Validate the merged model end-to-end (catches unique-name violations
    # across tables, dangling relationships, etc.).
    e_list, w_list = validate_with_warnings(merged_model, schema=schema)
    errors.extend(f"[{schema_name} merged] {e}" for e in e_list)
    warnings.extend(f"[{schema_name} merged] {w}" for w in w_list)

    return (merged_model, errors, warnings)


def validate_directory(
    profile_dir: Path, *, schema: dict | None = None
) -> tuple[list[str], list[str]]:
    """
    Validate a profile directory in either v1.2 (per-schema single-file) or
    v1.3 (per-table) layout. Layout is detected per-schema from the `file`
    field in index.yaml.schemas[]:

      - "<name>.yaml"           → v1.2: file is a standalone OSI doc
      - "<name>/_schema.yaml"   → v1.3: directory contains _schema.yaml + per-table yamls

    Each schema yaml or per-table yaml is validated as a standalone OSI v0.1.1
    document. Then cross-validates: dataset name uniqueness across schemas,
    cross-schema relationship endpoints resolve to real datasets, and the
    schema-yaml model-level extension's `agami.schema` matches the schema name.
    """
    if schema is None:
        schema = load_osi_schema()

    errors: list[str] = []
    warnings: list[str] = []

    if not profile_dir.exists() or not profile_dir.is_dir():
        return ([f"[Index] profile directory not found: {profile_dir}"], warnings)

    index_path = profile_dir / "index.yaml"
    if not index_path.exists():
        return ([f"[Index] missing index.yaml at {index_path}"], warnings)

    try:
        with index_path.open() as f:
            index = yaml.safe_load(f)
    except yaml.YAMLError as e:
        return ([f"[Index] {index_path.name}: invalid YAML: {e}"], warnings)

    errors.extend(_index_errors(index or {}))

    if errors:
        return (errors, warnings)

    qualified_datasets: set[str] = set()  # "<schema>.<dataset>"

    for s in index.get("schemas", []):
        s_name = s.get("name")
        s_file = s.get("file")
        if not s_name or not s_file:
            continue
        s_path = profile_dir / s_file
        if not s_path.exists():
            errors.append(
                f"[Index] schema '{s_name}': file '{s_file}' is missing from "
                f"{profile_dir}"
            )
            continue

        # Detect layout.
        if s_file.endswith("/_schema.yaml") or s_path.name == "_schema.yaml":
            # v1.3 — per-table layout
            schema_dir = s_path.parent
            merged_model, e_list, w_list = _merge_v13_schema(
                schema_dir, s_name, s_path, schema=schema
            )
            errors.extend(e_list)
            warnings.extend(w_list)

            sm_list = merged_model.get("semantic_model") or []
            for sm in sm_list:
                if not isinstance(sm, dict):
                    continue
                for ds in sm.get("datasets", []):
                    ds_name = ds.get("name")
                    if ds_name:
                        qualified_datasets.add(f"{s_name}.{ds_name}")
        else:
            # v1.2 — single-file layout
            try:
                with s_path.open() as f:
                    s_model = yaml.safe_load(f)
            except yaml.YAMLError as e:
                errors.append(f"[Schema] {s_file}: invalid YAML: {e}")
                continue
            if not isinstance(s_model, dict):
                errors.append(f"[Schema] {s_file}: top-level must be an object")
                continue

            e_list, w_list = validate_with_warnings(s_model, schema=schema)
            errors.extend(f"[{s_file}] {e}" for e in e_list)
            warnings.extend(f"[{s_file}] {w}" for w in w_list)

            sm_list = s_model.get("semantic_model") if isinstance(s_model, dict) else None
            if isinstance(sm_list, list) and sm_list:
                sm = sm_list[0]
                for ext in sm.get("custom_extensions", []) if isinstance(sm, dict) else []:
                    agami, _ = _parse_agami_payload(ext)
                    if agami and agami.get("schema") not in (None, s_name):
                        errors.append(
                            f"[Schema] {s_file}: agami.schema='{agami.get('schema')}' "
                            f"doesn't match index.yaml schema name '{s_name}'"
                        )

                for ds in sm.get("datasets", []) if isinstance(sm, dict) else []:
                    ds_name = ds.get("name")
                    if ds_name:
                        qualified_datasets.add(f"{s_name}.{ds_name}")

    # Cross-schema relationship endpoints must resolve to datasets we actually
    # loaded. (Endpoints are required to be qualified — _index_errors caught the
    # unqualified ones above.)
    for i, r in enumerate(index.get("cross_schema_relationships", []) or []):
        if not isinstance(r, dict):
            continue
        for endpoint_key in ("from", "to"):
            ep = r.get(endpoint_key)
            if isinstance(ep, str) and "." in ep and ep not in qualified_datasets:
                errors.append(
                    f"[Index] cross_schema_relationships[{i}].{endpoint_key} "
                    f"'{ep}' does not match any dataset in the loaded schemas"
                )

    return (errors, warnings)


# --- CLI --------------------------------------------------------------------

def _format_errors(errors: list[str]) -> str:
    return "\n".join(f"  ✗ {e}" for e in errors)


def _format_warnings(warnings: list[str]) -> str:
    return "\n".join(f"  ⚠ {w}" for w in warnings)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Validate an OSI v0.1.1 semantic model with Agami extensions."
    )
    p.add_argument(
        "path",
        nargs="?",
        help="Path to the YAML semantic model file (single-file mode)",
    )
    p.add_argument(
        "--directory",
        help=(
            "Path to a per-schema profile directory containing index.yaml + "
            "<schema>.yaml files. Validates each schema yaml individually, "
            "then runs cross-schema checks."
        ),
    )
    p.add_argument(
        "--no-warnings",
        action="store_true",
        help="Suppress SQL-parse warnings (errors still print)",
    )
    args = p.parse_args(argv)

    if args.directory and args.path:
        sys.stderr.write("Pass either <path> or --directory, not both.\n")
        return 2

    if args.directory:
        profile_dir = Path(args.directory)
        errors, warnings = validate_directory(profile_dir)
        label = f"directory {profile_dir}"
    else:
        if not args.path:
            sys.stderr.write("Provide either <path> or --directory.\n")
            return 2
        yaml_path = Path(args.path)
        if not yaml_path.exists():
            sys.stderr.write(f"File not found: {yaml_path}\n")
            return 2
        with yaml_path.open() as f:
            try:
                model = yaml.safe_load(f)
            except yaml.YAMLError as e:
                sys.stderr.write(f"Invalid YAML: {e}\n")
                return 2
        if not isinstance(model, dict):
            sys.stderr.write("Top-level YAML must be an object.\n")
            return 2
        errors, warnings = validate_with_warnings(model)
        label = yaml_path.name

    if errors:
        print(f"Validation FAILED ({len(errors)} error(s)):")
        print(_format_errors(errors))
        if warnings and not args.no_warnings:
            print(f"\nWarnings ({len(warnings)}):")
            print(_format_warnings(warnings))
        return 1

    print(f"Validation PASSED: {label}")
    if warnings and not args.no_warnings:
        print(f"\nWarnings ({len(warnings)}):")
        print(_format_warnings(warnings))
    return 0


if __name__ == "__main__":
    sys.exit(main())
