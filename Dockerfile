# ── Stage 1: Builder ─────────────────────────────────────────────────────────
# Install dependencies in an isolated layer so the final image only carries
# the compiled packages, not build tools or pip cache.
FROM python:3.12-slim AS builder

WORKDIR /build

# gcc is required by some transitive C-extension wheels (e.g. langdetect)
RUN apt-get update \
    && apt-get install -y --no-install-recommends gcc \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .

RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir --prefix=/install -r requirements.txt


# ── Stage 2: Runtime ─────────────────────────────────────────────────────────
# Lean production image — no build tools, no cache, minimal attack surface.
FROM python:3.12-slim AS runtime

# ── Security: non-root user ───────────────────────────────────────────────────
RUN groupadd --system appgroup \
    && useradd --system --gid appgroup --no-create-home appuser

WORKDIR /app

# Copy only the installed packages from the builder stage
COPY --from=builder /install /usr/local

# Copy application source (order: least-to-most frequently changed for cache)
COPY requirements.txt .
COPY src/            ./src/
COPY templates/      ./templates/
COPY main.py         .

# Hand ownership to the non-root user
RUN chown -R appuser:appgroup /app

USER appuser

# ── Runtime configuration ─────────────────────────────────────────────────────
# Cloud Run injects $PORT automatically; fall back to 8080 for local docker run.
# Do NOT set GOOGLE_APPLICATION_CREDENTIALS here — Cloud Run uses ADC (Workload Identity).
# Pass all other settings (GOOGLE_CLOUD_PROJECT, VERTEX_AI_ENDPOINTS, etc.)
# as Cloud Run environment variables or via Secret Manager, never baked into the image.
ENV PORT=8080 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

EXPOSE 8080

# exec form + shell wrapper lets $PORT expand at runtime while keeping PID 1
# so Cloud Run's graceful-shutdown SIGTERM is delivered to the uvicorn process.
CMD ["sh", "-c", "exec uvicorn main:app --host 0.0.0.0 --port ${PORT}"]
