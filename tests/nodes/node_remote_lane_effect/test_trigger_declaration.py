# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_remote_lane_effect declares what runs it, and the declaration holds.

The remote-lane runner asks this node through ``onex node node_remote_lane_effect
--input <request.json> --output receipt``; no event bus subscription starts it. The
trigger declaration check refuses a pull request that touches a node with no declared
trigger, so the contract names that command and an owner.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from omnimarket.models.model_node_trigger import ModelRuntimeDispatchTriggers
from omnimarket.nodes.node_trigger_declaration_check_effect.handlers import (
    HandlerTriggerDeclarationCheck,
)
from omnimarket.nodes.node_trigger_declaration_check_effect.models import (
    EnumTriggerFindingReason,
    ModelTriggerCheckRequest,
)

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[3]
NODE = "node_remote_lane_effect"
CONTRACT = f"src/omnimarket/nodes/{NODE}/contract.yaml"


def _check(
    contract: str, root: Path = REPO_ROOT
) -> tuple[int, tuple[EnumTriggerFindingReason, ...]]:
    result = HandlerTriggerDeclarationCheck().handle(
        ModelTriggerCheckRequest(root=str(root), contracts=(contract,))
    )
    return result.needing_trigger, tuple(f.reason for f in result.findings)


def test_the_node_declares_an_on_demand_command_and_owner() -> None:
    contract = yaml.safe_load((REPO_ROOT / CONTRACT).read_text("utf-8"))
    on_demand = ModelRuntimeDispatchTriggers.model_validate(
        contract["runtime_dispatch"]
    ).on_demand
    assert on_demand is not None
    assert f"onex node {NODE} --input" in on_demand.command
    assert on_demand.owner.strip()


def test_the_trigger_declaration_check_passes_the_node() -> None:
    needing, reasons = _check(CONTRACT)
    assert needing == 1
    assert reasons == ()


def test_control_a_node_with_no_declaration_is_refused(tmp_path: Path) -> None:
    contract = "src/omnimarket/nodes/node_undeclared_probe/contract.yaml"
    path = tmp_path / contract
    path.parent.mkdir(parents=True)
    path.write_text(
        'name: "node_undeclared_probe"\nruntime_dispatch:\n  command_topic: "t.v1"\n',
        encoding="utf-8",
    )
    needing, reasons = _check(contract, tmp_path)
    assert needing == 1
    assert reasons == (EnumTriggerFindingReason.NO_TRIGGER_DECLARED,)
