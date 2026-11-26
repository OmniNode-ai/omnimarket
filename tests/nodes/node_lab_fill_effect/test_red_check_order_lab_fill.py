# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-26624 -- the lab fill places a row naming a red delegation check before every other row."""

from __future__ import annotations

from pathlib import Path

import pytest

from omnimarket.nodes.node_lab_fill_effect.models import (
    ModelLabFillTickHost,
    ModelLabFillTickResult,
)
from omnimarket.nodes.node_lab_placement_compute.models import ModelRedCheckReading

from .test_golden_chain_lab_fill_tick import _request, _row, _tick

pytestmark = pytest.mark.unit

ONE_SLOT = (ModelLabFillTickHost(host="h201", free=1),)


def _run(
    tmp_path: Path, rows: list[dict[str, object]], reading: ModelRedCheckReading | None
) -> ModelLabFillTickResult:
    handler, _, _ = _tick(rows)
    result = handler.handle(
        _request(tmp_path, apply=False, hosts=ONE_SLOT, red_check_reading=reading)
    )
    assert isinstance(result, ModelLabFillTickResult)
    return result


def _rows() -> list[dict[str, object]]:
    red = _row(200, repo="omniclaude")
    red["goal"] = "repair C13 customer-local"
    return [_row(100, kind="wiring", repo="omnimarket"), red]


def test_red_check_row_takes_the_one_free_slot_before_an_mvp_row(
    tmp_path: Path,
) -> None:
    result = _run(
        tmp_path, _rows(), ModelRedCheckReading(red=("C13",), read_from="probe-results")
    )
    assert [d.ticket for d in result.dispatched] == ["OMN-200"]
    assert result.dispatched[0].priority == "factory-unblock"
    assert result.red_checks_unplaced == ()


def test_same_rows_with_the_check_green_keep_today_order(tmp_path: Path) -> None:
    result = _run(
        tmp_path, _rows(), ModelRedCheckReading(red=(), read_from="probe-results")
    )
    assert [d.ticket for d in result.dispatched] == ["OMN-100"]
    assert result.red_check_report is None


def test_red_check_with_no_row_is_reported_not_dropped(tmp_path: Path) -> None:
    result = _run(
        tmp_path,
        [_row(100)],
        ModelRedCheckReading(red=("C29",), read_from="probe-results"),
    )
    assert result.red_checks_unplaced == ("C29",)


def test_unreadable_reading_is_reported_and_orders_as_today(tmp_path: Path) -> None:
    result = _run(
        tmp_path, _rows(), ModelRedCheckReading(unreadable="probe-results: refused")
    )
    assert [d.ticket for d in result.dispatched] == ["OMN-100"]
    assert result.red_check_report is not None
    assert result.red_check_report.startswith("RED-CHECK-UNREADABLE:")
    assert "probe-results: refused" in result.red_check_report


def test_no_reading_leaves_the_tick_as_it_was(tmp_path: Path) -> None:
    result = _run(tmp_path, _rows(), None)
    assert [d.ticket for d in result.dispatched] == ["OMN-100"]
    assert result.red_check_report is None
