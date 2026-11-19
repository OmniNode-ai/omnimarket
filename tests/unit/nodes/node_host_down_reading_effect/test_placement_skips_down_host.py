# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Lab placement skips a down host: the host-down reading reaches the placement judge (OMN-22905)."""

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


def test_a_down_host_is_skipped_and_the_other_is_placed() -> None:
    handler = HandlerLabPoolPlace(table_source=_table, down_hosts=lambda: ())
    placement = handler.handle(
        ModelLabPoolPlaceRequest(
            advertisements=(_ad("example-host-a"), _ad("example-host-b")),
            evaluated_at=NOW,
            down_hosts=("example-host-a",),
        )
    )
    assert placement.decision is EnumPlacementDecision.PLACED
    assert placement.host_name == "example-host-b"
    assert any("down" in v.reason for v in placement.verdicts)


def test_a_lane_pinned_to_a_down_host_is_replaced_on_the_other() -> None:
    handler = HandlerLabPoolPlace(table_source=_table, down_hosts=lambda: ())
    placement = handler.handle(
        ModelLabPoolPlaceRequest(
            advertisements=(_ad("example-host-a"), _ad("example-host-b")),
            evaluated_at=NOW,
            only_host="example-host-a",
            down_hosts=("example-host-a",),
        )
    )
    assert placement.decision is EnumPlacementDecision.PLACED
    assert placement.host_name == "example-host-b"
    assert placement.replaced_from == "example-host-a"


def test_a_lane_pinned_to_a_down_host_with_no_other_is_refused() -> None:
    handler = HandlerLabPoolPlace(table_source=_table, down_hosts=lambda: ())
    placement = handler.handle(
        ModelLabPoolPlaceRequest(
            advertisements=(_ad("example-host-a"),),
            evaluated_at=NOW,
            only_host="example-host-a",
            down_hosts=("example-host-a",),
        )
    )
    assert placement.decision is EnumPlacementDecision.REFUSED
    assert placement.replaced_from == "example-host-a"
    assert any("down" in v.reason for v in placement.verdicts)


def test_a_down_host_with_no_advertisement_is_listed_in_the_verdicts() -> None:
    handler = HandlerLabPoolPlace(table_source=_table, down_hosts=lambda: ())
    placement = handler.handle(
        ModelLabPoolPlaceRequest(
            advertisements=(_ad("example-host-b"),),
            evaluated_at=NOW,
            down_hosts=("example-host-a",),
        )
    )
    assert placement.decision is EnumPlacementDecision.PLACED
    assert placement.host_name == "example-host-b"
    down_verdicts = [v for v in placement.verdicts if v.host_name == "example-host-a"]
    assert len(down_verdicts) == 1
    assert down_verdicts[0].eligible is False
    assert "down" in down_verdicts[0].reason


def test_with_an_empty_down_set_the_placement_equals_the_one_before() -> None:
    handler = HandlerLabPoolPlace(table_source=_table, down_hosts=lambda: ())
    placement = handler.handle(
        ModelLabPoolPlaceRequest(
            advertisements=(_ad("example-host-a"), _ad("example-host-b")),
            evaluated_at=NOW,
            only_host="example-host-a",
        )
    )
    assert placement.decision is EnumPlacementDecision.PLACED
    assert placement.host_name == "example-host-a"
    assert placement.replaced_from == ""


def test_an_unreadable_down_source_does_not_refuse_the_placement() -> None:
    def unreadable() -> tuple[str, ...]:
        raise OSError("down store unreadable")

    handler = HandlerLabPoolPlace(table_source=_table, down_hosts=unreadable)
    placement = handler.handle(
        ModelLabPoolPlaceRequest(
            advertisements=(_ad("example-host-a"), _ad("example-host-b")),
            evaluated_at=NOW,
            only_host="example-host-a",
        )
    )
    assert placement.decision is EnumPlacementDecision.PLACED
    assert placement.host_name == "example-host-a"
    assert "down store unreadable" in placement.detail


def test_a_source_that_reads_nothing_down_places_the_pinned_lane_on_the_down_host() -> (
    None
):
    """FAILING CONTROL: without the down reading, the pinned lane is placed on the down host."""
    handler = HandlerLabPoolPlace(table_source=_table, down_hosts=lambda: ())
    placement = handler.handle(
        ModelLabPoolPlaceRequest(
            advertisements=(_ad("example-host-a"), _ad("example-host-b")),
            evaluated_at=NOW,
            only_host="example-host-a",
            down_hosts=("example-host-a",),
        )
    )
    # With the down reading active, the pinned lane is re-placed on the other host.
    assert placement.decision is EnumPlacementDecision.PLACED
    assert placement.host_name == "example-host-b"
    assert placement.replaced_from == "example-host-a"
