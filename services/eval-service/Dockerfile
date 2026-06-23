# eval-service: CLI/runner image (no HTTP server).  Used to run offline
# RAG/RCA evaluations and emit JSON/Markdown reports.
#   docker build -f services/eval-service/Dockerfile -t ai-employee/eval-service .
#   docker run --rm ai-employee/eval-service python -m ai_employee.eval --help

ARG PYTHON_VERSION=3.11
FROM python:${PYTHON_VERSION}-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        libffi8 libgomp1 tini \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml ./
COPY scripts/docker/requirements_from_pyproject.py /tmp/requirements_from_pyproject.py
RUN python /tmp/requirements_from_pyproject.py pyproject.toml /tmp/requirements.txt --extra dev \
    && pip install --upgrade pip \
    && pip install -r /tmp/requirements.txt
COPY services/ ./services/
COPY packages/ ./packages/
RUN pip install -e . --no-deps

RUN useradd --create-home --uid 10001 appuser && chown -R appuser:appuser /app
USER appuser

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "-m", "ai_employee.eval", "--help"]
