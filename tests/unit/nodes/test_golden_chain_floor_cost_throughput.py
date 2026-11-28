# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain of the cost-and-throughput panel: the recorded readings in, the panel with every figure's source out."""

from __future__ import annotations

import pytest

from omnimarket.models.floor_cost_throughput import SCHEMA_ID, UNREADABLE
from omnimarket.nodes.node_floor_cost_throughput_compute.handlers.handler_floor_cost_throughput import (
    HandlerFloorCostThroughput,
)

from .node_floor_cost_throughput_compute.test_handler_floor_cost_throughput import (
    _request,
)

pytestmark = pytest.mark.unit


def test_golden_chain_panel_from_all_readings() -> None:
    panel = HandlerFloorCostThroughput().handle(_request())
    assert panel.schema_id == SCHEMA_ID
    assert panel.merges_per_hour.value == 7
    assert panel.seen_running_per_hour.value == pytest.approx(
        48 / (1439 / 60), rel=1e-6
    )
    assert panel.cost_per_change_seen_running.value == pytest.approx(5.0 / 48)
    assert {row.engine for row in panel.engines} == {"Claude", "Codex", "GLM", "local"}
    assert all(row.tokens.display == UNREADABLE for row in panel.engines)


def test_error_chain_no_readings_reads_unreadable_everywhere() -> None:
    from omnimarket.models.floor_cost_throughput import ModelCostThroughputRequest

    panel = HandlerFloorCostThroughput().handle(
        ModelCostThroughputRequest(generated_at="2026-11-20T15:00:00Z")
    )
    assert panel.merges_per_hour.display == UNREADABLE
    assert panel.seen_running_per_hour.display == UNREADABLE
    assert panel.cost_per_change_seen_running.display == UNREADABLE
    assert panel.trend.status == UNREADABLE
