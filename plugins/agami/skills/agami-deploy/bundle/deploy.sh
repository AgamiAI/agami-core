#!/bin/sh
# Bring up the agami stack from this prepared bundle. `/agami-deploy` already validated + filled your
# agami.env locally (signing secret generated, host derived), so this just pulls the published image and runs.
# `--env-file agami.env` on every call: compose doesn't auto-load it (visible name, not the hidden `.env`).
set -e
cd "$(dirname "$0")"

docker compose --env-file agami.env pull
docker compose --env-file agami.env up -d

echo "agami is starting. It's live at your PUBLIC_BASE_URL once Caddy issues the certificate (a few seconds)."
echo "Share  \${PUBLIC_BASE_URL}/mcp  with your team; manage users at  \${PUBLIC_BASE_URL}/admin"
