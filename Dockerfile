# ---------------------------------------------------------------------------
# Stage 1 — dependency installation
# ---------------------------------------------------------------------------
FROM python:3.11-slim AS builder

WORKDIR /build

# Install only what's needed to compile any C extensions in dependencies.
RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
 && pip install --no-cache-dir --prefix=/install -r requirements.txt

# ---------------------------------------------------------------------------
# Stage 2 — runtime image
# ---------------------------------------------------------------------------
FROM python:3.11-slim AS runtime

# Security: run as a non-root user.
RUN groupadd --gid 10001 appgroup \
 && useradd --uid 10001 --gid appgroup --no-create-home appuser

WORKDIR /app

# Copy installed packages from the builder stage.
COPY --from=builder /install /usr/local

# Copy application source and the entrypoint script.
COPY app/ ./app/
COPY entrypoint.sh ./entrypoint.sh

# Non-root user has no write access to /app; the app itself only reads files.
# Use a writable /tmp for any runtime temp files.
RUN chown -R appuser:appgroup /app \
 && chmod +x ./entrypoint.sh

USER appuser

# Expose the application port (default 8000; overridable via APP_PORT / MCP_PORT).
EXPOSE 8000

# Healthcheck — works for both REST (GET /health on APP_PORT) and MCP HTTP
# (GET /health on MCP_PORT) modes.  MCP stdio has no HTTP port, but the health
# check never fires for that transport because the container exits when the stdio
# session ends.
# MCP_PORT takes precedence over APP_PORT so that mode=mcp containers probe the
# correct port without a separate Dockerfile layer.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:${MCP_PORT:-${APP_PORT:-8000}}/health')" \
    || exit 1

# Default: REST API mode.  Override MODE=mcp to start the MCP server.
# The entrypoint reads MODE (api|mcp) and MCP_TRANSPORT (stdio|http).
CMD ["/app/entrypoint.sh"]
