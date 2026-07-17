# The agami MCP server image. Build context is the repo root (see docker-compose.yml `build.context: ..`).
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 HOST=0.0.0.0 PORT=8000

# The migrations + static assets now live INSIDE the package (src/migrations, src/static) and resolve
# next to store.py / mcp_http.py, so copying the package source is enough — no separate COPY needed.
WORKDIR /app
COPY packages/agami-core /app/packages/agami-core
COPY deploy/entrypoint.sh /app/entrypoint.sh
# Install as root, then run as a dedicated unprivileged user (smaller blast radius if the app is compromised).
RUN pip install --no-cache-dir -e "/app/packages/agami-core[server,model]" \
 && chmod +x /app/entrypoint.sh \
 && useradd --create-home --uid 10001 agami \
 && chown -R agami:agami /app
USER agami

ENTRYPOINT ["/app/entrypoint.sh"]
