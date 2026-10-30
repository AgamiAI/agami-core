# The agami MCP server image. Build context is the repo root (see docker-compose.yml `build.context: ..`).
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 HOST=0.0.0.0 PORT=8000

# Upgrade pip first, from a hash-pinned requirement: the pip bundled with python:3.12-slim vendors a
# certifi that still trusts the eMudhra (emSign) intermediates, and it would otherwise both fetch every
# package this image installs and stay in the image. The hash, not the old pip's TLS, is what makes
# this one download trustworthy. Bump the version and the sha256 together, from
# https://pypi.org/project/pip/#files.
RUN printf 'pip==26.2.2 --hash=sha256:fd7f4dfa8190e014013b9f08f3e973f4e4c26ffd179a204895533a7254813a70\n' > /tmp/pip.txt \
 && pip install --no-cache-dir --require-hashes -r /tmp/pip.txt \
 && rm /tmp/pip.txt

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
