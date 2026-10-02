# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-20327: the lane-health exposure declares only columns its table has.

On the .201 dev lane ``GET /projection/onex.snapshot.projection.lab.lane-health.v1``
answered 503 ``projection_column_missing``: the contract listed ``census_status``,
``health_status`` and ``receipt_status`` (the decayed verdicts), the table stores
only each fact's pre-decay verdict and observed-at, and the table-backed read
selects every declared column. The decayed verdict is deliberately not stored --
it ages, and a stored one needs a timer to stay true -- so the contract, not the
table, was wrong.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

import omnimarket.nodes.node_projection_lab_lane_health as lane_health_node
from omnimarket.nodes.node_projection_lab_lane_health.models.model_lab_lane_health_row import (
    ModelLabLaneHealthRow,
)

pytestmark = pytest.mark.unit

_NODE_DIR = Path(lane_health_node.__file__).parent
_DECAYED_VERDICTS = {"census_status", "health_status", "receipt_status"}


def _table_columns() -> set[str]:
    sql = (_NODE_DIR / "migrations" / "0000_create_lab_lane_health.sql").read_text()
    body = re.search(
        r"CREATE TABLE IF NOT EXISTS omninode_internal\.lab_lane_health \((.*?)\n\);",
        re.sub(r"--[^\n]*", "", sql),
        re.DOTALL,
    )
    assert body is not None, "the migration no longer creates lab_lane_health"
    return {
        match.group(1)
        for line in body.group(1).split("\n")
        if (match := re.match(r"\s*([a-z_]+)\s+[A-Z]", line))
    }


def _exposure_columns() -> list[str]:
    contract = yaml.safe_load((_NODE_DIR / "contract.yaml").read_text())
    columns: list[str] = contract["projection_api"]["columns"]
    return columns


def test_every_declared_column_is_a_column_of_the_table() -> None:
    table = _table_columns()
    assert {"lane", "census_original_status", "projected_at"} <= table
    assert [c for c in _exposure_columns() if c not in table] == []


def test_the_decayed_verdicts_are_derived_not_stored_or_declared() -> None:
    assert not _DECAYED_VERDICTS & _table_columns()
    assert not _DECAYED_VERDICTS & set(_exposure_columns())


def test_the_published_row_still_carries_the_decayed_verdicts() -> None:
    """The snapshot delta is unchanged: only the table-backed read lost them."""
    from datetime import UTC, datetime

    from omnimarket.nodes.node_projection_lab_lane_health.models.enum_lab_lane import (
        EnumLabLane,
    )

    row = ModelLabLaneHealthRow(lane=EnumLabLane(next(iter(EnumLabLane)).value))
    published = row.to_exposure_row(now=datetime(2026, 10, 1, tzinfo=UTC))
    assert set(published) >= _DECAYED_VERDICTS
