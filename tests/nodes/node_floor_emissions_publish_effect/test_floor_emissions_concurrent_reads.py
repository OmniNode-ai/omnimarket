# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The floor emissions publish handler reads its emission topics at once (OMN-19998): a pass costs one read window, not one per topic."""

from __future__ import annotations

import asyncio
import contextlib
import os
import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest
import yaml

from omnimarket.handlers.container.container_inmemory import bind_infra_event_bus
from omnimarket.handlers.emission_sink import ChainEmitter, InMemoryEmissionSink
from omnimarket.handlers.floor_projection import (
    BusSnapshotSource,
    emissions_from_payload,
)
from omnimarket.handlers.overlay_store.bus_opener import open_lane_bus, workspace_root
from omnimarket.models.floor_17a import ModelNodeEmission
from omnimarket.models.floor_17a.model_floor_17a import ChainOrigin
from omnimarket.models.floor_projection import (
    ModelFloorEmissionsOverlay,
)
from omnimarket.nodes.node_floor_emissions_publish_effect.handlers import (
    HandlerFloorEmissionsPublish,
    InMemoryEnvelopePublisher,
)

pytestmark = pytest.mark.unit

NOW = datetime(2026, 11, 11, 20, 0, tzinfo=UTC)
NODE_DIR = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_floor_emissions_publish_effect"
)
OVERLAY = ModelFloorEmissionsOverlay.model_validate(
    yaml.safe_load((NODE_DIR / "overlay.example.yaml").read_text())
)
STARTED, SUCCEEDED, FAILED = OVERLAY.emission_topics


def chain(
    origin: str, cid: str, *, at: datetime = NOW, fail: bool = False
) -> list[ModelNodeEmission]:
    sink = InMemoryEmissionSink()
    emitter = ChainEmitter(
        sink,
        node="node_x:x",
        piece=3,
        clock=lambda: at,
        correlation_id=cid,
        origin=cast(ChainOrigin, origin),
    )
    emitter.start()
    if fail:
        emitter.failure("x_failed", "planted")
    else:
        emitter.success({"n": 1})
    return sink.events


def topic_of(event: ModelNodeEmission) -> str:
    return {"start": STARTED, "success": SUCCEEDED, "failure": FAILED}[event.phase]


def handler(
    source: Any, publisher: InMemoryEnvelopePublisher
) -> HandlerFloorEmissionsPublish:
    return HandlerFloorEmissionsPublish(
        source=source,
        publisher=publisher,
        overlay=OVERLAY,
        producer="test:host",
        clock=lambda: NOW,
    )


def test_failing_control_all_topics_are_read_at_once() -> None:
    """A barrier that only passes when every topic is read at once: sequential reads time out, concurrent reads finish."""
    n = len(OVERLAY.emission_topics)

    class BarrierSource:
        def __init__(self) -> None:
            self._barrier: asyncio.Barrier | None = None

        async def publications(self, topic: str) -> list[bytes]:
            if self._barrier is None:
                self._barrier = asyncio.Barrier(n)
            await self._barrier.wait()
            return []

    publisher = InMemoryEnvelopePublisher()
    result = asyncio.run(
        asyncio.wait_for(handler(BarrierSource(), publisher).handle(), timeout=2)
    )
    assert result.published
    assert publisher.published


def test_golden_chain_publications_keep_overlay_topic_order() -> None:
    """The last topic answers first, but the published events keep overlay topic order."""
    events = chain("real", "c1")
    by_topic: dict[str, list[bytes]] = {t: [] for t in OVERLAY.emission_topics}
    for e in events:
        by_topic[topic_of(e)].append(e.model_dump_json().encode())

    class SlowLastSource:
        async def publications(self, topic: str) -> list[bytes]:
            if topic == FAILED:
                await asyncio.sleep(0.05)
            return list(by_topic[topic])

    publisher = InMemoryEnvelopePublisher()
    result = asyncio.run(handler(SlowLastSource(), publisher).handle())
    assert result.published
    assert result.events == 2
    (envelope,) = publisher.published
    read_back = emissions_from_payload(envelope.payload)
    assert [e.origin for e in read_back] == ["real", "real"]
    assert read_back == tuple(events)


