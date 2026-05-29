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

# Copy application source.
COPY app/ ./app/

# Non-root user has no write access to /app; the app itself only reads files.
# Use a writable /tmp for any runtime temp files.
RUN chown -R appuser:appgroup /app

USER appuser

# Expose the application port (default 8000; overridable via APP_PORT env var).
EXPOSE 8000

# Healthcheck so Docker / Kubernetes knows when the container is ready.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:${APP_PORT:-8000}/health')" \
    || exit 1

# Default command — APP_PORT is read by the app; we also pass it to uvicorn
# via the shell so Kubernetes can override it with an env var.
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${APP_PORT:-8000} --log-level ${LOG_LEVEL:-info}"]
