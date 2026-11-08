# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The h202 host rows across the copies of the lab host table (OMN-22900).

The old rows are the failing control: h202 read ``available: false`` in the packaged and config copies and
read available with ``unit_slots: 3`` in the overlays copy. The drift read must name every one of those
differences. The corrected rows, h202 available with the same cap in every copy, must read WORKING.
"""

from __future__ import annotations

from typing import Any

import pytest

from omnimarket.nodes.node_host_table_copies_drift_compute.models import (
    ModelHostTableDriftCommand,
)
from tests.nodes.node_host_table_copies_drift_compute.drift_support import (
    COPIES,
    World,
)

pytestmark = pytest.mark.unit

COMMAND = ModelHostTableDriftCommand(correlation_id="c-h202", write=False)
H202_CAP = 5


def h202_row(**fields: Any) -> dict[str, Any]:
    return {"name": "h202", "target": "h202.example.test", "cap": H202_CAP, **fields}


def world_with_h202(rows: dict[str, dict[str, Any]]) -> World:
    world = World()
    for copy_name in COPIES:
        world.hosts[copy_name] = [
            row for row in world.hosts[copy_name] if row["name"] != "h202"
        ] + [rows[copy_name]]
    return world


def test_the_old_h202_rows_read_not_working_and_name_each_differing_field() -> None:
    world = world_with_h202(
        {
            "packaged": h202_row(available=False),
            "config": h202_row(available=False),
            "overlays": h202_row(unit_slots=3),
        }
    )
    result = world.handler(None).handle(COMMAND)
    assert result.verdict.value == "NOT_WORKING"
    assert {(d.host, d.field) for d in result.differences} == {
        ("h202", "available"),
        ("h202", "unit_slots"),
    }
    (available,) = [d for d in result.differences if d.field == "available"]
    assert {v.copy_name: v.value for v in available.values} == {
        "packaged": "false",
        "config": "false",
        "overlays": "<absent>",
    }
    assert result.differing_hosts == 1


def test_the_old_overlays_row_that_read_h202_absent_true_is_named() -> None:
    world = world_with_h202(
        {
            "packaged": h202_row(available=False),
            "config": h202_row(available=False),
            "overlays": h202_row(available=True),
        }
    )
    result = world.handler(None).handle(COMMAND)
    assert result.verdict.value == "NOT_WORKING"
    (available,) = [d for d in result.differences if d.field == "available"]
    assert {v.copy_name: v.value for v in available.values} == {
        "packaged": "false",
        "config": "false",
        "overlays": "true",
    }


def test_h202_missing_from_one_copy_is_a_difference_of_its_own() -> None:
    world = world_with_h202(
        {name: h202_row(available=True) for name in COPIES},
    )
    world.hosts["config"] = [r for r in world.hosts["config"] if r["name"] != "h202"]
    result = world.handler(None).handle(COMMAND)
    assert result.verdict.value == "NOT_WORKING"
    assert [d.host for d in result.differences] == ["h202"]


def test_the_corrected_h202_rows_read_working_with_the_cap_in_every_copy() -> None:
    world = world_with_h202({name: h202_row(available=True) for name in COPIES})
    result = world.handler(None).handle(COMMAND)
    assert result.verdict.value == "WORKING"
    assert result.differences == ()
    for copy_name in COPIES:
        (row,) = [r for r in world.hosts[copy_name] if r["name"] == "h202"]
        assert row["available"] is True
        assert row["cap"] == H202_CAP
