# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-15876 -- the SnapshotCache consume loop was unsupervised, so its death
was both PERMANENT and INVISIBLE.

``SnapshotCache.start()`` launches ``_consume_loop`` with
``asyncio.ensure_future`` and never awaits it again except in ``stop()``. Every
broker call inside that loop can raise:

* ``AIOKafkaConsumer.position(tp)`` raises ``IllegalStateError`` the instant
  the partition read out of ``assignment()`` is revoked by a rebalance before
  the await lands on it -- aiokafka ``consumer/consumer.py``, verbatim:
  ``raise IllegalStateError(f"Partition {partition} is not assigned")``. The
  assignment read and that await are separated by an ``end_offsets()`` broker
  round trip, so the window is real, and a topic created LATER (onex-dev's
  boot-side per-contract topic provisioner reached Ready ~21 minutes after the
  projection-api pod subscribed, probe run 34014341106) is exactly what
  triggers the metadata change that rebalances a running group.
* ``end_offsets()`` raises ``KafkaTimeoutError`` on a slow broker and is
  documented to "block indefinitely if the partition does not exist".
* ``getmany()`` surfaces broker-side fetch and authorization errors.

Before this change ANY of those ended consumption for the life of the process
and printed NOTHING: asyncio logs an unretrieved task exception only when the
Task object is garbage collected, and ``self._consume_task`` holds a strong
reference until shutdown, so it never is. The live symptom was exactly that
shape -- ``omnimarket-projection-api`` on onex-dev, pod
``5d94f4747f-vhsml``, ``RestartCount 0``, ``/health`` 200, ``/ready`` 503
with 8 of 10 topics ``bootstrapped=True`` and the remaining two frozen; two
readings six minutes apart byte-identical (probe runs 34013732493 /
34014004177); the only non-uvicorn log line in the pod's entire retained
window was the bounded-poll warning, with no traceback anywhere.

