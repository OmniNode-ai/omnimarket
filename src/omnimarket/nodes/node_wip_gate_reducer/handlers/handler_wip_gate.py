# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Decide one PR-opening dispatch against its repository's limit."""

from __future__ import annotations

from omnibase_core.models.contracts.subcontracts.model_fsm_subcontract import (
    ModelFSMSubcontract,
)

from omnimarket.handlers.rules_wip_gate import (
    evaluate_wip_gate,
    load_wip_gate_contract,
)
from omnimarket.models.wip_gate import ModelWipGateDecision, ModelWipGateRequest


class HandlerWipGate:
    """``handle(ModelWipGateRequest) -> ModelWipGateDecision`` through the contract's machine."""

    def __init__(self, *, fsm: ModelFSMSubcontract | None = None) -> None:
        self._fsm = fsm if fsm is not None else load_wip_gate_contract()[0]

    def handle(self, request: ModelWipGateRequest) -> ModelWipGateDecision:
        return evaluate_wip_gate(request, self._fsm)
