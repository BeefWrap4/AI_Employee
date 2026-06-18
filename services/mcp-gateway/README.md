# mcp-gateway

Unified MCP-compatible tool gateway (spec §9 deployable unit
`mcp-gateway`).  The single front door for tool registration,
discovery (MCP `tools/list`), routing, and invocation, with a
per-tool circuit breaker (spec §5.3).

Reuses `ai_employee.common_schemas.tool_registry.ToolRegistry` for the
in-process registry + MCP shape.  The agent-platform delegates tool
calls here over HTTP via `McpGatewayClient` when `MCP_GATEWAY_URL` is
set (see `agent_platform_api/clients.py`).

## Endpoints

| Method | Path | Purpose |
| --- | --- | --- |
| GET  | `/health` | liveness |
| POST | `/api/v1/tools` | register a tool |
| GET  | `/api/v1/tools` | list (MCP `tools/list` shape, `?service_name=`) |
| GET  | `/api/v1/tools/{name}` | fetch one |
| POST | `/api/v1/tools/{name}/invoke` | invoke a registered tool |
| GET  | `/api/v1/tools/{name}/health` | circuit-breaker state |

A built-in `echo` tool is seeded on startup.  Tools registered over
HTTP without a `handler_kind` are listable but not invokable (the
tool-registry contract).  `handler_kind: "echo" | "noop"` binds a demo
handler so smoke tests can exercise invocation.

## Run

```bash
docker build -f services/mcp-gateway/Dockerfile -t ai-employee/mcp-gateway .
docker run -p 8050:8050 ai-employee/mcp-gateway
```
