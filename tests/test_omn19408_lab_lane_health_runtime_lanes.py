# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19408 — the lab lane-health projection attaches only on a lab lane.

The node's fold keys rows for the three lab lanes only (OMN-18769 AC6). Until
this change it was nonetheless ATTACHED on every main runtime, including the
.201 stability-test lane's, where every health event it consumed was correctly
dropped, it upserted zero rows, and the omnibase_infra projection_apply_divergence
detector held the runtime Docker-unhealthy for eleven hours. The detector was
right; the attachment was wrong.

The contract now declares ``runtime_lanes``, which the omnibase_infra auto-wiring
ownership filter honours: the node attaches only on a runtime whose declared
lane (``ONEX_RUNTIME_LANE``) is in that list, and fails closed with a discovery
error on a runtime that declares none.

The attachment scope and the fold scope must be the SAME set, so this test
reads both from their one source each -- the contract and ``EnumLabLane`` --
and fails if they drift apart. A lane in the fold but not the scope would never
get a row; a lane in the scope but not the fold is this incident again.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_projection_lab_lane_health.models.enum_lab_lane import (
    EnumLabLane,
    normalize_lane,
)

pytestmark = pytest.mark.unit

CONTRACT = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_projection_lab_lane_health"
    / "contract.yaml"
)


def _contract() -> dict[str, object]:
    loaded = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def test_the_contract_declares_a_runtime_lane_scope() -> None:
    lanes = _contract().get("runtime_lanes")

    assert isinstance(lanes, list), (
        "projection_lab_lane_health declares no runtime_lanes, so it attaches "
        "on every main runtime -- stability-test included, where it can never "
        "write a row and holds the runtime unhealthy (OMN-19408)"
    )
    assert lanes, "an empty scope would detach the node on every lane"


def test_the_attachment_scope_is_exactly_the_fold_scope() -> None:
    lanes = _contract()["runtime_lanes"]
    assert isinstance(lanes, list)

    assert sorted(lanes) == sorted(lane.value for lane in EnumLabLane)


@pytest.mark.parametrize("lane", ["stability-test", "judge", "lakshman", "dogfood"])
def test_no_lane_outside_the_lab_is_in_either_scope(lane: str) -> None:
    lanes = _contract()["runtime_lanes"]
    assert isinstance(lanes, list)

    assert lane not in lanes
    assert normalize_lane(lane) is None, "OMN-18769 AC6 is unchanged"
