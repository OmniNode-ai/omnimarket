# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden and error chain of the host announcer on the in-memory bus (OMN-19998).

Golden: start, heartbeat, change, stop in that order on the announced topic, each valid for a registry reader. Error chain:
an unreadable host publishes a typed failure and no announcement and the next readable beat is still ``start``; a failed
publish does not spend ``start``. Failing controls: an announcer that always says ``start`` fails the order test, and one
that swallows a failure leaves the failure topic empty.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from omnibase_infra.errors import RuntimeHostError

from omnimarket.handlers.overlay_store.bus_opener import open_lane_bus
from omnimarket.models.model_host_introspection_announcement import (
    EnumHostAnnouncementReason as Reason,
)
from omnimarket.models.model_host_introspection_announcement import (
    ModelHostIntrospectionAnnouncement,
    ModelHostIntrospectionFailure,
)
from omnimarket.nodes.node_host_introspection_announce_effect.handlers import (
    HandlerHostIntrospectionGather,
    HostAnnouncer,
    load_announce_topics,
)
from omnimarket.nodes.node_host_introspection_announce_effect.protocols import (
    HostFactsUnreadableError,
)
from omnimarket.nodes.node_lab_work_unit_effect.handlers.handler_host_capacity_advertise_effect import (
    HandlerHostCapacityAdvertiseEffect,
)
from tests.host_introspection_support import (
    HOST,
    NOW,
    FakeCapacityReader,
    FakeFactsReader,
    facts,
    install_overlay,
    table_for,
)

pytestmark = pytest.mark.unit


def _announcer(
    bus: Any,
    reader: FakeFactsReader,
    overlay: Any,
    *,
    table_available: bool = True,
) -> HostAnnouncer:
    gather = HandlerHostIntrospectionGather(
        overlay=overlay,
        reader=reader,
        table=lambda: table_for(available=table_available),
        capacity=HandlerHostCapacityAdvertiseEffect(
            FakeCapacityReader(tools=("git", "claude-login")), now=lambda: NOW
        ),
        now=lambda: NOW,
    )
    return HostAnnouncer(bus, gather, HOST, overlay.cadence_seconds, now=lambda: NOW)


async def _collect(bus: Any, topic: str) -> list[dict[str, Any]]:
    seen: list[dict[str, Any]] = []

    async def on_message(message: Any) -> None:
        seen.append(json.loads(message.value)["payload"])

    await bus.subscribe(topic, on_message=on_message, group_id=f"probe-{topic}")
    return seen


