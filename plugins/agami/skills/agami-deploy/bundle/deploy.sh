#!/bin/sh
# Bring up the agami stack from this prepared bundle. `/agami-deploy` already validated + filled your
# .env locally (signing secret generated, host derived), so this just pulls the published image and runs.
set -e
cd "$(dirname "$0")"

docker compose pull
docker compose up -d

echo "agami is starting. It's live at your PUBLIC_BASE_URL once Caddy issues the certificate (a few seconds)."
echo "Share  \${PUBLIC_BASE_URL}/mcp  with your team; manage users at  \${PUBLIC_BASE_URL}/admin"