GATE DIRECTION. Every assertion below is that a failure is RECORDED and
REFUSED, never that it is tolerated: nothing here can mark a topic
bootstrapped that is not, and the new ``consume_failure`` is a fresh reason
for ``/ready`` to fail closed, never a reason to pass.
"""

from __future__ import annotations

import asyncio
import contextlib

import pytest
from aiokafka import TopicPartition
from aiokafka.errors import IllegalStateError

from omnimarket.projection.models import ProjectionTableConfig
from omnimarket.projection.snapshot_cache import SnapshotCache

pytestmark = pytest.mark.unit

_TOPIC = "onex.snapshot.projection.test-supervision.v1"


def _make_cache() -> SnapshotCache:
    exposure = ProjectionTableConfig(
        topic=_TOPIC,
        table="test_table",
        columns=("id", "value"),
        bus_backed=True,
        key_columns=("id",),
        limit=100,
    )
    return SnapshotCache(
        {_TOPIC: exposure},
        bootstrap_servers="unused:9092",
        group_id="test-supervision-group",
    )


class _FakeConsumer:
    """An empty, already-assigned topic, with per-call failure injection.

    ``fail_position_times`` / ``fail_end_offsets_times`` / ``fail_getmany_times``
    raise on the first N calls and then behave. ``assignment_raises`` raises
    from ``assignment()`` itself -- the one call in the catch-up check that is
    deliberately NOT guarded, so it exercises the outer supervisor.
    """

    def __init__(
        self,
        *,
        fail_position_times: int = 0,
        fail_position_exc: type[BaseException] = IllegalStateError,
        fail_end_offsets_times: int = 0,
        fail_getmany_times: int = 0,
        assignment_raises: bool = False,
    ) -> None:
        self._tp = TopicPartition(_TOPIC, 0)
        self._fail_position_times = fail_position_times
        self._fail_position_exc = fail_position_exc
        self._fail_end_offsets_times = fail_end_offsets_times
        self._fail_getmany_times = fail_getmany_times
        self._assignment_raises = assignment_raises
        self.position_calls = 0
        self.end_offsets_calls = 0
        self.getmany_calls = 0

    def assignment(self) -> frozenset[TopicPartition]:
        if self._assignment_raises:
            raise RuntimeError("assignment blew up")
        return frozenset({self._tp})

    async def end_offsets(
        self, partitions: list[TopicPartition]
    ) -> dict[TopicPartition, int]:
        self.end_offsets_calls += 1
        if self.end_offsets_calls <= self._fail_end_offsets_times:
            raise TimeoutError("end_offsets timed out")
        return {self._tp: 0}

    async def position(self, tp: TopicPartition) -> int:
        self.position_calls += 1
        if self.position_calls <= self._fail_position_times:
            raise self._fail_position_exc(f"Partition {tp} is not assigned")
        return 0

    async def getmany(
        self, *, timeout_ms: int = 0, max_records: int | None = None
    ) -> dict[TopicPartition, list[object]]:
        self.getmany_calls += 1
        if self.getmany_calls <= self._fail_getmany_times:
            raise ConnectionError("fetch failed")
        await asyncio.sleep(0)
        return {}


async def _drive(
    cache: SnapshotCache,
    *,
    until: object,
    timeout: float = 5.0,
) -> None:
    task = asyncio.ensure_future(cache._consume_loop())
    try:
        async with asyncio.timeout(timeout):
            while not until():  # type: ignore[operator]
                await asyncio.sleep(0)
    finally:
        cache._running = False
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


@pytest.fixture(autouse=True)
def _fast_poll(monkeypatch: pytest.MonkeyPatch) -> None:
    import omnimarket.projection.snapshot_cache as module

    monkeypatch.setattr(module, "_BOOTSTRAP_POLL_INTERVAL_SECONDS", 0.0)
    monkeypatch.setattr(module, "_BOOTSTRAP_POLL_MAX_ATTEMPTS", 1)


async def test_an_exception_escaping_the_loop_is_recorded_not_silent() -> None:
    """The whole defect in one assertion: before this change the loop ended
    and NOTHING anywhere said so."""
    cache = _make_cache()
    cache._consumer = _FakeConsumer(assignment_raises=True)  # type: ignore[assignment]
    cache._running = True

    await cache._consume_loop()

    assert cache.consume_failure is not None
    assert "RuntimeError" in cache.consume_failure
    assert not cache.is_bootstrapped(_TOPIC), (
        "a cache whose consumer died must never report a topic bootstrapped"
    )


async def test_a_revoked_partition_does_not_end_consumption() -> None:
    """``IllegalStateError`` from ``position()`` is a rebalance race, not a
    terminal fault: skip the partition and re-read the assignment next pass."""
    cache = _make_cache()
    fake = _FakeConsumer(fail_position_times=2)
    cache._consumer = fake  # type: ignore[assignment]
    cache._running = True

    await _drive(cache, until=lambda: cache.is_bootstrapped(_TOPIC))

    assert cache.is_bootstrapped(_TOPIC)
    assert cache.consume_failure is None
    assert fake.position_calls > 2


async def test_a_transient_end_offsets_failure_is_retried() -> None:
    cache = _make_cache()
    fake = _FakeConsumer(fail_end_offsets_times=2)
    cache._consumer = fake  # type: ignore[assignment]
    cache._running = True

    await _drive(cache, until=lambda: cache.is_bootstrapped(_TOPIC))

    assert cache.is_bootstrapped(_TOPIC)
    assert cache.consume_failure is None


async def test_a_transient_getmany_failure_is_retried() -> None:
    cache = _make_cache()
    fake = _FakeConsumer(fail_getmany_times=2, fail_end_offsets_times=99)
    cache._consumer = fake  # type: ignore[assignment]
    cache._running = True

    # end_offsets fails throughout, so the topic never bootstraps -- the point
    # is that the LOOP survives a getmany() error and keeps polling, while
    # readiness stays refused (the fail-closed direction).
    task = asyncio.ensure_future(cache._consume_loop())
    try:
        async with asyncio.timeout(5.0):
            while fake.getmany_calls < 5:
                await asyncio.sleep(0)
    finally:
        cache._running = False
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    assert fake.getmany_calls >= 5
    assert not cache.is_bootstrapped(_TOPIC)
    assert cache.consume_failure is None


async def test_an_unassigned_topic_reports_zero_assigned_partitions() -> None:
    """The discriminator ``/ready`` now publishes.

    A topic the broker never offers a partition for -- which on a cluster with
    auto-create off means it does not exist -- reports ``0`` here, while
    ``bootstrapped`` reports ``False`` exactly as a mid-replay topic would.
    """
    cache = _make_cache()
    assert cache.assigned_partition_count(_TOPIC) == 0
    assert cache.assigned_partition_count("onex.snapshot.projection.unknown.v1") == 0

    fake = _FakeConsumer()
    cache._consumer = fake  # type: ignore[assignment]
    cache._running = True
    await _drive(cache, until=lambda: cache.is_bootstrapped(_TOPIC))
    assert cache.assigned_partition_count(_TOPIC) == 1


async def test_a_finished_task_is_reported_while_the_cache_still_runs() -> None:
    """Even a loop that returns without raising -- which the supervisor cannot
    see as an exception -- must not read as healthy while ``_running`` is set.
    """
    cache = _make_cache()
    cache._running = True

    async def _returns_immediately() -> None:
        return None

    cache._consume_task = asyncio.ensure_future(_returns_immediately())
    await cache._consume_task
    assert cache.consume_failure == (
        "consume loop exited while the cache was still running"
    )


async def test_a_hanging_broker_call_is_bounded_and_retried(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The one failure mode a try/except cannot catch.

    aiokafka's own ``end_offsets`` docstring says it "may block indefinitely
    if the partition does not exist", and ``position()`` loops until the
    partition has a valid position. A permanently-awaited coroutine renders
    EXACTLY like a slow replay -- same 503, same per-topic map, no log line,
    no restart, because the readinessProbe only marks NotReady. Bounding the
    call makes the hang a recorded transient; the topic stays un-bootstrapped
    while it hangs, which is the fail-closed direction.
    """
    import omnimarket.projection.snapshot_cache as module

    monkeypatch.setattr(module, "_BOOTSTRAP_RPC_TIMEOUT_SECONDS", 0.05)

    class _HangingConsumer(_FakeConsumer):
        def __init__(self) -> None:
            super().__init__()
            self.hangs = 0

        async def end_offsets(
            self, partitions: list[TopicPartition]
        ) -> dict[TopicPartition, int]:
            self.end_offsets_calls += 1
            if self.end_offsets_calls <= 2:
                self.hangs += 1
                await asyncio.sleep(3600)
            return {self._tp: 0}

    cache = _make_cache()
    fake = _HangingConsumer()
    cache._consumer = fake  # type: ignore[assignment]
    cache._running = True

    await _drive(cache, until=lambda: cache.is_bootstrapped(_TOPIC), timeout=10.0)

    assert fake.hangs == 2, "the hanging call was not actually exercised"
    assert cache.is_bootstrapped(_TOPIC)
    assert cache.consume_failure is None
