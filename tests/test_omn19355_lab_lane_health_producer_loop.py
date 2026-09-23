# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19355 — the lane-health writer must not reuse a producer across loops.

The runtime dispatches ``LabLaneHealthProjectionWriter.handle`` once per
consumed message through ``asyncio.to_thread``, so every message runs inside
its own ``asyncio.run`` loop, which is closed when the message returns. A lab-lane
health event republishes its row through ``publish_snapshot_delta``, which
builds an ``AIOKafkaProducer`` on first use and caches it on the runner.

The writer closed its asyncpg pool at the end of each message but never stopped
that producer. The next lab-lane event therefore called ``send_and_wait`` on a
producer whose sender task had died with the previous loop. The batch future is
resolved only by that sender, so the call never returned. Measured on the .201
dev lane on 2026-09-23: exactly one snapshot delta per runtime lifetime
(19:47:58Z, 20:36:03Z), then the consumer hung on the next ``compose-dev``
health tick (offsets 51800 and 51808). It never polled or rejoined again after
aiokafka evicted it.

The zero-row health ticks from runtimes on no lab lane are interleaved here
because they were interleaved on the lane. They write nothing and publish
nothing, so they are not the trigger, but a test without them would not match
the live sequence.
"""

from __future__ import annotations

import asyncio
import json
import threading
from typing import Any

import pytest

from omnimarket.nodes.node_projection_lab_lane_health.handlers.handler_lab_lane_health_runner import (
    LabLaneHealthProjectionWriter,
)
from omnimarket.projection import runner as runner_module
from omnimarket.projection.runner import BaseProjectionRunner

#: How long a send on a dead-loop producer is allowed to wait before the fake
#: reports the hang. The real client waits forever; a bounded stand-in keeps a
#: regression from wedging the test session the way it wedged the consumer.
_HANG_BOUND_SECONDS = 2.0


class _LoopBoundProducer:
    """Reduced ``AIOKafkaProducer``: its sender lives on the loop that started it.

    ``send_and_wait`` from any other loop awaits a future only that dead sender
    could resolve, which is what the real client does.
    """

    instances: list[_LoopBoundProducer] = []

    def __init__(self, **_kwargs: Any) -> None:
        self.loop: asyncio.AbstractEventLoop | None = None
        self.stopped = False
        self.sent: list[bytes | None] = []
        _LoopBoundProducer.instances.append(self)

    async def start(self) -> None:
        self.loop = asyncio.get_running_loop()

    async def stop(self) -> None:
        self.stopped = True

    async def send_and_wait(
        self,
        topic: str,
        value: bytes | None = None,
        key: bytes | None = None,
        headers: list[tuple[str, bytes]] | None = None,
    ) -> None:
        if self.stopped or asyncio.get_running_loop() is not self.loop:
            never = asyncio.get_running_loop().create_future()
            try:
                await asyncio.wait_for(never, timeout=_HANG_BOUND_SECONDS)
            except TimeoutError:
                raise AssertionError(
                    "send_and_wait on a producer started on another, closed "
                    "event loop never returns (OMN-19355)"
                ) from None
        self.sent.append(value)


class _RowStore:
    """Reduced ``AsyncpgAdapter``: stores the health column group per lane."""

    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}
        self.connects = 0

    async def connect(self) -> None:
        self.connects += 1

    async def close(self) -> None:
        return None

    async def execute(self, query: str, *params: Any) -> None:
        assert "health_observed_at" in query, "only health facts are driven here"
        lane, _now, observed_at, _status, aggregate, dimensions = params
        self.rows[lane] = {
            "lane": lane,
            "health_observed_at": observed_at.isoformat(),
            "health_aggregate": aggregate,
            "health_dimensions": dimensions,
        }

    async def fetchval(self, _query: str, lane: str) -> str | None:
        row = self.rows.get(lane)
        return None if row is None else json.dumps(row)


def _health(offset: int, *, lane: str | None) -> dict[str, Any]:
    event: dict[str, Any] = {
        "correlation_id": f"00000000-0000-4000-8000-{offset:012d}",
        "timestamp": f"2026-09-23T20:{offset % 60:02d}:00+00:00",
        "status": "HEALTHY",
        "dimensions": [{"name": "empty_consumer_groups", "status": "HEALTHY"}],
        "_topic": "onex.evt.omnibase-infra.runtime-health-check.v1",
        "_partition": 0,
        "_offset": offset,
    }
    if lane is not None:
        event["lane"] = lane
    return event


@pytest.fixture
def writer(monkeypatch: pytest.MonkeyPatch) -> LabLaneHealthProjectionWriter:
    _LoopBoundProducer.instances.clear()
    monkeypatch.setattr(runner_module, "AIOKafkaProducer", _LoopBoundProducer)
    monkeypatch.setattr(
        BaseProjectionRunner,
        "kafka_bootstrap_servers",
        property(lambda _self: "fixture-broker:9092"),
    )
    instance = LabLaneHealthProjectionWriter()
    instance._db = _RowStore()  # type: ignore[assignment]
    assert instance._snapshot_exposure is not None, (
        "the exposure is bus_backed; without it no delta is published and this "
        "test is inert"
    )
    return instance


async def _dispatch(
    writer: LabLaneHealthProjectionWriter, event: dict[str, Any]
) -> dict[str, Any]:
    """Call ``handle`` the way the runtime's projection wiring does."""
    return await asyncio.to_thread(writer.handle, dict(event))


