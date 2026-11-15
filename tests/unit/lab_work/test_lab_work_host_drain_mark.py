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
from datetime import UTC, datetime, timedelta

import pytest
from omnibase_infra.event_bus.event_bus_inmemory import EventBusInmemory
from pydantic import ValidationError

from omnimarket.lab_work.bus import LabWorkHost
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
