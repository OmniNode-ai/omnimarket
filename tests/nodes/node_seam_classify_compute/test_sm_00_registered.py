# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The node registers classify_sm_00 and the factory line names its shape (OMN-21475, T-S3)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_seam_classify_compute import NodeSeamClassifyCompute
from omnimarket.nodes.node_seam_classify_compute.models.model_seam_classify_request import (
    ModelSeamClassifyRequest,
)

pytestmark = pytest.mark.unit

ORCHESTRATOR = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_seam_read_orchestrator/contract.yaml"
)


def test_the_node_dispatches_classify_sm_00_over_a_window_with_two_malformed_items() -> (
    None
):
    items = [
        {"kind": "ticket", "key": "ok-1", "repo": "r", "required_engine": "claude"},
        {"kind": "ticket", "key": "prose", "repo": "r", "required_engine": "any"},
        {"kind": "ticket", "key": "", "repo": "r"},
        {"kind": "ticket", "key": "ok-2", "repo": "r"},
    ]
    result = NodeSeamClassifyCompute().handle(
        ModelSeamClassifyRequest(
            operation="classify_sm_00",
            facts={
                "items": items,
                "window_start": "2026-10-27T00:00:00+00:00",
                "window_end": "2026-10-28T00:00:00+00:00",
            },
            now=datetime(2026, 10, 28, tzinfo=UTC),
        )
    )
    [reading] = result.readings
    assert (reading.seam_id, reading.crossings, reading.mismatched) == ("SM-00", 4, 2)
    assert reading.examples == ("prose", "item-2")


def test_the_factory_line_names_the_shape_of_sm_00_and_no_gap() -> None:
    nodes = yaml.safe_load(ORCHESTRATOR.read_text(encoding="utf-8"))[
        "workflow_coordination"
    ]["workflow_definition"]["execution_graph"]["nodes"]
    [sm_00] = [n for n in nodes if n["node_id"] == "sm_00_backlog_to_fill"]
    assert sm_00["step_config"] == {
        "operation": "classify_sm_00",
        "shape": "ModelBacklogItem",
    }
