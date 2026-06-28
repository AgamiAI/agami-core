"""Load the local YAML semantic model into the serving Postgres — the deploy's `load model` step.

The hosted server serves the model **from the database** (`tools._serve_model` reads the DB when one is
configured), so a deploy has to push the YAML model into Postgres. The read→write primitives already exist
and are idempotent; this is the orchestrator that wires them for every datasource under the artifacts dir.
Run by the deploy entrypoint (ACE-009) and re-run on restart to pick up an edited model.

    AGAMI_DB_URL=postgresql://…  AGAMI_ARTIFACTS_DIR=/…/agami-artifacts  python -m model_deploy [datasource …]
"""

from __future__ import annotations

import sys
from pathlib import Path

import agami_paths
import model_store
from store import Store


def deploy_one(store: Store, datasource: str, profile_dir: Path) -> None:
    """Load one datasource's model (org + examples + memory + version) from `profile_dir` into the store.

    Reads + parses **everything first, then writes** — so a malformed model/examples fails *before* any DB
    write rather than half-way through. Each model_store writer is individually idempotent (clear-then-
    insert), so a clean re-run fully overwrites a datasource; the deploy entrypoint fail-closes on a non-zero
    exit, so a partial write from a rare mid-write DB error is never served and self-heals on the next deploy.
    (The writers commit individually, so this isn't one transaction — load-then-write is the practical guard.)"""
    from semantic_model import loader
    from semantic_model.snapshot import newest_version

    # --- read + parse everything first (where malformed input fails, before any write) ---
    org = loader.load_organization(profile_dir)
    # Examples live per subject area (prompt_examples/<area>/examples.yaml); tag each with its area so the
    # served row carries it (write_examples reads ex["area"]). A malformed examples file for one area is
    # skipped with a warning, not fatal — examples are best-effort few-shots, not the model itself, and a bad
    # one shouldn't block the team's model from deploying.
    examples: list[dict] = []
    for sa in org.subject_areas:
        try:
            area_examples = loader.list_prompt_examples(profile_dir, sa.name)
        except Exception as e:  # noqa: BLE001 — a bad examples file mustn't abort the model deploy
            print(f"model_deploy: skipping malformed examples for area {sa.name!r}: {e}", file=sys.stderr)
            continue
        examples.extend({**ex, "area": ex.get("area") or sa.name} for ex in area_examples if isinstance(ex, dict))
    org_md = profile_dir / "ORGANIZATION.md"
    user_md = profile_dir / "USER_MEMORY.md"
    org_text = org_md.read_text() if org_md.exists() else None
    user_text = user_md.read_text() if user_md.exists() else None
    version = newest_version(profile_dir) or "deployed"

    # --- then write (version last, so its presence marks a completed deploy) ---
    model_store.write_organization(store, datasource, org)
    if examples:
        model_store.write_examples(store, datasource, examples)
    model_store.write_memory(store, datasource, organization=org_text, user=user_text)
    model_store.write_model_version(store, datasource, version)
    store.commit()


def deploy_models(store: Store, artifacts_dir: Path) -> list[str]:
    """Load every datasource model under `artifacts_dir` (a subdir with an `org.yaml`) into the store.
    Returns the datasources loaded. A non-model subdir (e.g. `local/`, which holds credentials) is skipped
    because it has no `org.yaml`."""
    loaded: list[str] = []
    for prof in sorted(p for p in artifacts_dir.iterdir() if (p / "org.yaml").exists()):
        deploy_one(store, prof.name, prof)
        loaded.append(prof.name)
    return loaded


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    store = Store.from_env()
    if store is None:
        print("model_deploy: no database configured (set AGAMI_DB_URL).", file=sys.stderr)
        return 2
    # Ensure the model tables exist before writing — this runs in the deploy entrypoint *before* the
    # server (whose lifespan auto-migrates, ACE-019) is up, so the schema may not be applied yet.
    # run_migrations is idempotent, so the later lifespan pass is a harmless no-op.
    store.run_migrations()
    artifacts_dir = agami_paths.artifacts_dir()
    if not artifacts_dir.is_dir():  # clean exit, not an uncaught FileNotFoundError from iterdir()
        store.close()
        print(f"model_deploy: artifacts dir not found: {artifacts_dir}", file=sys.stderr)
        return 1
    try:
        if args:  # deploy only the named datasources
            loaded: list[str] = []
            for ds in args:
                prof = artifacts_dir / ds
                if not (prof / "org.yaml").exists():
                    print(
                        f"model_deploy: no model for datasource {ds!r} at {prof}/org.yaml",
                        file=sys.stderr,
                    )
                    return 1
                deploy_one(store, ds, prof)
                loaded.append(ds)
        else:  # deploy every model under the artifacts dir
            loaded = deploy_models(store, artifacts_dir)
            if not loaded:
                print(
                    f"model_deploy: no model found under {artifacts_dir} "
                    "(a datasource is a subdir with an org.yaml).",
                    file=sys.stderr,
                )
                return 1
    finally:
        store.close()
    print(f"model_deploy: loaded model for: {', '.join(loaded)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
