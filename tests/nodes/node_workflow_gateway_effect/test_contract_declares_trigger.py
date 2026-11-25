# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The gateway node's real contract declares what runs it (it subscribes to no topic: a request starts it)."""

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
CONTRACT = "src/omnimarket/nodes/node_workflow_gateway_effect/contract.yaml"


def test_gateway_contract_names_its_trigger() -> None:
    result = HandlerTriggerDeclarationCheck().handle(
        ModelTriggerCheckRequest(root=str(REPO_ROOT), contracts=(CONTRACT,))
    )
    assert result.needing_trigger == 1
    assert result.findings == ()
