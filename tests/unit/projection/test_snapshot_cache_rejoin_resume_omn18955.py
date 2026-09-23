# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-18955: a consumer-group rejoin resumes the cache, it does not replay.

Measured on the .201 dev lane 2026-09-23: the projection API's heartbeat
session expired repeatedly while the host was loaded, and the group rejoined.
The group commits no offsets and resets to earliest, so every rejoin handed
back every partition at the log start and the cache re-read what it had
already applied. consumer-flow sat at 2,008,219 drops since its last apply,
re-reading an 8.9M-record log, and the idle prod-promotion-gate topic's drop
streak (828) held ``/ready`` at 503 with lag zero until its writer next
published.

What is pinned here:

* a rejoin seeks each partition to the offset after the last record applied,
  so the re-read produces no drops and the drop streak cannot trip;
* the first assignment is left alone (nothing applied yet);
* a ``position()`` round trip, which also seeds ``next_position``, is never
  mistaken for applied state -- resuming from it would skip records nobody
  applied;
* ``start()`` installs the listener, and ``/ready`` reports the rejoin count.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

import pytest
from aiokafka import TopicPartition
from fastapi.testclient import TestClient

from omnimarket.projection.api_server import app, get_snapshot_cache, get_topic_map
from omnimarket.projection.models import (
    ModelProjectionSnapshotDelta,
    ProjectionTableConfig,
)
from omnimarket.projection.snapshot_cache import SnapshotCache

pytestmark = pytest.mark.unit

_BUSY_TOPIC = "onex.snapshot.projection.consumer-flow.v1"
_IDLE_TOPIC = "onex.snapshot.projection.prod-promotion-gate.v1"
_KEYS = 10


def _cfg(topic: str) -> ProjectionTableConfig:
    return ProjectionTableConfig(
        topic=topic,
        table="t",
        columns=("k", "v"),
        bus_backed=True,
        key_columns=("k",),
        key_grain="mutable",
    )


class _Msg:
    """A real upsert whose source offset is its own offset, so re-reading an
    applied record is a counted drop -- the signature a rejoin left."""

    def __init__(self, *, topic: str, offset: int) -> None:
        key = f"k{offset % _KEYS}"
        self.topic = topic
        self.partition = 0
        self.offset = offset
        self.key = key.encode()
        self.value = (
            ModelProjectionSnapshotDelta(
                topic=topic,
                key=(key,),
                op="upsert",
                row={"k": key, "v": offset},
                observed_at="2026-09-23T04:00:00Z",
                source_event_id=f"evt-{offset}",
                source_topic="src",
                source_partition=0,
                source_offset=offset,
            )
            .model_dump_json()
            .encode()
        )
        self.headers: list[tuple[str, bytes]] = []


class _FakeConsumer:
    """One partition per topic, each a log of ``end`` records from offset 0.

    ``seek`` moves the fetch position the way the real consumer does, and
    ``reset_to_log_start`` is what aiokafka does to every partition on a
    rejoin for a group with no committed offsets under
    ``auto_offset_reset="earliest"``.
    """

    def __init__(self, *, end: int) -> None:
        self._end = end
        self._assigned = frozenset(
            {TopicPartition(_BUSY_TOPIC, 0), TopicPartition(_IDLE_TOPIC, 0)}
        )
        self.position_by_tp: dict[TopicPartition, int] = dict.fromkeys(
            self._assigned, 0
        )
        self.seeks: list[tuple[TopicPartition, int]] = []

    def assignment(self) -> frozenset[TopicPartition]:
        return self._assigned

    def seek(self, tp: TopicPartition, offset: int) -> None:
        self.seeks.append((tp, offset))
        self.position_by_tp[tp] = offset

    def reset_to_log_start(self) -> None:
        for tp in self.position_by_tp:
            self.position_by_tp[tp] = 0

    def highwater(self, tp: TopicPartition) -> int | None:
        return self._end

    async def end_offsets(
        self, partitions: list[TopicPartition]
    ) -> dict[TopicPartition, int]:
        return dict.fromkeys(partitions, self._end)

    async def position(self, tp: TopicPartition) -> int:
        return self.position_by_tp[tp]

    async def getmany(
        self, *, timeout_ms: int = 0, max_records: int | None = None
    ) -> dict[TopicPartition, list[_Msg]]:
        batches: dict[TopicPartition, list[_Msg]] = {}
        for tp, pos in self.position_by_tp.items():
            take = min(self._end - pos, max_records or self._end)
            if take > 0:
                batches[tp] = [
                    _Msg(topic=tp.topic, offset=pos + i) for i in range(take)
                ]
                self.position_by_tp[tp] = pos + take
        await asyncio.sleep(0)
        return batches


def _cache(consumer: _FakeConsumer) -> SnapshotCache:
    cache = SnapshotCache(
        {_BUSY_TOPIC: _cfg(_BUSY_TOPIC), _IDLE_TOPIC: _cfg(_IDLE_TOPIC)},
        bootstrap_servers="unused:9092",
        group_id="test-group",
    )
    cache._consumer = consumer
    cache._running = True
    return cache


async def _consume_until_bootstrapped(cache: SnapshotCache) -> None:
    """Run the real consume loop until both topics are caught up, then stop it."""
    task = asyncio.ensure_future(cache._consume_loop())
    try:
        async with asyncio.timeout(5):
            while not (
                cache.is_bootstrapped(_BUSY_TOPIC)
                and cache.is_bootstrapped(_IDLE_TOPIC)
            ):
                await asyncio.sleep(0)
            # One more pass so a re-read, if any, has been applied or dropped.
            await asyncio.sleep(0.05)
    finally:
        cache._running = False
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    cache._running = True