def test_error_chain_the_first_failing_topic_in_overlay_order_is_named_and_nothing_is_published() -> (
    None
):
    """Every read waits at a barrier that only opens when all topics are read at once, then two of them fail."""
    barrier_size = len(OVERLAY.emission_topics)

    class TwoDown:
        def __init__(self) -> None:
            self._barrier: asyncio.Barrier | None = None

        async def publications(self, topic: str) -> list[bytes]:
            if self._barrier is None:
                self._barrier = asyncio.Barrier(barrier_size)
            await self._barrier.wait()
            if topic == SUCCEEDED:
                raise ConnectionError(topic)
            if topic == FAILED:
                raise TimeoutError(topic)
            return []

    publisher = InMemoryEnvelopePublisher()
    result = asyncio.run(
        asyncio.wait_for(handler(TwoDown(), publisher).handle(), timeout=2)
    )
    assert not result.published
    assert SUCCEEDED in result.reason
    assert "ConnectionError" in result.reason
    assert publisher.published == []


def test_error_chain_cancellation_is_not_swallowed() -> None:
    """The cancellation is raised only once every topic is being read; a sequential handler never gets there and times out instead."""
    barrier_size = len(OVERLAY.emission_topics)

    class CancellingSource:
        def __init__(self) -> None:
            self._barrier: asyncio.Barrier | None = None

        async def publications(self, topic: str) -> list[bytes]:
            if self._barrier is None:
                self._barrier = asyncio.Barrier(barrier_size)
            await self._barrier.wait()
            raise asyncio.CancelledError()

    publisher = InMemoryEnvelopePublisher()

    async def go() -> None:
        await asyncio.wait_for(
            handler(CancellingSource(), publisher).handle(), timeout=2
        )

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(go())
    assert publisher.published == []


@pytest.mark.parametrize(
    "backend",
    [
        pytest.param("inmemory", id="inmemory"),
        pytest.param(
            "redpanda",
            id="redpanda",
            marks=[pytest.mark.integration, pytest.mark.kafka],
        ),
    ],
)
async def test_seam_a_pass_through_the_bus_costs_one_read_not_one_per_topic(
    backend: str,
) -> None:
    """Seam: the handler and the bus source. A read of one topic is timed alone, then the three through the handler; the pass must cost under twice the single read (sequential reads cost three times). Read only: nothing is published on the emission topics."""
    if backend == "inmemory":
        bus = bind_infra_event_bus(environment="local", group="floor-emissions")
        await bus.start()

        @contextlib.asynccontextmanager
        async def open_bus() -> AsyncIterator[Any]:
            yield bus

        window_s = 0.3
    else:
        lane = os.environ.get("FLOOR_ORIGIN_SEAM_LANE")
        bootstrap = os.environ.get("FLOOR_ORIGIN_SEAM_BOOTSTRAP")
        if not (bootstrap or lane):
            pytest.skip("Kafka not reachable: no lab broker named for the seam")

        def open_bus() -> Any:
            return open_lane_bus(
                bus="kafka",
                lane=None if bootstrap else lane,
                kafka_bootstrap=bootstrap,
                root=workspace_root(os.environ),
            )

        window_s = 2.0
    source = BusSnapshotSource(
        open_bus=open_bus, group_prefix="floor-emissions", window_s=window_s
    )
    started = time.monotonic()
    await source.publications(STARTED)
    one_read_s = time.monotonic() - started
    publisher = InMemoryEnvelopePublisher()
    started = time.monotonic()
    result = await handler(source, publisher).handle()
    pass_s = time.monotonic() - started
    assert result.published
    assert pass_s < 2 * one_read_s, (
        f"a pass took {pass_s:.2f} s against {one_read_s:.2f} s for one topic"
    )
