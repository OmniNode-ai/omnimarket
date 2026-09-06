# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-15876 -- a fresh pod's bootstrap replay must not pay a broker round
trip per consumed batch.

MEASURED, not inferred (live, onex-dev / dev-system i-06169517a92b45f86):

* ``onex.snapshot.projection.live-events.v1`` retains 60,724+ compacted
  records; a FRESH ``omnimarket-projection-api`` pod took 20-63 minutes to
  finish its initial replay of it and of
  ``onex.snapshot.projection.consumer-flow.v1`` (ledger row
  ``2026-09-06T11:42:04Z``, probes 34025192401 / 34030482485). That is
  15-50 records/second.
* The apply path is not the cost. Driving 60,724 synthetic deltas of the
  live-events shape through :meth:`SnapshotCache.apply_message` -- including
  ``model_validate_json`` and the ``limit * 4`` row-cap eviction sort that
  fires on essentially every record once the 400-row cap is reached --
  completes in 3.44s, i.e. 17,673 records/second (0.057 ms/record). So
  >99.7% of the live wall time was spent in awaits, not in applying records.
* What is left is the per-batch bootstrap catch-up check.
  ``_CONSUME_BATCH_MAX_RECORDS`` is 500, so 60,724 records is >=122
  ``getmany()`` batches, and ``_run_consume_loop`` awaited
  ``_mark_bootstrap_complete_when_caught_up()`` after EVERY batch while any
  topic was still un-bootstrapped -- one ``end_offsets()`` broker round trip
  across all assigned partitions plus one ``position()`` await per assigned
  partition (ten of them on onex-dev), each wrapped in a 30s
  ``asyncio.wait_for``. 122 batches x 10-31s of stalled check == the 20-63
  minutes observed.

OMN-15876 #2051 bounded this check by BATCH count instead of MESSAGE count,
which is what made the replay finish at all. It is still O(backlog) round
trips, and O(backlog) is the property that does not fit inside the
Deployment's 600s ``progressDeadlineSeconds``.

The fix is that during an active replay the check needs NO round trip at
all. ``AIOKafkaConsumer.highwater(tp)`` is a synchronous, zero-RPC accessor
that aiokafka populates from every ``FetchResponse``
(``aiokafka/consumer/fetcher.py``: ``tp_state.highwater = highwater``), so it
is strictly FRESHER than a periodic ``end_offsets()`` call, and the consumed
records carry their own offsets. The RPC check is retained unchanged as the
authority for the case the fast path cannot answer -- a partition that
delivers no records at all (an empty topic, or a compacted partition whose
retained head is entirely below the log start) -- and is rate-limited.

RED before the fix: this test's bound is a small constant; the pre-fix loop
calls ``end_offsets()`` once per batch, i.e. ceil(60724/500) = 122 times for
the backlog below, which no constant bound of 5 can satisfy.

