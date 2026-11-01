# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Admit or refuse new work into a scope from the open stops."""

from __future__ import annotations

from omnibase_core.models.contracts.subcontracts.model_fsm_subcontract import (
    ModelFSMSubcontract,
)

from omnimarket.handlers.rules_stop_gate import load_andon_stop_contract, stop_gate
from omnimarket.models.stop_gate import ModelStopGateRequest, ModelStopGateResult


class HandlerStopGate:
    """``handle(ModelStopGateRequest) -> ModelStopGateResult``: the shared rule, as a node operation."""

    def __init__(self, *, fsm: ModelFSMSubcontract | None = None) -> None:
        self._fsm = fsm if fsm is not None else load_andon_stop_contract()[0]

    def handle(self, request: ModelStopGateRequest) -> ModelStopGateResult:
        return stop_gate(request, self._fsm)
