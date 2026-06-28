# The agami MCP server image. Build context is the repo root (see docker-compose.yml `build.context: ..`).
FROM python:3.12-slim

# An editable install keeps the package source at /app/packages/agami-core/src, so the package's relative
# migrations path (store.py resolves parents[3]/migrations/core) points at /app/migrations.
WORKDIR /app
COPY packages/agami-core /app/packages/agami-core
COPY migrations /app/migrations
COPY deploy/entrypoint.sh /app/entrypoint.sh
RUN pip install --no-cache-dir -e "/app/packages/agami-core[server,model]" \
 && chmod +x /app/entrypoint.sh

ENV HOST=0.0.0.0 PORT=8000
ENTRYPOINT ["/app/entrypoint.sh"]
