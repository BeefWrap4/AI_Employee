# AI Employee Web Portal

React + Vite + Ant Design SPA for the AI Employee telecom operations platform.

## Views

- **总览 (Dashboard)** — RCA operational metrics + raw Prometheus `/metrics` dump.
- **RCA 诊断** — list RCA runs, drill into reports, submit reviews (accept/reject/need-more), import approved candidate knowledge.
- **知识库** — list documents and run natural-language queries with cited evidence.
- **评测中心** — list eval runs and compare two runs side-by-side (per-metric delta).
- **工具注册** — list registered tools (MCP `tools/list` shape) and invoke read-only tools.

## Dev proxy

`vite.config.js` proxies API prefixes to the local backend services:

| Prefix | Service | Port |
|--------|---------|------|
| `/api/knowledge` | knowledge-api | 8010 |
| `/api/rca` | rca-agent | 8020 |
| `/api/platform` | agent-platform-api | 8030 |
| `/api/tools` | tool-registry | 8040 |

## Develop

```bash
cd apps/web-portal
npm install
npm run dev   # http://localhost:5173
```

## Build

```bash
npm run build   # outputs to dist/
```

The static `dist/` bundle is served by the platform's ingress / API gateway in production (see `infra/k8s`).

## Auth

A JWT can be stored in `localStorage` under `ai_employee_jwt`; it is attached as a `Bearer` header on every API call by `src/api.js`.
