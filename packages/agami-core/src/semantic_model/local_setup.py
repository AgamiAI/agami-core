"""`sm choose-folder`, `sm init-local` and `sm set-config`: the first-run files under
`<artifacts_dir>/local/`, written by agami, so a skill needs neither a `mkdir` nor a Write outside the
working folder (each of which stopped onboarding for permission) and the calls can be pre-approved.

`choose-folder` is the one that names a folder: it saves the person's choice as agami's artifacts
folder (the pointer at `~/.config/agami/path`), and is never pre-approved. It runs before the other
two, which then act on that folder only.

Nothing a call carries is written as content. `init-local` writes only text agami defines (the
`.gitignore` line, the sample's credential section). `set-config` writes `.config` from fields it
checks one by one: a tier from a fixed list, a profile name, interpreter and tool paths that are
executables of the expected name, and the reviewer's identity read from a scratch file (the
person's words never go on a command line) and checked as an email and a one-line role.

Both act only on agami's own artifacts folder (`agami_paths.artifacts_dir()`), never on a folder the
call names elsewhere: a pre-approved call can't be pointed at another place to create or overwrite.
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from agami_paths import LOCAL_SUBDIR

from .scratch import LinkedTarget

TIERS = ("cli", "duckdb", "python")
TOOLS = ("psql", "mysql", "snowsql", "sqlite3", "duckdb")
PROFILE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*")
PYTHON_NAME = re.compile(r"python(?:3(?:\.\d+)?)?(?:\.exe)?", re.I)
EMAIL = re.compile(r"[A-Za-z0-9._%+'-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
ROLE_MAX = 80
SAMPLE_PROFILE = "agami-example"


class SetupRefused(ValueError):
    """A value `init-local` or `set-config` won't write, in plain words."""


def own_artifacts_dir(path: str) -> Path:
    """`path` when it is agami's artifacts folder, resolved; SetupRefused otherwise."""
    from agami_paths import artifacts_dir

    if not path or not os.path.isabs(os.path.expanduser(path)):
        raise SetupRefused(f"{path!r}: pass the artifacts folder as an absolute path")
    given = Path(os.path.expanduser(path)).resolve()
    art = artifacts_dir()
    if given != art and os.environ.get("AGAMI_ARTIFACTS_DIR"):
        raise SetupRefused(_env_outranks(art, given))
    if given != art:
        raise SetupRefused(
            f"{given} is not agami's artifacts folder ({art}). A folder other than "
            "~/agami-artifacts is set with AGAMI_ARTIFACTS_DIR or by writing its path to "
            "~/.config/agami/path first."
        )
    return art


def _env_outranks(art: Path, given: Path) -> str:
    # The variable lives in the person's shell or Claude's settings, out of a skill's reach: a
    # Bash call can't change it for later calls, so the person changes it, once.
    return (
        f"AGAMI_ARTIFACTS_DIR is set to {art}, and it outranks the saved folder, so agami would "
        f"keep using {art}. Change AGAMI_ARTIFACTS_DIR to {given} (or remove it) where it is "
        "set, restart Claude Code, and connect again."
    )


def choose_folder(path: str) -> dict[str, Any]:
    """Save `path` as agami's artifacts folder (the pointer file), after checking it can be one:
    absolute, not a file, not inside the current folder's `local/`, and not overruled by an
    AGAMI_ARTIFACTS_DIR naming another folder. Creates nothing else; `init-local` makes the folder."""
    import agami_paths

    if not path or not os.path.isabs(os.path.expanduser(path)):
        raise SetupRefused(f"{path!r}: pass the artifacts folder as an absolute path")
    given = Path(os.path.expanduser(path)).resolve()
    if given.exists() and not given.is_dir():
        raise SetupRefused(f"{given} is a file, not a folder: choose a folder for agami's files")
    if os.environ.get("AGAMI_ARTIFACTS_DIR") and given != agami_paths.artifacts_dir():
        raise SetupRefused(_env_outranks(agami_paths.artifacts_dir(), given))
    current_local = agami_paths.local_dir(agami_paths.artifacts_dir())
    if given == current_local or current_local in given.parents:
        raise SetupRefused(
            f"{given} is inside {current_local}, agami's private folder: choose a folder outside it"
        )
    try:
        saved = agami_paths.set_artifacts_dir(given)
    except OSError as exc:
        raise SetupRefused(
            f"could not save the folder in {agami_paths.POINTER_PATH}: {exc}"
        ) from None
    return {"artifacts_dir": str(saved), "pointer": str(agami_paths.POINTER_PATH)}


