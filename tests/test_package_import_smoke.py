"""agami-core is an importable package with flat module names.

Consumers import ONE package with **flat top-level names** and **no `sys.path` manipulation**.
This test imports the four library surfaces without touching `sys.path` at all — if it passes,
the package is installed and the names resolve the way consumers depend on.
"""

from __future__ import annotations

import sys

import pytest

# These are pure-stdlib library modules — always importable from the installed package.
# semantic_model needs the [model] extra (pydantic/sqlglot/pyyaml); skip cleanly without it
# so a bare `pip install -e packages/agami-core` (no extra) still runs this file.


def test_flat_modules_import_without_syspath():
    """from mcp_harness import TOOLS / import execute_sql / import agami_paths — flat, no sys.path."""
    before = list(sys.path)

    import agami_paths  # noqa: F401
    import execute_sql  # noqa: F401
    from mcp_harness import TOOLS

    # Importing the package must not smuggle in a sys.path hack.
    assert sys.path == before, "importing the package mutated sys.path"

    assert isinstance(TOOLS, dict) and TOOLS, "TOOLS registry should be a non-empty map"
    # execute_sql is the single execution chokepoint — it exposes a CLI main().
    assert hasattr(execute_sql, "main")


def test_semantic_model_imports_with_model_extra():
    """import semantic_model (+ a representative submodule) when the [model] extra is present."""
    pytest.importorskip("pydantic")
    pytest.importorskip("sqlglot")

    import semantic_model  # noqa: F401
    from semantic_model import loader, runtime, units  # noqa: F401


def test_modules_resolve_from_installed_package_not_scripts_dir():
    """The library no longer lives under plugins/agami/scripts/ — it resolves from the package."""
    import execute_sql
    import mcp_harness

    for mod in (mcp_harness, execute_sql):
        assert "plugins/agami/scripts" not in (mod.__file__ or ""), (
            f"{mod.__name__} still resolves from the old scripts dir: {mod.__file__}"
        )
