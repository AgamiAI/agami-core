#!/usr/bin/env python3
"""
Apply a batch of exclude / include actions from the model-explorer dashboard
to the per-table YAMLs in <artifacts_dir>/<profile>/.

Flow per call:
  1. Read the actions JSON from --actions-file.
  2. For each tables/column target, locate the agami extension and update
     review_state (exclude → 'rejected'; include → 'unreviewed' with
     signed_off_* cleared).
  3. Run validate_semantic_model.py on the profile dir.
  4. If validator passes, append one line per applied change to
     curation_log.jsonl. Best-effort `git commit` in the profile repo.
  5. If validator fails, revert any modified files via `git checkout`
     (the profile dir is git-init'd by agami-connect Phase 3e) and print
     the validator errors verbatim.

Always exits 0 (the LLM reads the JSON to decide what to surface).
The output JSON shape:

  {
    "applied":   {"exclude_tables": N, "include_tables": N,
                  "exclude_columns": N, "include_columns": N},
    "skipped":   [{"qname": "X.Y", "reason": "..."}, ...],
    "errors":    ["..."],
    "validator_ok":   true|false,
    "validator_output": "verbatim text from validate_semantic_model.py"
  }
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import yaml


VALIDATOR = Path(__file__).resolve().parent / "validate_semantic_model.py"


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def _read_yaml_raw(p: Path) -> str:
    return p.read_text()


def _read_yaml(p: Path) -> dict | None:
    if not p.exists():
        return None
    try:
        return yaml.safe_load(p.read_text())
    except yaml.YAMLError:
        return None


def _find_agami_index(custom_extensions: list | None) -> tuple[int, dict] | tuple[None, None]:
    """Return (index_in_array, parsed_agami_dict) for the COMMON+agami entry,
    or (None, None) if missing."""
    if not custom_extensions:
        return None, None
    for i, ext in enumerate(custom_extensions):
        if not isinstance(ext, dict):
            continue
        if ext.get("vendor_name") != "COMMON":
            continue
        data = ext.get("data")
        if not isinstance(data, str):
            continue
        try:
            payload = json.loads(data)
        except json.JSONDecodeError:
            continue
        agami = payload.get("agami")
        if isinstance(agami, dict):
            return i, agami
    return None, None


def _write_agami(custom_extensions: list, idx: int, agami: dict) -> None:
    """Re-encode and write back the COMMON+agami entry's `data` string."""
    payload = {"agami": agami}
    custom_extensions[idx] = dict(custom_extensions[idx])  # shallow copy
    custom_extensions[idx]["data"] = json.dumps(payload)


def _set_excluded(agami: dict, excluded: bool) -> None:
    """Flip review_state to rejected (exclude) or unreviewed (include).
    Clears sign-off fields either way — per Hard Rule #10 these must be
    null for non-approved states. The `previous_review_state` sidecar is
    not used; on include the user can re-approve via /agami-review."""
    if excluded:
        agami["review_state"] = "rejected"
    else:
        agami["review_state"] = "unreviewed"
    agami["signed_off_by"] = None
    agami["signed_off_at"] = None
    agami["signed_off_role"] = None


def _find_table_yaml(profile_dir: Path, schema: str, table: str) -> Path | None:
    candidate = profile_dir / schema / f"{table}.yaml"
    if candidate.exists():
        return candidate
    # Fallback: scan _schema.yaml for the file pointer
    schema_yaml = _read_yaml(profile_dir / schema / "_schema.yaml") or {}
    for tm in (schema_yaml.get("tables") or []):
        if tm.get("name") == table:
            f = tm.get("file")
            if f:
                p = profile_dir / schema / f
                if p.exists():
                    return p
    return None