def sample_credential(art: Path) -> str:
    db = art / "local" / "samples" / "store.db"
    return f"[{SAMPLE_PROFILE}]\ntype = sqlite\npath = {db}\n"


def _refuse_links(*folders: Path) -> None:
    # Checked before anything is written: through a link, the scratch folder and the credential
    # template would land wherever it points, outside the artifacts folder and its 0700 `local/`.
    for folder in folders:
        if folder.is_symlink():
            raise SetupRefused(
                f"{folder} is a link to another place, so agami won't set up its private files "
                "through it. Replace the link with a real folder (move what it points at here), "
                "then try again."
            )


def ignores_local(text: str) -> bool:
    """Whether a `.gitignore` with this text, at the artifacts folder's top, ignores `local/`.

    git applies the last rule that matches, so a `local/` line followed by `!local/` ignores
    nothing. Only rules that can match the folder itself count: once it is ignored, git can't
    re-include a file inside it, so a later `!local/credentials` changes nothing."""
    ignored = False
    for raw in text.splitlines():
        line = raw.rstrip()
        if not line or line.startswith("#"):
            continue
        negated = line.startswith("!")
        pattern = line[1:] if negated else line
        pattern = pattern.removeprefix("\\").rstrip("/")
        pattern = pattern.removeprefix("/").removeprefix("**/")
        if pattern and "/" not in pattern and fnmatch.fnmatchcase(LOCAL_SUBDIR, pattern):
            ignored = not negated
    return ignored


def _ensure_ignored(art: Path) -> str:
    """`had_local` when `<art>/.gitignore` already ignores `local/`, `added_local` once agami's line
    is appended and the file then does; SetupRefused, before any credential exists, when neither."""
    gi = art / ".gitignore"
    try:
        existing = gi.read_text(encoding="utf-8", errors="replace") if gi.exists() else ""
        if ignores_local(existing):
            return "had_local"
        with gi.open("a", encoding="utf-8", newline="\n") as fh:
            if existing and not existing.endswith("\n"):
                fh.write("\n")
            fh.write(f"# agami secrets + per-user state — never commit\n{LOCAL_SUBDIR}/\n")
        effective = ignores_local(gi.read_text(encoding="utf-8", errors="replace"))
    except OSError as exc:
        raise SetupRefused(_not_ignored(gi, str(exc))) from None
    if not effective:
        raise SetupRefused(_not_ignored(gi, "a rule in it still lets local/ through"))
    return "added_local"


def _not_ignored(gi: Path, why: str) -> str:
    return (
        f"{gi} can't be made to ignore local/ ({why}), so agami won't put credentials there. "
        f"Make it a file you can edit with a line reading local/ after any !local rule, then try "
        "again."
    )


def _sample_template_present(example: Path, art: Path) -> bool:
    """True when `example` is the sample's own template (a re-run), False when there is none;
    SetupRefused when it is anything else. A template left by an earlier connection attempt is
    that connection's: the sample flow would promote it and never write its own."""
    if not (example.exists() or example.is_symlink()):
        return False
    try:
        same = not example.is_symlink() and example.read_text(encoding="utf-8") == (
            sample_credential(art)
        )
    except (OSError, UnicodeDecodeError):
        same = False
    if same:
        return True
    raise SetupRefused(
        f"{example} already holds another connection's template, from an earlier attempt to "
        "connect a database, and agami won't replace it with the sample's. Finish that connection "
        "first (agami-connect picks the file up once it is filled in), or move the file somewhere "
        "else, then try the sample again."
    )


