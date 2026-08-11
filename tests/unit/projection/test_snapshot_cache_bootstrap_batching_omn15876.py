# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-15876 -- SnapshotCache's bootstrap catch-up check must be batched,
not fired once per individually-consumed message.

Root cause (live, deploy runs 31510796348/31519764040/31522100811, byte-
proven via `/ready`'s JSON body -- captured live for the first time this
incident -- and refuted against the two prior fix strikes, omninode_infra#865
and #866, which are producer-side/deploy-mechanics only and do not touch
this file):

``_consume_loop`` iterated the ``AIOKafkaConsumer`` one message at a time
(``async for msg in self._consumer``) and, for every message from a
not-yet-bootstrapped topic, awaited
``_mark_bootstrap_complete_when_caught_up()`` -- an unbatched
``end_offsets()`` (broker round trip across every assigned partition) plus a
per-partition ``position()`` round trip. Onex-dev's
``onex.snapshot.projection.live-events.v1`` exposure holds 60,724+ retained
messages and is actively growing, so a fresh pod's bootstrap replay fired
this RPC-heavy check tens of thousands of times before catching up --
observed live as a pod that ran 47+ minutes with zero restarts and never
left ``/ready`` 503 (``readinessProbe`` only marks NotReady; it does not
restart the pod, so this is a genuine non-convergent-in-practice algorithm,
not a crash).

The fix: consume in batches via ``AIOKafkaConsumer.getmany()`` and run the
catch-up check at most once per batch (bounded by ``max_records``), not once
per message -- bounding RPC overhead independent of backlog size.