async def test_golden_start_heartbeat_change_stop(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    overlay = install_overlay(monkeypatch, tmp_path)
    topics = load_announce_topics()
    reader = FakeFactsReader(facts(), facts(), facts(python_version="3.13.0"))
    async with open_lane_bus(
        bus="inmemory", lane=None, kafka_bootstrap=None, root=None
    ) as bus:
        seen = await _collect(bus, topics.announced_topic)
        announcer = _announcer(bus, reader, overlay)
        await announcer.announce_once()
        await announcer.announce_once()
        await announcer.announce_once()
        await announcer.announce_once(Reason.STOP)
        await asyncio.sleep(0)
    parsed = [ModelHostIntrospectionAnnouncement.model_validate(p) for p in seen]
    assert [a.reason for a in parsed] == [
        Reason.START,
        Reason.HEARTBEAT,
        Reason.CHANGE,
        Reason.STOP,
    ]
    assert {a.host_name for a in parsed} == {HOST}
    assert parsed[0].facts_digest == parsed[1].facts_digest != parsed[2].facts_digest
    assert [x.name for x in parsed[0].runtimes] == ["python", "uv"]
    assert parsed[0].availability.available is True
    assert parsed[0].roles == ("lab",)


async def test_golden_the_run_loop_announces_start_then_stop(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    overlay = install_overlay(monkeypatch, tmp_path)
    topics = load_announce_topics()
    async with open_lane_bus(
        bus="inmemory", lane=None, kafka_bootstrap=None, root=None
    ) as bus:
        seen = await _collect(bus, topics.announced_topic)
        announcer = _announcer(bus, FakeFactsReader(facts()), overlay)
        stop = asyncio.Event()
        task = asyncio.create_task(announcer.run(stop))
        for _ in range(50):
            if seen:
                break
            await asyncio.sleep(0.05)
        stop.set()
        await asyncio.wait_for(task, timeout=10)
    reasons = [p["reason"] for p in seen]
    assert reasons[0] == "start"
    assert reasons[-1] == "stop"


async def test_error_an_unreadable_host_publishes_a_failure_and_no_announcement(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    overlay = install_overlay(monkeypatch, tmp_path)
    topics = load_announce_topics()
    reader = FakeFactsReader(HostFactsUnreadableError("nvidia-smi exited 9"), facts())
    async with open_lane_bus(
        bus="inmemory", lane=None, kafka_bootstrap=None, root=None
    ) as bus:
        announced = await _collect(bus, topics.announced_topic)
        failed = await _collect(bus, topics.failed_topic)
        announcer = _announcer(bus, reader, overlay)
        first = await announcer.announce_once()
        second = await announcer.announce_once()
        await asyncio.sleep(0)
    assert isinstance(first, ModelHostIntrospectionFailure)
    failure = ModelHostIntrospectionFailure.model_validate(failed[0])
    assert (failure.host_name, failure.error_type) == (HOST, "HostFactsUnreadableError")
    assert "nvidia-smi exited 9" in failure.error
    # the unreadable beat did not spend `start`: the first announcement to reach the bus is still `start`
    assert isinstance(second, ModelHostIntrospectionAnnouncement)
    assert [p["reason"] for p in announced] == ["start"]
    assert announcer.failures == 1


class _FlakyBus:
    """A bus whose first publish fails, then works."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.refused = 0

    async def publish(
        self, topic: str, key: bytes | None, value: bytes, headers: Any = None
    ) -> object:
        if self.refused == 0:
            self.refused += 1
            raise RuntimeHostError("broker unavailable")
        return await self._inner.publish(topic, key, value, headers)

    async def subscribe(self, *args: Any, **kwargs: Any) -> Any:
        return await self._inner.subscribe(*args, **kwargs)


async def test_error_a_failed_publish_is_retried_and_start_is_not_spent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    overlay = install_overlay(monkeypatch, tmp_path)
    topics = load_announce_topics()
    async with open_lane_bus(
        bus="inmemory", lane=None, kafka_bootstrap=None, root=None
    ) as inner:
        seen = await _collect(inner, topics.announced_topic)
        announcer = _announcer(_FlakyBus(inner), FakeFactsReader(facts()), overlay)
        assert await announcer.announce_once() is None
        assert announcer.started is False
        assert isinstance(
            await announcer.announce_once(), ModelHostIntrospectionAnnouncement
        )
        await asyncio.sleep(0)
    assert [p["reason"] for p in seen] == ["start"]
    assert announcer.publish_failures == 1


async def test_error_a_host_the_table_marks_unavailable_announces_why(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    overlay = install_overlay(monkeypatch, tmp_path)
    async with open_lane_bus(
        bus="inmemory", lane=None, kafka_bootstrap=None, root=None
    ) as bus:
        announcer = _announcer(
            bus, FakeFactsReader(facts()), overlay, table_available=False
        )
        published = await announcer.announce_once()
    assert isinstance(published, ModelHostIntrospectionAnnouncement)
    assert (published.availability.available, published.availability.reason) == (
        False,
        "in repair",
    )


async def test_control_an_announcer_that_always_says_start_fails_the_order_check(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The injected break: the announcer forgets that it has started. The golden order check reads it."""
    overlay = install_overlay(monkeypatch, tmp_path)
    topics = load_announce_topics()
    async with open_lane_bus(
        bus="inmemory", lane=None, kafka_bootstrap=None, root=None
    ) as bus:
        seen = await _collect(bus, topics.announced_topic)
        announcer = _announcer(bus, FakeFactsReader(facts()), overlay)
        await announcer.announce_once()
        announcer.started = False  # the break
        await announcer.announce_once()
        await asyncio.sleep(0)
    assert [p["reason"] for p in seen] != ["start", "heartbeat"]
    assert [p["reason"] for p in seen] == ["start", "start"]
