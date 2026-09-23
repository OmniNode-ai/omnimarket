# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-18955: a cold SnapshotCache starts a horizon exposure at its horizon.

The consumer-flow snapshot topic retains by time, not by compaction, so a cold
projection API replayed every window of the last seven days before it could
report the exposure bootstrapped: 8.87M records on the .201 dev lane, which did
not finish inside the 900s compose-dev lab-pass settle budget, so every
receipt read FAIL on the readiness wait.

An exposure may now declare ``bootstrap_horizon_seconds``. The cache seeks each
assigned partition to the first record inside the horizon before the first
fetch. What must stay true, and is pinned here:

* only a declared exposure is sought; every other topic replays in full;
* a horizon with nothing inside it, or a lookup the broker cannot answer,
  falls back to the full replay rather than serving less than it would;
* the seek never marks a partition caught up -- readiness still requires the
  consumer's own position to reach the end offset;
* the declaration is validated at contract load and reported on ``/ready``.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from pathlib import Path
from typing import Any

import pytest
import yaml
from aiokafka import TopicPartition
from aiokafka.structs import OffsetAndTimestamp
from fastapi.testclient import TestClient
from pydantic import ValidationError

from omnimarket.projection.api_server import app, get_snapshot_cache, get_topic_map
from omnimarket.projection.discovery import load_projection_exposures_from_contract
from omnimarket.projection.models import (
    ModelProjectionSnapshotDelta,
    ProjectionTableConfig,
)
from omnimarket.projection.snapshot_cache import SnapshotCache

pytestmark = pytest.mark.unit

_HORIZON_TOPIC = "onex.snapshot.projection.consumer-flow.v1"
_FULL_TOPIC = "onex.snapshot.projection.registration.v1"
_CONTRACT_PATH = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_projection_consumer_flow"
    / "contract.yaml"
)


def _cfg(topic: str, *, horizon: int | None) -> ProjectionTableConfig:
    return ProjectionTableConfig(
        topic=topic,
        table="t",
        columns=("k", "v"),
        bus_backed=True,
        key_columns=("k",),
        key_grain="mutable",
        bootstrap_horizon_seconds=horizon,
    )


class _Msg:
    def __init__(self, *, topic: str, offset: int) -> None:
        self.topic = topic
        self.partition = 0
        self.offset = offset
        self.key = f"k{offset}".encode()
        self.value = None  # a tombstone: applying it touches no state
        self.headers: list[tuple[str, bytes]] = []


class _FakeConsumer:
    """One partition per topic, a log of ``end`` records starting at ``start``.

    ``offsets_for_times`` answers from ``horizon_hit`` (offset or ``None``) or
    raises ``lookup_error``. ``seek`` moves the fetch position the way the real
    consumer does, so the backlog served by ``getmany`` starts there.
    """

    def __init__(
        self,
        *,
        end: int,
        horizon_hit: int | None,
        lookup_error: Exception | None = None,
    ) -> None:
        self._end = end
        self._horizon_hit = horizon_hit
        self._lookup_error = lookup_error
        self._assigned = frozenset(
            {TopicPartition(_HORIZON_TOPIC, 0), TopicPartition(_FULL_TOPIC, 0)}
        )
        self.position_by_tp: dict[TopicPartition, int] = dict.fromkeys(
            self._assigned, 0
        )
        self.lookups: list[dict[TopicPartition, int]] = []
        self.seeks: list[tuple[TopicPartition, int]] = []

    def assignment(self) -> frozenset[TopicPartition]:
        return self._assigned

    async def offsets_for_times(
        self, timestamps: dict[TopicPartition, int]
    ) -> dict[TopicPartition, OffsetAndTimestamp | None]:
        self.lookups.append(dict(timestamps))
        if self._lookup_error is not None:
            raise self._lookup_error
        return {
            tp: (
                None
                if self._horizon_hit is None
                else OffsetAndTimestamp(self._horizon_hit, ts)
            )
            for tp, ts in timestamps.items()
        }

    def seek(self, tp: TopicPartition, offset: int) -> None:
        self.seeks.append((tp, offset))
        self.position_by_tp[tp] = offset

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
        {
            _HORIZON_TOPIC: _cfg(_HORIZON_TOPIC, horizon=3600),
            _FULL_TOPIC: _cfg(_FULL_TOPIC, horizon=None),
        },
        bootstrap_servers="unused:9092",
        group_id="test-group",
    )
    cache._consumer = consumer
    cache._running = True
    return cache


