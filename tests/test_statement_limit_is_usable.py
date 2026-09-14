"""`tools.statement_limit_is_usable` — the executor's rule for a per-organisation limit, made public.

An embedder's settings screen needs to refuse at save time what the executor would otherwise decline,
with a warning, on every statement. Before this it had to import a private function to do that, which
breaks silently on a rename. One rule, used by both the provider path and the embedder.
"""

from __future__ import annotations

import threading

import pytest

pytest.importorskip("pydantic")

import execute_sql  # noqa: E402
import tools  # noqa: E402


@pytest.mark.parametrize("key", ["max_rows", "timeout_s"])
@pytest.mark.parametrize("value", [1, 30, 1000, 86_400])
def test_positive_whole_numbers_are_usable(key, value):
    assert tools.statement_limit_is_usable(key, value) is True


@pytest.mark.parametrize("key", ["max_rows", "timeout_s"])
@pytest.mark.parametrize("value", [0, -1, True, False, "30", 2.5, None, [30]])
def test_anything_else_is_not(key, value):
    assert tools.statement_limit_is_usable(key, value) is False


def test_there_is_no_row_cap_ceiling():
    assert tools.statement_limit_is_usable("max_rows", 10**15) is True


def test_a_timeout_the_platform_cannot_arm_is_not_usable():
    edge = int(threading.TIMEOUT_MAX) - execute_sql._SUPERVISOR_SKEW_S
    assert tools.statement_limit_is_usable("timeout_s", edge - 1) is True
    assert tools.statement_limit_is_usable("timeout_s", edge) is False


def test_an_unknown_key_is_the_callers_bug():
    with pytest.raises(ValueError, match="unknown statement limit"):
        tools.statement_limit_is_usable("max_bytes", 10)


@pytest.mark.parametrize(
    "key, value",
    [
        ("max_rows", 500),
        ("max_rows", 0),
        ("max_rows", True),
        ("timeout_s", 45),
        ("timeout_s", int(threading.TIMEOUT_MAX)),
    ],
)
def test_the_provider_path_applies_the_same_rule(key, value):
    """One rule: a value the public check accepts is the value the provider path keeps."""
    kept = tools._provider_limit("acme", key, value)
    assert (kept == value) is tools.statement_limit_is_usable(key, value)
