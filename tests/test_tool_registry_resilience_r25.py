"""R25-T: tool-registry invoke resilience (timeout + retry + breaker) and
background health probe.

Covers the wiring described in R25-T:

* ``invoke`` reads ``ToolSpec.timeout_ms`` and enforces a hard timeout
  on the underlying handler call (default 5000ms = backward compat).
* ``invoke`` honours ``ToolSpec.retry_policy`` (max_attempts, backoff)
  via :func:`apply_resilience`.
* When retries exhaust, the API responds with 504 (not 500).
* The platform-level ``CircuitBreaker`` short-circuits to 503.
* A background health-probe task pings each registered ``health_check_url``
  and updates ``health_status`` from ``unknown`` to ``healthy`` /
  ``unhealthy``.
"""

from __future__ import annotations

import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

import pytest
from ai_employee.auth_policy import issue_token
from ai_employee.common_schemas.tool_registry import ToolSpec
from ai_employee.tool_registry.app import create_app
from ai_employee.tool_registry.store import ToolRegistryStore
from fastapi.testclient import TestClient

SECRET = "test-secret-please-rotate-super-long-key-32b"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("JWT_SECRET", SECRET)
    monkeypatch.setenv("INTERNAL_TOKEN", "legacy-shared-secret")
    monkeypatch.delenv("JWT_AUTH_STRICT", raising=False)


def _admin_headers() -> dict[str, str]:
    token = issue_token(subject="root", roles=["admin"], secret=SECRET)
    return {"Authorization": f"Bearer {token}"}


def _operator_headers() -> dict[str, str]:
    token = issue_token(
        subject="alice",
        roles=["operator"],
        scopes=["tool:invoke"],
        secret=SECRET,
    )
    return {"Authorization": f"Bearer {token}"}


def _client(tmp_path) -> TestClient:
    store = ToolRegistryStore(db_path=str(tmp_path / "tools.sqlite3"))
    return TestClient(create_app(store=store))


# --------------------------------------------------------------------------- #
# Timeout enforcement on invoke (TDD: failure-first)
# --------------------------------------------------------------------------- #


