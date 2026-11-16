# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-23607: invalid inputs are refused by validation; the handler never sees them."""

from __future__ import annotations

import copy
from typing import Any

import pytest
from pydantic import ValidationError

from omnimarket.handlers.node_overlay_reader import NodeOverlayNotFoundError
from omnimarket.nodes.node_pool_usage_verdict_compute.handlers.handler_pool_usage_verdict import (
    HandlerPoolUsageVerdict,
)
from omnimarket.nodes.node_pool_usage_verdict_compute.models import (
    ModelPoolUsageRequest,
)
from omnimarket.nodes.node_pool_usage_verdict_compute.overlay import load_policy

pytestmark = pytest.mark.unit

NOW = "2026-12-01T12:00:00Z"

REQUEST: dict[str, Any] = {
    "now": NOW,
    "policy": {
        "five_hour": {"drain_at": 0.9, "limited_at": 1.0},
        "weekly": {"drain_at": 0.85, "limited_at": 1.0},
        "max_reading_age_seconds": 900,
    },
    "readings": [
        {
            "label": "alpha",
            "read_at": NOW,
            "five_hour_utilization": 0.42,
            "seven_day_utilization": 0.2,
        }
    ],
    "correlation_id": "00000000-0000-4000-8000-000000000093",
}


def _refused(request: dict[str, Any], *fragments: str) -> None:
    """The request never validates, so the handler is never handed it."""
    handler = HandlerPoolUsageVerdict()
    with pytest.raises(ValidationError) as raised:
        ModelPoolUsageRequest.model_validate(request)
    assert callable(handler.handle)
    message = str(raised.value)
    for fragment in fragments:
        assert fragment in message


def test_percent_scaled_utilization_is_refused_not_clamped() -> None:
    request = copy.deepcopy(REQUEST)
    request["readings"][0]["five_hour_utilization"] = 42
    _refused(request, "five_hour_utilization")


def test_duplicate_labels_are_refused() -> None:
    request = copy.deepcopy(REQUEST)
    request["readings"].append(copy.deepcopy(request["readings"][0]))
    _refused(request, "duplicate reading for label alpha")


def test_naive_now_is_refused() -> None:
    request = copy.deepcopy(REQUEST)
    request["now"] = "2026-12-01T12:00:00"
    _refused(request, "now", "timezone")


def test_drain_at_above_limited_at_is_refused() -> None:
    request = copy.deepcopy(REQUEST)
    request["policy"]["five_hour"] = {"drain_at": 0.95, "limited_at": 0.9}
    _refused(request, "drain_at must be <= limited_at")


def test_missing_overlay_refuses_rather_than_defaulting() -> None:
    with pytest.raises(NodeOverlayNotFoundError):
        load_policy(environ={})
