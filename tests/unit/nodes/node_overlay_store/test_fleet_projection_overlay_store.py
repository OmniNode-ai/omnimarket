# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The overlay store tick publishes this host's view of the fleet as a floor projection (OMN-23391)."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import pytest

from omnimarket.models.floor_projection import ModelFloorProjectionEnvelope
from omnimarket.models.overlay_store import ModelOverlayStoreReadOverlay
from omnimarket.nodes.node_overlay_store_read_effect import (
    runtime_overlay_store_tick as tick,
)
from tests.unit.nodes.node_overlay_store.support import FakeBus

pytestmark = pytest.mark.unit

NOW = datetime(2026, 11, 9, 22, 0, tzinfo=UTC)
FLEET = {
    "readings": {"h1:lab_hosts": {"state": "fresh", "read_at": "2026-11-09T21:59:50Z"}}
}


def _overlay(**over: Any) -> ModelOverlayStoreReadOverlay:
    base: dict[str, Any] = {
        "backend": "bus_snapshot",
        "bus_lane": "test-bus",
        "snapshot_root": "/tmp/unused",
        "read_window_s": 0.1,
        "host_name_env": "HOST",
        "overlays": {"lab_hosts": {"stale_after_s": 3600, "on_stale": "read"}},
        "fleet_projection_id": "overlay_store_fleet",
    }
    return ModelOverlayStoreReadOverlay.model_validate({**base, **over})


def _use(monkeypatch: pytest.MonkeyPatch, bus: FakeBus) -> None:
    @contextlib.asynccontextmanager
    async def opened(**_: Any) -> AsyncIterator[FakeBus]:
        yield bus

    monkeypatch.setattr(tick, "open_lane_bus", opened)


def test_golden_chain_the_tick_publishes_its_view_stamped_with_the_time_it_stood_by_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bus = FakeBus()
    _use(monkeypatch, bus)
    asyncio.run(tick._publish_fleet_projection(_overlay(), {"HOST": "h1"}, FLEET, NOW))
    ((topic, key, value),) = bus.published
    assert topic == "onex.snapshot.projection.floor-overlay-store-fleet.v1"
    assert key == b"overlay_store_fleet"
    envelope = ModelFloorProjectionEnvelope.model_validate_json(value)
    assert envelope.as_of == NOW
    assert envelope.producer == "node_overlay_store_read_effect:h1"
    assert envelope.payload == FLEET


def test_error_chain_a_broker_that_refuses_raises_to_the_tick_which_reports_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Refused:
        async def __aenter__(self) -> FakeBus:
            raise ConnectionError("broker refused")

        async def __aexit__(self, *_: Any) -> None:
            return None

    def refused(**_: Any) -> Refused:
        return Refused()

    monkeypatch.setattr(tick, "open_lane_bus", refused)
    with pytest.raises(ConnectionError):
        asyncio.run(
            tick._publish_fleet_projection(_overlay(), {"HOST": "h1"}, FLEET, NOW)
        )


def test_named_break_nothing_is_published_without_an_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bus = FakeBus()
    _use(monkeypatch, bus)
    overlay = _overlay(fleet_projection_id=None)
    asyncio.run(tick._publish_fleet_projection(overlay, {"HOST": "h1"}, FLEET, NOW))
    assert bus.published == []


@pytest.mark.parametrize("backend", ["in_memory", "postgres"])
def test_error_a_backend_with_no_bus_refuses_the_projection_id_at_load(
    backend: str,
) -> None:
    with pytest.raises(ValueError, match="no bus to publish"):
        _overlay(backend=backend, bus_lane=None, database_url_env="DB")
