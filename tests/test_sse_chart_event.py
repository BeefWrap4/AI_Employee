"""R19-3: SSE chart event extension tests.

Verifies that ``/api/v1/chat/query/stream`` emits an additional
``event: chart`` SSE event with ``chart_id`` + ``schema_url`` alongside
the existing ``meta`` / ``token`` / ``citations`` / ``done`` events.
"""
from __future__ import annotations

import json

from fastapi.testclient import TestClient


def _upload_and_publish(client: TestClient) -> None:
    r = client.post(
        "/api/v1/documents",
        data={
            "title": "RRC 排障 SOP",
            "metadata_json": json.dumps({"network_type": "5g"}),
            "acl_tags_json": json.dumps(["wireless"]),
            "version": "v1",
            "mime_type": "text/markdown",
        },
        files={
            "file": (
                "sop.md",
                "RRC 建立失败先检查告警 KPI。".encode("utf-8"),
                "text/markdown",
            )
        },
    )
    assert r.status_code == 202, r.text
    doc_id = r.json()["doc_id"]
    pub = client.post(f"/api/v1/documents/{doc_id}/publish")
    assert pub.status_code == 200


def _attach_fake_aggregator(client: TestClient) -> None:
    """Inject a fake aggregator that returns 3 buckets."""
    from ai_employee.knowledge_api.echarts import (
        AlarmAggregator,
        EChartsAggregator,
    )

    class _FakeAlarm(AlarmAggregator):
        def bucket_alarms(self, *, metric, window_minutes, now):  # type: ignore[no-untyped-def]
            return [
                {"ts": "2026-06-19T00:00:00+00:00", "count": 1, "severity": "major"},
                {"ts": "2026-06-19T00:01:00+00:00", "count": 2, "severity": "major"},
                {"ts": "2026-06-19T00:02:00+00:00", "count": 3, "severity": "major"},
            ]

    class _FakeKpi:
        def bucket_kpi(self, *, metric, site_id, window_minutes, now):  # type: ignore[no-untyped-def]
            return []

    agg = EChartsAggregator(alarm=_FakeAlarm(), kpi=_FakeKpi())
    client.app.state.echarts_aggregator = agg


def test_stream_emits_chart_event_with_id_and_schema_url(api_factory) -> None:
    client = api_factory()
    _upload_and_publish(client)
    _attach_fake_aggregator(client)

    r = client.post(
        "/api/v1/chat/query/stream",
        json={
            "session_id": "s-chart",
            "question": "RRC 建立失败先检查告警 KPI",
            "knowledge_scopes": ["wireless"],
            "stream": True,
        },
    )
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("text/event-stream")
    body = r.text
    # All existing events still present
    assert "event: meta" in body
    assert "event: token" in body
    assert "event: citations" in body
    assert "event: done" in body
    # New chart event emitted
    assert "event: chart" in body
    # Extract the chart event payload and verify shape
    chart_event = None
    for line in body.splitlines():
        if line.startswith("event: chart"):
            continue
        if line.startswith("data: ") and chart_event is None:
            # Heuristic: first data: line that follows an event: chart
            pass
    # Walk SSE blocks separated by blank lines
    blocks = [b for b in body.split("\n\n") if b.strip()]
    chart_payload = None
    for block in blocks:
        if "event: chart" in block:
            for ln in block.splitlines():
                if ln.startswith("data: "):
                    chart_payload = json.loads(ln[len("data: "):])
                    break
            break
    assert chart_payload is not None, f"chart event missing in SSE: {body[:400]}"
    assert "chart_id" in chart_payload
    assert chart_payload["chart_id"].startswith("chart_")
    assert "schema_url" in chart_payload
    assert chart_payload["schema_url"].startswith("/api/v1/chat/echarts/schema/")
    # Resolving the schema_url via HTTP returns the full option dict.
    full = client.get(chart_payload["schema_url"])
    assert full.status_code == 200, full.text
    schema_body = full.json()
    assert schema_body["chart_id"] == chart_payload["chart_id"]
    assert "xAxis" in schema_body
    assert "yAxis" in schema_body
    assert "series" in schema_body


def test_stream_chart_event_can_be_disabled(api_factory) -> None:
    """When no aggregator yields data, the chart event must be omitted."""
    client = api_factory()
    _upload_and_publish(client)
    # No aggregator attached → default stubs return empty → no chart event
    r = client.post(
        "/api/v1/chat/query/stream",
        json={
            "session_id": "s-no-chart",
            "question": "RRC",
            "knowledge_scopes": ["wireless"],
            "stream": True,
        },
    )
    assert r.status_code == 200
    body = r.text
    assert "event: meta" in body
    assert "event: done" in body
    assert "event: chart" not in body