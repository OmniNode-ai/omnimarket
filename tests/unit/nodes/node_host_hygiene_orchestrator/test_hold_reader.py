# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The hold readers of the host hygiene orchestrator (OMN-24610).

``InMemoryHostHoldReader`` returns a copy of its held hosts, and
``AdvertisedHostHoldReader`` decides a host's hold from the newest capacity
advertisement it can see, refusing to hold on a stale one.
"""

from __future__ import annotations

import datetime as dt

import pytest

from omnimarket.models.model_host_capacity_advertisement import (
    ModelHostCapacityAdvertisement,
)
from omnimarket.nodes.node_host_hygiene_orchestrator.handlers.hold_reader import (
    AdvertisedHostHoldReader,
    InMemoryHostHoldReader,
)
from omnimarket.nodes.node_lane_host_pool_snapshot_effect.handlers.pool_snapshot_ports import (
    InMemoryHostAdvertisementSource,
)

pytestmark = pytest.mark.unit

NOW = dt.datetime(2026, 11, 13, 12, 0, tzinfo=dt.UTC)
MAX_AGE = 300


def _ad(
    host: str,
    capacity_refusal: str,
    advertised_at: dt.datetime,
) -> ModelHostCapacityAdvertisement:
    return ModelHostCapacityAdvertisement(
        host_name=host,
        cores=4,
        load1=1.0,
        mem_available_bytes=1024,
        advertised_at=advertised_at,
        cadence_seconds=60,
        capacity_refusal=capacity_refusal,
    )


def _held(reader: InMemoryHostHoldReader | AdvertisedHostHoldReader) -> dict[str, str]:
    return reader.held_hosts(NOW, MAX_AGE)


def test_in_memory_reader_returns_what_it_was_given() -> None:
    reader = InMemoryHostHoldReader({"h1": "heat", "h2": "cold"})
    assert _held(reader) == {"h1": "heat", "h2": "cold"}


def test_in_memory_reader_returns_a_copy_not_a_reference() -> None:
    reader = InMemoryHostHoldReader({"h1": "heat"})
    first = _held(reader)
    first["h1"] = "mutated"
    assert _held(reader) == {"h1": "heat"}


def test_advertised_reader_holds_a_host_whose_newest_advertisement_refuses() -> None:
    source = InMemoryHostAdvertisementSource(
        [_ad("h1", "heat", NOW - dt.timedelta(seconds=10))]
    )
    reader = AdvertisedHostHoldReader(source)
    assert _held(reader) == {"h1": "heat"}


def test_advertised_reader_does_not_hold_a_host_with_an_empty_refusal() -> None:
    source = InMemoryHostAdvertisementSource(
        [_ad("h1", "", NOW - dt.timedelta(seconds=10))]
    )
    reader = AdvertisedHostHoldReader(source)
    assert _held(reader) == {}


def test_advertised_reader_holds_nobody_on_a_stale_advertisement() -> None:
    source = InMemoryHostAdvertisementSource(
        [_ad("h1", "heat", NOW - dt.timedelta(seconds=10_000))]
    )
    reader = AdvertisedHostHoldReader(source)
    assert _held(reader) == {}


def test_advertised_reader_the_newer_advertisement_decides() -> None:
    # Old refuses, new does not: the newer one wins, so the host is not held.
    source = InMemoryHostAdvertisementSource(
        [
            _ad("h1", "heat", NOW - dt.timedelta(seconds=100)),
            _ad("h1", "", NOW - dt.timedelta(seconds=10)),
        ]
    )
    reader = AdvertisedHostHoldReader(source)
    assert _held(reader) == {}


def test_advertised_reader_the_newer_advertisement_decides_reverse() -> None:
    # Old does not refuse, new does: the newer one wins, so the host is held.
    source = InMemoryHostAdvertisementSource(
        [
            _ad("h1", "", NOW - dt.timedelta(seconds=100)),
            _ad("h1", "heat", NOW - dt.timedelta(seconds=10)),
        ]
    )
    reader = AdvertisedHostHoldReader(source)
    assert _held(reader) == {"h1": "heat"}


def test_advertised_reader_with_no_advertisements_holds_nobody() -> None:
    source = InMemoryHostAdvertisementSource([])
    reader = AdvertisedHostHoldReader(source)
    assert _held(reader) == {}
