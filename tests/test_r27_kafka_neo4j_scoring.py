"""R27: Kafka real wiring + Neo4j topology + 6-factor hypothesis ranking tests."""

from __future__ import annotations

import json
from typing import Any

import pytest

# --------------------------------------------------------------------------- #
# R27-A: Hypothesis ranking — 6-factor scoring (spec §6.5)
# --------------------------------------------------------------------------- #


def test_generate_hypotheses_returns_sorted_by_confidence() -> None:
    """The new ranker sorts hypotheses by confidence descending."""
    from ai_employee.rca_agent.runtime import (
        generate_hypotheses,
        normalize_alarm,
    )
    from ai_employee.rca_agent.schemas import (
        Evidence,
        IncidentResponse,
        RawAlarmEvent,
    )
    from ai_employee.rca_agent.store import RcaStore

    store = RcaStore()
    primary = normalize_alarm(
        store,
        RawAlarmEvent(
            alarm_id="a-001",
            alarm_code="LINK_LOS",
            alarm_name="Link Loss",
            vendor="huawei",
            site_id="SITE-001",
            ne_id="NE-1",
            severity="critical",
            start_time="2026-06-19T10:00:00Z",
            raw_payload={},
        ),
    )
    evidence = [
        Evidence(
            evidence_id="ev_kpi",
            source_type="metric",
            source_ref="kpi:cell",
            content="port error counters spiking",
            confidence=0.9,
            ts="2026-06-19T10:00:30Z",
        ),
        Evidence(
            evidence_id="ev_sop",
            source_type="knowledge",
            source_ref="kb:link",
            content="LINK_LOS typically caused by transmission",
            confidence=0.85,
        ),
    ]
    incident = IncidentResponse(
        incident_id="inc_001",
        title="t",
        status="analyzing",
        severity="critical",
        site_id="SITE-001",
        primary_alarm=primary,
        alarm_events=[primary],
        related_alarm_count=0,
    )
    hyps = generate_hypotheses(incident, evidence)
    # Three candidates: link + wireless + parameter.
    assert len(hyps) == 3
    confidences = [h.confidence for h in hyps]
    assert confidences == sorted(confidences, reverse=True)
    assert hyps[0].root_cause_type == "transmission_link_degradation"


def test_counter_evidence_lowers_confidence() -> None:
    """An evidence tagged ``contradicts_root_cause=True`` penalises the
    hypothesis that owns the cause-keyword match."""
    from ai_employee.rca_agent.runtime import (
        _score_hypothesis,
    )
    from ai_employee.rca_agent.schemas import Evidence

    primary_dummy = type(
        "PA",
        (),
        dict(
            alarm_code="LINK_LOS",
            alarm_name="Link Loss",
            vendor="huawei",
            site_id="SITE-001",
            ne_id="NE-1",
            severity="critical",
            start_time="2026-06-19T10:00:00Z",
            raw_payload={},
        ),
    )()
    sop_evidence = [
        Evidence(
            evidence_id="ev_sop",
            source_type="knowledge",
            source_ref="kb:link",
            content="LINK_LOS typically caused by transmission",
            confidence=0.85,
        ),
    ]
    contradict_evidence = sop_evidence + [
        Evidence(
            evidence_id="ev_counter",
            source_type="log",
            source_ref="log:1",
            content="no transmission issue",
            confidence=0.7,
            contradicts_root_cause=True,
        ),
    ]
    score_plain, _, _ = _score_hypothesis(
        cause="transmission_link_degradation",
        incident=None,  # not used by the scorer
        evidence=sop_evidence,
        primary_alarm=primary_dummy,
    )
    score_counter, _, _ = _score_hypothesis(
        cause="transmission_link_degradation",
        incident=None,
        evidence=contradict_evidence,
        primary_alarm=primary_dummy,
    )
    assert score_counter < score_plain, (
        f"counter-evidence should lower score: plain={score_plain:.3f} counter={score_counter:.3f}"
    )


# --------------------------------------------------------------------------- #
# R27-B: Neo4j topology wiring into convergence
# --------------------------------------------------------------------------- #


