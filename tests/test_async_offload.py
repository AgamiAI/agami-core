"""ACE-048: run_blocking offloads a sync callable (with args AND kwargs) to a worker thread, and the
uvicorn factory import string resolves so `WORKERS=N` can fork worker processes."""

from __future__ import annotations

import sys
import threading
from pathlib import Path

import pytest

pytest.importorskip("anyio")

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agami" / "scripts"))
# async_offload + mcp_http live in the packaged src tree, not the plugin scripts dir — add it so
# this test imports them when run in isolation (matches the other server tests, e.g. test_admin).
PKG_SRC = REPO_ROOT / "packages" / "agami-core" / "src"
if str(PKG_SRC) not in sys.path:
    sys.path.insert(0, str(PKG_SRC))

import anyio  # noqa: E402
from async_offload import run_blocking  # noqa: E402


def test_run_blocking_offloads_to_a_worker_thread_with_args_and_kwargs():
    main_thread = threading.get_ident()
    seen: dict[str, int] = {}

    def work(a, b, *, c):
        seen["thread"] = threading.get_ident()
        return (a, b, c)

    async def _call():
        # positional AND keyword args must both reach the callable (to_thread is positional-only, hence
        # the functools.partial inside run_blocking).
        return await run_blocking(work, 1, 2, c=3)

    result = anyio.run(_call)
    assert result == (1, 2, 3)
    assert seen["thread"] != main_thread  # actually ran off the event-loop thread


def test_run_blocking_propagates_the_callables_exception():
    async def _call():
        return await run_blocking(lambda: 1 // 0)

    with pytest.raises(ZeroDivisionError):
        anyio.run(_call)


def test_uvicorn_factory_import_string_resolves(monkeypatch):
    """`main()` binds uvicorn to the import string 'mcp_http:build_app' with factory=True — the callable
    must resolve and produce a Starlette app so multi-worker forking works."""
    pytest.importorskip("starlette")
    pytest.importorskip("mcp")
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://agami.example.test")

    import importlib

    from starlette.applications import Starlette

    module_name, _, attr = "mcp_http:build_app".partition(":")
    factory = getattr(importlib.import_module(module_name), attr)
    assert callable(factory)
    assert isinstance(factory(), Starlette)