GATE-DIRECTION LAW: nothing here marks a topic bootstrapped that is not.
``test_fast_path_never_marks_a_partition_that_has_not_reached_highwater``
and ``test_unknown_highwater_does_not_bootstrap`` pin the refusal direction.
"""

from __future__ import annotations

import asyncio
import contextlib
import json

import pytest
from aiokafka import TopicPartition

from omnimarket.projection.models import ProjectionTableConfig
from omnimarket.projection.snapshot_cache import SnapshotCache

_TOPIC = "onex.snapshot.projection.live-events.v1"
_SOURCE_TOPIC = "onex.evt.platform.node-heartbeat.v1"

# The live backlog measured on onex-dev, used verbatim so the bound below is
# a statement about the real topic and not about a convenient small number.
_LIVE_BACKLOG_RECORDS = 60_724

# A fresh pod's replay may spend at most this many bootstrap-check broker
# round trips. It is a CONSTANT: the whole defect is that the pre-fix count
# scaled with the backlog. Five leaves room for the initial bounded poll and
# for the rate-limited settle call once the flow stops.
_MAX_BOOTSTRAP_RPC_CALLS = 5


def _make_cache(limit: int = 100) -> SnapshotCache:
    """The live ``live-events`` exposure shape, limit included.

    ``limit=100`` is what ``node_projection_live_events/contract.yaml``
    declares, so the ``limit * 4`` row cap and its eviction sort are exercised
    exactly as they are in the deployed pod rather than being tuned away.
    """
    exposure = ProjectionTableConfig(
        topic=_TOPIC,
        table="live_events",
        columns=(
            "id",
            "event_id",
            "type",
            "timestamp",
            "source",
            "topic",
            "summary",
            "payload",
            "correlation_id",
            "created_at",
        ),
        order_by="created_at DESC",
        freshness_column="created_at",
        limit=limit,
        bus_backed=True,
        key_columns=("event_id",),
    )
    return SnapshotCache(
        {_TOPIC: exposure},
        bootstrap_servers="unused:9092",
        group_id="test-replay-throughput-group",
    )


def _delta_bytes(index: int) -> bytes:
    payload = {
        "topic": _TOPIC,
        "key": [f"evt-{index}"],
        "op": "upsert",
        "row": {"id": index, "event_id": f"evt-{index}"},
        "observed_at": f"2026-09-06T00:{(index // 60) % 60:02d}:{index % 60:02d}+00:00",
        "source_event_id": f"evt-{index}",
        "source_topic": _SOURCE_TOPIC,
        "source_partition": 0,
        "source_offset": index,
        "projection_version": "projection_snapshot.v1",
    }
    return json.dumps(payload).encode("utf-8")


class _FakeMessage:
    __slots__ = ("headers", "key", "offset", "partition", "topic", "value")

    def __init__(
        self, *, topic: str, partition: int, offset: int, value: bytes
    ) -> None:
        self.topic = topic
        self.partition = partition
        self.offset = offset
        self.key = None
        self.value = value
        self.headers: list[tuple[str, bytes]] = [("tenant_id", b"omninode")]


class _CountingConsumer:
    """An ``AIOKafkaConsumer`` stand-in that counts the two RPCs.

    ``highwater()`` is synchronous and free, exactly as aiokafka's is -- it is
    served out of the same backlog metadata a real ``FetchResponse`` carries,
    and it is deliberately NOT counted, because not costing a round trip is
    the entire point of the fast path.
    """

    def __init__(
        self,
        *,
        topic: str,
        backlog: list[_FakeMessage],
        highwater_known: bool = True,
    ) -> None:
        self._tp = TopicPartition(topic, 0)
        self._backlog = list(backlog)
        self._end_offset = len(backlog)
        self._position = 0
        self._highwater_known = highwater_known
        self.end_offsets_calls = 0
        self.position_calls = 0
        self.getmany_calls = 0

    # -- zero-RPC accessor -------------------------------------------------
    def highwater(self, tp: TopicPartition) -> int | None:
        if not self._highwater_known:
            return None
        return self._end_offset

    # -- assignment / RPCs -------------------------------------------------
    def assignment(self) -> frozenset[TopicPartition]:
        return frozenset({self._tp})

    async def end_offsets(
        self, partitions: list[TopicPartition]
    ) -> dict[TopicPartition, int]:
        self.end_offsets_calls += 1
        return {self._tp: self._end_offset}

    async def position(self, tp: TopicPartition) -> int:
        self.position_calls += 1
        return self._position

    async def getmany(
        self, *, timeout_ms: int = 0, max_records: int | None = None
    ) -> dict[TopicPartition, list[_FakeMessage]]:
        self.getmany_calls += 1
        if not self._backlog:
            await asyncio.sleep(0)
            return {}
        take = len(self._backlog) if max_records is None else max_records
        batch, self._backlog = self._backlog[:take], self._backlog[take:]
        self._position += len(batch)
        return {self._tp: batch}


def _backlog(n: int) -> list[_FakeMessage]:
    return [
        _FakeMessage(topic=_TOPIC, partition=0, offset=i, value=_delta_bytes(i))
        for i in range(n)
    ]


async def _run_until_bootstrapped(cache: SnapshotCache, *, timeout: float) -> None:
    task = asyncio.ensure_future(cache._consume_loop())
    try:
        async with asyncio.timeout(timeout):
            while not cache.is_bootstrapped(_TOPIC):
                await asyncio.sleep(0)
    finally:
        cache._running = False
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


@pytest.fixture(autouse=True)
def _collapse_poll_interval(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove the fixed pre-iteration sleep, which is not this test's subject."""
    import omnimarket.projection.snapshot_cache as module

    monkeypatch.setattr(module, "_BOOTSTRAP_POLL_INTERVAL_SECONDS", 0.0)
    monkeypatch.setattr(module, "_BOOTSTRAP_POLL_MAX_ATTEMPTS", 1)