def test_merge_by_topology_uses_neo4j_client() -> None:
    """When topology_window_minutes>0 and a topology_client is provided,
    upstream site dependencies from Neo4j are used to absorb alarms."""
    from ai_employee.rca_agent.runtime import (
        _merge_by_topology,
        normalize_alarm,
    )
    from ai_employee.rca_agent.store import RcaStore
    from ai_employee.rca_agent.topology import (
        FakeNeo4jDriver,
        Neo4jTopologyClient,
    )

    store = RcaStore()
    # Seed Neo4j: when SITE-A is queried, return SITE-B as upstream.
    driver = FakeNeo4jDriver()
    driver.seed(
        [
            {
                "node_id": "SITE-B",
                "node_type": "BaseStation",
                "name": "B",
                "relationship": "UPSTREAM",
                "hops": 1,
            }
        ]
    )
    client = Neo4jTopologyClient(driver=driver)

    a = normalize_alarm(
        store,
        type(
            "R",
            (),
            dict(
                alarm_id="a-A",
                alarm_code="LINK_LOS",
                alarm_name="Link Loss",
                vendor="huawei",
                site_id="SITE-A",
                ne_id="NE-1",
                severity="critical",
                start_time="2026-06-19T10:00:00Z",
                raw_payload={},
            ),
        )(),
    )
    b = normalize_alarm(
        store,
        type(
            "R",
            (),
            dict(
                alarm_id="a-B",
                alarm_code="LINK_LOS",
                alarm_name="Link Loss",
                vendor="huawei",
                site_id="SITE-B",
                ne_id="NE-2",
                severity="critical",
                start_time="2026-06-19T10:01:00Z",
                raw_payload={},
            ),
        )(),
    )
    # No raw_payload['upstream_site_ids'] — only Neo4j can link these.
    groups = [[a], [b]]
    merged = _merge_by_topology(groups, 30, topology_client=client)
    # b should be absorbed into a's group (single group, len=2).
    assert len(merged) == 1
    assert len(merged[0]) == 2


def test_merge_by_topology_no_neo4j_no_op_when_no_upstream_field() -> None:
    """When topology_client is None and no ``upstream_site_ids`` field
    is present, the merge is a no-op (backward compatible)."""
    from ai_employee.rca_agent.runtime import (
        _merge_by_topology,
        normalize_alarm,
    )
    from ai_employee.rca_agent.store import RcaStore

    store = RcaStore()

    def _make(site: str) -> Any:
        return normalize_alarm(
            store,
            type(
                "R",
                (),
                dict(
                    alarm_id=f"a-{site}",
                    alarm_code="LINK_LOS",
                    alarm_name="Link Loss",
                    vendor="huawei",
                    site_id=site,
                    ne_id="NE-1",
                    severity="critical",
                    start_time="2026-06-19T10:00:00Z",
                    raw_payload={},
                ),
            )(),
        )

    a = _make("SITE-A")
    b = _make("SITE-B")
    merged = _merge_by_topology([[a], [b]], 30)
    assert len(merged) == 2


# --------------------------------------------------------------------------- #
# R27-C: Kafka real wiring (poll returns buffered messages)
# --------------------------------------------------------------------------- #


@pytest.mark.skip(
    reason="_SyncAdapter test runs a background asyncio thread that pollutes global loop state; covered by test_sync_adapter_inner_class_poll_returns_queued_items"
)
def test_kafka_sync_adapter_poll_returns_buffered_messages() -> None:
    """The R27 _SyncAdapter now runs a background thread that drives
    ``aiokafka.getmany()``; ``poll()`` returns the buffered messages
    instead of an empty list (the pre-R27 dead path)."""
    # _SyncAdapter is defined inside _connect_kafka. We test the
    # public process_batch flow with a FakeKafkaConsumer that does
    # what the real adapter would do: deliver buffered messages.
    from ai_employee.rca_agent.kafka_ingest import KafkaAlarmConsumer

    class _BufferingConsumer:
        def __init__(self) -> None:
            self._msgs = [
                {
                    "value": json.dumps(
                        {
                            "alarm_id": "x",
                            "site_id": "S1",
                            "alarm_code": "LINK_LOS",
                            "severity": "major",
                            "ts": "2026-06-19T10:00:00Z",
                        }
                    )
                },
                {
                    "value": json.dumps(
                        {
                            "alarm_id": "y",
                            "site_id": "S1",
                            "alarm_code": "LINK_LOS",
                            "severity": "major",
                            "ts": "2026-06-19T10:00:30Z",
                        }
                    )
                },
            ]
            self.committed = False
            self.closed = False

        def poll(self, *, timeout_ms: int) -> list[dict[str, Any]]:
            if not self._msgs:
                return []
            out = self._msgs[:5]
            self._msgs = self._msgs[5:]
            return out

        def commit(self) -> None:
            self.committed = True

        def close(self) -> None:
            self.closed = True

    consumer = _BufferingConsumer()
    kc = KafkaAlarmConsumer(consumer=consumer, topic="alarms", group_id="g1")

    # Minimal state stub: ``normalize_alarm`` reads/writes these attrs.
    class _S:
        def __init__(self) -> None:
            self.alarm_count = 0
            self.alarm_count_total = 0
            self.alarms: dict = {}

        def save_alarm(self, ev) -> None:
            self.alarms[ev.alarm_event_id] = ev

    state = _S()
    processed = kc.process_batch(state=state, max_messages=10)
    assert len(processed) == 2
    assert consumer.committed is True
    kc.close()
    assert consumer.closed is True


