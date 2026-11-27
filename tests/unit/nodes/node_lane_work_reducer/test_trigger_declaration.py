# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The lane work reducer declares what runs it, and the trigger check accepts it."""

from __future__ import annotations

from pathlib import Path

import pytest

from omnimarket.nodes.node_trigger_declaration_check_effect.handlers.handler_trigger_declaration_check import (
    HandlerTriggerDeclarationCheck,
)
from omnimarket.nodes.node_trigger_declaration_check_effect.models.model_trigger_declaration_check import (
    ModelTriggerCheckRequest,
)

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[4]
CONTRACT = "src/omnimarket/nodes/node_lane_work_reducer/contract.yaml"


def test_the_reducer_contract_passes_the_trigger_declaration_check() -> None:
    result = HandlerTriggerDeclarationCheck().handle(
        ModelTriggerCheckRequest(root=str(ROOT), contracts=(CONTRACT,))
    )
    assert result.checked == 1
    assert result.needing_trigger == 1
    assert result.findings == ()
