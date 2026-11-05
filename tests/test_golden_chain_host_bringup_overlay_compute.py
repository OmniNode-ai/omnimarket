# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain for node_host_bringup_overlay_compute (OMN-19998 AC11).

The handler the contract routes to, run on the fixture host overlay against the
shipped orchestrator rows, resolves every target, and the plan compute folds the
result into a planned bring-up that skips exactly the satisfied targets.
"""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.nodes.node_host_bringup_overlay_compute.handlers.handler_host_bringup_overlay import (
    HandlerHostBringupOverlay,
    check_rows_from_contract,
    load_host_overlay,
)
from omnimarket.nodes.node_host_bringup_overlay_compute.models import (
    EnumOverlayStatus,
    ModelHostBringupOverlayRequest,
)
from omnimarket.nodes.node_host_bringup_plan_compute.handlers.handler_host_bringup_plan import (
    HandlerHostBringupPlan,
    load_bringup_graph,
)
from omnimarket.nodes.node_host_bringup_plan_compute.models import (
    EnumPlanStatus,
    EnumStepDecision,
    ModelCheckObservation,
    ModelHostBringupPlanRequest,
)

pytestmark = pytest.mark.unit

_REPO = Path(__file__).resolve().parent.parent
_NODES = _REPO / "src/omnimarket/nodes"
_FIXTURES = _REPO / "tests/fixtures/host_bringup"


def _yaml(path: Path) -> dict[str, Any]:
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def test_golden_chain_overlay_to_plan() -> None:
    contract = _yaml(_NODES / "node_host_bringup_overlay_compute/contract.yaml")
    module_name, class_name = contract["handler_routing"]["default_handler"].split(":")
    handler_cls = getattr(importlib.import_module(module_name), class_name)
    assert handler_cls is HandlerHostBringupOverlay

    graph_contract = _yaml(_NODES / "node_host_bringup_orchestrator/contract.yaml")
    resolved = handler_cls().handle(
        ModelHostBringupOverlayRequest(
            checks=check_rows_from_contract(graph_contract),
            overlay=load_host_overlay(_yaml(_FIXTURES / "fixture_host_overlay.yaml")),
        )
    )
    assert resolved.status is EnumOverlayStatus.RESOLVED
    assert len(resolved.observations) == 57

    plan = HandlerHostBringupPlan().handle(
        ModelHostBringupPlanRequest(
            graph=load_bringup_graph(graph_contract),
            observations=tuple(
                ModelCheckObservation(target=o.target, state=o.state)
                for o in resolved.observations
            ),
        )
    )
    assert plan.status is EnumPlanStatus.PLANNED
    skipped = [s.target for s in plan.steps if s.decision is EnumStepDecision.SKIP]
    assert len(skipped) == 9
    assert plan.steps[0].target == "card"
