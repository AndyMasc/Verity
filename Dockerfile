# Build & Dependencies
FROM python:3.13-slim-bookworm AS builder

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# Install system dependencies needed to compile Python packages
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Upgrade pip and build dependencies inside a virtual environment
# (BuildKit cache mount keeps pip's HTTP cache across rebuilds without bloating the image)
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
RUN --mount=type=cache,target=/root/.cache/pip pip install --upgrade pip

# Install Python dependencies
COPY requirements.txt .
RUN --mount=type=cache,target=/root/.cache/pip pip install -r requirements.txt


# Runtime Production Image
FROM python:3.13-slim-bookworm AS runner

ENV PYTHONUNBUFFERED=1
ENV DJANGO_SETTINGS_MODULE=Verity.settings
# Dispatcher in Verity/settings/__init__.py loads production.py when set.
# Overridable at runtime via env_file/Coolify envs.
ENV DJANGO_ENV=production
ENV PATH="/opt/venv/bin:$PATH"
ENV VIRTUAL_ENV="/opt/venv"

WORKDIR /app

# Install runtime-only essentials (curl for health check, libpq for Postgres)
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Create a secure, non-root user to run the application
RUN useradd -m -U django && chown -R django:django /app
USER django

# Copy compiled dependencies from the builder stage
COPY --from=builder /opt/venv /opt/venv

# Copy the codebase
COPY --chown=django:django . .

EXPOSE 8000

# Health check (uses previously installed curl to check if the application is responding)
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:8000/core/health/ || exit 1

# Fallback if no start command is given on the Coolify dash: runs the `web` process.
# In prod, run multiple instances of the app. One for web, one for each dramatiq
# queue (each uses a different number of threads), one for periodiq — see Procfile.
# Uses the ASGI app with uvicorn's worker, matching Procfile's `web` process.
CMD ["gunicorn", "Verity.asgi:application", "-k", "uvicorn.workers.UvicornWorker", "--bind", "0.0.0.0:8000", "--workers", "2", "--timeout", "120"]
