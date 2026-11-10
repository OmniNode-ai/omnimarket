# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Planted defects (OMN-23331): a clean day reads WORKING, and a day with one planted defect of each kind reads NOT WORKING naming it."""

from __future__ import annotations

import pytest

from omnimarket.models.model_delegation_events_defect_scan import (
    ModelDefectScanBounds,
)
from omnimarket.models.model_reading import EnumReadingVerdict as V
from tests.nodes.node_delegation_events_defect_scan_compute.defect_scan_support import (
    NOW,
    clean_day,
    fact,
    overlay,
    overlay_data,
    planted,
    request,
    resolve_handler,
)

pytestmark = pytest.mark.unit

DEFECTS = ("missing_terminal", "cost_unread_as_zero", "unattributed")


def test_delegation_events_defect_scan_clean_day_reads_working() -> None:
    scan = resolve_handler()().handle(request())
    assert scan.verdict is V.WORKING
    assert (scan.missing_terminal, scan.cost_unread_as_zero, scan.unattributed) == (
        0,
        0,
        0,
    )
    assert scan.rows_scanned == 120
    assert scan.reasons == ()


@pytest.mark.parametrize("defect", DEFECTS)
def test_delegation_events_defect_scan_planted_defect_reads_not_working(
    defect: str,
) -> None:
    scan = resolve_handler()().handle(request(events=planted(defect, 3)))
    assert scan.verdict is V.NOT_WORKING
    assert getattr(scan, defect) == 3
    others = [d for d in DEFECTS if d != defect]
    assert all(getattr(scan, d) == 0 for d in others)
    (reason,) = scan.reasons
    assert reason == f"{defect.replace('_', '-')}-above-bound:3>0"
    assert f"{defect}=3" in scan.ledger_row
    assert "verdict=NOT_WORKING" in scan.ledger_row


def test_delegation_events_defect_scan_same_rows_read_green_when_only_the_bound_moves() -> (
    None
):
    rows = planted("missing_terminal", 3)
    handler = resolve_handler()()
    assert handler.handle(request(events=rows)).verdict is V.NOT_WORKING
    bounds = {**overlay_data()["bounds"], "max_missing_terminal": 3}
    loose = overlay(bounds=ModelDefectScanBounds.model_validate(bounds))
    assert handler.handle(request(events=rows, overlay=loose)).verdict is V.WORKING


def test_delegation_events_defect_scan_a_row_inside_the_terminal_grace_is_not_missing() -> (
    None
):
    in_flight = fact(10, ok=None)
    scan = resolve_handler()().handle(request(events=(*clean_day(), in_flight)))
    assert scan.missing_terminal == 0
    assert scan.verdict is V.WORKING


def test_delegation_events_defect_scan_a_zero_cost_with_a_measurement_source_is_not_a_defect() -> (
    None
):
    local = fact(300, cost="0", source_of_cost="local-model-free")
    free_without_tokens = fact(300, cost="0", source_of_cost=None, tokens=0)
    scan = resolve_handler()().handle(
        request(events=(*clean_day(), local, free_without_tokens))
    )
    assert scan.cost_unread_as_zero == 0
    assert scan.verdict is V.WORKING


def test_delegation_events_defect_scan_a_null_cost_is_unpriced_and_not_a_zero() -> None:
    unpriced = fact(300, cost=None, source_of_cost=None)
    scan = resolve_handler()().handle(request(events=(*clean_day(), unpriced)))
    assert scan.cost_unread_as_zero == 0


def test_delegation_events_defect_scan_blank_lane_counts_as_unattributed() -> None:
    scan = resolve_handler()().handle(
        request(events=(*clean_day(), fact(300, lane="  ")))
    )
    assert scan.unattributed == 1


def test_delegation_events_defect_scan_rows_outside_the_day_and_shadow_rows_are_not_judged() -> (
    None
):
    old = fact(60 * 24 + 5, ok=None, lane=None)
    shadow = fact(300, ok=None, lane=None, shadow=True)
    other = fact(300, ok=None, lane=None, source="synthetic")
    future = fact(-30, ok=None, lane=None)
    scan = resolve_handler()().handle(
        request(events=(*clean_day(), old, shadow, other, future))
    )
    assert scan.verdict is V.WORKING
    assert scan.rows_scanned == 120
    assert scan.rows_future_dated == 1


def test_delegation_events_defect_scan_ledger_row_is_one_status_row_with_the_read_time() -> (
    None
):
    scan = resolve_handler()().handle(request(events=planted("unattributed", 2)))
    assert scan.ledger_row.startswith(
        f"{NOW.strftime('%Y-%m-%dT%H:%M:%SZ')} | STATUS | lane=delegation-defect-scan | src=delegation-defect-scan | drill=no |"
    )
    assert "\n" not in scan.ledger_row
