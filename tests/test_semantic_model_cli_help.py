"""`sm --help` and `sm <verb> --help` print usage and exit 0, for every verb.

argparse expands `%` in help text as a format string, so one bare `%` in any verb's help made the
top-level `sm --help` raise a TypeError instead of printing. This walks the real parser, so a verb
added later is covered without editing the test."""

from __future__ import annotations

import argparse
import contextlib
import io

import pytest

pytest.importorskip("pydantic")
pytest.importorskip("sqlglot")
pytest.importorskip("yaml")

from semantic_model import cli  # noqa: E402


def _command_paths(parser: argparse.ArgumentParser, path: tuple[str, ...] = ()) -> list[tuple[str, ...]]:
    paths = [path]
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            for name, sub in action.choices.items():
                paths.extend(_command_paths(sub, (*path, name)))
    return paths


_PATHS = _command_paths(cli.build_parser())


def test_the_walk_finds_the_verbs():
    # Guards the walk itself: an empty list would make every case below vacuous.
    assert len(_PATHS) > 50
    assert ("overview",) in _PATHS


@pytest.mark.parametrize("path", _PATHS, ids=lambda p: " ".join(p) or "top-level")
def test_help_prints_and_exits_zero(path):
    out = io.StringIO()
    with contextlib.redirect_stdout(out), pytest.raises(SystemExit) as exit_info:
        cli.main([*path, "--help"])
    assert exit_info.value.code == 0
    assert "usage:" in out.getvalue()
