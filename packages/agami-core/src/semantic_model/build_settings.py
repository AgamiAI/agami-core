"""Settings a build reads and nothing deployed carries (#675).

They live in the gitignored `<artifacts_dir>/local/.config` (JSON, the file that already holds the
active profile and the reviewer's identity), not in `organization.yaml` or `datasource.yaml`: those
are deployed and restored by every core, and their models refuse a key an older core doesn't know.

  * `personal_data` — `enforced_by_warehouse` when the warehouse's own column- or row-level security
    decides who sees personal data, so a build flags nothing. Absent means agami flags it (the default).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

PERSONAL_DATA_ENFORCED = "enforced_by_warehouse"
PERSONAL_DATA_DEFAULT = "flagged_by_agami"
PERSONAL_DATA_CHOICES = (PERSONAL_DATA_ENFORCED, PERSONAL_DATA_DEFAULT)


def config_path(artifacts_dir: str | Path) -> Path:
    return Path(artifacts_dir) / "local" / ".config"


def _read(artifacts_dir: str | Path) -> dict:
    try:
        doc = json.loads(config_path(artifacts_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return doc if isinstance(doc, dict) else {}


def warehouse_enforces_personal_data(artifacts_dir: str | Path) -> bool:
    """The person said their warehouse controls who sees personal data: a build flags nothing."""
    return _read(artifacts_dir).get("personal_data") == PERSONAL_DATA_ENFORCED


def set_personal_data(artifacts_dir: str | Path, setting: str) -> Path:
    """Write the setting into the local config, keeping every other key. The default is the absence
    of the key, so `flagged_by_agami` removes it."""
    if setting not in PERSONAL_DATA_CHOICES:
        raise ValueError(f"personal_data must be one of {', '.join(PERSONAL_DATA_CHOICES)}")
    path = config_path(artifacts_dir)
    doc = _read(artifacts_dir)
    if setting == PERSONAL_DATA_ENFORCED:
        doc["personal_data"] = setting
    else:
        doc.pop("personal_data", None)
    write_private(path, json.dumps(doc, indent=2) + "\n")
    return path


def write_private(path: Path, text: str) -> None:
    """`text` at `path`, a file under `<artifacts_dir>/local/`, readable by its owner only (it holds
    the reviewer's identity), with `local/` made or tightened to 0700. Created at 0600 rather than
    written and then chmod-ed, so it is never readable by others even for a moment."""
    from agami_paths import ensure_local_dir

    ensure_local_dir(path.parent.parent)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.chmod(path, 0o600)  # an existing file keeps its old mode through O_CREAT