def _apply_table_action(profile_dir: Path, qname: str, excluded: bool) -> tuple[bool, str]:
    """Apply exclude/include on a dataset (table-level)."""
    parts = qname.split(".")
    if len(parts) != 2:
        return False, f"malformed table qname: {qname!r} (expected '<schema>.<table>')"
    schema, table = parts
    path = _find_table_yaml(profile_dir, schema, table)
    if path is None:
        return False, f"table yaml not found: {schema}/{table}.yaml"

    doc = yaml.safe_load(path.read_text())
    if not isinstance(doc, dict):
        return False, f"{path}: not a YAML mapping"

    sm = doc.get("semantic_model") or []
    if not sm:
        return False, f"{path}: no semantic_model[]"

    found = False
    for entry in sm:
        for ds in (entry.get("datasets") or []):
            if ds.get("name") != table:
                continue
            ce = ds.get("custom_extensions")
            idx, agami = _find_agami_index(ce)
            if idx is None or agami is None:
                return False, f"{schema}.{table}: dataset has no agami extension"
            _set_excluded(agami, excluded)
            _write_agami(ce, idx, agami)
            found = True
            break
        if found:
            break
    if not found:
        return False, f"{path}: dataset named {table!r} not found in semantic_model[]"

    path.write_text(yaml.safe_dump(doc, sort_keys=False, default_flow_style=False, width=120))
    return True, "ok"


def _apply_column_action(profile_dir: Path, qname: str, excluded: bool) -> tuple[bool, str]:
    """Apply exclude/include on a field (column-level)."""
    parts = qname.split(".")
    if len(parts) < 3:
        return False, f"malformed column qname: {qname!r} (expected '<schema>.<table>.<column>')"
    schema, table = parts[0], parts[1]
    column = ".".join(parts[2:])  # column names with dots are unusual but tolerated
    path = _find_table_yaml(profile_dir, schema, table)
    if path is None:
        return False, f"table yaml not found: {schema}/{table}.yaml"

    doc = yaml.safe_load(path.read_text())
    if not isinstance(doc, dict):
        return False, f"{path}: not a YAML mapping"

    sm = doc.get("semantic_model") or []
    found_field = False
    for entry in sm:
        for ds in (entry.get("datasets") or []):
            if ds.get("name") != table:
                continue
            for f in (ds.get("fields") or []):
                if f.get("name") != column:
                    continue
                ce = f.get("custom_extensions")
                idx, agami = _find_agami_index(ce)
                if idx is None or agami is None:
                    return False, f"{schema}.{table}.{column}: field has no agami extension"
                _set_excluded(agami, excluded)
                _write_agami(ce, idx, agami)
                found_field = True
                break
            if found_field:
                break
        if found_field:
            break
    if not found_field:
        return False, f"{path}: field {column!r} not found on dataset {table!r}"

    path.write_text(yaml.safe_dump(doc, sort_keys=False, default_flow_style=False, width=120))
    return True, "ok"


def _run_validator(profile_dir: Path) -> tuple[bool, str]:
    proc = subprocess.run(
        ["python3", str(VALIDATOR), "--directory", str(profile_dir)],
        capture_output=True, text=True,
    )
    out = (proc.stdout or "") + (proc.stderr or "")
    return proc.returncode == 0, out


def _git_revert(profile_dir: Path) -> None:
    """Revert any uncommitted YAML changes via `git checkout -- .`. Best-effort."""
    if not (profile_dir / ".git").is_dir():
        return
    subprocess.run(
        ["git", "-C", str(profile_dir), "checkout", "--", "."],
        capture_output=True, text=True,
    )


def _git_commit(profile_dir: Path, actor: str, summary: str) -> None:
    """Commit applied changes to the profile's git repo. Best-effort — never raise."""
    if not (profile_dir / ".git").is_dir():
        return
    env = os.environ.copy()
    env["GIT_AUTHOR_NAME"] = "curator"
    env["GIT_AUTHOR_EMAIL"] = actor or "curator@local"
    env["GIT_COMMITTER_NAME"] = "curator"
    env["GIT_COMMITTER_EMAIL"] = actor or "curator@local"
    subprocess.run(["git", "-C", str(profile_dir), "add", "-A"],
                   capture_output=True, text=True, env=env)
    subprocess.run(
        ["git", "-C", str(profile_dir), "commit", "-q", "-m", summary],
        capture_output=True, text=True, env=env,
    )


def _append_curation_log(profile_dir: Path, actor: str, events: list[dict]) -> None:
    log = profile_dir / "curation_log.jsonl"
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("a") as f:
        for ev in events:
            f.write(json.dumps(ev) + "\n")
    try:
        log.chmod(0o600)
    except OSError:
        pass


