# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AdvertisedLaneSource: the lane count a host advertises (OMN-24489).

The source reads the host's own advertisements: the count is the maximum of running_units plus the lanes read
across the window, and the names are the lanes the host advertised. A host that never read its lanes, that
advertised only once, or that is silent has no measured count and raises ValueError. The held reason is the
newest advertisement's capacity_refusal. A lane that appears only in the second advertisement still counts.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from omnimarket.models.model_host_capacity_advertisement import (
    ModelHostCapacityAdvertisement,
)
from omnimarket.nodes.node_host_drain_effect.adapters.bus_drain import (
    AdvertisedLaneSource,
)
from omnimarket.nodes.node_host_drain_effect.adapters.fakes import LoopbackBus
from omnimarket.nodes.node_lab_work_unit_effect.protocols.local_host_reader import (
    tools_for_drain_and_lanes,
)

NOW = datetime(2026, 11, 7, 15, 0, tzinfo=UTC)
HOST = "example-host-b"


def _bus(
    *,
    lanes: dict[str, int | None] | None = None,
    units: dict[str, int] | None = None,
    silent: bool = False,
    per_host: int = 2,
    held: dict[str, str] | None = None,
) -> LoopbackBus:
    return LoopbackBus(
        [HOST],
        clock=lambda: NOW,
        lanes=lanes if lanes is not None else {HOST: 0},
        units=units if units is not None else {HOST: 0},
        silent=frozenset([HOST]) if silent else frozenset(),
        held=held if held is not None else {},
        advertisements_per_host=per_host,
    )


def test_the_count_is_the_max_of_running_units_plus_lanes() -> None:
    bus = _bus(lanes={HOST: 2}, units={HOST: 3})
    source = AdvertisedLaneSource(bus)
    assert source.running(HOST) == 5


def test_the_names_are_the_lanes_the_host_advertised() -> None:
    bus = _bus(lanes={HOST: 2}, units={HOST: 0})
    source = AdvertisedLaneSource(bus)
    assert source.names(HOST) == ("lane-1", "lane-2")


def test_a_host_that_never_read_its_lanes_raises() -> None:
    bus = _bus(lanes={HOST: None}, units={HOST: 0})
    source = AdvertisedLaneSource(bus)
    with pytest.raises(ValueError, match=r"advertis|lanes|nothing"):
        source.running(HOST)


def test_a_host_that_advertised_only_once_raises() -> None:
    bus = _bus(lanes={HOST: 0}, units={HOST: 0}, per_host=1)
    source = AdvertisedLaneSource(bus)
    with pytest.raises(ValueError, match=r"advertis|lanes|nothing"):
        source.running(HOST)


def test_a_silent_host_raises() -> None:
    bus = _bus(silent=True)
    source = AdvertisedLaneSource(bus)
    with pytest.raises(ValueError, match=r"advertis|lanes|nothing"):
        source.running(HOST)


def test_the_held_reason_is_the_newest_advertisement_refusal() -> None:
    bus = _bus(lanes={HOST: 0}, units={HOST: 0}, held={HOST: "capacity full"})
    source = AdvertisedLaneSource(bus)
    assert source.held_reason(HOST) == "capacity full"


def test_a_lane_that_appears_only_in_the_second_advertisement_still_counts() -> None:
    """The failing control of 'twice': a lane that started between two beats is seen by the second one."""
    first = _ad(NOW - timedelta(seconds=10), [])
    second = _ad(NOW, ["lane-new"])

    class _Window:
        def advertisements(self) -> tuple[ModelHostCapacityAdvertisement, ...]:
            return (first, second)

        def invalidate(self) -> None:
            return None

    assert AdvertisedLaneSource(_Window()).running(HOST) == 1


def _ad(at: datetime, lanes: list[str]) -> ModelHostCapacityAdvertisement:
    return ModelHostCapacityAdvertisement(
        host_name=HOST,
        cores=8,
        load1=0.5,
        mem_available_bytes=1 << 30,
        tools=tools_for_drain_and_lanes(None, lanes),
        advertised_at=at,
        cadence_seconds=10,
    )