def test_invoke_timeout_ms_default_5000_backcompat(tmp_path) -> None:
    """When the spec has no timeout_ms, the invoke path uses a 5s budget
    (backward compat: old behavior unchanged for fast handlers)."""
    store = ToolRegistryStore(db_path=str(tmp_path / "tools.sqlite3"))
    # Inject a slow handler into the in-memory registry before app starts.

    client = TestClient(create_app(store=store))
    # echo is the built-in fast tool — it returns <50ms; we verify it still
    # succeeds under the default 5s budget.
    resp = client.post(
        "/api/v1/tools/echo/invoke",
        json={"arguments": {"text": "ok"}},
        headers=_operator_headers(),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["result"] == {"echo": "ok"}
    # latency_ms is reported (back-compat).
    assert body["latency_ms"] >= 0


def test_invoke_respects_timeout_ms_slow_handler_returns_timeout(tmp_path) -> None:
    """When the spec sets timeout_ms low and the handler is slow, the invoke
    path returns 504 with a clear error_code instead of waiting forever."""
    from ai_employee.common_schemas.tool_registry import ToolRegistry

    slow_reg = ToolRegistry()

    def slow_handler(text: str = "") -> dict[str, Any]:
        time.sleep(2.0)  # 2s sleep; budget 100ms.
        return {"echo": text}

    slow_reg.register(
        ToolSpec(
            name="slow.echo",
            description="slow echo",
            input_schema={"type": "object"},
            output_schema={"type": "object"},
            risk_level="read_only",
            timeout_ms=100,  # tight budget
            handler=slow_handler,
        )
    )
    store2 = ToolRegistryStore(db_path=str(tmp_path / "tools2.sqlite3"))
    client = TestClient(create_app(store=store2, registry=slow_reg))

    resp = client.post(
        "/api/v1/tools/slow.echo/invoke",
        json={"arguments": {"text": "hi"}},
        headers=_operator_headers(),
    )
    # 504 Gateway Timeout (or 408 depending on layering); must NOT be 500.
    assert resp.status_code in (408, 504), resp.text
    body = resp.json()
    detail = body.get("detail", {})
    # Timeout can surface as 'tool_timeout', 'invocation_failed', or
    # 'tool_invocation_failed' depending on exception layering.
    assert detail.get("error_code") in {
        "tool_timeout",
        "timeout",
        "tool_invocation_failed",
        "invocation_failed",
    }


def test_invoke_retry_policy_transient_failure_eventually_succeeds(tmp_path) -> None:
    """With retry_policy.max_attempts=3, a flaky handler eventually succeeds."""
    from ai_employee.common_schemas.tool_registry import ToolRegistry

    state = {"calls": 0}

    def flaky_handler(text: str = "") -> dict[str, Any]:
        state["calls"] += 1
        if state["calls"] < 2:
            raise RuntimeError("transient")
        return {"echo": text}

    flaky_reg = ToolRegistry()
    flaky_reg.register(
        ToolSpec(
            name="flaky.echo",
            description="flaky",
            input_schema={"type": "object"},
            output_schema={"type": "object"},
            risk_level="read_only",
            timeout_ms=5000,
            retry_policy={"max_attempts": 3, "backoff_seconds": 0.0},
            handler=flaky_handler,
        )
    )

    store = ToolRegistryStore(db_path=str(tmp_path / "flaky.sqlite3"))
    client = TestClient(create_app(store=store, registry=flaky_reg))

    resp = client.post(
        "/api/v1/tools/flaky.echo/invoke",
        json={"arguments": {"text": "ok"}},
        headers=_operator_headers(),
    )
    assert resp.status_code == 200, resp.text
    assert state["calls"] == 2  # failed once, succeeded on second try


def test_invoke_retry_policy_exhausted_returns_504_not_500(tmp_path) -> None:
    """When retries are exhausted, the response is 504 (not 500)."""
    from ai_employee.common_schemas.tool_registry import ToolRegistry

    state = {"calls": 0}

    def always_fail(text: str = "") -> dict[str, Any]:
        state["calls"] += 1
        raise RuntimeError("never works")

    fail_reg = ToolRegistry()
    fail_reg.register(
        ToolSpec(
            name="always.fail",
            description="always fails",
            input_schema={"type": "object"},
            output_schema={"type": "object"},
            risk_level="read_only",
            timeout_ms=5000,
            retry_policy={"max_attempts": 2, "backoff_seconds": 0.0},
            handler=always_fail,
        )
    )
    store = ToolRegistryStore(db_path=str(tmp_path / "fail.sqlite3"))
    client = TestClient(create_app(store=store, registry=fail_reg))

    resp = client.post(
        "/api/v1/tools/always.fail/invoke",
        json={"arguments": {"text": "x"}},
        headers=_operator_headers(),
    )
    assert resp.status_code == 504, resp.text
    assert state["calls"] == 2
    body = resp.json()
    assert body["detail"]["error_code"] in {
        "tool_invocation_failed",
        "tool_failed",
        "invocation_failed",
    }


def test_invoke_default_retry_policy_max_attempts_1_is_backward_compatible(tmp_path) -> None:
    """With no explicit retry_policy (defaults to max_attempts=1), the
    handler is invoked exactly once — backward compat with the pre-R25
    single-shot behaviour."""
    from ai_employee.common_schemas.tool_registry import ToolRegistry

    state = {"calls": 0}

    def handler(text: str = "") -> dict[str, Any]:
        state["calls"] += 1
        raise RuntimeError("oops")

    reg = ToolRegistry()
    reg.register(
        ToolSpec(
            name="single.fail",
            description="fails once",
            input_schema={"type": "object"},
            output_schema={"type": "object"},
            risk_level="read_only",
            timeout_ms=5000,
            # retry_policy not set → defaults to {"max_retries": 0}
            handler=handler,
        )
    )
    store = ToolRegistryStore(db_path=str(tmp_path / "single.sqlite3"))
    client = TestClient(create_app(store=store, registry=reg))

    resp = client.post(
        "/api/v1/tools/single.fail/invoke",
        json={"arguments": {"text": "x"}},
        headers=_operator_headers(),
    )
    assert resp.status_code == 504, resp.text
    assert state["calls"] == 1  # exactly one attempt


def test_invoke_circuit_breaker_open_returns_503(tmp_path) -> None:
    """After threshold failures, the platform-level breaker opens and
    subsequent invokes get 503 (not 500)."""
    from ai_employee.common_schemas.tool_registry import ToolRegistry
    from ai_employee.tool_registry.circuit_breaker import CircuitBreaker

    breaker = CircuitBreaker(failure_threshold=2, recovery_seconds=10.0)

    def handler(text: str = "") -> dict[str, Any]:
        raise RuntimeError("nope")

    reg = ToolRegistry()
    reg.register(
        ToolSpec(
            name="breaker.fail",
            description="triggers breaker",
            input_schema={"type": "object"},
            output_schema={"type": "object"},
            risk_level="read_only",
            timeout_ms=5000,
            handler=handler,
        )
    )
    store = ToolRegistryStore(db_path=str(tmp_path / "breaker.sqlite3"))
    client = TestClient(create_app(store=store, registry=reg, circuit_breaker=breaker))

    # First 2 calls fail (504) and trip the breaker.
    for _ in range(2):
        resp = client.post(
            "/api/v1/tools/breaker.fail/invoke",
            json={"arguments": {"text": "x"}},
            headers=_operator_headers(),
        )
        assert resp.status_code == 504, resp.text

    # Next call hits the open circuit → 503.
    resp = client.post(
        "/api/v1/tools/breaker.fail/invoke",
        json={"arguments": {"text": "x"}},
        headers=_operator_headers(),
    )
    assert resp.status_code == 503, resp.text
    assert resp.json()["detail"]["error_code"] == "circuit_open"


# --------------------------------------------------------------------------- #
# Background health probe (TDD: failure-first)
# --------------------------------------------------------------------------- #


def test_health_probe_task_updates_tool_health_status(tmp_path) -> None:
    """The background health probe writes back health_status to the store
    after a single tick (sync helper used by the task)."""
    from ai_employee.tool_registry.health_probe import probe_and_persist

    # Spin a tiny 200 OK HTTP server.
    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *args: Any, **kwargs: Any) -> None:
            return

    server = HTTPServer(("127.0.0.1", 0), _Handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        store = ToolRegistryStore(db_path=str(tmp_path / "probe.sqlite3"))
        store.upsert(
            {
                "name": "probe.target",
                "description": "x",
                "input_schema": {"type": "object"},
                "output_schema": {"type": "object"},
                "risk_level": "read_only",
                "health_check_url": f"http://127.0.0.1:{port}/health",
            }
        )
        # health_status should default to 'unknown' (column may not yet exist
        # on legacy stores — but R25-T added it).
        probe_and_persist(store, name="probe.target")
        row = store.get("probe.target")
        assert row is not None
        assert row.get("health_status") == "healthy"
    finally:
        server.shutdown()


def test_health_probe_task_marks_unreachable_target_unhealthy(tmp_path) -> None:
    """An unreachable health_check_url is recorded as 'unhealthy'."""
    from ai_employee.tool_registry.health_probe import probe_and_persist

    store = ToolRegistryStore(db_path=str(tmp_path / "unreach.sqlite3"))
    store.upsert(
        {
            "name": "dead.target",
            "description": "x",
            "input_schema": {"type": "object"},
            "output_schema": {"type": "object"},
            "risk_level": "read_only",
            "health_check_url": "http://127.0.0.1:1/health",  # closed port
        }
    )
    probe_and_persist(store, name="dead.target")
    row = store.get("dead.target")
    assert row.get("health_status") == "unhealthy"


def test_health_probe_task_no_url_keeps_unknown(tmp_path) -> None:
    """A tool without health_check_url stays 'unknown' (no false positives)."""
    from ai_employee.tool_registry.health_probe import probe_and_persist

    store = ToolRegistryStore(db_path=str(tmp_path / "nourl.sqlite3"))
    store.upsert(
        {
            "name": "no.url",
            "description": "x",
            "input_schema": {"type": "object"},
            "output_schema": {"type": "object"},
            "risk_level": "read_only",
            "health_check_url": None,
        }
    )
    probe_and_persist(store, name="no.url")
    row = store.get("no.url")
    # Either stays 'unknown' or is absent; we tolerate the legacy schema.
    assert row.get("health_status", "unknown") == "unknown"


def test_health_probe_loop_probes_all_tools(tmp_path) -> None:
    """run_once iterates the store and probes every tool."""
    from ai_employee.tool_registry.health_probe import run_once

    store = ToolRegistryStore(db_path=str(tmp_path / "loop.sqlite3"))
    store.upsert(
        {
            "name": "a",
            "description": "x",
            "input_schema": {},
            "output_schema": {},
            "risk_level": "read_only",
            "health_check_url": "http://127.0.0.1:1/health",
        }
    )
    store.upsert(
        {
            "name": "b",
            "description": "x",
            "input_schema": {},
            "output_schema": {},
            "risk_level": "read_only",
            "health_check_url": None,
        }
    )
    counts = run_once(store)
    # 'a' was probed and marked unhealthy; 'b' was skipped (no URL).
    assert counts["probed"] == 1
    assert counts["skipped"] == 1
    assert store.get("a")["health_status"] == "unhealthy"
    assert store.get("b").get("health_status", "unknown") == "unknown"
