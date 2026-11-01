#!/usr/bin/env python3
"""A one-time page on 127.0.0.1 that takes a database password and puts it in the OS credential store.

The agami pane collects the non-secret connection fields and starts this helper with them. The
password is typed into the helper's page in the browser, so it never passes through the chat, a
command line, the pane, or a plain-text file. On submit the helper stores it with
`credential_store.put` (Keychain on macOS, Credential Manager on Windows), writes the profile's
section to `<artifacts_dir>/local/credentials` with `password = keyring:agami/<profile>`, runs
`SELECT 1` through `execute_sql`, and exits.

    connect_helper.py --spec <path.json>
    spec: {"profile": "demo", "db_type": "postgres", "fields": {"host": ..., "port": ..., ...},
           "auth": "sso"  (optional: a browser or cloud sign-in; no page, no secret)}

Stdout is JSON lines, nothing else:
    {"url": "http://127.0.0.1:<port>/<token>/", "expires_in": 300}       once listening
    {"status": "connected" | "connect_failed", "profile": ..., "detail": "<one line>"}
    {"status": "refused", "reason": <REASONS>, "profile": ..., "detail": "<one line>"}
    {"status": "missing_driver", "package": "<pip package(s)>", "profile": ..., "detail": "<one line>"}
    {"status": "expired"}                                                 no submit in 300 s; nothing written
Exit code is 0 for every outcome; the status (and a refusal's `reason`) says what happened, and `detail` is
the line to show a person. A run that ends without a final line (an unexpected error, SIGTERM) removes the
section and the stored password it wrote.

The server binds 127.0.0.1 only, on a random port, behind a token of 43 URL-safe characters. Any
other path, a wrong token, or a second submit gets 404. Stdlib only, apart from agami-core's own
`credential_store` and `execute_sql`.
"""

from __future__ import annotations

import argparse
import configparser
import contextlib
import html
import http.server
import importlib
import json
import logging
import os
import platform
import re
import secrets
import signal
import sys
import threading
import urllib.parse
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from typing import Any, TextIO

from _agami_lib import ensure_importable  # noqa: E402

ensure_importable()
import agami_paths  # noqa: E402

# connect_resolve owns the db_type -> driver table; promote_credentials owns the "never overwrite an
# existing [profile]" rule. Reuse both, not copies.
from connect_resolve import driver_packages  # noqa: E402
from promote_credentials import _sections  # noqa: E402

EXPIRES_IN = 300
HOST = "127.0.0.1"
_SHARED = Path(__file__).resolve().parent.parent / "shared"
_PAGE = _SHARED / "connect-helper-page.html"
_LOGO = _SHARED / "agami-logo-dark.svg"

# Same rule the pane and agami-connect 0a.3 apply: it becomes an INI section, a Keychain account
# name and a directory name, so nothing that needs quoting in any of them.
_PROFILE_RE = re.compile(r"^[a-z0-9_-]{1,32}$")
_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_DB_TYPE_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
# `password` is what the page is for; `url` can carry a password inside it; `type` comes from db_type.
_RESERVED_FIELDS = {"password", "url", "type"}
_UNREADABLE_VALUE_RE = re.compile(r"%|^[#;]|\s[#;]")
_MAX_BODY = 8192
# Seconds a connection may sit idle or mid-request. Each request has its own daemon thread, so a slow
# or idle client holds only that thread, never the server or the 300-second exit.
_REQUEST_TIMEOUT = 10
_STORE_NAMES = {"Darwin": "your Keychain", "Windows": "Windows Credential Manager"}
# Where a person removes an entry the store already holds under a profile's name.
_STORE_CLEAR = {
    "Darwin": "open Keychain Access, search agami",
    "Windows": "open Credential Manager, Windows Credentials, agami",
}
# Every `reason` a refusal can carry; the pane branches on these and shows `detail`.
REASONS = (
    "bad_spec", "bad_name", "bad_field", "name_taken", "unreadable_credentials", "stale_install",
    "unsupported_os", "no_store", "saved_password_exists", "store_error", "write_failed",
)


def _emit(out: TextIO, obj: dict[str, Any]) -> None:
    out.write(json.dumps(obj) + "\n")
    out.flush()


