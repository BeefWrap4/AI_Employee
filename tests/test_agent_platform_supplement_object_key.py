"""Regression test: supplement attachment accepts ``object_key`` (R22).

Previously the supplement flow only accepted inline ``{"uri": ...}``
attachments.  R22 lets callers reference objects uploaded via
``POST /api/v1/objects`` — the platform resolves the key into a
download URL through the configured object store.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def platform_with_supplement(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """Platform app with LocalFs object store + a knowledge_qa run that
    triggers an approval task (which can then be supplemented)."""
    monkeypatch.delenv("OBJECT_STORE_URL", raising=False)
    monkeypatch.setenv("OBJECT_STORE_LOCAL_ROOT", str(tmp_path / "objects"))
    monkeypatch.setenv("OBJECT_STORE_PREFIX", "uploads")
    from ai_employee.agent_platform_api.app import create_app

    return TestClient(create_app())


def _create_approval_task(client: TestClient) -> str:
    """Create a draft run + pending approval task; return task_id."""
    # Use the change_assessment template (requires_approval=True).
    run_resp = client.post(
        "/api/v1/agent-runs",
        json={
            "template_id": "change_assessment",
            "requested_by": "tester",
            "input": {"change_id": "CHG-OBJ-1"},
        },
    )
    assert run_resp.status_code in (200, 201), run_resp.text
    body = run_resp.json()
    task_id = body.get("approval_task_id")
    if task_id:
        return task_id
    # Fallback: use a manual task via the approval-task create path.
    return body.get("run_id", "")


def test_supplement_accepts_object_key(platform_with_supplement: TestClient) -> None:
    client = platform_with_supplement
    # Step 1: upload an object via POST /api/v1/objects.
    upload = client.post(
        "/api/v1/objects",
        files={"file": ("report.pdf", b"%PDF-1.4 content", "application/pdf")},
    )
    assert upload.status_code == 200, upload.text
    object_key = upload.json()["object_key"]

    # Step 2: simulate a supplement request that references the key
    # instead of inlining a URI.  We exercise the normalize_attachments
    # helper directly so we don't need to thread an approval task.
    from ai_employee.agent_platform_api.object_refs import (
        normalize_attachments,
    )

    normalized = normalize_attachments(
        [{"name": "report.pdf", "object_key": object_key, "content_type": "application/pdf"}]
    )
    assert normalized[0]["uri"].endswith(f"/api/v1/objects/{object_key}")
    assert normalized[0]["object_key"] == object_key
    assert normalized[0]["name"] == "report.pdf"


def test_supplement_rejects_attachment_without_uri_or_key(
    platform_with_supplement: TestClient,
) -> None:
    client = platform_with_supplement
    from ai_employee.agent_platform_api.object_refs import (
        normalize_attachments,
    )

    with pytest.raises(ValueError, match="must include either uri or object_key"):
        normalize_attachments([{"name": "lonely.txt"}])


def test_supplement_with_inline_uri_still_works(platform_with_supplement: TestClient) -> None:
    """Backward compat: a plain uri-only attachment is accepted unchanged."""
    client = platform_with_supplement
    from ai_employee.agent_platform_api.object_refs import (
        normalize_attachments,
    )

    normalized = normalize_attachments([{"name": "manual.txt", "uri": "https://example.com/x.txt"}])
    assert normalized[0]["uri"] == "https://example.com/x.txt"
