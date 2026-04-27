# ---------------------------------------------------------------------------
# FD Rate Aggregator — backend container
#
# Base image ships with Python + Playwright + Chromium pre-installed,
# avoiding the ~400 MB browser download at cold start.
# ---------------------------------------------------------------------------
FROM mcr.microsoft.com/playwright/python:v1.47.0-jammy

# Don't write .pyc, flush logs, and keep pip quiet
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PORT=8000 \
    LOCAL_RESULTS_ENABLED=false

WORKDIR /app

# Install Python dependencies first (better layer caching)
COPY backend/requirements.txt /app/requirements.txt
RUN pip install -r /app/requirements.txt

# Copy backend source. Frontend, infra, .env, and local result caches are
# excluded by .dockerignore.
COPY backend/ /app/

EXPOSE 8000

# Single worker keeps the in-memory progress buffer (agent/progress.py)
# consistent across requests; threads handle concurrent HTTP calls.
# Long --timeout is required because /api/scrape can run several minutes.
CMD ["gunicorn", \
     "--bind", "0.0.0.0:8000", \
     "--workers", "1", \
     "--threads", "8", \
     "--timeout", "1200", \
     "--access-logfile", "-", \
     "--error-logfile", "-", \
     "dev_server:app"]
