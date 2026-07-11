"""Offload blocking work off the asyncio event loop (ACE-048).

The hosted HTTP server runs on a single asyncio worker, so a synchronous argon2 verify (~50-100 ms), an
OIDC token exchange (up to a 10 s timeout), or a per-tool-call audit INSERT run directly on the loop would
freeze every other in-flight request. `run_blocking` hands such a call to a worker thread so the loop stays
responsive. A leaf module (no agami imports) so any handler can use it without an import cycle.

anyio ships with Starlette/uvicorn (the `[server]` extra). `to_thread.run_sync` is positional-only, so we
wrap in `functools.partial` to carry keyword arguments (e.g. `exchange_code(p, code=..., redirect_uri=...)`).
"""

from __future__ import annotations

import functools
from typing import Callable, TypeVar

from anyio import to_thread

_T = TypeVar("_T")


async def run_blocking(fn: Callable[..., _T], /, *args: object, **kwargs: object) -> _T:
    """Run a blocking sync callable in a worker thread, keeping the event loop free for other requests."""
    return await to_thread.run_sync(functools.partial(fn, *args, **kwargs))
