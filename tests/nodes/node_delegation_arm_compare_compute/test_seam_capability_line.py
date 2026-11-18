# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Seam with the capability map's class line (plan E3, OMN-23628): the line this node reads is that line's shape.

Since OMN-23749 the line model carries no defaults and the values come from the overlay key
delegation.capability_map.line; the example overlay must carry exactly the model's fields. This test fails when
either side changes a field name, and when the node stops sizing its test from the line it is given, so the
node never judges from a line the capability map no longer holds.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from omnimarket.models.delegation_arm_compare.model_arm_compare_overlay import (
    ModelArmCompareOverlay,
)
from omnimarket.models.delegation_capability_map.model_capability_line import (
    ModelCapabilityLine,
)
from omnimarket.nodes.node_delegation_arm_compare_compute.handlers.handler_delegation_arm_compare import (
    HandlerDelegationArmCompare,
)
from omnimarket.ranges.compare import required_comparison_size
from tests.nodes.node_delegation_arm_compare_compute.support import (
    overlay,
    planted,
    request,
)

pytestmark = pytest.mark.unit

EXAMPLE = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_delegation_arm_compare_compute/overlay.example.yaml"
)
CAPABILITY_MAP_EXAMPLE = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_projection_delegation_capability_map/overlay.example.yaml"
)


def test_the_example_line_has_the_capability_map_line_fields() -> None:
    document = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    capability_document = yaml.safe_load(
        CAPABILITY_MAP_EXAMPLE.read_text(encoding="utf-8")
    )
    example = ModelArmCompareOverlay.model_validate(document)
    assert example.line == ModelCapabilityLine.model_validate(
        capability_document["line"]
    )
    assert set(document["line"]) == set(ModelCapabilityLine.model_fields)


def test_the_required_n_the_node_answers_with_is_the_sized_n_of_the_line_it_is_given() -> (
    None
):
    line = overlay().line
    a, b = planted(both_pass=10, both_fail=0, a_only=0, b_only=10)
    result = HandlerDelegationArmCompare(overlay=overlay()).handle(request(a, b)).result
    assert result is not None
    assert result.required_n == required_comparison_size(
        margin=line.margin, confidence=line.confidence, power=line.power
    )
    assert result.margin == line.margin
    assert result.confidence == line.confidence
