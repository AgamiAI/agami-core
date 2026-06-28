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
    Idempotent — the model_store writers clear each datasource's rows before re-inserting."""
    from semantic_model import loader
    from semantic_model.snapshot import newest_version

    org = loader.load_organization(profile_dir)
    model_store.write_organization(store, datasource, org)

    # Examples live per subject area (prompt_examples/<area>/examples.yaml); tag each with its area so the
    # served row carries it (write_examples reads ex["area"]).
    examples: list[dict] = []
    for sa in org.subject_areas:
        for ex in loader.list_prompt_examples(profile_dir, sa.name):
            examples.append({**ex, "area": ex.get("area") or sa.name})
    if examples:
        model_store.write_examples(store, datasource, examples)

    org_md = profile_dir / "ORGANIZATION.md"
    user_md = profile_dir / "USER_MEMORY.md"
    model_store.write_memory(
        store,
        datasource,
        organization=org_md.read_text() if org_md.exists() else None,
        user=user_md.read_text() if user_md.exists() else None,
    )
    model_store.write_model_version(store, datasource, newest_version(profile_dir) or "deployed")
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
