# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-24489 -- the drain mark and the running lanes ride the capacity advertisement as pseudo-tools, and the lane reader parses the host's systemd scopes."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from omnimarket.nodes.node_lab_work_unit_effect import (
    HandlerHostCapacityAdvertiseEffect,
    ModelHostCapacityAdvertisement,
    ModelHostCapacityProbeRequest,
)
from omnimarket.nodes.node_lab_work_unit_effect.protocols import (
    HostReading,
    LocalLaneReader,
    draining_until_from_tools,
    running_lanes_from_tools,
)

pytestmark = pytest.mark.unit


class _Reader:
    """A host reader that returns a fixed reading, the way the bus tests fake one."""

    def read(self, tools: list[str]) -> HostReading:
        return HostReading(
            cores=32,
            load1=2.0,
            mem_available_bytes=32 * 1024**3,
            tools=("git", "uv"),
        )


def _ad(**overrides: object) -> ModelHostCapacityAdvertisement:
    fields: dict[str, object] = {"host_name": "h101"}
    fields.update(overrides)
    return HandlerHostCapacityAdvertiseEffect(_Reader()).handle(
        ModelHostCapacityProbeRequest.model_validate(fields)
    )


def test_golden_drain_and_lanes_ride_the_advertisement_as_tools() -> None:
    stamp = datetime(2026, 11, 15, 7, 0, 0, tzinfo=UTC)
    ad = _ad(draining_until=stamp, running_lanes=["lane-a", "lane-b"])
    tools = set(ad.tools)
    assert "draining-until:2026-11-15T07:00:00Z" in tools
    assert "lane-running:lane-a" in tools
    assert "lane-running:lane-b" in tools
    assert "lanes-read:ok" in tools
    assert draining_until_from_tools(ad.tools) == stamp
    assert running_lanes_from_tools(ad.tools) == ("lane-a", "lane-b")


def test_an_empty_lane_list_is_a_read_of_zero_not_an_unread() -> None:
    ad = _ad(running_lanes=[])
    tools = set(ad.tools)
    assert "lanes-read:ok" in tools
    assert not any(tool.startswith("lane-running:") for tool in tools)
    assert running_lanes_from_tools(ad.tools) == ()


def test_an_unread_lane_count_is_none_not_zero() -> None:
    ad = _ad(running_lanes=None)
    tools = set(ad.tools)
    assert "lanes-read:ok" not in tools
    assert running_lanes_from_tools(ad.tools) is None


def test_no_drain_mark_leaves_no_drain_tool_and_a_malformed_one_reads_none() -> None:
    ad = _ad()
    assert not any(tool.startswith("draining-until:") for tool in ad.tools)
    assert draining_until_from_tools(ad.tools) is None
    assert draining_until_from_tools(["draining-until:garbage"]) is None


def test_the_advertisement_gains_no_field_so_older_readers_still_accept_it() -> None:
    stamp = datetime(2026, 11, 15, 7, 0, 0, tzinfo=UTC)
    with_drain = _ad(draining_until=stamp, running_lanes=["lane-a", "lane-b"])
    without = _ad()
    assert set(with_drain.model_dump().keys()) == set(without.model_dump().keys())
    ModelHostCapacityAdvertisement.model_validate(with_drain.model_dump())


def test_the_lane_reader_reads_the_running_lanes_from_the_host_scopes() -> None:
    class _Ok:
        returncode = 0
        stdout = "onexlane-lane-x-abc123.scope loaded active running desc\nother.scope loaded active running d\n"

    class _Failed:
        returncode = 1
        stdout = ""

    def _raise(*args: object, **kwargs: object) -> None:
        raise OSError("no systemctl")

    def _reader(run: object) -> LocalLaneReader:
        return LocalLaneReader(
            ["systemctl", "--user", "list-units"],
            r"^onexlane-(?P<lane>.+)-[0-9a-f]{6}\.scope$",
            10,
            run=run,  # type: ignore[arg-type]
        )

    assert _reader(lambda *_a, **_k: _Ok()).read() == ["lane-x"]
    assert _reader(lambda *_a, **_k: _Failed()).read() is None
    assert _reader(_raise).read() is None
    assert _reader(lambda *_a, **_k: _Empty()).read() == []


class _Empty:
    returncode = 0
    stdout = ""


def test_the_handler_does_not_add_the_lane_read_tool_when_the_lanes_were_not_read() -> (
    None
):
    stamp = datetime(2026, 11, 15, 7, 0, 0, tzinfo=UTC)
    unread = _ad(draining_until=stamp, running_lanes=None)
    read = _ad(draining_until=stamp, running_lanes=[])
    assert "lanes-read:ok" in set(read.tools)
    assert "lanes-read:ok" not in set(unread.tools)
    assert set(unread.tools) != set(read.tools)
