"""Circuit breaker for tool invocation (spec §5.3 governance).

Wraps a tool handler call so repeated failures trip an open circuit,
fast-failing subsequent calls instead of hammering a broken downstream.
After a recovery window the breaker enters half-open: one trial call is
allowed; success closes the circuit, failure re-opens it.

States per tool name:
  - closed:   calls pass through; consecutive failures counted.
  - open:     calls fast-fail with :class:`CircuitOpenError`.
  - half-open: one trial call permitted after the recovery window.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any


class CircuitOpenError(Exception):
    """Raised when a call is attempted against an open circuit."""


class CircuitBreaker:
    def __init__(self, *, failure_threshold: int = 5, recovery_seconds: float = 30.0) -> None:
        self.failure_threshold = failure_threshold
        self.recovery_seconds = recovery_seconds
        # Per-tool state.
        self._failures: dict[str, int] = {}
        self._state: dict[str, str] = {}  # "closed" | "open" | "half_open"
        self._opened_at: dict[str, float] = {}

    def _now(self) -> float:
        return time.time()

    def state(self, tool_name: str) -> str:
        current = self._state.get(tool_name, "closed")
        if current == "open":
            # Check whether the recovery window has elapsed → half-open.
            opened = self._opened_at.get(tool_name, 0.0)
            if self._now() - opened >= self.recovery_seconds:
                self._state[tool_name] = "half_open"
                return "half_open"
        return current

    def call(self, tool_name: str, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        state = self.state(tool_name)
        if state == "open":
            raise CircuitOpenError(
                f"circuit open for tool {tool_name!r} (failures={self._failures.get(tool_name, 0)})"
            )
        try:
            result = fn(*args, **kwargs)
        except Exception:
            self._on_failure(tool_name)
            raise
        self._on_success(tool_name)
        return result

    def _on_success(self, tool_name: str) -> None:
        self._failures[tool_name] = 0
        self._state[tool_name] = "closed"
        self._opened_at.pop(tool_name, None)

    def _on_failure(self, tool_name: str) -> None:
        self._failures[tool_name] = self._failures.get(tool_name, 0) + 1
        if self._failures[tool_name] >= self.failure_threshold:
            self._state[tool_name] = "open"
            self._opened_at[tool_name] = self._now()
        elif self._state.get(tool_name) == "half_open":
            # A failure during half-open re-opens immediately.
            self._state[tool_name] = "open"
            self._opened_at[tool_name] = self._now()

    def reset(self, tool_name: str | None = None) -> None:
        if tool_name is None:
            self._failures.clear()
            self._state.clear()
            self._opened_at.clear()
        else:
            self._failures.pop(tool_name, None)
            self._state.pop(tool_name, None)
            self._opened_at.pop(tool_name, None)


__all__ = ["CircuitBreaker", "CircuitOpenError"]
