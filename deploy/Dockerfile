# The agami MCP server image. Build context is the repo root (see docker-compose.yml `build.context: ..`).
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 HOST=0.0.0.0 PORT=8000

# An editable install keeps the package source at /app/packages/agami-core/src, so the package's relative
# migrations path (store.py resolves parents[3]/migrations/core) points at /app/migrations.
WORKDIR /app
COPY packages/agami-core /app/packages/agami-core
COPY migrations /app/migrations
COPY deploy/entrypoint.sh /app/entrypoint.sh
# Install as root, then run as a dedicated unprivileged user (smaller blast radius if the app is compromised).
RUN pip install --no-cache-dir -e "/app/packages/agami-core[server,model]" \
 && chmod +x /app/entrypoint.sh \
 && useradd --create-home --uid 10001 agami \
 && chown -R agami:agami /app
USER agami

ENTRYPOINT ["/app/entrypoint.sh"]
