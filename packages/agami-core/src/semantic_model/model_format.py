"""Which agami-core may rewrite a model (#633).

A core that rebuilds a model written by a newer one drops what it does not understand. Each write
stamps `datasource.yaml` with the core that wrote it and `rewrite_needs`, the oldest core that can
rewrite it safely, taken from `FORMATS` below. The writer computes `rewrite_needs` because an older
core's own list ends at its release, so it cannot know which later formats were breaking. A rewrite
by a core older than `rewrite_needs` is refused; a missing or unreadable stamp never refuses.
Loading, validating and querying are never refused.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

import yaml

WRITTEN_BY = "written_by"
REWRITE_NEEDS = "rewrite_needs"
STAMP_KEYS = frozenset({WRITTEN_BY, REWRITE_NEEDS})
_PREFIX = "agami-core "

# (version, breaking, reason), oldest first. Add an entry whenever a release changes the model format;
# mark it breaking when an older core rewriting the model would lose or corrupt what the release added.
FORMATS: tuple[tuple[str, bool, str], ...] = (
    ("0.9.16", True, "adds `ambiguities:`"),
    ("0.9.21", True, "keeps `choice_field` keys untrimmed; older cores merge 'Open' and 'Open '"),
)


class ModelTooNew(RuntimeError):
    """A rewrite was refused because the model needs a newer agami-core."""


def _parse(value: object) -> Optional[tuple[int, int, int]]:
    if not isinstance(value, str):
        return None
    m = re.match(r"\s*(?:agami-core\s+)?(\d+)\.(\d+)\.(\d+)", value)
    return tuple(int(g) for g in m.groups()) if m else None  # type: ignore[return-value]


def core_version() -> Optional[str]:
    """The running agami-core's version, or None when it isn't installed as a package."""
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version("agami-core")
    except PackageNotFoundError:
        return None


def stamp() -> dict[str, str]:
    """The keys a write records for the running core; empty when its version is unknown."""
    mine = core_version()
    parsed = _parse(mine)
    if parsed is None:
        return {}
    out = {WRITTEN_BY: _PREFIX + mine}
    breaking = [v for v, is_breaking, _ in FORMATS if is_breaking and _parse(v) <= parsed]
    if breaking:
        out[REWRITE_NEEDS] = _PREFIX + max(breaking, key=_parse)
    return out


def rewrite_refusal(root: str | Path) -> Optional[str]:
    """Why the running core may not rewrite the model at `root`, or None when it may."""
    path = Path(root) / "datasource.yaml"
    if not path.is_file():
        return None
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError):
        return None
    if not isinstance(doc, dict):
        return None
    needs, mine = _parse(doc.get(REWRITE_NEEDS)), _parse(core_version())
    if needs is None or mine is None or mine >= needs:
        return None
    writer = doc.get(WRITTEN_BY) if _parse(doc.get(WRITTEN_BY)) else "a newer agami-core"
    return (f"This model was written by {writer} and uses features this version "
            f"({core_version()}) doesn't understand. Upgrade agami-core to "
            f"{'.'.join(map(str, needs))} or later before rebuilding it.")


def check_rewrite(root: str | Path) -> None:
    """Raise `ModelTooNew` when the running core may not rewrite the model at `root`."""
    refusal = rewrite_refusal(root)
    if refusal:
        raise ModelTooNew(refusal)
