#!/bin/sh
# The container's boot sequence: wait for the DB, then migrate + load the model, then serve.
# Fail-closed — a bad migration or model aborts the boot (set -e) rather than serving a broken state.
set -e

# The bundled Postgres may still be coming up; wait for it before model_deploy (which migrates + loads).
# We don't use a compose `depends_on` so the external-DB / cloud-run profiles work without the bundled DB.
python - <<'PY'
import os, sys, time

sys.path.insert(0, "/app/packages/agami-core/src")
from store import Store

url = os.environ.get("AGAMI_DB_URL") or os.environ.get("APP_DATABASE_URL") or ""
for _ in range(60):
    try:
        Store.connect(url).close()
        break
    except Exception:
        time.sleep(1)
else:
    sys.exit("entrypoint: database not reachable after 60s")
PY

python -m model_deploy      # migrate (ACE-019) + load the model into Postgres (ACE-022); fail-closed
exec python -m mcp_http     # serve (also re-migrates + seeds the admin on startup; both idempotent)
