#!/bin/sh
# One-shot deploy from this dir: validate/complete .env, then build + bring the stack up.
# The preflight needs the agami-core package on PATH (the /agami-deploy skill runs it via uvx; manually:
# `pip install -e ../packages/agami-core`).
set -e
cd "$(dirname "$0")"

python -m deploy_preflight .env       # validate hard-floor inputs; generate+persist the signing secret; derive the host
docker compose up -d --build

echo "agami is starting. It's live at your PUBLIC_BASE_URL once Caddy issues the certificate (a few seconds)."
echo "Share  \${PUBLIC_BASE_URL}/mcp  with your team; manage users at  \${PUBLIC_BASE_URL}/admin"
