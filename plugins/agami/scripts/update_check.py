"""Say when a newer agami is published, at most once a day (#633).

agami-connect runs this at the start of every session. It asks PyPI for the newest agami-core by running
`pip index versions agami-core` as a child process, so the person's own pip configuration (index mirror,
proxy) applies and agami itself opens no connection. When the newest release is above this plugin's
version it prints a short note: both versions, that release's CHANGELOG, and the update command. It
prints nothing otherwise.

The plugin's version is the one compared, because the plugin pins the library and `sm` upgrades the
library to match it; updating the plugin is the step a person takes. Never fails: pip missing, slow or
failing is the same as no newer version.

    python update_check.py
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from datetime import date
from pathlib import Path
from typing import Optional

from _agami_lib import ensure_importable  # noqa: E402

ensure_importable()
import agami_paths  # noqa: E402
from _plugin_version import plugin_version, release  # noqa: E402

STATE_FILE = "update_check.json"
CHANGELOG_URL = "https://github.com/AgamiAI/agami-core/blob/v{version}/CHANGELOG.md"
UPDATE_COMMAND = "/plugin update agami-core@agami"


def _state_path() -> Path:
    return agami_paths.local_dir() / STATE_FILE


def _checked_today(path: Path, today: date) -> bool:
    try:
        return json.loads(path.read_text(encoding="utf-8")).get("checked_on") == today.isoformat()
    except (OSError, ValueError, AttributeError):
        return False


def _record(path: Path, today: date) -> None:
    # Runs before first-time setup has chosen the artifacts folder, so it never creates one.
    if not path.parent.is_dir():
        return
    try:
        path.write_text(json.dumps({"checked_on": today.isoformat()}), encoding="utf-8")
    except OSError:
        pass


def newest_published() -> Optional[str]:
    """The newest agami-core version on the index pip is configured for, or None."""
    try:
        out = subprocess.run(
            [sys.executable, "-m", "pip", "index", "versions", "agami-core",
             "--timeout", "3", "--retries", "0", "--disable-pip-version-check"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    # pip's verbose mode puts diagnostic lines ahead of the result.
    m = re.search(r"^agami-core \(([^)\s]+)\)", out.stdout or "", re.MULTILINE)
    return m.group(1) if out.returncode == 0 and m else None


def note(installed: str, newest: str) -> str:
    return (f"agami {newest} is available (you have {installed}).\n"
            f"What's new: {CHANGELOG_URL.format(version=newest)}\n"
            f"Update: {UPDATE_COMMAND}")


def main(today: Optional[date] = None) -> int:
    installed = plugin_version()
    if not installed:
        return 0
    today = today or date.today()
    path = _state_path()
    if _checked_today(path, today):
        return 0
    newest = newest_published()
    # Recorded even when pip fails, so an offline machine doesn't wait on it every session.
    _record(path, today)
    if newest and release(newest) > release(installed):
        print(note(installed, newest))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