@pytest.mark.unit
async def test_replay_of_the_live_backlog_costs_a_constant_number_of_round_trips() -> (
    None
):
    """The measured defect: bootstrap-check RPCs scaled with the backlog.

    Pre-fix this loop calls ``end_offsets()`` once per ``getmany()`` batch --
    ceil(60724/500) = 122 times -- and each of those calls is what the live
    pod spent its 20-63 minutes inside. Post-fix the replay is driven by
    ``highwater()``, which costs nothing, so the count is a small constant
    that does not move when the backlog grows.
    """
    cache = _make_cache()
    consumer = _CountingConsumer(topic=_TOPIC, backlog=_backlog(_LIVE_BACKLOG_RECORDS))
    cache._consumer = consumer  # type: ignore[assignment]
    cache._running = True

    await _run_until_bootstrapped(cache, timeout=120.0)

    assert cache.is_bootstrapped(_TOPIC)
    assert consumer.getmany_calls >= _LIVE_BACKLOG_RECORDS // 500, (
        "the backlog must actually have been consumed in batches for this "
        f"bound to mean anything (getmany calls: {consumer.getmany_calls})"
    )
    assert consumer.end_offsets_calls <= _MAX_BOOTSTRAP_RPC_CALLS, (
        f"bootstrap catch-up made {consumer.end_offsets_calls} end_offsets() "
        f"round trips replaying {_LIVE_BACKLOG_RECORDS} records; the count "
        "must not scale with the backlog"
    )
    assert consumer.position_calls <= _MAX_BOOTSTRAP_RPC_CALLS, (
        f"bootstrap catch-up made {consumer.position_calls} position() "
        "awaits; the count must not scale with the backlog"
    )


@pytest.mark.unit
async def test_rpc_count_does_not_grow_when_the_backlog_grows() -> None:
    """Scaling, stated as a comparison rather than as a single bound.

    A 10x backlog must not cost 10x round trips. Pre-fix it costs exactly
    10x, because the check ran once per batch.
    """
    counts: list[int] = []
    for size in (6_000, 60_000):
        cache = _make_cache()
        consumer = _CountingConsumer(topic=_TOPIC, backlog=_backlog(size))
        cache._consumer = consumer  # type: ignore[assignment]
        cache._running = True
        await _run_until_bootstrapped(cache, timeout=120.0)
        assert cache.is_bootstrapped(_TOPIC)
        counts.append(consumer.end_offsets_calls)

    small, large = counts
    assert large <= small + 1, (
        f"end_offsets() calls grew from {small} to {large} when the backlog "
        "grew 10x; bootstrap-check cost must be independent of backlog size"
    )


@pytest.mark.unit
async def test_fast_path_never_marks_a_partition_that_has_not_reached_highwater() -> (
    None
):
    """GATE-DIRECTION LAW: the cheap path may only ever REFUSE earlier.

    The consumer reports a highwater above the delivered backlog, i.e. records
    exist that this cache has not applied. No amount of consuming the records
    it CAN see may mark the topic bootstrapped.
    """
    cache = _make_cache()
    consumer = _CountingConsumer(topic=_TOPIC, backlog=_backlog(1_000))
    # Ten records exist beyond what will ever be delivered.
    consumer._end_offset = 1_010
    cache._consumer = consumer  # type: ignore[assignment]
    cache._running = True

    task = asyncio.ensure_future(cache._consume_loop())
    try:
        for _ in range(2_000):
            await asyncio.sleep(0)
    finally:
        cache._running = False
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    assert not cache.is_bootstrapped(_TOPIC), (
        "topic was marked bootstrapped while the consumer's own highwater "
        "reported unread records -- readiness must fail closed"
    )


