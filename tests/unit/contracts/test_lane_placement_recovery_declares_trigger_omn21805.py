# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_lane_placement_recovery_effect declares what runs it (the trigger-declaration check refused it)."""

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
NODE = "node_lane_placement_recovery_effect"


def test_the_node_declares_a_trigger() -> None:
    result = HandlerTriggerDeclarationCheck().handle(
        ModelTriggerCheckRequest(
            root=str(REPO_ROOT),
            contracts=(f"src/omnimarket/nodes/{NODE}/contract.yaml",),
            all_nodes=False,
        )
    )
    assert result.needing_trigger == 1
    assert result.findings == ()
