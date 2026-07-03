"""Make the agami-core library importable for the plugin's runtime scripts.

A refactor moved the library (`agami_paths`, `execute_sql`, `semantic_model`, …) out of this scripts dir
into the pip package `packages/agami-core/src`. The marketplace ships only `plugins/agami/`, so those
modules aren't on `sys.path` there and nothing pip-installs them — which broke every marketplace install
(agami-connect died on `import agami_paths`). This resolver makes the library importable in every layout,
**no pip required**:

  1. already importable  → a pip-installed package wins; do nothing.
  2. the bundled `lib/`   → the drift-checked copy shipped next to the scripts (`plugins/agami/lib/`),
                            present in BOTH the marketplace cache and a dev checkout (kept in sync by
                            `dev.py sync-lib`).
  3. the dev source       → `…/packages/agami-core/src`, a belt-and-suspenders fallback for a dev checkout.

Stdlib only (it runs before the library is even on the path).
"""

from __future__ import annotations

import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent


def ensure_importable() -> None:
    """Ensure the agami-core library modules can be imported. Idempotent; safe to call at every script's top.

    Raises ImportError with a clear message if the library can't be found in any layout, rather than
    letting the caller's `import agami_paths` fail with a harder-to-diagnose ModuleNotFoundError."""
    try:
        import agami_paths  # noqa: F401  — a pip-installed package wins; nothing to add.

        return
    except ImportError:
        pass
    # `<scripts>/../lib` resolves in the marketplace cache (`<version>/lib`) AND a dev checkout
    # (`plugins/agami/lib`); the packages/ source is only there in a dev checkout. Verify each candidate
    # actually provides the library (a present-but-incomplete dir shouldn't count as success).
    for candidate in (_SCRIPTS.parent / "lib", _SCRIPTS.parents[2] / "packages" / "agami-core" / "src"):
        if not candidate.is_dir():
            continue
        sys.path.insert(0, str(candidate))
        try:
            import agami_paths  # noqa: F401

            return
        except ImportError:
            sys.path.pop(0)  # this candidate didn't provide the library — try the next
    raise ImportError(
        "agami-core library not found: no installed package, and the plugin's bundled lib/ is "
        "missing or incomplete. Reinstall the agami plugin, or (in a dev checkout) run "
        "`uv run dev.py sync-lib`."
    )
