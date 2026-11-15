# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-24489 -- the drain mark rides the bus to the host and out again on its capacity advertisement.

The host takes a drain command from the bus (the drain node's command topic), holds the newest one for its own name, and
advertises the mark, and the lanes it runs, as pseudo-tools of every advertisement. Every launcher and the lab fill read the
mark from that advertisement; none reads a file of its own.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from omnibase_infra.event_bus.event_bus_inmemory import EventBusInmemory
from pydantic import ValidationError

from omnimarket.lab_work.bus import LabWorkHost, host_group_id
from omnimarket.models.model_host_drain_command import (
    ModelHostDrainCommand,
    load_host_drain_topic,
)
from omnimarket.nodes.node_lab_work_unit_effect import (
    HandlerHostCapacityAdvertiseEffect,
    HandlerLabWorkUnitEffect,
    ModelHostCapacityAdvertisement,
)
from omnimarket.nodes.node_lab_work_unit_effect.protocols import (
    HostReading,
    draining_until_from_tools,
    running_lanes_from_tools,
)

pytestmark = pytest.mark.unit

NOW = datetime(2026, 11, 15, 4, 0, tzinfo=UTC)
HOST = "h051"


class _Reader:
    def read(self, tools: list[str]) -> HostReading:
        return HostReading(
            cores=8, load1=0.5, mem_available_bytes=16 * 2**30, tools=tuple(tools)
        )


class _Runner:
    def run(self, request: object, host_name: str) -> object:
        raise AssertionError("no unit runs in this test")


def _command(
    draining: bool = True,
    *,
    host: str = HOST,
    issued: datetime = NOW,
    until: datetime | None = None,
) -> ModelHostDrainCommand:
    if draining and until is None:
        until = NOW + timedelta(hours=1)
    return ModelHostDrainCommand(
        host_name=host,
        draining=draining,
        until=until if draining else None,
        reason="power supply move",
        issued_at=issued,
    )


async def _publish(
    bus: EventBusInmemory, command: ModelHostDrainCommand | bytes
) -> None:
    value = (
        command
        if isinstance(command, bytes)
        else json.dumps({"payload": command.model_dump(mode="json")}).encode()
    )
    await bus.publish(load_host_drain_topic(), HOST.encode(), value)


async def _advertise(
    commands: list[ModelHostDrainCommand | bytes],
    *,
    now: datetime = NOW,
    lanes: list[str] | None | str = "absent",
) -> ModelHostCapacityAdvertisement:
    bus = EventBusInmemory(environment="local", group="drain-mark-test")
    await bus.start()
    kwargs: dict[str, object] = {}
    if lanes != "absent":
        kwargs["lane_reader"] = lambda: lanes
    host = LabWorkHost(
        bus,
        HandlerLabWorkUnitEffect(HOST, _Runner()),  # type: ignore[arg-type]
        HandlerHostCapacityAdvertiseEffect(_Reader()),
        now=lambda: now,
        **kwargs,  # type: ignore[arg-type]
    )
    await host.start(advertise=False)
    try:
        for command in commands:
            await _publish(bus, command)
        await asyncio.sleep(0.05)
        ad = await host.advertise_once()
    finally:
        await host.stop()
        await bus.close()
    assert ad is not None
    return ad


class _RecordingBus:
    """The in-memory bus with the broker's group semantics, recording subscribes.

    The bare in-memory bus delivers only to subscribers live at publish, so it
    cannot show the replay a restarted serve relies on. This wrapper forwards
    publish/subscribe, records every (topic, group_id) it is asked to join, and
    models the broker: a group it has not seen replays the topic's retained
    history from the earliest offset (a fresh group with
    ``auto_offset_reset=earliest``), while a group that has already subscribed
    resumes where its committed offset left it.
    """

    def __init__(self, bus: EventBusInmemory) -> None:
        self._bus = bus
        self.subscriptions: list[tuple[str, str]] = []

    async def publish(
        self, topic: str, key: bytes | None, value: bytes, headers: Any = None
    ) -> object:
        return await self._bus.publish(topic, key, value, headers)

    async def subscribe(
        self,
        topic: str,
        node_identity: Any = None,
        on_message: Callable[[Any], Awaitable[None]] | None = None,
        *,
        group_id: str | None = None,
    ) -> Callable[[], Awaitable[None]]:
        if on_message is None or group_id is None:
            raise AssertionError("a grouped subscribe needs on_message and group_id")
        fresh = (topic, group_id) not in self.subscriptions
        self.subscriptions.append((topic, group_id))
        handle = await self._bus.subscribe(
            topic, on_message=on_message, group_id=group_id
        )
        if fresh:
            history = await self._bus.get_event_history(limit=1000, topic=topic)
            for message in history:
                await on_message(message)
        return handle


def test_golden_a_drain_command_rides_the_next_advertisement() -> None:
    ad = asyncio.run(_advertise([_command()]))
    assert "draining-until:2026-11-15T05:00:00Z" in ad.tools
    assert draining_until_from_tools(ad.tools) == NOW + timedelta(hours=1)


def test_no_command_no_mark() -> None:
    ad = asyncio.run(_advertise([]))
    assert draining_until_from_tools(ad.tools) is None


def test_failing_control_a_command_for_another_host_leaves_no_mark() -> None:
    """The golden case fails on a host that ignores the host name: this one must read no mark."""
    ad = asyncio.run(_advertise([_command(host="h053")]))
    assert draining_until_from_tools(ad.tools) is None


def test_a_later_undrain_clears_the_mark_and_an_older_command_does_not_replace_a_newer() -> (
    None
):
    undrain = _command(False, issued=NOW + timedelta(minutes=5))
    ad = asyncio.run(_advertise([_command(), undrain]))
    assert draining_until_from_tools(ad.tools) is None
    stale_drain = _command(True, issued=NOW - timedelta(minutes=5))
    ad = asyncio.run(_advertise([undrain, stale_drain]))
    assert draining_until_from_tools(ad.tools) is None


def test_a_mark_past_its_expiry_reads_as_not_draining() -> None:
    ad = asyncio.run(_advertise([_command()], now=NOW + timedelta(hours=1)))
    assert draining_until_from_tools(ad.tools) is None


def test_error_chain_malformed_commands_are_ignored_and_the_host_keeps_advertising() -> (
    None
):
    bad_json = b"not json"
    no_until = json.dumps(
        {
            "payload": {
                "host_name": HOST,
                "draining": True,
                "issued_at": NOW.isoformat(),
            }
        }
    ).encode()
    ad = asyncio.run(_advertise([bad_json, no_until]))
    assert draining_until_from_tools(ad.tools) is None
    assert ad.host_name == HOST


def test_the_lanes_the_host_runs_are_advertised_when_read() -> None:
    ad = asyncio.run(_advertise([], lanes=["lane-a"]))
    assert running_lanes_from_tools(ad.tools) == ("lane-a",)


def test_a_failed_lane_read_is_unread_not_zero() -> None:
    ad = asyncio.run(_advertise([], lanes=None))
    assert running_lanes_from_tools(ad.tools) is None


def test_no_lane_reader_is_unread_not_zero() -> None:
    ad = asyncio.run(_advertise([]))
    assert running_lanes_from_tools(ad.tools) is None


def test_an_empty_lane_list_is_a_measured_zero() -> None:
    ad = asyncio.run(_advertise([], lanes=[]))
    assert running_lanes_from_tools(ad.tools) == ()


def test_command_validator_refuses_a_naive_stamp_a_drain_without_until_and_an_undrain_with_one() -> (
    None
):
    with pytest.raises(ValidationError):
        ModelHostDrainCommand(
            host_name=HOST,
            draining=True,
            until=NOW,
            issued_at=datetime(2026, 11, 15, 4, 0),
        )
    with pytest.raises(ValidationError):
        ModelHostDrainCommand(host_name=HOST, draining=True, issued_at=NOW)
    with pytest.raises(ValidationError):
        ModelHostDrainCommand(host_name=HOST, draining=False, until=NOW, issued_at=NOW)


def test_a_mark_reads_not_draining_at_its_expiry_instant() -> None:
    command = _command()
    assert command.until is not None
    assert command.reads_draining(command.until - timedelta(seconds=1)) is True
    assert command.reads_draining(command.until) is False


def test_a_restarted_serve_rebuilds_the_mark_from_the_topic() -> None:
    """The mark survives a serve restart: the next start joins a fresh drain
    group, replays the topic, and advertises the mark again without any new
    command -- where the one shared group committed past the only command and
    the restarted serve advertised none."""

    async def scenario() -> tuple[ModelHostCapacityAdvertisement, _RecordingBus]:
        inner = EventBusInmemory(environment="local", group="drain-mark-test")
        await inner.start()
        bus = _RecordingBus(inner)

        def serve() -> LabWorkHost:
            return LabWorkHost(
                bus,
                HandlerLabWorkUnitEffect(HOST, _Runner()),  # type: ignore[arg-type]
                HandlerHostCapacityAdvertiseEffect(_Reader()),
                now=lambda: NOW,
            )

        first = serve()
        await first.start(advertise=False)
        try:
            await _publish(inner, _command())
            await asyncio.sleep(0.05)
        finally:
            await first.stop()
        second = serve()
        ad: ModelHostCapacityAdvertisement | None = None
        try:
            await second.start(advertise=False)
            await asyncio.sleep(0.05)
            ad = await second.advertise_once()
        finally:
            await second.stop()
            await inner.close()
        assert ad is not None
        return ad, bus

    ad, bus = asyncio.run(scenario())
    assert draining_until_from_tools(ad.tools) == NOW + timedelta(hours=1)
    drain_groups = [
        group for topic, group in bus.subscriptions if topic == load_host_drain_topic()
    ]
    assert len(drain_groups) == 2
    assert drain_groups[0] != drain_groups[1]
    for group in drain_groups:
        assert group.startswith(host_group_id(HOST))
