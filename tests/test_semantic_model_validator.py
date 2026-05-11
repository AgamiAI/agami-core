"""
Tests for plugins/agami/scripts/validate_semantic_model.py.

Three classes of test:
1. Valid models pass cleanly.
2. OSI structural violations (missing required fields, wrong enum values, etc.)
   are caught by Layer 1 (JSON Schema).
3. Agami invariants (unknown agami extension keys, bad type values, dangling
   relationship refs, duplicate names) are caught by Layer 2.

The connect and save-correction skills both call validate() before writing.
This test suite is the contract that says: any of these violations is an
absolute refusal, not a warning.
"""

from __future__ import annotations

import copy
import sys
import yaml
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agami" / "scripts"))

from validate_semantic_model import (  # noqa: E402
    ALLOWED_AGAMI_TYPES,
    validate,
    validate_directory,
    validate_with_warnings,
)


# --- Fixtures ---------------------------------------------------------------

def minimal_valid_model() -> dict:
    """The smallest model that passes both OSI and Agami checks."""
    return {
        "version": "0.1.1",
        "semantic_model": [
            {
                "name": "shop",
                "description": "Test model.",
                "datasets": [
                    {
                        "name": "customers",
                        "source": "shop.public.customers",
                        "primary_key": ["id"],
                        "fields": [
                            {
                                "name": "id",
                                "expression": {
                                    "dialects": [
                                        {"dialect": "ANSI_SQL", "expression": "id"}
                                    ]
                                },
                                "custom_extensions": [
                                    {
                                        "vendor_name": "COMMON",
                                        "data": '{"agami": {"type": "integer"}}',
                                    }
                                ],
                            },
                            {
                                "name": "name",
                                "expression": {
                                    "dialects": [
                                        {"dialect": "ANSI_SQL", "expression": "name"}
                                    ]
                                },
                                "custom_extensions": [
                                    {
                                        "vendor_name": "COMMON",
                                        "data": '{"agami": {"type": "string"}}',
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "name": "orders",
                        "source": "shop.public.orders",
                        "primary_key": ["id"],
                        "fields": [
                            {
                                "name": "id",
                                "expression": {
                                    "dialects": [
                                        {"dialect": "ANSI_SQL", "expression": "id"}
                                    ]
                                },
                            },
                            {
                                "name": "customer_id",
                                "expression": {
                                    "dialects": [
                                        {
                                            "dialect": "ANSI_SQL",
                                            "expression": "customer_id",
                                        }
                                    ]
                                },
                            },
                        ],
                    },
                ],
                "relationships": [
                    {
                        "name": "orders_to_customers",
                        "from": "orders",
                        "to": "customers",
                        "from_columns": ["customer_id"],
                        "to_columns": ["id"],
                    }
                ],
            }
        ],
    }


# --- Class 1: valid models pass --------------------------------------------

def test_minimal_model_passes():
    errors = validate(minimal_valid_model())
    assert errors == [], f"unexpected errors: {errors}"


def test_canonical_osi_tpcds_sample_passes():
    """The official OSI TPC-DS example must validate."""
    sample = REPO_ROOT / "tests" / "integration" / "fixtures" / "sample_osi_tpcds.yaml"
    if not sample.exists():
        pytest.skip("OSI TPC-DS sample fixture not present")
    with sample.open() as f:
        model = yaml.safe_load(f)
    errors = validate(model)
    assert errors == [], f"OSI canonical sample failed validation: {errors}"


def test_model_with_choice_field_passes():
    m = minimal_valid_model()
    m["semantic_model"][0]["datasets"][1]["fields"].append({
        "name": "status",
        "expression": {"dialects": [{"dialect": "ANSI_SQL", "expression": "status"}]},
        "custom_extensions": [{
            "vendor_name": "COMMON",
            "data": '{"agami": {"type": "string", "choice_field": {'
                    '"pending": "Pending", "shipped": "Shipped"}}}',
        }],
    })
    assert validate(m) == []


def test_model_with_metrics_passes():
    m = minimal_valid_model()
    m["semantic_model"][0]["metrics"] = [
        {
            "name": "total_orders",
            "expression": {
                "dialects": [
                    {"dialect": "ANSI_SQL", "expression": "COUNT(orders.id)"}
                ]
            },
        }
    ]
    assert validate(m) == []


def test_model_with_performance_hints_passes():
    m = minimal_valid_model()
    m["semantic_model"][0]["datasets"][1]["custom_extensions"] = [{
        "vendor_name": "COMMON",
        "data": '{"agami": {"performance_hints": {"estimated_row_count": 50000000, '
                '"recommended_filters": [{"column": "placed_at", "reason": "partition"}], '
                '"indexes": [["customer_id"]]}}}',
    }]
    assert validate(m) == []


def test_non_agami_common_extension_passes_through():
    """A COMMON extension without an `agami` key is left alone."""
    m = minimal_valid_model()
    m["semantic_model"][0]["datasets"][0]["custom_extensions"] = [{
        "vendor_name": "COMMON",
        "data": '{"some_other_tool": {"foo": "bar"}}',
    }]
    assert validate(m) == []


def test_dbt_extension_passes_through():
    """Other vendors' extensions are preserved without inspection."""
    m = minimal_valid_model()
    m["semantic_model"][0]["datasets"][0]["custom_extensions"] = [{
        "vendor_name": "DBT",
        "data": '{"materialized": "table", "tags": ["daily"]}',
    }]
    assert validate(m) == []


# --- Class 2: OSI structural violations (Layer 1) --------------------------

def test_missing_version_fails():
    m = minimal_valid_model()
    del m["version"]
    errors = validate(m)
    assert any("version" in e for e in errors), errors


def test_wrong_version_fails():
    m = minimal_valid_model()
    m["version"] = "1.0"
    errors = validate(m)
    assert any("version" in e or "0.1.1" in e for e in errors), errors


def test_missing_semantic_model_fails():
    m = minimal_valid_model()
    del m["semantic_model"]
    errors = validate(m)
    assert any("semantic_model" in e for e in errors), errors


def test_dataset_missing_source_fails():
    m = minimal_valid_model()
    del m["semantic_model"][0]["datasets"][0]["source"]
    errors = validate(m)
    assert any("source" in e for e in errors), errors


def test_field_missing_expression_fails():
    m = minimal_valid_model()
    del m["semantic_model"][0]["datasets"][0]["fields"][0]["expression"]
    errors = validate(m)
    assert any("expression" in e for e in errors), errors


def test_relationship_missing_from_fails():
    m = minimal_valid_model()
    del m["semantic_model"][0]["relationships"][0]["from"]
    errors = validate(m)
    assert any("from" in e for e in errors), errors


def test_invalid_dialect_fails():
    m = minimal_valid_model()
    m["semantic_model"][0]["datasets"][0]["fields"][0]["expression"]["dialects"][0][
        "dialect"
    ] = "MYSQL"  # not in the OSI dialect enum
    errors = validate(m)
    assert any("dialect" in e.lower() or "MYSQL" in e for e in errors), errors


def test_unknown_top_level_field_fails():
    """OSI's JSON schema sets additionalProperties=false at the top level."""
    m = minimal_valid_model()
    m["custom_top_level_thing"] = "nope"
    errors = validate(m)
    assert any("custom_top_level_thing" in e or "additionalProperties" in e.lower()
               for e in errors), errors


def test_unknown_field_inside_dataset_fails():
    m = minimal_valid_model()
    m["semantic_model"][0]["datasets"][0]["bogus"] = "x"
    errors = validate(m)
    assert any("bogus" in e or "additionalProperties" in e.lower() for e in errors), errors


# --- Class 3: Agami invariants (Layer 2) -----------------------------------

def test_unknown_agami_extension_key_rejected():
    """Adding a key not in agami-osi-extensions.md must fail validation."""
    m = minimal_valid_model()
    m["semantic_model"][0]["datasets"][0]["fields"][0]["custom_extensions"] = [{
        "vendor_name": "COMMON",
        "data": '{"agami": {"type": "integer", "secret_new_key": 42}}',
    }]
    errors = validate(m)
    assert any("secret_new_key" in e for e in errors), errors


def test_invalid_agami_type_rejected():
    m = minimal_valid_model()
    m["semantic_model"][0]["datasets"][0]["fields"][0]["custom_extensions"] = [{
        "vendor_name": "COMMON",
        "data": '{"agami": {"type": "real_number"}}',  # not in the allowlist
    }]
    errors = validate(m)
    assert any("real_number" in e or "agami.type" in e for e in errors), errors


@pytest.mark.parametrize("good_type", sorted(ALLOWED_AGAMI_TYPES))
def test_every_allowed_type_passes(good_type):
    m = minimal_valid_model()
    m["semantic_model"][0]["datasets"][0]["fields"][0]["custom_extensions"] = [{
        "vendor_name": "COMMON",
        "data": '{"agami": {"type": "' + good_type + '"}}',
    }]
    assert validate(m) == []


def test_choice_field_with_numeric_key_rejected():
    m = minimal_valid_model()
    m["semantic_model"][0]["datasets"][0]["fields"][0]["custom_extensions"] = [{
        "vendor_name": "COMMON",
        # JSON object keys are always strings — we plant a non-string by using
        # a dict literal at build-time and re-serializing. This validates the
        # validator's defense against weird payloads.
        "data": '{"agami": {"type": "string", "choice_field": {"1": 99}}}',
    }]
    errors = validate(m)
    # The value 99 is numeric — must be flagged
    assert any("choice_field" in e for e in errors), errors


def test_dangling_relationship_from_rejected():
    m = minimal_valid_model()
    m["semantic_model"][0]["relationships"][0]["from"] = "nonexistent_dataset"
    errors = validate(m)
    assert any("nonexistent_dataset" in e for e in errors), errors


def test_dangling_relationship_to_rejected():
    m = minimal_valid_model()
    m["semantic_model"][0]["relationships"][0]["to"] = "ghost_dataset"
    errors = validate(m)
    assert any("ghost_dataset" in e for e in errors), errors


def test_relationship_column_count_mismatch_rejected():
    m = minimal_valid_model()
    m["semantic_model"][0]["relationships"][0]["from_columns"] = ["a", "b"]
    m["semantic_model"][0]["relationships"][0]["to_columns"] = ["x"]
    errors = validate(m)
    assert any("differ in length" in e for e in errors), errors


def test_duplicate_dataset_name_rejected():
    m = minimal_valid_model()
    dup = copy.deepcopy(m["semantic_model"][0]["datasets"][0])
    dup["source"] = "shop.public.dup"
    m["semantic_model"][0]["datasets"].append(dup)
    errors = validate(m)
    assert any("duplicate dataset" in e.lower() for e in errors), errors


def test_duplicate_field_name_within_dataset_rejected():
    m = minimal_valid_model()
    fields = m["semantic_model"][0]["datasets"][0]["fields"]
    fields.append(copy.deepcopy(fields[0]))  # duplicate "id"
    errors = validate(m)
    assert any("duplicate field" in e.lower() for e in errors), errors


def test_duplicate_metric_name_rejected():
    m = minimal_valid_model()
    metric = {
        "name": "total_orders",
        "expression": {"dialects": [{"dialect": "ANSI_SQL", "expression": "COUNT(*)"}]},
    }
    m["semantic_model"][0]["metrics"] = [metric, copy.deepcopy(metric)]
    errors = validate(m)
    assert any("duplicate metric" in e.lower() for e in errors), errors


def test_duplicate_relationship_name_rejected():
    m = minimal_valid_model()
    m["semantic_model"][0]["relationships"].append(
        copy.deepcopy(m["semantic_model"][0]["relationships"][0])
    )
    errors = validate(m)
    assert any("duplicate relationship" in e.lower() for e in errors), errors


def test_malformed_json_in_custom_extension_rejected():
    m = minimal_valid_model()
    m["semantic_model"][0]["datasets"][0]["fields"][0]["custom_extensions"] = [{
        "vendor_name": "COMMON",
        "data": "{not valid json",
    }]
    errors = validate(m)
    assert any("not valid JSON" in e for e in errors), errors


def test_unknown_performance_hint_key_rejected():
    m = minimal_valid_model()
    m["semantic_model"][0]["datasets"][0]["custom_extensions"] = [{
        "vendor_name": "COMMON",
        "data": '{"agami": {"performance_hints": {"estimated_row_count": 1000, '
                '"undocumented_subkey": true}}}',
    }]
    errors = validate(m)
    assert any("undocumented_subkey" in e for e in errors), errors


def test_unknown_introspect_meta_key_rejected():
    m = minimal_valid_model()
    m["semantic_model"][0]["custom_extensions"] = [{
        "vendor_name": "COMMON",
        "data": '{"agami": {"introspect_meta": {"introspected_at": "2026-05-06T12:00:00Z", '
                '"surprising_field": "bad"}}}',
    }]
    errors = validate(m)
    assert any("surprising_field" in e for e in errors), errors


# --- The contract: validate() never raises ---------------------------------

def test_validate_does_not_raise_on_garbage():
    """Any bad input produces errors, never an exception."""
    cases = [
        {},
        {"version": "0.1.1"},  # missing semantic_model
        {"version": "0.1.1", "semantic_model": "not an array"},
        {"version": 12345},
        {"version": "0.1.1", "semantic_model": []},  # empty array
    ]
    for c in cases:
        try:
            errors = validate(c)
            # If it returned, the contract is honored — errors should be non-empty
            # for genuinely invalid inputs.
            assert isinstance(errors, list)
        except Exception as e:  # noqa: BLE001
            pytest.fail(f"validate() raised on input {c!r}: {e}")


def test_validate_with_warnings_returns_two_lists():
    errors, warnings = validate_with_warnings(minimal_valid_model())
    assert isinstance(errors, list)
    assert isinstance(warnings, list)


# --- Directory mode (per-schema layout) ------------------------------------

def _schema_yaml(profile: str, schema_name: str, datasets: list[dict]) -> dict:
    """Build a standalone OSI doc for one schema."""
    return {
        "version": "0.1.1",
        "semantic_model": [
            {
                "name": profile,
                "description": f"{schema_name} schema",
                "custom_extensions": [
                    {
                        "vendor_name": "COMMON",
                        "data": (
                            '{"agami": {"profile": "' + profile + '", '
                            '"db_type": "postgres", '
                            '"schema": "' + schema_name + '"}}'
                        ),
                    }
                ],
                "datasets": datasets,
            }
        ],
    }


def _basic_dataset(name: str, schema: str) -> dict:
    return {
        "name": name,
        "source": f"db.{schema}.{name}",
        "primary_key": ["id"],
        "fields": [
            {
                "name": "id",
                "expression": {
                    "dialects": [{"dialect": "ANSI_SQL", "expression": "id"}]
                },
                "custom_extensions": [
                    {
                        "vendor_name": "COMMON",
                        "data": '{"agami": {"type": "integer"}}',
                    }
                ],
            }
        ],
    }


def _write_yaml(path: Path, doc: dict) -> None:
    with path.open("w") as f:
        yaml.safe_dump(doc, f)


def _build_profile_dir(tmp_path: Path) -> Path:
    """Two-schema profile: public.users + analytics.events, plus a cross-schema rel."""
    pdir = tmp_path / "finbud"
    pdir.mkdir()

    public = _schema_yaml(
        "finbud",
        "public",
        [_basic_dataset("users", "public")],
    )
    analytics = _schema_yaml(
        "finbud",
        "analytics",
        [_basic_dataset("events", "analytics")],
    )

    _write_yaml(pdir / "public.yaml", public)
    _write_yaml(pdir / "analytics.yaml", analytics)

    index = {
        "version": "0.1.1",
        "profile": "finbud",
        "db_type": "postgres",
        "schemas": [
            {"name": "public", "file": "public.yaml", "table_count": 1},
            {"name": "analytics", "file": "analytics.yaml", "table_count": 1},
        ],
        "cross_schema_relationships": [
            {
                "name": "events_to_users",
                "from": "analytics.events",
                "to": "public.users",
                "from_columns": ["id"],
                "to_columns": ["id"],
            }
        ],
        "introspect_meta": {
            "introspected_at": "2026-05-08T00:00:00Z",
            "tier": "cli",
            "source_db_version": "PostgreSQL 16.2",
        },
    }
    _write_yaml(pdir / "index.yaml", index)
    return pdir


def test_directory_minimal_passes(tmp_path):
    pdir = _build_profile_dir(tmp_path)
    errors, _ = validate_directory(pdir)
    assert errors == [], errors


def test_directory_missing_index_fails(tmp_path):
    pdir = tmp_path / "ghost"
    pdir.mkdir()
    errors, _ = validate_directory(pdir)
    assert any("index.yaml" in e for e in errors), errors


def test_directory_missing_schema_file_fails(tmp_path):
    pdir = _build_profile_dir(tmp_path)
    (pdir / "analytics.yaml").unlink()
    errors, _ = validate_directory(pdir)
    assert any("analytics.yaml" in e and "missing" in e for e in errors), errors


def test_directory_cross_schema_endpoint_unqualified_rejected(tmp_path):
    pdir = _build_profile_dir(tmp_path)
    with (pdir / "index.yaml").open() as f:
        idx = yaml.safe_load(f)
    idx["cross_schema_relationships"][0]["from"] = "events"  # missing schema prefix
    _write_yaml(pdir / "index.yaml", idx)
    errors, _ = validate_directory(pdir)
    assert any("must be qualified" in e for e in errors), errors


def test_directory_cross_schema_endpoint_unknown_dataset_rejected(tmp_path):
    pdir = _build_profile_dir(tmp_path)
    with (pdir / "index.yaml").open() as f:
        idx = yaml.safe_load(f)
    idx["cross_schema_relationships"][0]["from"] = "analytics.ghost_table"
    _write_yaml(pdir / "index.yaml", idx)
    errors, _ = validate_directory(pdir)
    assert any("ghost_table" in e for e in errors), errors


def test_directory_schema_yaml_with_mismatched_schema_name_rejected(tmp_path):
    pdir = _build_profile_dir(tmp_path)
    with (pdir / "public.yaml").open() as f:
        m = yaml.safe_load(f)
    m["semantic_model"][0]["custom_extensions"][0]["data"] = (
        '{"agami": {"profile": "finbud", "db_type": "postgres", "schema": "wrong"}}'
    )
    _write_yaml(pdir / "public.yaml", m)
    errors, _ = validate_directory(pdir)
    assert any("doesn't match" in e for e in errors), errors


def test_directory_index_unknown_top_key_rejected(tmp_path):
    pdir = _build_profile_dir(tmp_path)
    with (pdir / "index.yaml").open() as f:
        idx = yaml.safe_load(f)
    idx["surprise_field"] = "nope"
    _write_yaml(pdir / "index.yaml", idx)
    errors, _ = validate_directory(pdir)
    assert any("surprise_field" in e for e in errors), errors


def test_directory_index_missing_required_key_rejected(tmp_path):
    pdir = _build_profile_dir(tmp_path)
    with (pdir / "index.yaml").open() as f:
        idx = yaml.safe_load(f)
    del idx["db_type"]
    _write_yaml(pdir / "index.yaml", idx)
    errors, _ = validate_directory(pdir)
    assert any("db_type" in e for e in errors), errors


def test_directory_schema_yaml_with_invalid_osi_rejected(tmp_path):
    """A broken inner schema yaml surfaces with file prefix in the error."""
    pdir = _build_profile_dir(tmp_path)
    with (pdir / "public.yaml").open() as f:
        m = yaml.safe_load(f)
    del m["semantic_model"][0]["datasets"][0]["fields"][0]["expression"]
    _write_yaml(pdir / "public.yaml", m)
    errors, _ = validate_directory(pdir)
    assert any("public.yaml" in e and "expression" in e for e in errors), errors


# --- Directory mode v1.3 (per-table layout) --------------------------------

def _per_table_yaml(profile: str, schema_name: str, table_name: str) -> dict:
    """Build a standalone OSI doc for a single table."""
    return {
        "version": "0.1.1",
        "semantic_model": [
            {
                "name": profile,
                "custom_extensions": [
                    {
                        "vendor_name": "COMMON",
                        "data": (
                            '{"agami": {"profile": "' + profile + '", '
                            '"db_type": "postgres", '
                            '"schema": "' + schema_name + '", '
                            '"table": "' + table_name + '"}}'
                        ),
                    }
                ],
                "datasets": [_basic_dataset(table_name, schema_name)],
            }
        ],
    }


def _build_v13_profile_dir(tmp_path: Path) -> Path:
    """Two schemas, three tables total, one cross-schema rel — per-table layout."""
    pdir = tmp_path / "finbud_v13"
    pdir.mkdir()

    # public/ schema directory
    public_dir = pdir / "public"
    public_dir.mkdir()
    _write_yaml(public_dir / "users.yaml", _per_table_yaml("finbud", "public", "users"))
    _write_yaml(public_dir / "orders.yaml", _per_table_yaml("finbud", "public", "orders"))
    _write_yaml(public_dir / "_schema.yaml", {
        "version": "0.1.1",
        "schema": "public",
        "description": "Core OLTP",
        "tables": [
            {"name": "users",  "file": "users.yaml",  "primary_key": ["id"]},
            {"name": "orders", "file": "orders.yaml", "primary_key": ["id"]},
        ],
        "relationships": [
            {
                "name": "orders_to_users",
                "from": "orders",
                "to": "users",
                "from_columns": ["id"],
                "to_columns": ["id"],
            }
        ],
    })

    # analytics/ schema directory
    analytics_dir = pdir / "analytics"
    analytics_dir.mkdir()
    _write_yaml(analytics_dir / "events.yaml", _per_table_yaml("finbud", "analytics", "events"))
    _write_yaml(analytics_dir / "_schema.yaml", {
        "version": "0.1.1",
        "schema": "analytics",
        "description": "Aggregated analytics",
        "tables": [
            {"name": "events", "file": "events.yaml", "primary_key": ["id"]},
        ],
    })

    # index.yaml — points each schema at its _schema.yaml
    _write_yaml(pdir / "index.yaml", {
        "version": "0.1.1",
        "profile": "finbud",
        "db_type": "postgres",
        "schemas": [
            {"name": "public",    "file": "public/_schema.yaml",    "table_count": 2},
            {"name": "analytics", "file": "analytics/_schema.yaml", "table_count": 1},
        ],
        "cross_schema_relationships": [
            {
                "name": "events_to_users",
                "from": "analytics.events",
                "to": "public.users",
                "from_columns": ["id"],
                "to_columns": ["id"],
            }
        ],
    })
    return pdir


def test_directory_v13_per_table_passes(tmp_path):
    pdir = _build_v13_profile_dir(tmp_path)
    errors, _ = validate_directory(pdir)
    assert errors == [], errors


def test_directory_v13_missing_table_file_fails(tmp_path):
    pdir = _build_v13_profile_dir(tmp_path)
    (pdir / "public" / "orders.yaml").unlink()
    errors, _ = validate_directory(pdir)
    assert any("orders.yaml" in e and "missing" in e for e in errors), errors


def test_directory_v13_table_with_wrong_schema_extension_rejected(tmp_path):
    pdir = _build_v13_profile_dir(tmp_path)
    with (pdir / "public" / "orders.yaml").open() as f:
        m = yaml.safe_load(f)
    m["semantic_model"][0]["custom_extensions"][0]["data"] = (
        '{"agami": {"profile": "finbud", "db_type": "postgres", '
        '"schema": "wrong", "table": "orders"}}'
    )
    _write_yaml(pdir / "public" / "orders.yaml", m)
    errors, _ = validate_directory(pdir)
    assert any("doesn't match" in e and "schema" in e.lower() for e in errors), errors


def test_directory_v13_table_with_wrong_table_extension_rejected(tmp_path):
    pdir = _build_v13_profile_dir(tmp_path)
    with (pdir / "public" / "orders.yaml").open() as f:
        m = yaml.safe_load(f)
    m["semantic_model"][0]["custom_extensions"][0]["data"] = (
        '{"agami": {"profile": "finbud", "db_type": "postgres", '
        '"schema": "public", "table": "wrong_name"}}'
    )
    _write_yaml(pdir / "public" / "orders.yaml", m)
    errors, _ = validate_directory(pdir)
    assert any("doesn't match" in e and "table" in e.lower() for e in errors), errors


def test_directory_v13_per_table_yaml_with_two_datasets_rejected(tmp_path):
    """Each <table>.yaml should hold exactly one dataset."""
    pdir = _build_v13_profile_dir(tmp_path)
    with (pdir / "public" / "orders.yaml").open() as f:
        m = yaml.safe_load(f)
    extra = copy.deepcopy(m["semantic_model"][0]["datasets"][0])
    extra["name"] = "smuggled_in"
    extra["source"] = "db.public.smuggled_in"
    m["semantic_model"][0]["datasets"].append(extra)
    _write_yaml(pdir / "public" / "orders.yaml", m)
    errors, _ = validate_directory(pdir)
    assert any("expected exactly 1 dataset" in e for e in errors), errors


def test_directory_v13_schema_yaml_with_unknown_top_key_rejected(tmp_path):
    pdir = _build_v13_profile_dir(tmp_path)
    with (pdir / "public" / "_schema.yaml").open() as f:
        s = yaml.safe_load(f)
    s["surprise"] = "nope"
    _write_yaml(pdir / "public" / "_schema.yaml", s)
    errors, _ = validate_directory(pdir)
    assert any("surprise" in e for e in errors), errors


def test_directory_v13_within_schema_relationship_dangling_rejected(tmp_path):
    """A relationship pointing at a non-existent table fails."""
    pdir = _build_v13_profile_dir(tmp_path)
    with (pdir / "public" / "_schema.yaml").open() as f:
        s = yaml.safe_load(f)
    s["relationships"][0]["to"] = "nonexistent_table"
    _write_yaml(pdir / "public" / "_schema.yaml", s)
    errors, _ = validate_directory(pdir)
    assert any("nonexistent_table" in e for e in errors), errors


def test_directory_v13_cross_schema_relationship_passes(tmp_path):
    """Cross-schema relationships in index.yaml resolve to merged datasets."""
    pdir = _build_v13_profile_dir(tmp_path)
    errors, _ = validate_directory(pdir)
    # The cross-schema rel in _build_v13_profile_dir should pass.
    assert errors == [], errors


def test_directory_v13_cross_schema_relationship_unknown_dataset_rejected(tmp_path):
    pdir = _build_v13_profile_dir(tmp_path)
    with (pdir / "index.yaml").open() as f:
        idx = yaml.safe_load(f)
    idx["cross_schema_relationships"][0]["from"] = "analytics.ghost_dataset"
    _write_yaml(pdir / "index.yaml", idx)
    errors, _ = validate_directory(pdir)
    assert any("ghost_dataset" in e for e in errors), errors


def test_directory_mixed_v12_and_v13_supported(tmp_path):
    """A profile dir can mix layouts during incremental migration."""
    pdir = tmp_path / "mixed"
    pdir.mkdir()

    # public/ in v1.3 layout
    public_dir = pdir / "public"
    public_dir.mkdir()
    _write_yaml(public_dir / "users.yaml", _per_table_yaml("mixed", "public", "users"))
    _write_yaml(public_dir / "_schema.yaml", {
        "version": "0.1.1",
        "schema": "public",
        "tables": [{"name": "users", "file": "users.yaml", "primary_key": ["id"]}],
    })

    # analytics in v1.2 layout (single file)
    _write_yaml(pdir / "analytics.yaml", _schema_yaml(
        "mixed", "analytics", [_basic_dataset("events", "analytics")]
    ))

    _write_yaml(pdir / "index.yaml", {
        "version": "0.1.1",
        "profile": "mixed",
        "db_type": "postgres",
        "schemas": [
            {"name": "public",    "file": "public/_schema.yaml"},
            {"name": "analytics", "file": "analytics.yaml"},
        ],
    })
    errors, _ = validate_directory(pdir)
    assert errors == [], errors


# --- Class 4: Trust-layer extensions ---------------------------------------
#
# Mirrors plugins/agami/shared/agami-osi-extensions.md → Trust-layer extensions.
# The trust spine introduces:
#   - Universal keys on field / dataset / relationship / metric / named_filter:
#     confidence, signal_breakdown, review_state, origin, signed_off_*
#   - Metric-level definitional keys: definition_prose, assumptions, excludes
#   - Model-level named_filters array (with its own per-filter Rule 1)
# And the validator enforces Hard Rules #7 / #8 / #9 / #10.


def _agami_ext(payload: dict) -> dict:
    """Helper: build a custom_extensions entry from an agami payload dict."""
    import json
    return {"vendor_name": "COMMON", "data": json.dumps({"agami": payload})}


def _approved(by: str = "ashwin@agami.ai", role: str | None = None,
              at: str = "2026-05-10T14:23:11Z") -> dict:
    """Helper: build a Rule 2 approved trust block (no role required)."""
    out = {"review_state": "approved", "signed_off_by": by, "signed_off_at": at}
    if role is not None:
        out["signed_off_role"] = role
    return out


def _approved_rule1(by: str = "jane.smith@example.com", role: str = "cfo",
                    at: str = "2026-03-15T10:00:00Z") -> dict:
    """Helper: full Rule 1 sign-off block."""
    return {
        "review_state": "approved",
        "signed_off_by": by,
        "signed_off_at": at,
        "signed_off_role": role,
    }


def test_relationship_with_trust_layer_passes():
    m = minimal_valid_model()
    m["semantic_model"][0]["relationships"][0]["custom_extensions"] = [_agami_ext({
        "confidence": 0.62,
        "review_state": "unreviewed",
        "origin": "introspect_heuristic",
        "signed_off_by": None,
        "signed_off_at": None,
        "signed_off_role": None,
        "signal_breakdown": {
            "fk_declared": False,
            "unique_index_match": True,
            "column_type_match": True,
            "column_name_similarity": 1.0,
        },
    })]
    assert validate(m) == []


def test_relationship_approved_with_full_signoff_passes():
    m = minimal_valid_model()
    m["semantic_model"][0]["relationships"][0]["custom_extensions"] = [_agami_ext({
        "confidence": 1.0,
        "origin": "fk",
        **_approved(by="agami_introspect_v1", role="system"),
    })]
    assert validate(m) == []


def test_field_with_trust_layer_passes():
    m = minimal_valid_model()
    m["semantic_model"][0]["datasets"][0]["fields"][0]["custom_extensions"] = [_agami_ext({
        "type": "integer",
        "confidence": 0.95,
        "origin": "fk",
        **_approved(by="agami_introspect_v1", role="system"),
    })]
    assert validate(m) == []


def test_dataset_with_trust_layer_passes():
    m = minimal_valid_model()
    m["semantic_model"][0]["datasets"][0]["custom_extensions"] = [_agami_ext({
        "performance_hints": {"estimated_row_count": 1000},
        "confidence": 0.9,
        "review_state": "unreviewed",
        "signed_off_by": None,
        "signed_off_at": None,
    })]
    assert validate(m) == []


# --- Confidence number range ------------------------------------------------

def test_confidence_above_1_rejected():
    m = minimal_valid_model()
    m["semantic_model"][0]["relationships"][0]["custom_extensions"] = [_agami_ext({
        "confidence": 1.5,
        "review_state": "unreviewed",
    })]
    errors = validate(m)
    assert any("confidence 1.5 outside [0, 1]" in e for e in errors), errors


def test_confidence_below_0_rejected():
    m = minimal_valid_model()
    m["semantic_model"][0]["relationships"][0]["custom_extensions"] = [_agami_ext({
        "confidence": -0.1,
        "review_state": "unreviewed",
    })]
    errors = validate(m)
    assert any("outside [0, 1]" in e for e in errors), errors


def test_confidence_non_number_rejected():
    m = minimal_valid_model()
    m["semantic_model"][0]["relationships"][0]["custom_extensions"] = [_agami_ext({
        "confidence": "high",
        "review_state": "unreviewed",
    })]
    errors = validate(m)
    assert any("confidence must be a number" in e for e in errors), errors


def test_confidence_boolean_rejected():
    """A bare True looks numeric in Python (1.0) — explicitly reject it."""
    m = minimal_valid_model()
    m["semantic_model"][0]["relationships"][0]["custom_extensions"] = [_agami_ext({
        "confidence": True,
        "review_state": "unreviewed",
    })]
    errors = validate(m)
    assert any("confidence must be a number" in e for e in errors), errors


# --- Enum violations --------------------------------------------------------

def test_invalid_review_state_rejected():
    m = minimal_valid_model()
    m["semantic_model"][0]["relationships"][0]["custom_extensions"] = [_agami_ext({
        "review_state": "in_review",
    })]
    errors = validate(m)
    assert any("review_state 'in_review' invalid" in e for e in errors), errors


def test_invalid_origin_rejected():
    m = minimal_valid_model()
    m["semantic_model"][0]["relationships"][0]["custom_extensions"] = [_agami_ext({
        "origin": "magic",
        "review_state": "unreviewed",
    })]
    errors = validate(m)
    assert any("origin 'magic' invalid" in e for e in errors), errors


def test_invalid_signoff_role_rejected():
    m = minimal_valid_model()
    m["semantic_model"][0]["metrics"] = [{
        "name": "revenue",
        "expression": {"dialects": [{"dialect": "ANSI_SQL", "expression": "SUM(orders.id)"}]},
        "custom_extensions": [_agami_ext({
            "definition_prose": "Some prose.",
            "review_state": "approved",
            "signed_off_by": "x@y.com",
            "signed_off_at": "2026-05-10T00:00:00Z",
            "signed_off_role": "ceo",  # not in enum
        })],
    }]
    errors = validate(m)
    assert any("signed_off_role 'ceo' invalid" in e for e in errors), errors


# --- Hard Rule #9 (Rule 2): approved entry needs by + at -------------------

def test_rule2_approved_relationship_missing_signoff_rejected():
    m = minimal_valid_model()
    m["semantic_model"][0]["relationships"][0]["custom_extensions"] = [_agami_ext({
        "review_state": "approved",
        # missing signed_off_by, signed_off_at
    })]
    errors = validate(m)
    assert any("requires non-null signed_off_by" in e for e in errors), errors
    assert any("requires non-null signed_off_at" in e for e in errors), errors


def test_rule2_approved_relationship_role_optional():
    """Non-Rule-1 entries don't require signed_off_role."""
    m = minimal_valid_model()
    m["semantic_model"][0]["relationships"][0]["custom_extensions"] = [_agami_ext({
        **_approved(role=None),  # by + at, no role
    })]
    assert validate(m) == []


# --- Hard Rule #8 (Rule 1): metrics + named_filters -------------------------

def test_rule1_approved_metric_missing_role_rejected():
    m = minimal_valid_model()
    m["semantic_model"][0]["metrics"] = [{
        "name": "revenue",
        "expression": {"dialects": [{"dialect": "ANSI_SQL", "expression": "SUM(orders.id)"}]},
        "custom_extensions": [_agami_ext({
            "definition_prose": "Net revenue.",
            "review_state": "approved",
            "signed_off_by": "jane@example.com",
            "signed_off_at": "2026-03-15T10:00:00Z",
            # missing signed_off_role — Rule 1 violation
        })],
    }]
    errors = validate(m)
    assert any("Rule 1" in e and "signed_off_role" in e for e in errors), errors


def test_rule1_approved_metric_missing_definition_prose_rejected():
    m = minimal_valid_model()
    m["semantic_model"][0]["metrics"] = [{
        "name": "revenue",
        "expression": {"dialects": [{"dialect": "ANSI_SQL", "expression": "SUM(orders.id)"}]},
        "custom_extensions": [_agami_ext({
            **_approved_rule1(),
            # no definition_prose
        })],
    }]
    errors = validate(m)
    assert any("definition_prose" in e and "Rule 1" in e for e in errors), errors


def test_rule1_approved_metric_with_full_signoff_passes():
    m = minimal_valid_model()
    m["semantic_model"][0]["metrics"] = [{
        "name": "revenue",
        "expression": {"dialects": [{"dialect": "ANSI_SQL", "expression": "SUM(orders.id)"}]},
        "custom_extensions": [_agami_ext({
            "definition_prose": "Net revenue, gross of refunds.",
            "assumptions": ["FX is invoice-date USD"],
            "excludes": ["trial revenue"],
            "confidence": 1.0,
            "origin": "human_authored",
            **_approved_rule1(),
        })],
    }]
    assert validate(m) == []


# --- Hard Rule #10: sign-off coherence (unreviewed/rejected → null) --------

def test_unreviewed_with_signoff_by_rejected():
    m = minimal_valid_model()
    m["semantic_model"][0]["relationships"][0]["custom_extensions"] = [_agami_ext({
        "review_state": "unreviewed",
        "signed_off_by": "leftover@example.com",  # should be null
    })]
    errors = validate(m)
    assert any("review_state=unreviewed requires signed_off_by=null" in e for e in errors), errors


def test_rejected_with_signoff_role_rejected():
    m = minimal_valid_model()
    m["semantic_model"][0]["relationships"][0]["custom_extensions"] = [_agami_ext({
        "review_state": "rejected",
        "signed_off_role": "engineer",  # should be null
    })]
    errors = validate(m)
    assert any("review_state=rejected requires signed_off_role=null" in e for e in errors), errors


def test_stale_preserves_signoff():
    """Stale entries keep their previous sign-off (audit trail)."""
    m = minimal_valid_model()
    m["semantic_model"][0]["relationships"][0]["custom_extensions"] = [_agami_ext({
        "review_state": "stale",
        "signed_off_by": "previous.approver@example.com",
        "signed_off_at": "2026-04-01T00:00:00Z",
        "signed_off_role": "engineer",
    })]
    assert validate(m) == []


# --- Metric-level extensions -----------------------------------------------

def test_metric_with_definition_keys_passes():
    m = minimal_valid_model()
    m["semantic_model"][0]["metrics"] = [{
        "name": "revenue",
        "expression": {"dialects": [{"dialect": "ANSI_SQL", "expression": "SUM(orders.id)"}]},
        "custom_extensions": [_agami_ext({
            "definition_prose": "Some prose.",
            "assumptions": ["a1", "a2"],
            "excludes": ["e1"],
            "review_state": "unreviewed",
        })],
    }]
    assert validate(m) == []


def test_metric_unknown_extension_key_rejected():
    m = minimal_valid_model()
    m["semantic_model"][0]["metrics"] = [{
        "name": "revenue",
        "expression": {"dialects": [{"dialect": "ANSI_SQL", "expression": "SUM(orders.id)"}]},
        "custom_extensions": [_agami_ext({
            "definition_prose": "x",
            "review_state": "unreviewed",
            "made_up_key": "bad",
        })],
    }]
    errors = validate(m)
    assert any("unknown agami key" in e and "made_up_key" in e for e in errors), errors


def test_metric_assumptions_must_be_array():
    m = minimal_valid_model()
    m["semantic_model"][0]["metrics"] = [{
        "name": "revenue",
        "expression": {"dialects": [{"dialect": "ANSI_SQL", "expression": "SUM(orders.id)"}]},
        "custom_extensions": [_agami_ext({
            "assumptions": "single string is wrong",
            "review_state": "unreviewed",
        })],
    }]
    errors = validate(m)
    assert any("assumptions must be an array" in e for e in errors), errors


# --- Named filters (model-level extension) ---------------------------------

def test_named_filters_valid_passes():
    m = minimal_valid_model()
    m["semantic_model"][0]["custom_extensions"] = [_agami_ext({
        "profile": "default",
        "named_filters": [
            {
                "name": "active_customer",
                "expression": "customers.is_active = true",
                "definition_prose": "Currently active.",
                "synonyms": ["active customers"],
                "confidence": 1.0,
                "origin": "human_authored",
                **_approved_rule1(role="data_lead"),
            }
        ],
    })]
    assert validate(m) == []


def test_named_filter_missing_expression_rejected():
    m = minimal_valid_model()
    m["semantic_model"][0]["custom_extensions"] = [_agami_ext({
        "named_filters": [
            {"name": "no_expr", "review_state": "unreviewed"}
        ],
    })]
    errors = validate(m)
    assert any("missing required key 'expression'" in e for e in errors), errors


def test_named_filter_duplicate_names_rejected():
    m = minimal_valid_model()
    m["semantic_model"][0]["custom_extensions"] = [_agami_ext({
        "named_filters": [
            {"name": "active", "expression": "x = 1", "review_state": "unreviewed"},
            {"name": "active", "expression": "y = 2", "review_state": "unreviewed"},
        ],
    })]
    errors = validate(m)
    assert any("duplicate name 'active'" in e for e in errors), errors


def test_named_filter_rule1_approved_without_role_rejected():
    m = minimal_valid_model()
    m["semantic_model"][0]["custom_extensions"] = [_agami_ext({
        "named_filters": [{
            "name": "active",
            "expression": "x = 1",
            "definition_prose": "Some prose.",
            "review_state": "approved",
            "signed_off_by": "x@y.com",
            "signed_off_at": "2026-05-10T00:00:00Z",
            # missing signed_off_role — Rule 1
        }],
    })]
    errors = validate(m)
    assert any("Rule 1" in e and "signed_off_role" in e for e in errors), errors


def test_named_filter_unknown_key_rejected():
    m = minimal_valid_model()
    m["semantic_model"][0]["custom_extensions"] = [_agami_ext({
        "named_filters": [{
            "name": "active", "expression": "x = 1",
            "review_state": "unreviewed",
            "made_up": "bad",
        }],
    })]
    errors = validate(m)
    assert any("named_filters[0]" in e and "made_up" in e for e in errors), errors


def test_model_level_trust_keys_rejected():
    """Trust-layer keys on the model itself are not allowed (model isn't reviewable)."""
    m = minimal_valid_model()
    m["semantic_model"][0]["custom_extensions"] = [_agami_ext({
        "profile": "default",
        "confidence": 0.9,  # trust keys not allowed at model level
    })]
    errors = validate(m)
    assert any("unknown agami key" in e and "confidence" in e for e in errors), errors


# --- not_applicable / no_description (fields with empty descriptions) -------

def test_field_with_no_description_marked_not_applicable_passes():
    """Empty-description field stamped with not_applicable + no_description is
    the canonical shape. The introspect step uses this for fields where neither
    a DBA comment nor an LLM-proposed description was available."""
    m = minimal_valid_model()
    f = m["semantic_model"][0]["datasets"][0]["fields"][0]
    f["description"] = ""
    f["custom_extensions"] = [_agami_ext({
        "type": "string",
        "confidence": None,
        "review_state": "not_applicable",
        "origin": "no_description",
        "signed_off_by": None,
        "signed_off_at": None,
        "signed_off_role": None,
    })]
    assert validate(m) == []


def test_not_applicable_with_real_description_rejected():
    """not_applicable on a field with actual description content is incoherent
    — that's reviewable content, not a skip case. Validator must catch."""
    m = minimal_valid_model()
    f = m["semantic_model"][0]["datasets"][0]["fields"][0]
    f["description"] = "Customer status (active / churned / trial)"
    f["custom_extensions"] = [_agami_ext({
        "type": "string",
        "confidence": None,
        "review_state": "not_applicable",
        "origin": "no_description",
        "signed_off_by": None,
        "signed_off_at": None,
        "signed_off_role": None,
    })]
    errors = validate(m)
    assert any("not_applicable" in e and "description" in e for e in errors), errors


def test_not_applicable_without_no_description_origin_rejected():
    """not_applicable must pair with origin=no_description. Any other origin
    is misuse of the escape hatch (the prior bug: introspect writing
    review_state=approved + origin=introspect_heuristic on empty-description
    fields would now have been routed to not_applicable but with a wrong
    origin like llm_suggested — catch that)."""
    m = minimal_valid_model()
    f = m["semantic_model"][0]["datasets"][0]["fields"][0]
    f["description"] = ""
    f["custom_extensions"] = [_agami_ext({
        "type": "string",
        "confidence": None,
        "review_state": "not_applicable",
        "origin": "llm_suggested",  # wrong — must be no_description
        "signed_off_by": None,
        "signed_off_at": None,
        "signed_off_role": None,
    })]
    errors = validate(m)
    assert any(
        "not_applicable" in e and "no_description" in e
        for e in errors
    ), errors


def test_confidence_null_allowed_for_not_applicable():
    """For not_applicable fields, confidence is null (there's nothing to score)."""
    m = minimal_valid_model()
    f = m["semantic_model"][0]["datasets"][0]["fields"][0]
    f["description"] = ""
    f["custom_extensions"] = [_agami_ext({
        "type": "string",
        "confidence": None,
        "review_state": "not_applicable",
        "origin": "no_description",
        "signed_off_by": None,
        "signed_off_at": None,
        "signed_off_role": None,
    })]
    assert validate(m) == []
