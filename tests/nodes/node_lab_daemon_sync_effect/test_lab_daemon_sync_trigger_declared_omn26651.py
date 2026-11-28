# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_lab_daemon_sync_effect declares how it is started (OMN-26651).

The node is run by a host timer outside this repository, so its contract must
carry an on_demand trigger or the Node Trigger Declaration Check refuses any
pull request that changes the node (its overlay included).
"""

from __future__ import annotations

from pathlib import Path

from omnimarket.nodes.node_trigger_declaration_check_effect.handlers.handler_trigger_declaration_check import (
    HandlerTriggerDeclarationCheck,
)
from omnimarket.nodes.node_trigger_declaration_check_effect.models import (
    ModelTriggerCheckRequest,
)

_REPO = Path(__file__).resolve().parents[3]
_CONTRACT = "src/omnimarket/nodes/node_lab_daemon_sync_effect/contract.yaml"


def test_lab_daemon_sync_contract_declares_a_trigger() -> None:
    result = HandlerTriggerDeclarationCheck().handle(
        ModelTriggerCheckRequest(root=str(_REPO), contracts=(_CONTRACT,))
    )
    assert result.needing_trigger == 1
    assert result.findings == ()