async def test_only_the_declared_exposure_is_sought_to_its_horizon() -> None:
    consumer = _FakeConsumer(end=1000, horizon_hit=940)
    cache = _cache(consumer)
    before_ms = int(time.time() * 1000)

    await cache.on_partitions_assigned(set(consumer.assignment()))

    assert len(consumer.lookups) == 1
    (lookup,) = consumer.lookups
    assert set(lookup) == {TopicPartition(_HORIZON_TOPIC, 0)}
    target = lookup[TopicPartition(_HORIZON_TOPIC, 0)]
    after_ms = int(time.time() * 1000)
    assert before_ms - 3_600_000 <= target <= after_ms - 3_600_000
    assert consumer.seeks == [(TopicPartition(_HORIZON_TOPIC, 0), 940)]
    assert cache.bootstrap_horizon_report(_HORIZON_TOPIC) == {
        "horizon_seconds": 3600,
        "partitions": {"0": {"outcome": "started_at_horizon", "start_offset": 940}},
    }
    assert cache.bootstrap_horizon_report(_FULL_TOPIC) is None


async def test_no_record_inside_the_horizon_falls_back_to_the_full_replay() -> None:
    consumer = _FakeConsumer(end=1000, horizon_hit=None)
    cache = _cache(consumer)

    await cache.on_partitions_assigned(set(consumer.assignment()))

    assert consumer.seeks == []
    report = cache.bootstrap_horizon_report(_HORIZON_TOPIC)
    assert report is not None
    assert report["partitions"]["0"] == {
        "outcome": "no_record_inside_horizon",
        "start_offset": None,
    }


async def test_a_failed_lookup_falls_back_to_the_full_replay() -> None:
    consumer = _FakeConsumer(
        end=1000, horizon_hit=940, lookup_error=TimeoutError("broker slow")
    )
    cache = _cache(consumer)

    await cache.on_partitions_assigned(set(consumer.assignment()))

    assert consumer.seeks == []
    report = cache.bootstrap_horizon_report(_HORIZON_TOPIC)
    assert report is not None
    entry = report["partitions"]["0"]
    assert entry["outcome"] == "lookup_failed"
    assert entry["start_offset"] is None
    assert "broker slow" in entry["detail"]