def _final(out: TextIO, status: str, profile: str, detail: str, secret: str = "", **extra: str) -> int:
    # Every message below is written without the secret; this is the backstop for one that a
    # driver or backend put into its own error text.
    if secret:
        detail = detail.replace(secret, "[hidden]")
    _emit(out, {"status": status, **extra, "profile": profile, "detail": " ".join(detail.split())})
    return 0


def _refuse(out: TextIO, reason: str, profile: str, detail: str, secret: str = "") -> int:
    assert reason in REASONS, reason
    return _final(out, "refused", profile, detail, secret, reason=reason)


def _stale_install_detail() -> str | None:
    """Why this interpreter's agami-core cannot use a `keyring:` reference, or None if it can.

    A pip-installed agami-core wins over the plugin's bundled lib/ (`_agami_lib`). One that predates
    credential_store would read `keyring:agami/<profile>` as the literal password, so refuse before
    anything is stored rather than leave a profile that fails to sign in for no visible reason."""
    try:
        import credential_store  # noqa: F401
        import execute_sql
    except ImportError:
        execute_sql = None
    if execute_sql is None or not isinstance(getattr(execute_sql, "credential_store", None), ModuleType):
        return (
            "The agami-core installed for this Python is too old to read passwords from the OS "
            "credential store. Run sm install to update it, then connect again."
        )
    return None


def _spec_error(spec: Any) -> tuple[str, str] | None:
    """(reason, one-line detail) for what is wrong with the spec, or None."""
    if not isinstance(spec, dict):
        return "bad_spec", "The connection spec must be a JSON object."
    profile = spec.get("profile")
    if not isinstance(profile, str) or not _PROFILE_RE.match(profile):
        return "bad_name", (
            f"The profile name {profile!r} is not valid. Use 1 to 32 lowercase letters, digits, "
            "hyphens or underscores, for example demo."
        )
    db_type = spec.get("db_type")
    if not isinstance(db_type, str) or not _DB_TYPE_RE.match(db_type):
        return "bad_field", "The database type is missing or not valid."
    if spec.get("auth", "password") not in ("password", "sso"):
        return "bad_field", "auth must be password or sso."
    fields = spec.get("fields", {})
    if not isinstance(fields, dict):
        return "bad_field", "fields must be a JSON object of connection settings."
    for key, value in fields.items():
        if not isinstance(key, str) or not _KEY_RE.match(key) or key in _RESERVED_FIELDS:
            return "bad_field", f"The field {key!r} cannot be set here."
        if not isinstance(value, (str, int)) or isinstance(value, bool) or "\n" in str(value) or "\r" in str(value):
            return "bad_field", f"The value for {key} must be one line of text or a number."
        # execute_sql reads the file with `%` interpolation and `#`/`;` inline comments, so these
        # would reach the driver changed or cut short (or break the read altogether).
        if _UNREADABLE_VALUE_RE.search(str(value)):
            return "bad_field", (
                f"The value for {key} cannot contain %, or # or ; after a space; enter it in the credentials file by hand."
            )
    return None


def _missing_driver(db_type: str) -> tuple[str, str] | None:
    """(pip package(s), one-line detail) when this Python cannot import the driver `db_type` needs, else None.

    Checked in this process because the connection test runs here. Several packages are space-separated
    (duckdb also needs pytz), and only the missing ones are named."""
    missing = []
    for module, package in driver_packages(db_type).items():
        try:
            importlib.import_module(module)
        except ImportError:
            missing.append(package)
    if not missing:
        return None
    package = " ".join(missing)
    return package, f"The {db_type} driver is not installed for this Python. Install {package}, then connect again."


def _existing_profiles(creds: Path) -> list[str]:
    return _sections(creds) if creds.exists() else []


def _section_text(spec: dict[str, Any], password_ref: str | None) -> str:
    lines = [f"[{spec['profile']}]", f"type = {spec['db_type']}"]
    lines += [f"{k} = {v}" for k, v in spec.get("fields", {}).items() if str(v) != ""]
    if password_ref:
        lines.append(f"password = {password_ref}")
    return "\n".join(lines) + "\n"


