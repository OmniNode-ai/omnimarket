# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Only a refused preference pin permits an unpinned retry."""

import pytest

from omnimarket.nodes.node_lane_pin_fallback_compute import (
    NodeLanePinFallbackCompute,
)
from omnimarket.nodes.node_lane_pin_fallback_compute.models.model_lane_pin_fallback import (
    ModelLanePinFallbackRequest,
)

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("over", "refusal"),
    [
        ({}, "busy"),
        ({"pin": None}, None),
        ({"required": True}, None),
        ({"holdback": True}, None),
        ({"verdict": None}, None),
        ({"pin_read": False}, "host-a was not read"),
        ({"pin_refusal": None}, "the placement node took no lane slot on host-a"),
    ],
)
def test_pin_fallback(over, refusal):
    values = {
        "pin": "host-a",
        "required": False,
        "verdict": "acted",
        "holdback": False,
        "pin_read": True,
        "pin_refusal": "busy",
    }
    values.update(over)
    result = NodeLanePinFallbackCompute().handle(ModelLanePinFallbackRequest(**values))
    assert result.refusal == refusal
