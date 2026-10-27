"""The plugin's own version, and whether an interpreter's agami-core library is older than it.

The plugin and the agami-core package are bumped to the same version on every release, so the plugin's
version is the library its skills were written against. A plugin update refetches the plugin's files but
not a pip-installed library, and that installed library wins over the bundled `lib/`, so an old one keeps
running without a word. `sm`, `py`, `_agami_lib` and `connect_resolve` all ask this module.

Stdlib only, and it only reads files and installed metadata. It may run under a candidate interpreter
older than the package's floor, so it keeps to syntax that still parses there.

    python _plugin_version.py           print the plugin version (empty when unknown)
    python _plugin_version.py --check   print a warning to stderr if this Python's library is older
    python _plugin_version.py --probe   print JSON about this Python: pip, agami-core version, ...
    python _plugin_version.py --usable  exit 3 if `sm install` could never make this Python current
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
# Set by `py` once it has checked, so `_agami_lib` in the script it runs doesn't warn a second time.
CHECKED_ENV = "AGAMI_LIBRARY_VERSION_CHECKED"


def release(version: str) -> tuple:
    """The numeric release segment only, so `0.9.18rc1` compares as 0.9.18."""
    m = re.match(r"\d+(?:\.\d+)*", version or "")
    return tuple(int(p) for p in m.group(0).split(".")) if m else ()


def plugin_version(plugin_root: Path = PLUGIN_ROOT) -> str | None:
    """`version` from the plugin's manifest; else the cache folder's name when it is a version."""
    try:
        v = json.loads((plugin_root / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))["version"]
        if isinstance(v, str) and release(v):
            return v
    except (OSError, ValueError, KeyError, TypeError):
        pass
    # The cache folder can also be a commit hash, which may start with a digit.
    return plugin_root.name if re.fullmatch(r"\d+(?:\.\d+)+\S*", plugin_root.name) else None


def is_older(have: str | None, want: str | None) -> bool:
    """True only when both versions are known and `have` is below `want`."""
    h, w = release(have or ""), release(want or "")
    return bool(h and w) and h < w


def installed_library() -> dict:
    """The agami-core distribution this Python would import: its version, and the source folder when it
    is an editable install. Read from metadata, so the package is never imported."""
    out = {"version": None, "editable_from": None}
    try:
        from importlib.metadata import PackageNotFoundError, distribution
    except ImportError:
        return out
    try:
        dist = distribution("agami-core")
    except PackageNotFoundError:
        return out
    out["version"] = dist.version
    try:
        found = next((f for f in dist.files or () if f.name == "direct_url.json"), None)
        info = json.loads(found.read_text(encoding="utf-8")) if found else {}
        if (info.get("dir_info") or {}).get("editable") and info.get("url", "").startswith("file:"):
            from urllib.parse import unquote, urlparse

            path = unquote(urlparse(info["url"]).path)
            # file:///C:/x parses to /C:/x on Windows
            if re.match(r"/[A-Za-z]:", path):
                path = path[1:]
            out["editable_from"] = path
    except (OSError, ValueError, AttributeError):
        pass
    return out


def is_this_checkout(editable_from: str | None, plugin_root: Path = PLUGIN_ROOT) -> bool:
    """An editable install of the checkout this plugin sits in runs this checkout's own code."""
    if not editable_from:
        return False
    try:
        return Path(editable_from).resolve() == (plugin_root.parent.parent / "packages" / "agami-core").resolve()
    except OSError:
        return False


def stale_warning(interpreter: str, have: str, want: str, plugin_root: Path = PLUGIN_ROOT) -> str:
    return (
        f"agami: the agami-core library in {interpreter} is {have}, older than this plugin ({want}); "
        f"skills may fail against it. Fix: bash \"{plugin_root / 'scripts' / 'sm'}\" install "
        f"(upgrades it), or export AGAMI_PYTHON=/path/to/a/python/with/agami-core-{want}"
    )


def library_warning(interpreter: str | None = None, plugin_root: Path = PLUGIN_ROOT) -> str | None:
    """A warning when this Python's agami-core is older than the plugin; None when current or unknown."""
    lib = installed_library()
    want = plugin_version(plugin_root)
    if is_this_checkout(lib["editable_from"], plugin_root) or not is_older(lib["version"], want):
        return None
    return stale_warning(interpreter or sys.executable, lib["version"], want, plugin_root)


def probe() -> dict:
    """What connect_resolve needs to know about the Python running this."""
    import importlib.util

    lib = installed_library()
    return {
        "has_pip": importlib.util.find_spec("pip") is not None,
        "agami_core": lib["version"],
        "this_checkout": is_this_checkout(lib["editable_from"]),
    }


def cannot_be_brought_current(inst: dict, want: str | None) -> str | None:
    """Why `sm install` could never give this interpreter a current agami-core, or None if it could.
    Only a Python with no pip AND a missing or older library: pip-less but current still works."""
    if inst["has_pip"] is not False or want is None or inst["this_checkout"]:
        return None
    if inst["agami_core"] is None:
        return f"no pip, and no agami-core installed, so nothing could install agami-core {want}"
    if is_older(inst["agami_core"], want):
        return f"no pip, and agami-core {inst['agami_core']} is older than the plugin ({want}), so nothing could upgrade it"
    return None


def main(argv: list[str]) -> int:
    if argv[:1] == ["--probe"]:
        print(json.dumps(probe()))
    elif argv[:1] == ["--usable"]:
        # 3, not 1: a helper that crashes exits 1, and that must not get a candidate skipped.
        return 3 if cannot_be_brought_current(probe(), plugin_version()) else 0
    elif argv[:1] == ["--check"]:
        msg = library_warning()
        if msg:
            print(msg, file=sys.stderr)
    else:
        print(plugin_version() or "")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