@pytest.fixture(autouse=True)
def _fast_bootstrap_poll(monkeypatch: pytest.MonkeyPatch) -> None:
    import omnimarket.projection.snapshot_cache as snapshot_cache_module

    monkeypatch.setattr(snapshot_cache_module, "_BOOTSTRAP_POLL_INTERVAL_SECONDS", 0.0)
    monkeypatch.setattr(snapshot_cache_module, "_BOOTSTRAP_POLL_MAX_ATTEMPTS", 1)


async def test_a_rejoin_resumes_where_the_cache_left_off() -> None:
    consumer = _FakeConsumer(end=1000)
    cache = _cache(consumer)
    cache.on_partitions_assigned(set(consumer.assignment()))
    assert consumer.seeks == []  # first assignment: nothing applied, left alone
    await _consume_until_bootstrapped(cache)
    assert cache.reassignment_count == 0
    assert cache.row_count(_BUSY_TOPIC) == _KEYS
    assert cache.get_rows(_BUSY_TOPIC, unbounded=True)[0]["v"] == 990

    # The heartbeat session expires, the group rejoins, aiokafka resets every
    # partition to the log start and hands the assignment back.
    consumer.reset_to_log_start()
    cache.on_partitions_assigned(set(consumer.assignment()))

    assert cache.reassignment_count == 1
    assert sorted(consumer.seeks) == sorted(
        [(TopicPartition(_BUSY_TOPIC, 0), 1000), (TopicPartition(_IDLE_TOPIC, 0), 1000)]
    )
    await _consume_until_bootstrapped(cache)
    for topic in (_BUSY_TOPIC, _IDLE_TOPIC):
        report = cache.lag_report(topic)
        assert report is not None
        assert report["dropped_since_apply"] == 0
        assert report["dropped_total"] == 0
        assert report["lag"] == 0
        assert not cache.is_stale(topic)


async def test_without_the_resume_a_rejoin_trips_the_drop_streak() -> None:
    """Positive control: the failure the resume removes, reproduced.

    Skipping the listener's seek is exactly the pre-fix behaviour. The idle
    topic re-reads every applied record as a drop, and with no new record to
    reset the streak it reads STALE -- ``/ready`` 503 at lag zero.
    """
    consumer = _FakeConsumer(end=1000)
    cache = _cache(consumer)
    await _consume_until_bootstrapped(cache)

    consumer.reset_to_log_start()  # rejoin, no listener seek
    await _consume_until_bootstrapped(cache)

    report = cache.lag_report(_IDLE_TOPIC)
    assert report is not None
    assert report["lag"] == 0
    assert report["dropped_since_apply"] == 1000
    assert cache.is_stale(_IDLE_TOPIC)


async def test_a_position_round_trip_is_not_mistaken_for_applied_state() -> None:
    """The RPC catch-up check seeds next_position from position(); resuming
    from that would skip records nobody applied."""
    consumer = _FakeConsumer(end=1000)
    consumer.position_by_tp[TopicPartition(_IDLE_TOPIC, 0)] = 700
    cache = _cache(consumer)

    await cache._mark_bootstrap_complete_when_caught_up()
    cache.on_partitions_assigned(set(consumer.assignment()))

    assert consumer.seeks == []


class _FakeConsumerCapturingSubscribe:
    instances: list[_FakeConsumerCapturingSubscribe] = []

    def __init__(self, *topics: str, **kwargs: Any) -> None:
        self.topics = topics
        self.subscribed: tuple[list[str], Any] | None = None
        type(self).instances.append(self)

    def subscribe(self, topics: list[str], listener: Any = None) -> None:
        self.subscribed = (list(topics), listener)

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None


async def _noop() -> None:
    return None


async def test_start_subscribes_with_the_reassignment_listener(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import omnimarket.projection.snapshot_cache as snapshot_cache_module

    _FakeConsumerCapturingSubscribe.instances = []
    monkeypatch.setattr(
        snapshot_cache_module, "AIOKafkaConsumer", _FakeConsumerCapturingSubscribe
    )
    cache = SnapshotCache(
        {_BUSY_TOPIC: _cfg(_BUSY_TOPIC)},
        bootstrap_servers="unused:9092",
        group_id="test-group",
    )
    monkeypatch.setattr(cache, "_consume_loop", _noop)
    await cache.start()
    await cache.stop()

    (consumer,) = _FakeConsumerCapturingSubscribe.instances
    assert consumer.subscribed is not None
    topics, listener = consumer.subscribed
    assert sorted(topics) == sorted(consumer.topics)
    assert listener is not None
    # The listener drives the cache's own resume path.
    await listener.on_partitions_assigned(set())
    assert cache._assignment_count == 1


def test_ready_reports_the_rejoin_count() -> None:
    consumer = _FakeConsumer(end=1000)
    cache = _cache(consumer)
    cache.on_partitions_assigned(set(consumer.assignment()))
    cache.on_partitions_assigned(set(consumer.assignment()))
    topic_map = {_BUSY_TOPIC: _cfg(_BUSY_TOPIC), _IDLE_TOPIC: _cfg(_IDLE_TOPIC)}
    app.dependency_overrides[get_snapshot_cache] = lambda: cache
    app.dependency_overrides[get_topic_map] = lambda: topic_map
    try:
        response = TestClient(app).get("/ready")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 503  # nothing replayed yet; unchanged gate
    assert response.json()["consumer_reassignments"] == 1
