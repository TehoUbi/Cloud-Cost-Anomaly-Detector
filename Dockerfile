# syntax=docker/dockerfile:1
#
# API image. Two stages so the wheel build toolchain (scikit-learn and asyncpg
# both compile) stays out of the runtime layer.

FROM python:3.12-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build
COPY requirements.txt .
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --upgrade pip \
    && /opt/venv/bin/pip install -r requirements.txt


FROM python:3.12-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH"

# curl is here for the healthcheck below, not for convenience.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 sentinel

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app
COPY --chown=sentinel:sentinel alembic.ini pyproject.toml ./
COPY --chown=sentinel:sentinel migrations ./migrations
COPY --chown=sentinel:sentinel src ./src
COPY --chown=sentinel:sentinel scripts ./scripts

# Unprivileged: nothing this process does needs root, and a container that
# runs as root turns a code-execution bug into a host problem.
USER sentinel

EXPOSE 8000

# Hits /health, which deliberately does not touch the database — see
# src/api/routes/health.py.
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD curl -fsS http://localhost:8000/health || exit 1

CMD ["uvicorn", "src.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