def init_local(path: str, sample: bool = False) -> dict[str, Any]:
    """Create `<art>` and `<art>/local/tmp/` (local/ and tmp/ 0700, an existing local/ tightened),
    make `.gitignore` ignore `local/`, and with `sample` write the sample's credential as
    `local/credentials.example` for promote_credentials.py to take in (never over an existing one,
    and refused, the file kept, when an existing one isn't the sample's)."""
    from agami_paths import ensure_local_dir

    art = own_artifacts_dir(path)
    _refuse_links(art / LOCAL_SUBDIR, art / LOCAL_SUBDIR / "tmp")
    example = art / LOCAL_SUBDIR / "credentials.example"
    have_sample = sample and _sample_template_present(example, art)
    art.mkdir(parents=True, exist_ok=True)
    gitignore = _ensure_ignored(art)
    local = ensure_local_dir(art)
    tmp = local / "tmp"
    try:
        os.mkdir(tmp, 0o700)
    except FileExistsError:
        pass
    out: dict[str, Any] = {"artifacts_dir": str(art), "local": str(local), "gitignore": gitignore}
    if sample:
        if have_sample:
            out["credentials_example"] = "exists"
        else:
            fd = os.open(example, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(sample_credential(art))
            out["credentials_example"] = "written"
    return out


def _executable(path: str, name_ok, what: str) -> str:
    if not os.path.isabs(path):
        raise SetupRefused(f"{what} must be an absolute path: {path!r}")
    if not name_ok(os.path.basename(path)):
        raise SetupRefused(f"{path} doesn't look like {what}")
    if not (os.path.isfile(path) and os.access(path, os.X_OK)):
        raise SetupRefused(f"{path} is not an executable file")
    return path


def _identity(file: str) -> dict[str, str]:
    """`{"reviewer_email", "reviewer_role"}` from the scratch JSON file the skill wrote."""
    from . import scratch

    try:
        doc = json.loads(scratch.read_scratch(file, scratch.TEXT_CAP).decode("utf-8"))
    except scratch.NotScratch as exc:
        raise SetupRefused(str(exc)) from None
    except ValueError as exc:
        raise SetupRefused(f"could not read the identity from {file}: {exc}") from None
    if not isinstance(doc, dict):
        raise SetupRefused('the identity file holds {"email": "...", "role": "..."}')
    email, role = doc.get("email"), doc.get("role")
    if not isinstance(email, str) or not EMAIL.fullmatch(email.strip()):
        raise SetupRefused(f"{email!r} is not an email address")
    if not isinstance(role, str) or not role.strip() or len(role) > ROLE_MAX or "\n" in role:
        raise SetupRefused(f"the role must be one line of at most {ROLE_MAX} characters")
    return {"reviewer_email": email.strip(), "reviewer_role": role.strip()}


def set_config(
    path: str,
    *,
    tier: Optional[str] = None,
    active_profile: Optional[str] = None,
    python3: Optional[str] = None,
    tools: Optional[list[str]] = None,
    identity_file: Optional[str] = None,
) -> dict[str, Any]:
    """Merge the given fields into `<art>/local/.config` (every other key kept) and write it 0600.
    Every value is checked before anything is written."""
    from . import build_settings as BS

    art = own_artifacts_dir(path)
    fields: dict[str, Any] = {}
    if tier is not None:
        if tier not in TIERS:
            raise SetupRefused(f"tier must be one of {', '.join(TIERS)}")
        fields["tier"] = tier
    if active_profile is not None:
        if not PROFILE.fullmatch(active_profile) or active_profile.lower() == "local":
            raise SetupRefused(f"{active_profile!r} is not a profile name")
        fields["active_profile"] = active_profile
    paths: dict[str, str] = {}
    if python3 is not None:
        paths["python3"] = _executable(python3, PYTHON_NAME.fullmatch, "a Python interpreter")
    for spec in tools or []:
        name, sep, where = spec.partition("=")
        if not sep or name not in TOOLS:
            raise SetupRefused(f"--tool takes <name>=<path>, name one of {', '.join(TOOLS)}")
        paths[name] = _executable(
            where, lambda b, n=name: b in (n, f"{n}.exe"), f"the {name} program"
        )
    if identity_file is not None:
        fields.update(_identity(identity_file))
    cfg = BS.config_path(art)
    doc = BS._read(art)
    doc.update(fields)
    if paths:
        doc["tool_paths"] = {**(doc.get("tool_paths") or {}), **paths}
    doc["schema_version"] = 1
    doc["artifacts_dir"] = str(art)
    doc["detected_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        BS.write_private(cfg, json.dumps(doc, indent=2, ensure_ascii=False) + "\n")
    except LinkedTarget as exc:
        raise SetupRefused(str(exc)) from exc
    shown = {k: v for k, v in doc.items() if k not in ("reviewer_email", "reviewer_role")}
    return {
        "config": str(cfg),
        "written": sorted(set(fields) | ({"tool_paths"} if paths else set())),
        **shown,
    }
