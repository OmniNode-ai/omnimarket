# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Fold one writer's event into one stop."""

from __future__ import annotations

from omnibase_core.models.contracts.subcontracts.model_fsm_subcontract import (
    ModelFSMSubcontract,
)

from omnimarket.handlers.rules_stop_gate import (
    fold_stop_event,
    load_andon_stop_contract,
)
from omnimarket.models.stop_gate import (
    ModelAndonStopConfig,
    ModelStopFoldRequest,
    ModelStopFoldResult,
)


class HandlerAndonStop:
    """``handle(ModelStopFoldRequest) -> ModelStopFoldResult`` through the contract's machine and config."""

    def __init__(
        self,
        *,
        fsm: ModelFSMSubcontract | None = None,
        config: ModelAndonStopConfig | None = None,
    ) -> None:
        loaded_fsm, loaded_config = (
            load_andon_stop_contract()
            if fsm is None or config is None
            else (fsm, config)
        )
        self._fsm = fsm if fsm is not None else loaded_fsm
        self._config = config if config is not None else loaded_config

    def handle(self, request: ModelStopFoldRequest) -> ModelStopFoldResult:
        return fold_stop_event(request.stop, request.event, self._fsm, self._config)