@pytest.mark.unit
def test_a_second_lab_lane_write_after_zero_row_ticks_still_publishes(
    writer: LabLaneHealthProjectionWriter,
) -> None:
    """The live sequence: write, zero-row ticks, then the next write."""

    async def consume() -> list[dict[str, Any]]:
        results = []
        for event in (
            _health(51804, lane="compose-dev"),
            _health(51805, lane=None),
            _health(51806, lane=None),
            _health(51807, lane=None),
            _health(51808, lane="compose-dev"),
        ):
            results.append(await _dispatch(writer, event))
        return results

    results = asyncio.run(consume())

    assert [r["rows_upserted"] for r in results] == [1, 0, 0, 0, 1]
    published = sum(len(p.sent) for p in _LoopBoundProducer.instances)
    assert published == 2, "both lab-lane writes must reach the snapshot topic"


@pytest.mark.unit
def test_every_producer_a_message_opens_is_stopped_before_it_returns(
    writer: LabLaneHealthProjectionWriter,
) -> None:
    """No producer outlives the loop it was started on."""
    for offset in (1, 2, 3):
        result = writer.handle(_health(offset, lane="compose-dev"))
        assert result["rows_upserted"] == 1
        assert writer._producer is None, (
            "a producer cached past the message's loop is the OMN-19355 hang"
        )

    assert len(_LoopBoundProducer.instances) == 3
    assert all(p.stopped for p in _LoopBoundProducer.instances)


class _SharedPoolStore(_RowStore):
    """Reduced ``AsyncpgAdapter`` sharing one ``_pool`` slot across callers.

    ``connect`` assigns the slot and ``close`` clears it, as the real adapter
    does, and a pool answers only on the loop that opened it. The first caller
    holds its connect open briefly so a concurrent dispatch, if one can get
    in, lands inside the same bracket.
    """

    def __init__(self) -> None:
        super().__init__()
        self._pool: asyncio.AbstractEventLoop | None = None
        self._second_arrived = threading.Event()

    async def connect(self) -> None:
        self.connects += 1
        self._pool = asyncio.get_running_loop()
        if self.connects == 1:
            self._second_arrived.wait(timeout=0.5)
        else:
            self._second_arrived.set()

    async def close(self) -> None:
        self._pool = None

    async def execute(self, query: str, *params: Any) -> None:
        if self._pool is not asyncio.get_running_loop():
            raise RuntimeError("pool belongs to another dispatch's event loop")
        await super().execute(query, *params)


@pytest.mark.unit
def test_concurrent_dispatches_on_one_writer_do_not_share_a_bracket(
    writer: LabLaneHealthProjectionWriter,
) -> None:
    """One instance serves all three subscriptions, so dispatches can overlap.

    The runtime wires one writer per contract and each subscribed topic has its
    own consume loop, so a receipt or census message can be dispatched while a
    health message is still inside the bracket. The pool and the producer
    live on the instance, so an overlapping dispatch would use, or close, the
    other thread's loop-bound resources.
    """
    writer._db = _SharedPoolStore()  # type: ignore[assignment]

    async def consume() -> list[dict[str, Any]]:
        return list(
            await asyncio.gather(
                _dispatch(writer, _health(1, lane="compose-dev")),
                _dispatch(writer, _health(2, lane="compose-dev")),
            )
        )

    results = asyncio.run(consume())

    assert [r["rows_upserted"] for r in results] == [1, 1]
    assert sum(len(p.sent) for p in _LoopBoundProducer.instances) == 2
    assert all(p.stopped for p in _LoopBoundProducer.instances)
