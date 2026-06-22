"""api-gateway: single ingress-level API gateway (R32-A, spec §三 §5.1)."""

from ai_employee.api_gateway.app import (
    DEFAULT_BACKEND_URLS,
    ROUTE_TABLE,
    AuditMiddleware,
    BackendProxy,
    HttpBackendProxy,
    create_app,
)

__all__ = [
    "DEFAULT_BACKEND_URLS",
    "ROUTE_TABLE",
    "AuditMiddleware",
    "BackendProxy",
    "HttpBackendProxy",
    "create_app",
]