def apply_actions(profile_dir: Path, actions: dict, actor: str) -> dict:
    applied = {
        "exclude_tables": 0, "include_tables": 0,
        "exclude_columns": 0, "include_columns": 0,
    }
    skipped: list[dict] = []
    errors: list[str] = []
    curation_events: list[dict] = []

    def _record(action: str, kind: str, qname: str) -> None:
        nonlocal curation_events
        curation_events.append({
            "ts":          _now_iso(),
            "actor":       actor or "<unknown>",
            "action":      action,
            "entity_type": kind,
            "entity_qname": qname,
            "from_state":  "<unspecified>",
            "to_state":    "rejected" if action == "exclude" else "unreviewed",
            "confidence":  None,
        })

    for qname in (actions.get("exclude_tables") or []):
        ok, msg = _apply_table_action(profile_dir, qname, excluded=True)
        if ok:
            applied["exclude_tables"] += 1
            _record("exclude", "dataset", qname)
        else:
            skipped.append({"qname": qname, "reason": msg})

    for qname in (actions.get("include_tables") or []):
        ok, msg = _apply_table_action(profile_dir, qname, excluded=False)
        if ok:
            applied["include_tables"] += 1
            _record("include", "dataset", qname)
        else:
            skipped.append({"qname": qname, "reason": msg})

    for qname in (actions.get("exclude_columns") or []):
        ok, msg = _apply_column_action(profile_dir, qname, excluded=True)
        if ok:
            applied["exclude_columns"] += 1
            _record("exclude", "field", qname)
        else:
            skipped.append({"qname": qname, "reason": msg})

    for qname in (actions.get("include_columns") or []):
        ok, msg = _apply_column_action(profile_dir, qname, excluded=False)
        if ok:
            applied["include_columns"] += 1
            _record("include", "field", qname)
        else:
            skipped.append({"qname": qname, "reason": msg})

    # Validate everything before committing
    ok, output = _run_validator(profile_dir)
    if not ok:
        _git_revert(profile_dir)
        return {
            "applied": {k: 0 for k in applied},   # reverted
            "skipped": skipped,
            "errors":  [f"validator rejected the batch — all changes reverted via git checkout"],
            "validator_ok": False,
            "validator_output": output,
        }

    # Validator passed — append curation log + best-effort git commit
    if curation_events:
        _append_curation_log(profile_dir, actor, curation_events)
        total = sum(applied.values())
        summary_line = f"model: {applied['exclude_tables']} excluded / {applied['include_tables']} re-included tables, {applied['exclude_columns']} excluded / {applied['include_columns']} re-included columns by {actor or 'curator'}"
        _git_commit(profile_dir, actor, summary_line)

    return {
        "applied": applied,
        "skipped": skipped,
        "errors":  errors,
        "validator_ok": True,
        "validator_output": output.strip().splitlines()[-1] if output.strip() else "",
    }


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--profile", required=True)
    p.add_argument("--artifacts-dir", required=True)
    p.add_argument("--actor", default="",
                   help="Email of the curator (recorded in curation_log.jsonl + git commit)")
    p.add_argument("--actions-file", required=True,
                   help="JSON file with exclude_tables/include_tables/exclude_columns/include_columns lists")
    args = p.parse_args()

    profile_dir = (Path(os.path.expanduser(args.artifacts_dir)).resolve() / args.profile)
    if not profile_dir.is_dir():
        result = {"applied": {}, "skipped": [], "errors": [f"profile dir not found: {profile_dir}"], "validator_ok": False, "validator_output": ""}
        json.dump(result, sys.stdout)
        sys.stdout.write("\n")
        return 0

    actions_path = Path(os.path.expanduser(args.actions_file)).resolve()
    try:
        actions = json.loads(actions_path.read_text())
    except (OSError, json.JSONDecodeError) as e:
        result = {"applied": {}, "skipped": [], "errors": [f"failed to read actions file: {e}"], "validator_ok": False, "validator_output": ""}
        json.dump(result, sys.stdout)
        sys.stdout.write("\n")
        return 0

    if not isinstance(actions, dict):
        result = {"applied": {}, "skipped": [], "errors": ["actions file must be a JSON object"], "validator_ok": False, "validator_output": ""}
        json.dump(result, sys.stdout)
        sys.stdout.write("\n")
        return 0

    result = apply_actions(profile_dir, actions, args.actor)
    json.dump(result, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
