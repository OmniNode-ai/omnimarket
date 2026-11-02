# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain for node_host_bringup_plan_compute (OMN-21883).

The handler the contract routes to, run on the fixture graph and fixed check
results, yields the recorded plan snapshot byte for byte.
"""

from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_host_bringup_plan_compute.handlers.handler_host_bringup_plan import (
    HandlerHostBringupPlan,
    load_bringup_graph,
)
from omnimarket.nodes.node_host_bringup_plan_compute.models import (
    EnumObservedState,
    ModelCheckObservation,
    ModelHostBringupPlanRequest,
)

pytestmark = pytest.mark.unit

_REPO = Path(__file__).resolve().parent.parent
_CONTRACT = _REPO / "src/omnimarket/nodes/node_host_bringup_plan_compute/contract.yaml"
_FIXTURES = _REPO / "tests/fixtures/host_bringup"


def test_golden_chain_contract_handler_reproduces_snapshot() -> None:
    contract = yaml.safe_load(_CONTRACT.read_text(encoding="utf-8"))
    module_name, class_name = contract["handler_routing"]["default_handler"].split(":")
    handler_cls = getattr(importlib.import_module(module_name), class_name)
    assert handler_cls is HandlerHostBringupPlan

    graph = load_bringup_graph(
        yaml.safe_load((_FIXTURES / "fixture_graph.yaml").read_text(encoding="utf-8"))
    )
    request = ModelHostBringupPlanRequest(
        graph=graph,
        observations=(
            ModelCheckObservation(target="os_packages", state=EnumObservedState.OK),
            ModelCheckObservation(target="operator_sudo", state=EnumObservedState.OK),
            ModelCheckObservation(
                target="tailnet_joined", state=EnumObservedState.NOT_OK
            ),
        ),
    )
    plan = handler_cls().handle(request)
    rendered = json.dumps(plan.model_dump(mode="json"), indent=2) + "\n"
    assert rendered == (_FIXTURES / "fixture_plan_snapshot.json").read_text(
        encoding="utf-8"
    )