async def test_the_seek_never_marks_a_partition_caught_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The horizon shortens the replay; reaching the end still has to happen."""
    import omnimarket.projection.snapshot_cache as snapshot_cache_module

    monkeypatch.setattr(snapshot_cache_module, "_BOOTSTRAP_POLL_INTERVAL_SECONDS", 0.0)
    monkeypatch.setattr(snapshot_cache_module, "_BOOTSTRAP_POLL_MAX_ATTEMPTS", 1)
    consumer = _FakeConsumer(end=1000, horizon_hit=940)
    cache = _cache(consumer)

    await cache.on_partitions_assigned(set(consumer.assignment()))
    assert not cache.is_bootstrapped(_HORIZON_TOPIC)
    assert not cache.is_bootstrapped(_FULL_TOPIC)

    task = asyncio.ensure_future(cache._consume_loop())
    try:
        async with asyncio.timeout(5):
            while not (
                cache.is_bootstrapped(_HORIZON_TOPIC)
                and cache.is_bootstrapped(_FULL_TOPIC)
            ):
                await asyncio.sleep(0)
    finally:
        cache._running = False
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    # The horizon topic read 60 records from its start; the other read all.
    assert cache.lag_report(_HORIZON_TOPIC) == {
        "applied_offset": 1000,
        "end_offset": 1000,
        "lag": 0,
        "partitions": 1,
        "dropped_since_apply": 0,
        "dropped_total": 0,
    }
    assert consumer.position_by_tp[TopicPartition(_FULL_TOPIC, 0)] == 1000


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


async def _start_with_fake(
    monkeypatch: pytest.MonkeyPatch, exposures: dict[str, ProjectionTableConfig]
) -> _FakeConsumerCapturingSubscribe:
    import omnimarket.projection.snapshot_cache as snapshot_cache_module

    _FakeConsumerCapturingSubscribe.instances = []
    monkeypatch.setattr(
        snapshot_cache_module, "AIOKafkaConsumer", _FakeConsumerCapturingSubscribe
    )
    cache = SnapshotCache(
        exposures, bootstrap_servers="unused:9092", group_id="test-group"
    )
    # The consume loop is not under test here; stop it before it polls.
    monkeypatch.setattr(cache, "_consume_loop", _noop)
    await cache.start()
    await cache.stop()
    (consumer,) = _FakeConsumerCapturingSubscribe.instances
    return consumer


async def _noop() -> None:
    return None


async def test_start_subscribes_with_a_listener_when_a_horizon_is_declared(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    consumer = await _start_with_fake(
        monkeypatch,
        {
            _HORIZON_TOPIC: _cfg(_HORIZON_TOPIC, horizon=3600),
            _FULL_TOPIC: _cfg(_FULL_TOPIC, horizon=None),
        },
    )
    assert consumer.subscribed is not None
    topics, listener = consumer.subscribed
    assert sorted(topics) == sorted(consumer.topics)
    assert listener is not None


async def test_start_subscribes_with_a_listener_without_a_horizon_too(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every exposure needs the listener: a rejoin must resume, horizon or not."""
    consumer = await _start_with_fake(
        monkeypatch, {_FULL_TOPIC: _cfg(_FULL_TOPIC, horizon=None)}
    )
    assert consumer.subscribed is not None
    assert consumer.subscribed[1] is not None


async def _consume_until_bootstrapped(cache: SnapshotCache) -> None:
    task = asyncio.ensure_future(cache._consume_loop())
    try:
        async with asyncio.timeout(5):
            while not (
                cache.is_bootstrapped(_HORIZON_TOPIC)
                and cache.is_bootstrapped(_FULL_TOPIC)
            ):
                await asyncio.sleep(0)
    finally:
        cache._running = False
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    cache._running = True


class _UpsertMsg(_Msg):
    """A real upsert whose source offset is its own offset, so a re-read of
    an applied record is a counted drop -- the signature a rejoin left."""

    def __init__(self, *, topic: str, offset: int) -> None:
        super().__init__(topic=topic, offset=offset)
        self.key = f"k{offset % 10}".encode()
        key = f"k{offset % 10}"
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


class _UpsertConsumer(_FakeConsumer):
    async def getmany(
        self, *, timeout_ms: int = 0, max_records: int | None = None
    ) -> dict[TopicPartition, list[_Msg]]:
        batches = await super().getmany(timeout_ms=timeout_ms, max_records=max_records)
        return {
            tp: [_UpsertMsg(topic=m.topic, offset=m.offset) for m in msgs]
            for tp, msgs in batches.items()
        }

    def reset_to_log_start(self) -> None:
        """What aiokafka does to every partition on a rejoin for a group with
        no committed offsets under ``auto_offset_reset="earliest"``."""
        for tp in self.position_by_tp:
            self.position_by_tp[tp] = 0


