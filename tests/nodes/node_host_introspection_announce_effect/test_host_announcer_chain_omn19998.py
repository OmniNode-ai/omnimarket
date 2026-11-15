# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-24367 AC3 -- an unreadable host publishes a typed failure and no announcement; a failed publish keeps the start.

The announcer chain: start, heartbeat on the contract's cadence, facts_changed when a fact moves, stop on exit.
The clock is injected so the tests advance it instead of sleeping.
"""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_host_introspection_announce_effect.handlers import (
    HandlerHostFactsReadEffect,
    HostAnnouncer,
    load_host_announce_topics,
)
from omnimarket.nodes.node_host_introspection_announce_effect.models import (
    ModelHostFactsProbeRequest,
    ModelHostIntrospectionFailure,
)
from omnimarket.nodes.node_host_introspection_compute.models import (
    EnumAnnounceReason,
    ModelHostAnnouncement,
)
from tests.nodes.node_host_introspection_announce_effect.support import (
    AT,
    FakeFactsReader,
    RecordingBus,
    capacity,
)

pytestmark = pytest.mark.unit

TOPICS = load_host_announce_topics()


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _announcer(
    bus: RecordingBus, reader: FakeFactsReader, clock: _Clock
) -> HostAnnouncer:
    return HostAnnouncer(
        bus,
        ModelHostFactsProbeRequest(host_name="h201", roles=["lane"]),
        facts_handler=HandlerHostFactsReadEffect(reader),
        now=lambda: AT,
        clock=clock,
    )


def _reasons(bus: RecordingBus) -> list[object]:
    return [p["reason"] for p in bus.on(TOPICS.announced)]


@pytest.mark.asyncio
async def test_start_heartbeat_facts_changed_stop_in_order() -> None:
    bus, reader, clock = RecordingBus(), FakeFactsReader(), _Clock()
    await bus.start()
    announcer = _announcer(bus, reader, clock)
    first = await announcer.announce(capacity())
    assert isinstance(first, ModelHostAnnouncement)
    assert first.reason is EnumAnnounceReason.START
    assert (
        await announcer.announce(capacity()) is None
    )  # inside the heartbeat window: nothing due
    clock.now += TOPICS.heartbeat_seconds
    await announcer.announce(capacity())
    reader.docker_version = "27.4.0"
    await announcer.announce(
        capacity()
    )  # a runtime moved: announced at once, not at the next heartbeat
    await announcer.stop(capacity())
    assert _reasons(bus) == ["start", "heartbeat", "facts_changed", "stop"]
    assert bus.on(TOPICS.failed) == []


@pytest.mark.asyncio
async def test_unreadable_facts_publish_a_typed_failure_and_no_announcement() -> None:
    bus, reader, clock = RecordingBus(), FakeFactsReader(), _Clock()
    await bus.start()
    reader.unreadable = True
    result = await _announcer(bus, reader, clock).announce(capacity())
    assert isinstance(result, ModelHostIntrospectionFailure)
    assert bus.on(TOPICS.announced) == []
    (failure,) = bus.on(TOPICS.failed)
    assert failure["host_name"] == "h201"
    assert "driver not loaded" in str(failure["error_message"])


@pytest.mark.asyncio
async def test_failed_publish_does_not_spend_the_start_announcement() -> None:
    bus, reader, clock = RecordingBus(), FakeFactsReader(), _Clock()
    await bus.start()
    announcer = _announcer(bus, reader, clock)
    bus.down = {TOPICS.announced}
    assert await announcer.announce(capacity()) is None
    assert announcer.started is False
    bus.down = set()
    retried = await announcer.announce(capacity())
    assert isinstance(retried, ModelHostAnnouncement)
    assert (
        retried.reason is EnumAnnounceReason.START
    )  # the announcer still has not started
    assert _reasons(bus) == ["start"]


@pytest.mark.asyncio
async def test_unreadable_then_readable_still_starts() -> None:
    bus, reader, clock = RecordingBus(), FakeFactsReader(), _Clock()
    await bus.start()
    announcer = _announcer(bus, reader, clock)
    reader.unreadable = True
    await announcer.announce(capacity())
    reader.unreadable = False
    result = await announcer.announce(capacity())
    assert isinstance(result, ModelHostAnnouncement)
    assert result.reason is EnumAnnounceReason.START


@pytest.mark.asyncio
async def test_a_host_that_never_started_publishes_no_stop() -> None:
    bus, reader, clock = RecordingBus(), FakeFactsReader(), _Clock()
    await bus.start()
    assert await _announcer(bus, reader, clock).stop(capacity()) is None
    assert bus.records == []
