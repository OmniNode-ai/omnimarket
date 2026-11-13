# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Lab placement refuses a draining host: the drain verb's state reaches the placement judge (OMN-22905)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from omnimarket.models.model_host_capacity_advertisement import (
    ModelHostCapacityAdvertisement,
)
from omnimarket.models.overlay_keys import ModelLabHostsTable
from omnimarket.nodes.node_lab_placement_compute.handlers.handler_lab_pool_place import (
    HandlerLabPoolPlace,
)
from omnimarket.nodes.node_lab_placement_compute.models import (
    EnumPlacementDecision,
    ModelLabPoolPlaceRequest,
)

NOW = datetime(2026, 11, 7, 15, 0, tzinfo=UTC)


def _ad(name: str) -> ModelHostCapacityAdvertisement:
    return ModelHostCapacityAdvertisement(
        host_name=name,
        cores=8,
        load1=0.1,
        mem_available_bytes=32 * 1024**3,
        tools=[],
        running_units=0,
        max_units=4,
        advertised_at=NOW - timedelta(seconds=5),
        cadence_seconds=60,
    )


def _table() -> ModelLabHostsTable:
    return ModelLabHostsTable.model_validate(
        {
            "hosts": [
                {"name": "example-host-a", "target": "a.example", "available": True},
                {"name": "example-host-b", "target": "b.example", "available": True},
            ]
        }
    )


def test_placement_refuses_a_draining_host_and_places_on_the_other() -> None:
    handler = HandlerLabPoolPlace(table_source=_table)
    ads = (_ad("example-host-a"), _ad("example-host-b"))
    plain = handler.handle(
        ModelLabPoolPlaceRequest(
            advertisements=ads, evaluated_at=NOW, only_host="example-host-a"
        )
    )
    assert plain.decision is EnumPlacementDecision.PLACED
    drained = handler.handle(
        ModelLabPoolPlaceRequest(
            advertisements=ads,
            evaluated_at=NOW,
            only_host="example-host-a",
            draining_hosts=("example-host-a",),
        )
    )
    assert drained.decision is EnumPlacementDecision.REFUSED
    assert any("draining" in v.reason for v in drained.verdicts)
    other = handler.handle(
        ModelLabPoolPlaceRequest(
            advertisements=ads, evaluated_at=NOW, draining_hosts=("example-host-a",)
        )
    )
    assert other.decision is EnumPlacementDecision.PLACED
    assert other.host_name == "example-host-b"
