"""Langfuse LLM trace emitter (spec §6).

Buffers LLM call traces and flushes them to the Langfuse HTTP ingestion
endpoint.  Each call to :meth:`record_llm_call` appends a single
"generation" record; :meth:`flush` posts the buffer as one batch.  The
emitter is process-local and safe to call from worker threads.

Env vars consumed by :func:`build_langfuse_emitter`:

* ``LANGFUSE_ENABLED`` (default ``0``) — must be truthy to enable the
  HTTP backend.  When disabled, :meth:`record_llm_call` and
  :meth:`flush` are both no-ops, which is the safe default for tests
  and dev environments.
* ``LANGFUSE_PUBLIC_KEY`` / ``LANGFUSE_SECRET_KEY`` — HTTP Basic auth.
* ``LANGFUSE_HOST`` (default ``https://cloud.langfuse.com``) — base URL
  for the ``/api/public/ingestion`` endpoint.

Network failures during flush are caught and reported in the return
value rather than raised — losing telemetry must never break an agent
run.
"""
from __future__ import annotations

import base64
import json
import os
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol


class _HttpClient(Protocol):
    """Minimal interface so tests can inject a fake without httpx/requests."""

    def post(
        self, url: str, *, headers: dict[str, str], content: str, timeout: float,
    ) -> Any: ...


def _default_http_client() -> _HttpClient:
    """Lazy import httpx so the module loads without it installed."""
    import httpx

    return httpx.Client()


@dataclass
class LangfuseEmitter:
    enabled: bool
    public_key: str
    secret_key: str
    host: str = "https://cloud.langfuse.com"
    timeout_s: float = 5.0
    client: _HttpClient | None = None
    _buffer: list[dict[str, Any]] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def _ingestion_url(self) -> str:
        host = self.host.rstrip("/")
        return f"{host}/api/public/ingestion"

    def _auth_header(self) -> dict[str, str]:
        token = base64.b64encode(
            f"{self.public_key}:{self.secret_key}".encode("utf-8"),
        ).decode("ascii")
        return {
            "Authorization": f"Basic {token}",
            "Content-Type": "application/json",
            "X-Langfuse-Sdk-Name": "ai-employee-observability",
            "X-Langfuse-Sdk-Version": "0.1.0",
        }

    def record_llm_call(
        self,
        *,
        trace_id: str,
        span_id: str,
        model: str,
        prompt: str,
        response: str,
        latency_ms: float,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Buffer a single LLM call.  No-op when the emitter is disabled."""
        if not self.enabled:
            return
        record = {
            "id": trace_id,
            "type": "generation-create",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "body": {
                "id": span_id,
                "traceId": trace_id,
                "model": model,
                "input": [{"role": "user", "content": prompt}],
                "output": {"role": "assistant", "content": response},
                "usage": {
                    "unit": "MILLISECONDS",
                    "input": 0,
                    "output": 0,
                    "total": int(latency_ms),
                },
                "metadata": metadata or {},
            },
        }
        with self._lock:
            self._buffer.append(record)

    def flush(self) -> dict[str, Any]:
        """POST the buffer to Langfuse.  Returns a small status dict.

        ``dispatched`` is the count of records posted (0 on no-op or
        failure).  ``error`` carries the failure reason when present.
        """
        if not self.enabled:
            return {"dispatched": 0, "skipped": "disabled"}
        with self._lock:
            batch = list(self._buffer)
            self._buffer.clear()
        if not batch:
            return {"dispatched": 0}
        client = self.client or _default_http_client()
        body = {"batch": batch}
        try:
            resp = client.post(
                self._ingestion_url(),
                headers=self._auth_header(),
                content=json.dumps(body),
                timeout=self.timeout_s,
            )
            status_code = getattr(resp, "status_code", 200)
            if status_code >= 400:
                return {
                    "dispatched": 0,
                    "error": f"http {status_code}",
                    "queued": len(batch),
                }
            return {"dispatched": len(batch)}
        except Exception as exc:  # noqa: BLE001 — telemetry must never crash
            return {
                "dispatched": 0,
                "error": str(exc),
                "queued": len(batch),
            }


def build_langfuse_emitter(
    *,
    enabled: bool | None = None,
    public_key: str | None = None,
    secret_key: str | None = None,
    host: str | None = None,
) -> LangfuseEmitter:
    """Construct a :class:`LangfuseEmitter` from env or explicit args.

    Args left as ``None`` are read from the matching ``LANGFUSE_*`` env
    var.  ``enabled`` defaults to the env flag (false when unset).
    """
    env_enabled = os.environ.get("LANGFUSE_ENABLED", "").lower() in {
        "1", "true", "yes", "on",
    }
    return LangfuseEmitter(
        enabled=env_enabled if enabled is None else enabled,
        public_key=public_key if public_key is not None else os.environ.get("LANGFUSE_PUBLIC_KEY", ""),
        secret_key=secret_key if secret_key is not None else os.environ.get("LANGFUSE_SECRET_KEY", ""),
        host=host if host is not None else os.environ.get(
            "LANGFUSE_HOST", "https://cloud.langfuse.com",
        ),
    )


__all__ = [
    "LangfuseEmitter",
    "build_langfuse_emitter",
]
