#!/usr/bin/env python3
"""Deterministically promote credentials.example into credentials (under <artifacts_dir>/local/).

agami-connect Phase 0a writes a one-profile `credentials.example` for the user to fill in.
This script consumes it deterministically — replacing the brittle skill-inline
`if [ ! -f credentials ]; then mv ...` that ONLY handled the very first profile and trusted
the LLM to hand-roll the append for every profile after it:

  - no credentials file yet            -> MOVE the template into place (chmod 600)
  - credentials file already exists     -> APPEND the new profile's [section]
  - the new profile name already exists -> REFUSE (no silent [main]/[main] collision);
                                           leave both files untouched so the user can rename
  - template still holds placeholders   -> REFUSE (never promote an unedited template)

Stdout is a single status line (first token is the machine-readable status); the skill
reads it and acts. Stdlib only.

  SECURED <profiles>          credentials created from the template (chmod 600)        [0]
  APPENDED <profiles>         new profile(s) appended to existing credentials          [0]
  PLACEHOLDERS_REMAIN <list>  template still has placeholder values; not promoted       [2]
  COLLISION <profiles>        profile already in credentials; nothing changed           [3]
  NOTHING                     no credentials.example to promote                         [1]
  ERROR <message>             could not parse / no section / write failed               [4]
"""
from __future__ import annotations

import argparse
import configparser
import os
import re
import sys
from pathlib import Path

# agami_paths lives in the agami-core package; the resolver puts it on the path in every layout
# (pip-installed / the plugin's bundled lib / a dev checkout) with no pip required. A bare
# `import agami_paths` off the script's own dir breaks on a marketplace install, where agami_paths
# lives in lib/, not next to this script (mirrors connect_resolve.py).
from _agami_lib import ensure_importable  # noqa: E402

ensure_importable()
import agami_paths  # noqa: E402

# Placeholder tokens the Phase 0a templates ship with — their presence means the user
# hasn't filled the template in yet. Mirrors the grep guard the skill used inline.
_PLACEHOLDER_RE = re.compile(
    r"your-(?:username|password|host|server|workspace|coordinator|database|token)"
    r"|dapiXXX|/absolute/path/to|user:pass@host"
)


def _uncommented(text: str) -> str:
    """The fillable content only — comment lines and inline comments stripped. A placeholder
    inside a commented-out alternative form (e.g. the recommended `url = …your-password…` line
    the user disabled in favour of the discrete host/port/… fields) is NOT an unfilled field,
    so it must not trip the placeholder refusal. Detection only — the actual write preserves
    the user's exact lines via `_section_block`."""
    out: list[str] = []
    for line in text.splitlines():
        if line.lstrip().startswith(("#", ";")):
            continue  # whole-line comment
        m = re.search(r"\s[#;]", line)  # inline comment (prefix preceded by whitespace)
        out.append(line[: m.start()] if m else line)
    return "\n".join(out)


_BOMS = ((b"\xff\xfe", "utf-16"), (b"\xfe\xff", "utf-16"), (b"\xef\xbb\xbf", "utf-8-sig"))


def _normalize_to_utf8(path: Path) -> bool:
    """Rewrite `path` as plain UTF-8 if Notepad or PowerShell saved it with a byte-order mark
    (PowerShell's default is UTF-16). Everything downstream reads the credentials file as UTF-8,
    and the first profile is MOVED into place, so a template left as it is would become a
    credentials file nothing can read. False if the bytes are not text in any of these."""
    raw = path.read_bytes()
    encoding = next((enc for bom, enc in _BOMS if raw.startswith(bom)), "utf-8")
    try:
        text = raw.decode(encoding)
    except UnicodeDecodeError:
        return False
    if encoding != "utf-8":
        # Bytes, so Windows does not turn the file's own "\r\n" into "\r\r\n"; and via a temporary file,
        # so a failed write cannot cost the user the secrets they typed.
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_bytes(text.encode("utf-8"))
        os.replace(tmp, path)
    return True


def _sections(path: Path) -> list[str]:
    """Profile names ([section] headers) in an INI credentials file. strict=False so an
    already-imperfect existing file (e.g. a pre-existing duplicate) doesn't crash the read."""
    cfg = configparser.ConfigParser(inline_comment_prefixes=("#", ";"), strict=False)
    cfg.read(path, encoding="utf-8-sig")
    return cfg.sections()


def _section_block(text: str, name: str) -> str:
    """The `[name]` block verbatim (header line through the line before the next header /
    EOF) — preserves the user's exact formatting and values, dropping the template's
    instructional comment preamble."""
    out: list[str] = []
    capturing = False
    for line in text.splitlines():
        m = re.match(r"\s*\[(.+?)\]\s*$", line)
        if m:
            capturing = m.group(1) == name
        if capturing:
            out.append(line)
    return "\n".join(out).rstrip() + "\n"


def promote(agami_dir: Path) -> tuple[str, int]:
    creds = agami_dir / "credentials"
    example = agami_dir / "credentials.example"

    if not example.exists():
        return "NOTHING", 1

    if not _normalize_to_utf8(example):
        return "ERROR credentials.example is not UTF-8 text; save it as UTF-8 and run this again", 4
    text = example.read_text(encoding="utf-8")
    placeholders = sorted({m.group(0) for m in _PLACEHOLDER_RE.finditer(_uncommented(text))})
    if placeholders:
        return "PLACEHOLDERS_REMAIN " + ", ".join(placeholders), 2

    try:
        new = _sections(example)
    except configparser.Error as e:
        return f"ERROR could not parse credentials.example: {e}", 4
    if not new:
        return "ERROR credentials.example has no [profile] section", 4

    # First-ever profile: atomic move + lock down.
    if not creds.exists():
        os.replace(example, creds)
        creds.chmod(0o600)
        return "SECURED " + " ".join(new), 0

    # Nth profile: refuse on a name clash, else append the new section(s).
    try:
        existing = set(_sections(creds))
    except configparser.Error as e:
        return f"ERROR could not parse existing credentials: {e}", 4
    clash = [s for s in new if s in existing]
    if clash:
        return "COLLISION " + " ".join(clash), 3

    blocks = [_section_block(text, s) for s in new]
    with creds.open("a", encoding="utf-8") as fh:
        fh.write("\n" + "\n".join(blocks))
    creds.chmod(0o600)
    example.unlink()  # consume the template
    return "APPENDED " + " ".join(new), 0


def main() -> int:
    agami_paths.bootstrap()
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--agami-dir", default=None,
                   help="Directory holding credentials / credentials.example "
                        "(default: <artifacts_dir>/local).")
    args = p.parse_args()
    target = Path(args.agami_dir).expanduser() if args.agami_dir else agami_paths.local_dir()
    msg, code = promote(target)
    print(msg)
    return code


if __name__ == "__main__":
    sys.exit(main())