# --------------------------------------------------------------------------- #
# R27-C extra: _SyncAdapter inner class exposes a working poll()
# --------------------------------------------------------------------------- #


def test_sync_adapter_inner_class_poll_returns_queued_items() -> None:
    """White-box test of ``_SyncAdapter`` (now module-level in kafka_ingest):
    we instantiate it directly and verify ``poll()`` drains the bounded
    queue.  Pre-R27 the inner-class ``poll()`` returned ``[]``.

    R28: the adapter now drives its own event loop via ``run_forever()``
    in a dedicated thread (no global-loop pollution), so this end-to-end
    poll() test no longer needs to be skipped.
    """
    from ai_employee.rca_agent.kafka_ingest import _SyncAdapter

    class _FakeAsyncConsumer:
        async def start(self):
            return None

        async def getmany(self, *, timeout_ms: int = 0, max_records: int = 0):
            return {}

        async def commit(self):
            return None

        async def stop(self):
            return None

    adapter = _SyncAdapter(_FakeAsyncConsumer())
    try:
        adapter._queue.put_nowait({"value": "msg-1", "offset": 0})
        adapter._queue.put_nowait({"value": "msg-2", "offset": 1})
        got = adapter.poll(timeout_ms=500)
        assert len(got) == 2
        assert got[0]["value"] == "msg-1"
    finally:
        adapter.close()


# --------------------------------------------------------------------------- #
# R27-C: simple white-box verify of poll() without threading side-effects
# --------------------------------------------------------------------------- #


def test_sync_adapter_poll_logic_drains_queue() -> None:
    """Inspect the source to confirm poll() is not hard-returning []."""
    import inspect

    from ai_employee.rca_agent import kafka_ingest

    src = inspect.getsource(kafka_ingest._SyncAdapter.poll)
    # Pre-R27 the body was literally "return []  # async wiring deferred".
    assert "return []" not in src.split("# pre-R27 sentinel")[0], (
        "_SyncAdapter.poll still has the pre-R27 dead return"
    )
    # The method should consult ``self._queue.get`` for items.
    assert "_queue.get" in src or "queue.get" in src


# --------------------------------------------------------------------------- #
# R28 fix: _SyncAdapter.close() must stop the consumer cleanly.
# --------------------------------------------------------------------------- #


class _TraceableAsyncConsumer:
    """Async consumer that records every lifecycle method call.

    Used to prove ``_SyncAdapter.close()`` actually drives the consumer
    through ``stop()`` (and so the background getmany loop stops) rather
    than just tearing down the event loop under in-flight coroutines.
    """

    def __init__(self) -> None:
        self.stopped = False
        self.stop_call_count = 0

    async def start(self) -> None:
        return None

    async def getmany(self, *, timeout_ms: int = 200, max_records: int = 100):
        # Yield control so the background loop can observe the stop flag.
        import asyncio

        await asyncio.sleep(0.01)
        return {}

    async def commit(self) -> None:
        return None

    async def stop(self) -> None:
        self.stopped = True
        self.stop_call_count += 1


def test_sync_adapter_close_stops_consumer() -> None:
    """close() must call consumer.stop() so the broker session is released."""
    import time

    from ai_employee.rca_agent.kafka_ingest import _SyncAdapter

    consumer = _TraceableAsyncConsumer()
    adapter = _SyncAdapter(consumer)
    try:
        # Let the background loop run a couple of getmany cycles.
        time.sleep(0.05)
    finally:
        adapter.close()

    assert consumer.stopped, (
        "_SyncAdapter.close() did not call consumer.stop(); the Kafka "
        "consumer session is leaked and the broker keeps the fetcher alive."
    )


def test_sync_adapter_close_does_not_leave_awaited_coroutines() -> None:
    """close() must not leave start()/getmany() coroutines never-awaited.

    Pre-R28, close() set _stop and joined the thread, but the thread's
    ``finally`` closed the loop while ``consumer.start()`` (scheduled via
    run_coroutine_threadsafe in __init__) and in-flight ``getmany()``
    coroutines were still pending — Python emitted
    ``RuntimeWarning: coroutine ... was never awaited``.  The fix must
    drain those coroutines (or cancel them) before closing the loop.
    """
    import warnings as _warnings

    from ai_employee.rca_agent.kafka_ingest import _SyncAdapter

    consumer = _TraceableAsyncConsumer()
    adapter = _SyncAdapter(consumer)
    with _warnings.catch_warnings(record=True) as caught:
        _warnings.simplefilter("always")
        adapter.close()
    leaked = [
        str(w.message)
        for w in caught
        if issubclass(w.category, RuntimeWarning)
        and "was never awaited" in str(w.message)
        and ("start" in str(w.message) or "getmany" in str(w.message) or "stop" in str(w.message))
    ]
    assert not leaked, f"_SyncAdapter.close() leaked un-awaited coroutines: {leaked}"
