# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_factory_floor_compute subscribes to no topic, so its contract must name what runs it.

The node trigger declaration check refuses a changed node whose contract declares no tick, schedule, host unit,
on-demand command or caller. This runs that check on this node's own contract in this repository.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omnimarket.nodes.node_trigger_declaration_check_effect.handlers import (
    HandlerTriggerDeclarationCheck,
)
from omnimarket.nodes.node_trigger_declaration_check_effect.models import (
    ModelTriggerCheckRequest,
)

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[3]
NODE = "node_factory_floor_compute"
CONTRACT = f"src/omnimarket/nodes/{NODE}/contract.yaml"


def test_floor_compute_contract_declares_what_runs_it() -> None:
    result = HandlerTriggerDeclarationCheck().handle(
        ModelTriggerCheckRequest(root=str(REPO_ROOT), contracts=(CONTRACT,))
    )
    assert result.needing_trigger == 1
    assert [(f.node, f.reason.value, f.detail) for f in result.findings] == []
