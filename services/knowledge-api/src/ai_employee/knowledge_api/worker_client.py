from __future__ import annotations

from dataclasses import dataclass

import httpx

from ai_employee.common_schemas.knowledge import ParseResponse


@dataclass
class WorkerDispatchResult:
    dispatched: bool
    dispatch_status: str  # accepted / timeout / worker_unreachable / worker_error
    response: ParseResponse | None = None
    error: str | None = None


class WorkerClient:
    def __init__(
        self,
        base_url: str,
        internal_token: str,
        timeout_s: float = 30.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.internal_token = internal_token
        self.timeout_s = timeout_s

    def health(self) -> bool:
        try:
            resp = httpx.get(f"{self.base_url}/health", timeout=5.0)
            return resp.status_code == 200
        except httpx.HTTPError:
            return False

    def parse(
        self,
        doc_id: str,
        file_path: str,
        mime_type: str,
        metadata: dict,
    ) -> WorkerDispatchResult:
        payload = {
            "doc_id": doc_id,
            "file_path": file_path,
            "mime_type": mime_type,
            "metadata": metadata,
        }
        headers = {"X-Internal-Token": self.internal_token}
        last_error: str | None = None
        for _attempt in range(2):
            try:
                resp = httpx.post(
                    f"{self.base_url}/internal/parse",
                    json=payload,
                    headers=headers,
                    timeout=self.timeout_s,
                )
            except httpx.TimeoutException as exc:
                last_error = f"timeout: {exc}"
                continue
            except httpx.HTTPError as exc:
                return WorkerDispatchResult(
                    dispatched=False,
                    dispatch_status="worker_unreachable",
                    error=f"unreachable: {exc}",
                )
            if resp.status_code == 200:
                return WorkerDispatchResult(
                    dispatched=True,
                    dispatch_status="accepted",
                    response=ParseResponse(**resp.json()),
                )
            return WorkerDispatchResult(
                dispatched=False,
                dispatch_status="worker_error",
                error=f"worker returned {resp.status_code}: {resp.text}",
            )
        return WorkerDispatchResult(
            dispatched=False,
            dispatch_status="timeout",
            error=last_error,
        )