def _write_section(creds: Path, section: str) -> str:
    """Create the credentials file at 0600, or append to it and put it back to 0600. Returns the exact
    text added, so `_remove_section` can take it out again."""
    creds.parent.mkdir(parents=True, exist_ok=True)
    if not creds.exists():
        fd = os.open(creds, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(section)
        added = section
    else:
        existing = creds.read_bytes()
        added = ("" if not existing or existing.endswith(b"\n") else "\n") + "\n" + section
        with creds.open("a", encoding="utf-8", newline="\n") as fh:
            fh.write(added)
    creds.chmod(0o600)
    return added


def _remove_section(creds: Path, added: str) -> None:
    """Undo `_write_section`: drop the text it added, or the file if it created it. Only when that text is
    still the end of the file; anything written after it is someone else's, so the file is left alone."""
    text = creds.read_bytes().decode("utf-8")
    if not text.endswith(added):
        return
    rest = text[: len(text) - len(added)]
    if not rest:
        creds.unlink()
        return
    tmp = creds.with_name(creds.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
        fh.write(rest)
    os.replace(tmp, creds)


@contextlib.contextmanager
def _quiet(*loggers: logging.Logger | None) -> Iterator[None]:
    """Keep execute_sql's raw driver detail off stderr for the connection test, as its own CLI does."""
    saved = []
    null = logging.NullHandler()
    for log in filter(None, loggers):
        saved.append((log, log.propagate))
        log.addHandler(null)
        log.propagate = False
    try:
        yield
    finally:
        for log, propagate in saved:
            log.removeHandler(null)
            log.propagate = propagate


def _connection_test(profile: str) -> tuple[str, str]:
    """`SELECT 1` through execute_sql's guarded chokepoint; (status, one-line detail)."""
    try:
        import execute_sql

        execute_sql.CREDENTIALS_PATH = agami_paths.credentials_path()
        with _quiet(getattr(execute_sql, "_RAW_LOG", None), getattr(execute_sql, "_LOG", None)):
            env = execute_sql.execute_guarded(
                "SELECT 1", profile, None, executor=execute_sql.BUILTIN_EXECUTOR, no_safety=True
            )
    except (ImportError, AttributeError):
        # An SSO profile skips the stale-install guard, so an old agami-core can land here.
        return "connect_failed", "The installed agami-core could not run the connection test. Run sm install, then connect again."
    if env.status == "ok":
        return "connected", f"Connected. A test query ran on {profile}."
    if env.status == "refused":
        return "connect_failed", env.refusal.detail
    return "connect_failed", (env.failure.message.splitlines() or ["The connection test failed."])[0]


def render_page(profile: str, state: str) -> str:
    """The helper's one page, with the logo inlined and nothing loaded from anywhere else."""
    store = _STORE_NAMES.get(platform.system(), "the OS credential store")
    return (
        _PAGE.read_text(encoding="utf-8")
        .replace("{{LOGO}}", _LOGO.read_text(encoding="utf-8"))
        .replace("{{STORE}}", html.escape(store))
        .replace("{{PROFILE}}", html.escape(profile))
        .replace("{{STATE}}", state)
    )


class _Server(http.server.ThreadingHTTPServer):
    """Holds the one-time state the handler checks: the token, and whether it has been used."""

    daemon_threads = True  # a client that never finishes its request cannot keep the process alive
    block_on_close = False

    def __init__(self, profile: str) -> None:
        super().__init__((HOST, 0), _Handler)
        self.origin = f"http://{HOST}:{self.server_address[1]}"
        self.profile = profile
        self.path_ok = f"/{secrets.token_urlsafe(32)}/"
        self.secret = ""
        self.submitted = threading.Event()
        self.lock = threading.Lock()

    def handle_error(self, request: Any, client_address: Any) -> None:
        pass  # a dropped or timed-out connection is not news, and a traceback on stderr is not wanted


class _Handler(http.server.BaseHTTPRequestHandler):
    server: _Server
    timeout = _REQUEST_TIMEOUT

    def log_message(self, *args: Any) -> None:
        pass  # the request line carries the token; nothing about a request is logged

    def _live(self) -> bool:
        # The Host check stops a DNS-rebinding page from reaching this port under its own name.
        if self.headers.get("Host") != self.server.origin[len("http://"):]:
            return False
        path = self.path.encode("utf-8", "surrogateescape")
        return secrets.compare_digest(path, self.server.path_ok.encode()) and not self.server.submitted.is_set()

    def _send(self, code: int, body: str = "") -> None:
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        # Not no-referrer: under that policy a browser sends `Origin: null` with the form POST, which
        # the Origin check would refuse. The page links nowhere, so same-origin leaks the token nowhere.
        self.send_header("Referrer-Policy", "same-origin")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'",
        )
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:  # noqa: N802
        if not self._live():
            return self._send(404)
        self._send(200, render_page(self.server.profile, "form"))

    def do_POST(self) -> None:  # noqa: N802
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        origin = self.headers.get("Origin")
        if not self._live() or not 0 < length <= _MAX_BODY or origin not in (None, self.server.origin):
            return self._send(404)
        try:
            body = self.rfile.read(length)
        except OSError:  # the client stopped sending before Content-Length bytes arrived
            self.close_connection = True
            return
        form = urllib.parse.parse_qs(body.decode("utf-8", "replace"))
        password = (form.get("password") or [""])[0]
        if not password:
            return self._send(400, render_page(self.server.profile, "empty"))
        with self.server.lock:
            if self.server.submitted.is_set():  # a second submit that raced the first
                return self._send(404)
            self.server.secret = password
            self.server.submitted.set()
        self._send(200, render_page(self.server.profile, "done"))


def _store_and_connect(spec: dict[str, Any], secret: str, out: TextIO) -> int:
    import credential_store

    profile = spec["profile"]
    creds = agami_paths.credentials_path()
    ref = None
    try:
        # Checked again: another profile with this name may have been added while the page was open.
        if profile in _existing_profiles(creds):
            return _refuse(out, "name_taken", profile, f"A profile named {profile} already exists. Pick another name.")
        if secret:
            saved = _saved_elsewhere(profile)  # checked again, as the section is above
            if saved:
                return _refuse(out, saved[0], profile, saved[1])
        ref = credential_store.put(profile, secret) if secret else None
        added = _write_section(creds, _section_text(spec, ref))
    except credential_store.CredentialStoreError as e:
        return _refuse(out, "store_error", profile, str(e), secret)
    except (OSError, configparser.Error):
        detail = "Could not write the credentials file under <artifacts_dir>/local."
        return _refuse(out, "write_failed", profile, detail + _forget(profile, ref), secret)
    # From here a section and (for a password) a stored secret exist. Any way out that is not
    # `connected` takes both back, including an error nobody planned for and a SIGTERM (`main` turns
    # that into SystemExit), so a half-made profile never blocks the same name next time.
    connected = False
    try:
        missing = _missing_driver(spec["db_type"])
        if missing:
            package, detail = missing
            detail += _rollback(creds, added, profile, ref)
            return _final(out, "missing_driver", profile, detail, secret, package=package)
        status, detail = _connection_test(profile)
        connected = status == "connected"
        if not connected:
            detail += _rollback(creds, added, profile, ref)
        return _final(out, status, profile, detail, secret)
    except Exception:
        detail = "Something went wrong while testing the connection, so nothing was saved. Try again."
        detail += _rollback(creds, added, profile, ref)
        return _final(out, "connect_failed", profile, detail, secret)
    except BaseException:
        if not connected:
            _rollback(creds, added, profile, ref)
        raise


def _rollback(creds: Path, added: str, profile: str, ref: str | None) -> str:
    """Remove the section this run added and the secret it stored; '' or the sentences saying what to remove by hand."""
    note = ""
    try:
        _remove_section(creds, added)
    except (OSError, UnicodeDecodeError):
        note = f" Remove [{profile}] from the credentials file before trying again."
    return note + _forget(profile, ref)


def _saved_elsewhere(profile: str) -> tuple[str, str] | None:
    """(reason, detail) when `profile` cannot be used because the OS store already holds a password under its name.

    The store is shared by every artifacts dir on this computer. Taking the name would overwrite
    another project's password, and a failed connection's rollback would then delete it."""
    import credential_store

    try:
        taken = credential_store.has(profile)
    except credential_store.CredentialStoreError as e:
        return "store_error", str(e)
    if taken:
        where = _STORE_CLEAR.get(platform.system())
        clear = f" To clear it, {where} and delete the entry for {profile}." if where else ""
        return "saved_password_exists", (
            f"A saved password for {profile} already exists on this computer; choose another profile name.{clear}"
        )
    return None


def _forget(profile: str, ref: str | None) -> str:
    """Delete the stored secret this run put in the OS store; '' or a sentence saying it could not."""
    import credential_store

    if not ref:
        return ""
    try:
        credential_store.delete(profile)
    except credential_store.CredentialStoreError as e:
        return " " + str(e)
    return ""


def run(spec: Any, out: TextIO, *, expires_in: float = EXPIRES_IN) -> int:
    """Refuse, or serve the page once and act on the submit. Every outcome is one final JSON line."""
    profile = spec.get("profile", "") if isinstance(spec, dict) else ""
    profile = profile if isinstance(profile, str) else ""
    sso = isinstance(spec, dict) and spec.get("auth") == "sso"
    # An SSO profile writes no keyring reference, so an older agami-core reads it correctly.
    stale = None if sso else _stale_install_detail()
    if stale:
        return _refuse(out, "stale_install", profile, stale)
    problem = _spec_error(spec)
    if problem:
        return _refuse(out, problem[0], profile, problem[1])
    try:
        taken = profile in _existing_profiles(agami_paths.credentials_path())
    except configparser.Error:
        return _refuse(
            out, "unreadable_credentials", profile,
            "The credentials file could not be read. Fix or move it, then try again.",
        )
    if taken:
        return _refuse(out, "name_taken", profile, f"A profile named {profile} already exists. Pick another name.")
    # Before the page, so nobody types a password for a connection this Python cannot make.
    missing = _missing_driver(spec["db_type"])
    if missing:
        return _final(out, "missing_driver", profile, missing[1], package=missing[0])
    if sso:
        return _store_and_connect(spec, "", out)

    import credential_store

    if not credential_store.supported():
        if platform.system() in _STORE_NAMES:
            # macOS or Windows, so the store exists; agami's keyring library is missing or has no working backend.
            return _refuse(
                out, "no_store", profile,
                f"The secure store isn't set up for agami yet: {_STORE_NAMES[platform.system()]} needs the keyring "
                "library, which sm install adds.",
            )
        return _refuse(
            out, "unsupported_os", profile,
            "No OS credential store is available here, so use the manual setup: put the password in "
            "credentials.example and run promote_credentials.py.",
        )
    saved = _saved_elsewhere(profile)
    if saved:
        return _refuse(out, saved[0], profile, saved[1])
    server = _Server(profile)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        _emit(out, {"url": f"http://{HOST}:{server.server_address[1]}{server.path_ok}", "expires_in": int(expires_in)})
        if not server.submitted.wait(expires_in):
            _emit(out, {"status": "expired"})
            return 0
        # The server keeps answering until the work is done, so a second submit meanwhile gets 404.
        return _store_and_connect(spec, server.secret, out)
    finally:
        server.secret = ""
        server.shutdown()
        server.server_close()


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="One-time loopback page that saves a password in the OS store.")
    p.add_argument("--spec", required=True, help="Path to the connection spec JSON (no secrets in it).")
    args = p.parse_args(argv)
    agami_paths.bootstrap()
    try:
        spec = json.loads(Path(args.spec).expanduser().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return _refuse(sys.stdout, "bad_spec", "", "Could not read the connection spec file.")
    # The pane stops the helper with SIGTERM (Cancel, or the pane closing). As an exception it unwinds
    # through `_store_and_connect`, which takes back what this run stored; the default action would not.
    signal.signal(signal.SIGTERM, _terminated)
    return run(spec, sys.stdout)


def _terminated(signum: int, frame: Any) -> None:
    raise SystemExit(128 + signum)


if __name__ == "__main__":
    sys.exit(main())
