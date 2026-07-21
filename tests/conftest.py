"""Shared pytest fixtures for agami-core tests."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agami" / "scripts"))


@pytest.fixture(autouse=True)
def _reset_org_cache():
    """The per-process semantic-model cache (ACE-045) is module-global state; isolate every test from it
    (and from a leaked current-org) so one test's cached model never bleeds into the next."""
    try:
        import tools
    except Exception:
        yield
        return
    tools._ORG_CACHE.clear()
    tools._current_org_ctx.set(None)
    tools.resolved_org_id.cache_clear()  # F14: memoized org-id resolver; clear so env/profile changes take
    yield
    tools._ORG_CACHE.clear()
    tools._current_org_ctx.set(None)
    tools.resolved_org_id.cache_clear()


@pytest.fixture(autouse=True)
def _reset_validation_cache():
    """The incremental-curation-validation cache (ACE-046) is module-global too; clear it around
    each test so one test's cached per-area findings can't bleed into the next."""
    try:
        from semantic_model import curate
    except Exception:
        yield
        return
    curate._VALIDATION_CACHE.clear()
    yield
    curate._VALIDATION_CACHE.clear()
