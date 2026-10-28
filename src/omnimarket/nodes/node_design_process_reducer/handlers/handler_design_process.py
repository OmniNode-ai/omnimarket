# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pure transition of the design process FSM through core's executor (OMN-21460)."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from omnibase_core.errors.model_onex_error import ModelOnexError
from omnibase_core.models.contracts.subcontracts.model_fsm_subcontract import (
    ModelFSMSubcontract,
)
from omnibase_core.utils.util_fsm_executor import execute_transition

from omnimarket.nodes.contract_topics import contract_section
from omnimarket.nodes.node_design_process_reducer.models.model_design_process import (
    ModelDesignProcessRequest,
    ModelDesignProcessResult,
)

CONTRACT_PATH = Path(__file__).resolve().parent.parent / "contract.yaml"


def load_fsm(path: Path = CONTRACT_PATH) -> ModelFSMSubcontract:
    """Load the contract's ``state_machine`` block; fail closed when it is absent."""
    return ModelFSMSubcontract.model_validate(contract_section(path, "state_machine"))


class HandlerDesignProcess:
    """Fold one trigger into the record's state; a guard that fails holds the state."""

    def __init__(self, contract_path: Path = CONTRACT_PATH) -> None:
        self._fsm = load_fsm(contract_path)

    def handle(self, request: ModelDesignProcessRequest) -> ModelDesignProcessResult:
        prior = request.state or self._fsm.initial_state
        context = request.facts.model_dump(mode="json")
        coro = execute_transition(
            self._fsm, current_state=prior, trigger=request.trigger, context=context
        )
        try:
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                result = asyncio.run(coro)
            else:
                with ThreadPoolExecutor(max_workers=1) as pool:
                    result = pool.submit(asyncio.run, coro).result()
        except ModelOnexError as error:
            # An undeclared (state, trigger) pair holds the record where it is.
            return ModelDesignProcessResult(
                prior_state=prior,
                state=prior,
                moved=False,
                error=error.message,
                decided_at=request.now,
            )
        moved = bool(result.success) and result.new_state != prior
        return ModelDesignProcessResult(
            prior_state=prior,
            state=result.new_state if result.success else prior,
            moved=moved,
            error=None if result.success else (result.error or "transition refused"),
            failed_conditions=tuple(result.failed_conditions or ()),
            decided_at=request.now,
        )