@pytest.mark.unit
async def test_unknown_highwater_does_not_bootstrap_on_the_fast_path() -> None:
    """A partition whose highwater aiokafka has not yet learned is unknown,
    and unknown is refused, never assumed caught up."""
    cache = _make_cache()
    consumer = _CountingConsumer(
        topic=_TOPIC, backlog=_backlog(50), highwater_known=False
    )
    cache._consumer = consumer  # type: ignore[assignment]
    cache._running = True

    cache._mark_bootstrap_complete_from_fetch_metadata()

    assert not cache.is_bootstrapped(_TOPIC)
    cache._running = False


class _TwoPartitionConsumer:
    """One topic, two partitions: partition 0 streams a backlog, partition 1
    delivers nothing at all (an empty partition, or a compacted partition whose
    retained head sits entirely below the log start).

    This is the shape that makes "was the last batch empty?" the wrong gate for
    spending a round trip: partition 0 keeps the topic looking busy for the
    whole replay while partition 1 can only ever be settled by ``position()``.
    """

    def __init__(self, *, topic: str, backlog: list[_FakeMessage]) -> None:
        self._busy = TopicPartition(topic, 0)
        self._silent = TopicPartition(topic, 1)
        self._backlog = list(backlog)
        self._busy_end = len(backlog)
        self._busy_position = 0
        self.end_offsets_calls = 0
        self.position_calls = 0

    def highwater(self, tp: TopicPartition) -> int | None:
        if tp == self._busy:
            return self._busy_end
        # Never fetched from: aiokafka has no highwater for it.
        return None

    def assignment(self) -> frozenset[TopicPartition]:
        return frozenset({self._busy, self._silent})

    async def end_offsets(
        self, partitions: list[TopicPartition]
    ) -> dict[TopicPartition, int]:
        self.end_offsets_calls += 1
        return {self._busy: self._busy_end, self._silent: 0}

    async def position(self, tp: TopicPartition) -> int:
        self.position_calls += 1
        return self._busy_position if tp == self._busy else 0

    async def getmany(
        self, *, timeout_ms: int = 0, max_records: int | None = None
    ) -> dict[TopicPartition, list[_FakeMessage]]:
        if not self._backlog:
            await asyncio.sleep(0)
            return {}
        take = len(self._backlog) if max_records is None else max_records
        batch, self._backlog = self._backlog[:take], self._backlog[take:]
        self._busy_position += len(batch)
        return {self._busy: batch}


@pytest.mark.unit
async def test_a_silent_partition_on_a_busy_topic_is_still_settled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A partition the fast path cannot answer must still get its round trip.

    Gating the RPC-backed check on "the last batch delivered nothing" would
    starve this partition for as long as its sibling keeps delivering, and the
    topic would never reach bootstrap_complete -- fail-closed, but permanently
    and wrongly so. The gate is instead "is there an un-bootstrapped partition
    the fast path cannot settle", which this partition always answers yes to
    until a position has been read for it.
    """
    import omnimarket.projection.snapshot_cache as module

    monkeypatch.setattr(module, "_BOOTSTRAP_RPC_CHECK_MIN_INTERVAL_SECONDS", 0.0)

    cache = _make_cache()
    consumer = _TwoPartitionConsumer(topic=_TOPIC, backlog=_backlog(5_000))
    cache._consumer = consumer  # type: ignore[assignment]
    cache._running = True

    await _run_until_bootstrapped(cache, timeout=60.0)

    assert cache.is_bootstrapped(_TOPIC)
