# API and worker image. One image; the command picks the role.
FROM python:3.12.12-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 UV_LINK_MODE=copy UV_PROJECT_ENVIRONMENT=/app/.venv
COPY --from=ghcr.io/astral-sh/uv:0.8.22 /uv /usr/local/bin/uv
WORKDIR /app/backend
COPY backend/pyproject.toml backend/uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY backend/src ./src
COPY backend/data ./data
# Fake mode serves this synthetic page offline; it is never used with real providers.
COPY backend/tests/fixtures/sfmta_contest.html ./tests/fixtures/sfmta_contest.html
RUN uv sync --frozen --no-dev
RUN chmod -R a+rX /app/backend
# /monitor is the T24 monitor's incident state; a fresh named volume copies this owner.
RUN useradd --create-home --uid 10001 app && mkdir -p /data /monitor && chown app /data /monitor
USER app
ENV PATH=/app/.venv/bin:$PATH PB_DATA_DIR=/data
EXPOSE 8000
CMD ["uvicorn", "parkingbreaker.api.app:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]

# Prepared optional runtime dependencies only (PDF rasterization, OCR, headless renderer).
# Nothing imports or starts them and no renderer/OCR hardening is enabled; production keeps
# building the last stage (runtime = base) until the sandbox jail qualifies this target.
FROM base AS document-tools
USER root
RUN apt-get update \
    && apt-get install -y --no-install-recommends poppler-utils tesseract-ocr chromium \
    && rm -rf /var/lib/apt/lists/*
USER app

# Default (last) stage: exactly the base image above.
FROM base AS runtime
ARG PB_BUILD_COMMIT
LABEL org.opencontainers.image.revision=$PB_BUILD_COMMIT