RED before the fix (recorded 2026-08-11): ``_consume_loop`` used
``async for msg in self._consumer`` and called
``_mark_bootstrap_complete_when_caught_up()`` on every not-yet-bootstrapped
message. Driving 500 synthetic messages through the pre-fix loop calls the
fake consumer's ``end_offsets()`` once per message (500 times) -- this test
asserts a bound far below that (< 10), which pre-fix code cannot satisfy.
"""

from __future__ import annotations

import asyncio
import contextlib
import json

from aiokafka import TopicPartition

from omnimarket.projection.models import ProjectionTableConfig
from omnimarket.projection.snapshot_cache import SnapshotCache

_TOPIC = "onex.snapshot.projection.test-bootstrap-batching.v1"
_SOURCE_TOPIC = "onex.evt.platform.node-heartbeat.v1"


def _make_cache() -> SnapshotCache:
    exposure = ProjectionTableConfig(
        topic=_TOPIC,
        table="test_table",
        columns=("id", "value"),
        bus_backed=True,
        key_columns=("id",),
        # limit=1000 (well above n_messages=500 * 4-x eviction headroom in
        # the test below): this test's subject is bootstrap-check batching,
        # not the unrelated row-count eviction cap (state.rows capped at
        # exposure.limit * 4) -- a small limit would silently evict rows
        # and produce a row_count assertion failure orthogonal to what this
        # test verifies.
        limit=1000,
    )
    return SnapshotCache(
        {_TOPIC: exposure},
        bootstrap_servers="unused:9092",
        group_id="test-bootstrap-batching-group",
    )


def _delta_bytes(*, row_id: str, value: int, source_offset: int) -> bytes:
    payload = {
        "topic": _TOPIC,
        "key": [row_id],
        "op": "upsert",
        "row": {"id": row_id, "value": value},
        "observed_at": "2026-08-11T00:00:00+00:00",
        "source_event_id": f"evt-{row_id}-{source_offset}",
        "source_topic": _SOURCE_TOPIC,
        "source_partition": 0,
        "source_offset": source_offset,
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


class _FakeConsumer:
    """Minimal AIOKafkaConsumer stand-in: a fixed backlog on one partition,
    served via getmany() batches, with call counters on the two RPCs the
    bootstrap catch-up check makes (end_offsets, position).

    ``assignment_delay_calls`` simulates aiokafka's lazy post-group-join
    partition assignment (module docstring in snapshot_cache.py): the first
    N calls to ``assignment()`` return no partitions at all, as if the
    group join has not yet completed.
    """

    def __init__(
        self,
        *,
        topic: str,
        backlog: list[_FakeMessage],
        assignment_delay_calls: int = 0,
    ) -> None:
        self._tp = TopicPartition(topic, 0)
        self._backlog = list(backlog)
        self._end_offset = len(backlog)
        self._position = 0
        self._assignment_calls = 0
        self._assignment_delay_calls = assignment_delay_calls
        self.end_offsets_calls = 0
        self.position_calls = 0
        self.getmany_calls = 0
        self.max_records_seen: list[int | None] = []

    def assignment(self) -> frozenset[TopicPartition]:
        self._assignment_calls += 1
        if self._assignment_calls <= self._assignment_delay_calls:
            return frozenset()
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
        self.max_records_seen.append(max_records)
        if not self._backlog:
            # Idle: real aiokafka blocks up to timeout_ms then returns {}.
            await asyncio.sleep(0)
            return {}
        take = len(self._backlog) if max_records is None else max_records
        batch, self._backlog = self._backlog[:take], self._backlog[take:]
        self._position += len(batch)
        return {self._tp: batch}


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


async def test_bootstrap_catch_up_check_is_batched_not_per_message(
    monkeypatch: object,
) -> None:
    # Collapse the pre-iteration poll (module-level constants unrelated to
    # this test's subject) so the test exercises the batching behavior of
    # the consume loop itself, not the unrelated fixed poll-interval wait.
    import omnimarket.projection.snapshot_cache as snapshot_cache_module

    monkeypatch.setattr(  # type: ignore[attr-defined]
        snapshot_cache_module, "_BOOTSTRAP_POLL_INTERVAL_SECONDS", 0.0
    )
    monkeypatch.setattr(  # type: ignore[attr-defined]
        snapshot_cache_module, "_BOOTSTRAP_POLL_MAX_ATTEMPTS", 1
    )

    cache = _make_cache()
    n_messages = 500
    backlog = [
        _FakeMessage(
            topic=_TOPIC,
            partition=0,
            offset=i,
            value=_delta_bytes(row_id=f"row-{i}", value=i, source_offset=i),
        )
        for i in range(n_messages)
    ]
    fake = _FakeConsumer(topic=_TOPIC, backlog=backlog)
    cache._consumer = fake
    cache._running = True

    await _run_until_bootstrapped(cache, timeout=5.0)

    assert cache.is_bootstrapped(_TOPIC)
    assert cache.row_count(_TOPIC) == n_messages
    assert fake.end_offsets_calls < 10, (
        f"expected the bootstrap catch-up check to run once per BATCH, not "
        f"once per message -- {n_messages} messages produced "
        f"{fake.end_offsets_calls} end_offsets() broker round trips "
        "(pre-fix code produces one per message, i.e. ~500 here). This is "
        "the exact RPC-storm mechanism that leaves onex-dev's "
        "live-events.v1 exposure (60,724+ retained messages) permanently "
        "not_ready under real backlog size."
    )
    # CodeRabbit (PR #2051, PRRT_kwDOR6jjtc6YWuME): a getmany() call with no
    # bound would still pass the RPC-count assertion above (one call, one
    # batch, one check) without actually protecting the bounded-batch
    # contract. Assert every call actually requested a bounded batch.
    assert fake.max_records_seen, "getmany() was never called"
    assert all(n is not None and 0 < n <= 500 for n in fake.max_records_seen), (
        f"expected every getmany() call to request a bounded batch "
        f"(1..500), got max_records values {fake.max_records_seen} -- an "
        "unbounded (None) or oversized request defeats the batching fix's "
        "RPC-overhead bound."
    )


async def test_bootstrap_catch_up_check_still_runs_after_a_late_empty_assignment(
    monkeypatch: object,
) -> None:
    """CodeRabbit (PR #2051, PRRT_kwDOR6jjtc6YWuL5): if partition assignment
    happens AFTER the initial bounded poll window exhausts (aiokafka assigns
    partitions lazily post-group-join -- see the module docstring), and the
    assigned topic turns out to be empty, the consume loop must still notice
    and complete bootstrap -- not skip the catch-up check forever just
    because getmany() keeps returning no records."""
    import omnimarket.projection.snapshot_cache as snapshot_cache_module

    # Small, deterministic initial poll window that exhausts BEFORE the
    # fake's assignment ever appears (assignment_delay_calls > poll attempts
    # below), reproducing "assignment occurs after the initial poll window".
    monkeypatch.setattr(  # type: ignore[attr-defined]
        snapshot_cache_module, "_BOOTSTRAP_POLL_INTERVAL_SECONDS", 0.0
    )
    monkeypatch.setattr(  # type: ignore[attr-defined]
        snapshot_cache_module, "_BOOTSTRAP_POLL_MAX_ATTEMPTS", 2
    )

    cache = _make_cache()
    # Empty topic (zero backlog): assignment first appears on the 5th call
    # to assignment() -- after the 2-attempt initial poll window (which
    # calls assignment() once per attempt via
    # _mark_bootstrap_complete_when_caught_up) has already exhausted.
    fake = _FakeConsumer(topic=_TOPIC, backlog=[], assignment_delay_calls=4)
    cache._consumer = fake
    cache._running = True

    await _run_until_bootstrapped(cache, timeout=5.0)

    assert cache.is_bootstrapped(_TOPIC), (
        "bootstrap must complete once a late partition assignment appears, "
        "even though every getmany() call returns an empty batch (empty "
        "topic) -- skipping the catch-up check whenever a batch is empty "
        "leaves the cache permanently un-bootstrapped and /ready "
        "permanently 503 for any bus_backed topic with zero traffic that "
        "gets assigned after the initial poll window"
    )
