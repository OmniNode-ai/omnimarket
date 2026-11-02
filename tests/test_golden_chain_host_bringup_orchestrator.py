# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain for node_host_bringup_orchestrator (OMN-21883).

The orchestrator's committed target graph, run through the plan handler with no
observations, plans all 57 targets in need order with every target acting.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_host_bringup_plan_compute.handlers.handler_host_bringup_plan import (
    HandlerHostBringupPlan,
    load_bringup_graph,
)
from omnimarket.nodes.node_host_bringup_plan_compute.models import (
    EnumPlanStatus,
    EnumStepDecision,
    ModelHostBringupPlanRequest,
)

pytestmark = pytest.mark.unit

_CONTRACT = (
    Path(__file__).resolve().parent.parent
    / "src/omnimarket/nodes/node_host_bringup_orchestrator/contract.yaml"
)


def test_golden_chain_committed_graph_plans_all_57_targets_in_need_order() -> None:
    contract = yaml.safe_load(_CONTRACT.read_text(encoding="utf-8"))
    graph = load_bringup_graph(contract)
    plan = HandlerHostBringupPlan().handle(ModelHostBringupPlanRequest(graph=graph))

    assert plan.status is EnumPlanStatus.PLANNED
    assert plan.refusals == ()
    assert len(plan.steps) == 57
    assert {s.decision for s in plan.steps} == {EnumStepDecision.ACT}
    position = {s.target: s.order for s in plan.steps}
    for target in graph.targets:
        for need in target.needs:
            assert position[need] < position[target.name]
