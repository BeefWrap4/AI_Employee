# Service Dockerfile.  Build context = repo root:
#   docker build -f services/mcp-gateway/Dockerfile -t ai-employee/mcp-gateway .
#
# All Python services share the same install step (editable monorepo so
# cross-package imports resolve).  Only APP_MODULE / APP_PORT differ.

ARG PYTHON_VERSION=3.11
FROM python:${PYTHON_VERSION}-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1     PYTHONUNBUFFERED=1     PIP_NO_CACHE_DIR=1     PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update && apt-get install -y --no-install-recommends         libffi8 libgomp1 tini     && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml ./
COPY services/ ./services/
COPY packages/ ./packages/
RUN pip install --upgrade pip && pip install -e ".[dev]" PyJWT

RUN useradd --create-home --uid 10001 appuser && chown -R appuser:appuser /app
USER appuser

ARG APP_MODULE=ai_employee.mcp_gateway.app:app
ARG APP_PORT=8050
ENV APP_MODULE=${APP_MODULE} APP_PORT=${APP_PORT}
EXPOSE ${APP_PORT}

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["sh", "-c", "exec python -m uvicorn ${APP_MODULE} --host 0.0.0.0 --port ${APP_PORT}"]
