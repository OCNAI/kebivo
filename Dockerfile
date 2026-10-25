# Build the static frontend with Node, then copy its output into the Python image.
# The output is the same on every platform, so build it natively.
FROM --platform=$BUILDPLATFORM node:24-alpine AS frontend-build
WORKDIR /build/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
COPY config/project.json /build/config/project.json
RUN npm run build

FROM python:3.12-slim AS kebivo-runtime
ARG KEBIVO_VERSION=local
ARG KEBIVO_REVISION=unknown
ARG KEBIVO_CREATED=1970-01-01T00:00:00Z
ARG KEBIVO_SOURCE=https://github.com/OCNAI/kebivo
LABEL org.opencontainers.image.title="Kebivo" \
      org.opencontainers.image.version="${KEBIVO_VERSION}" \
      org.opencontainers.image.revision="${KEBIVO_REVISION}" \
      org.opencontainers.image.created="${KEBIVO_CREATED}" \
      org.opencontainers.image.source="${KEBIVO_SOURCE}" \
      org.opencontainers.image.licenses="Apache-2.0"
# MCP Registry ownership check: must equal "name" in server.json.
LABEL io.modelcontextprotocol.server.name="io.github.OCNAI/kebivo"
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    APP_ENV=production \
    DATABASE_URL=sqlite:////data/app.sqlite3
WORKDIR /app

COPY pyproject.toml uv.lock README.md ./
COPY kebivo ./kebivo
COPY migrations ./migrations
COPY scripts/sqlite_maintenance.py ./scripts/sqlite_maintenance.py
COPY alembic.ini ./alembic.ini
COPY config ./config
COPY --from=ghcr.io/astral-sh/uv:0.13.1 /uv /usr/local/bin/uv
RUN uv sync --frozen --no-dev --no-install-project

COPY --from=frontend-build /build/frontend/dist ./frontend/dist
COPY docker/entrypoint.sh /usr/local/bin/kebivo-entrypoint
RUN chmod +x /usr/local/bin/kebivo-entrypoint \
    && mkdir -p /data \
    && useradd --system --uid 10001 --home-dir /app appuser \
    && chown -R appuser:appuser /app /data

ENV PATH="/app/.venv/bin:$PATH"
VOLUME ["/data"]
EXPOSE 8000
USER appuser
ENTRYPOINT ["/usr/local/bin/kebivo-entrypoint"]
CMD ["uvicorn", "kebivo.app:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]

# Acceptance-suite runner (compose.e2e.yaml, scripts/e2e.sh): the runtime image
# plus the dev extras, Node for the TypeScript example, curl, and the tests.
FROM node:24-slim AS node-binary

FROM kebivo-runtime AS e2e
USER root
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*
COPY --from=node-binary /usr/local/bin/node /usr/local/bin/node
RUN uv sync --frozen --all-extras --no-install-project
COPY scripts/smoke-webhook.py scripts/fake_llm_server.py ./scripts/
COPY docs ./docs
COPY examples ./examples
COPY frontend/src/integrationSnippets.ts ./frontend/src/integrationSnippets.ts
COPY e2e ./e2e
ENTRYPOINT []
CMD ["python", "e2e/sidecar.py"]

# The last stage is the default build target: keep it the runtime image.
FROM kebivo-runtime AS runtime