async def test_a_rejoin_resumes_where_the_cache_left_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import omnimarket.projection.snapshot_cache as snapshot_cache_module

    monkeypatch.setattr(snapshot_cache_module, "_BOOTSTRAP_POLL_INTERVAL_SECONDS", 0.0)
    monkeypatch.setattr(snapshot_cache_module, "_BOOTSTRAP_POLL_MAX_ATTEMPTS", 1)
    consumer = _UpsertConsumer(end=1000, horizon_hit=940)
    cache = _cache(consumer)
    await cache.on_partitions_assigned(set(consumer.assignment()))
    await _consume_until_bootstrapped(cache)
    assert cache.reassignment_count == 0
    assert cache.row_count(_FULL_TOPIC) == 10
    assert cache.get_rows(_FULL_TOPIC, unbounded=True)[0]["v"] >= 990
    assert cache.lag_report(_FULL_TOPIC)["dropped_total"] == 0  # type: ignore[index]

    # The heartbeat session expires, the group rejoins, aiokafka resets every
    # partition to the log start and hands the assignment back.
    consumer.reset_to_log_start()
    consumer.seeks.clear()
    consumer.lookups.clear()
    await cache.on_partitions_assigned(set(consumer.assignment()))

    assert cache.reassignment_count == 1
    assert sorted(consumer.seeks) == sorted(
        [
            (TopicPartition(_HORIZON_TOPIC, 0), 1000),
            (TopicPartition(_FULL_TOPIC, 0), 1000),
        ]
    )
    # Resumed, not re-sought: the horizon is for a partition with nothing applied.
    assert consumer.lookups == []
    await _consume_until_bootstrapped(cache)
    for topic in (_HORIZON_TOPIC, _FULL_TOPIC):
        report = cache.lag_report(topic)
        assert report is not None
        assert report["dropped_since_apply"] == 0
        assert report["dropped_total"] == 0
        assert report["lag"] == 0
        assert not cache.is_stale(topic)


async def test_a_position_round_trip_is_not_mistaken_for_applied_state() -> None:
    """The RPC catch-up check seeds next_position from position(); a rejoin
    that resumed from that would skip records nobody applied."""
    consumer = _FakeConsumer(end=1000, horizon_hit=940)
    consumer.position_by_tp[TopicPartition(_FULL_TOPIC, 0)] = 700
    cache = _cache(consumer)

    await cache._mark_bootstrap_complete_when_caught_up()
    await cache.on_partitions_assigned(set(consumer.assignment()))

    assert (TopicPartition(_FULL_TOPIC, 0), 700) not in consumer.seeks
    assert consumer.seeks == [(TopicPartition(_HORIZON_TOPIC, 0), 940)]


def test_a_horizon_on_a_sql_served_exposure_is_refused() -> None:
    with pytest.raises(ValidationError, match="not bus_backed"):
        ProjectionTableConfig(
            topic="t",
            table="t",
            columns=("k",),
            bootstrap_horizon_seconds=60,
        )


@pytest.mark.parametrize("bad", [0, -5, "3600", 1.5, True])
def test_a_malformed_horizon_excludes_the_contract(bad: object) -> None:
    contract = yaml.safe_load(_CONTRACT_PATH.read_text())
    contract["projection_api"]["bootstrap_horizon_seconds"] = bad
    assert not load_projection_exposures_from_contract(
        contract, "projection_consumer_flow", _CONTRACT_PATH
    )


def test_consumer_flow_declares_a_one_hour_horizon() -> None:
    contract = yaml.safe_load(_CONTRACT_PATH.read_text())
    (cfg,) = load_projection_exposures_from_contract(
        contract, "projection_consumer_flow", _CONTRACT_PATH
    )
    assert cfg.topic == _HORIZON_TOPIC
    assert cfg.bootstrap_horizon_seconds == 3600


def test_ready_reports_the_horizon_without_changing_the_verdict() -> None:
    consumer = _FakeConsumer(end=1000, horizon_hit=940)
    cache = _cache(consumer)
    asyncio.run(cache.on_partitions_assigned(set(consumer.assignment())))
    topic_map = {
        _HORIZON_TOPIC: _cfg(_HORIZON_TOPIC, horizon=3600),
        _FULL_TOPIC: _cfg(_FULL_TOPIC, horizon=None),
    }
    app.dependency_overrides[get_snapshot_cache] = lambda: cache
    app.dependency_overrides[get_topic_map] = lambda: topic_map
    try:
        response = TestClient(app).get("/ready")
    finally:
        app.dependency_overrides.clear()

    # Sought but not yet replayed: still refused.
    assert response.status_code == 503
    body = response.json()
    assert body["bus_backed_topics"][_HORIZON_TOPIC] is False
    assert body["bootstrap_horizon"] == {
        _HORIZON_TOPIC: {
            "horizon_seconds": 3600,
            "partitions": {"0": {"outcome": "started_at_horizon", "start_offset": 940}},
        }
    }
