# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_seam_read_orchestrator: the handler lists the seam readings in line order (OMN-21459)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_seam_read_orchestrator.handlers.handler_seam_read_plan import (
    HandlerSeamReadPlan,
)
from omnimarket.nodes.node_seam_read_orchestrator.models.model_seam_read_plan import (
    ModelSeamReadPlanRequest,
)

CONTRACT = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_seam_read_orchestrator/contract.yaml"
)


@pytest.mark.unit
def test_the_plan_lists_the_fourteen_seams_in_line_order() -> None:
    raw = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))
    nodes = raw["workflow_coordination"]["workflow_definition"]["execution_graph"][
        "nodes"
    ]
    plan = HandlerSeamReadPlan().handle(ModelSeamReadPlanRequest(graph_nodes=nodes))
    assert len(plan.steps) == 14
    assert plan.steps[0].node_id == "sm_00_backlog_to_fill"
    assert plan.steps[0].operation == "classify_sm_00"
    assert plan.steps[-1].node_id == "sm_13_adoption_to_running"


@pytest.mark.unit
def test_a_seam_node_without_an_operation_refuses() -> None:
    nodes = [{"node_id": "sm_x", "node_type": "COMPUTE_GENERIC", "step_config": {}}]
    with pytest.raises(ValueError, match="sm_x"):
        HandlerSeamReadPlan().handle(ModelSeamReadPlanRequest(graph_nodes=nodes))
