# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The delegation seam orchestrator's read plan matches its contract edges."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_delegation_seam_orchestrator.handlers.handler_plan_seam_reads import (
    HandlerPlanSeamReads,
)
from omnimarket.nodes.node_delegation_seam_orchestrator.models.model_seam_read_plan import (
    ModelSeamReadPlanRequest,
)

CONTRACT = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_delegation_seam_orchestrator/contract.yaml"
)


@pytest.mark.unit
def test_plan_steps_equal_contract_edges() -> None:
    now = datetime(2026, 10, 28, tzinfo=UTC)
    plan = HandlerPlanSeamReads().handle(
        ModelSeamReadPlanRequest(now=now, workflow_ids=("W1",))
    )
    edges = yaml.safe_load(CONTRACT.read_text())["edges"]
    assert [(s.node, s.operation) for s in plan.steps] == [
        (e["to"], e["operation"]) for e in edges
    ]
    assert plan.planned_at == now
    assert plan.workflow_ids == ("W1",)


@pytest.mark.unit
def test_contract_declares_planned_terminal_event() -> None:
    contract = yaml.safe_load(CONTRACT.read_text())
    assert (
        contract["terminal_event"]
        == "onex.evt.omnimarket.delegation-seam-reads-planned.v1"
    )
