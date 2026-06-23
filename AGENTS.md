# Repository Guidelines

## Project Structure & Module Organization

This monorepo contains telecom-ops AI services and platform components. Source code lives under `services/<name>/src/ai_employee/<name>/` for FastAPI apps and `packages/<name>/src/ai_employee/<name>/` for shared libraries. The React portal is in `apps/web-portal/`. Tests live in `tests/`, with fixtures such as `tests/rca-replay/` and `tests/rag-eval/`. Architecture specs and phased plans are in `Docs/`; deployment assets are in `infra/docker-compose`, `infra/k8s`, and `infra/helm`.

## Build, Test, and Development Commands

Use Miniconda as the standard Python environment:

```powershell
conda env create -f environment.yml
conda activate ai-employee
python -m pip install -e ".[dev]"
pytest tests --ignore=tests/test_local_ci.py -q
```

Run individual services with `uvicorn`, for example:

```powershell
uvicorn ai_employee.knowledge_api.app:app --port 8010 --app-dir services/knowledge-api/src
uvicorn ai_employee.rca_agent.app:app --port 8020 --app-dir services/rca-agent/src
uvicorn ai_employee.agent_platform_api.app:app --port 8030 --app-dir services/agent-platform-api/src
```

Useful checks include `pytest tests/test_local_ci.py -q` for the local CI aggregator, `ruff check .`, `ruff format .`, `python scripts/m1_smoke.py --json`, and `python -m ai_employee.rca_agent.replay tests/rca-replay/sample_cases.jsonl --json`.

## Coding Style & Naming Conventions

Python modules use 4-space indentation, `from __future__ import annotations`, Pydantic models for API contracts, and explicit FastAPI response models. Service packages follow `ai_employee.<service_name>` with underscore package names even when directories use hyphens. Keep clients pluggable: prefer `Protocol` plus in-memory and HTTP implementations when crossing service boundaries.

## Testing Guidelines

The main framework is `pytest`; tests are named `tests/test_*.py`. Add focused tests before implementation, then run the targeted test and `pytest tests --ignore=tests/test_local_ci.py -q`. `pytest.ini` already sets Python paths for service and package sources, so avoid manual `PYTHONPATH`.

## Commit & Pull Request Guidelines

History uses concise Conventional Commit style, often with scope, for example `feat: add agent platform tool registry` or `fix(r35-b): make kind-smoke.sh idempotent`. Keep commits small and stage explicit paths; avoid `git add -A` from the repo root. PRs should describe behavior changes, list verification commands, link related specs/issues, and include screenshots for portal UI changes.

## Security & Configuration Tips

Do not commit secrets or local editor settings. Most services default to in-memory or local backends unless env vars such as `RCA_SQLITE_PATH`, `APPROVAL_SERVICE_URL`, or `MCP_GATEWAY_URL` are set. High-risk operations should remain approval-gated and auditable.
