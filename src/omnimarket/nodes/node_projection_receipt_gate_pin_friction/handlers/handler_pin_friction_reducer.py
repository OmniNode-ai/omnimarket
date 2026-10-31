# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerReceiptGatePinFrictionReducer - fold one observation into a pin pull request's row (OMN-20134).

A definition-B reducer: the prior row is in the request and the next row is in the
answer, so the handler keeps no state and reads no clock. The answer carries
the friction event the observation newly caused (a failed or refused updater run, or
the first touch of a pin pull request), which node_receipt_gate_pin_friction_ledger_effect
turns into a FRICTION ledger row. Every timestamp comes from the observation, so a replay derives the same row.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import yaml

from omnimarket.models.receipt_gate_pin_friction import (
    ModelPinFrictionFoldRequest,
    ModelPinFrictionFoldResult,
    ModelPinOutcomeEvent,
)
from omnimarket.nodes.node_projection_receipt_gate_pin_friction.models import (
    ModelPinFrictionConfig,
)
from omnimarket.nodes.node_projection_receipt_gate_pin_friction.reducers import (
    FoldResult,
    fold_pin_observation,
)

_CONTRACT_PATH = Path(__file__).resolve().parents[1] / "contract.yaml"
_CONFIG_KEY = "pin_friction_config"


def load_friction_config(
    contract: dict[str, Any] | None = None,
    contract_path: Path = _CONTRACT_PATH,
) -> ModelPinFrictionConfig:
    """The ``pin_friction_config`` block of this node's contract."""
    data = (
        contract
        if contract is not None
        else yaml.safe_load(contract_path.read_text(encoding="utf-8"))
    )
    return ModelPinFrictionConfig.model_validate(data[_CONFIG_KEY])


class HandlerReceiptGatePinFrictionReducer:
    """REDUCER handler: ``(prior row, observation)`` to ``(next row, friction events)``."""

    def __init__(self, config: ModelPinFrictionConfig | None = None) -> None:
        self._config = config if config is not None else load_friction_config()

    @property
    def handler_type(self) -> Literal["NODE_HANDLER"]:
        return "NODE_HANDLER"

    @property
    def handler_category(self) -> Literal["REDUCER"]:
        return "REDUCER"

    def fold(self, request: ModelPinFrictionFoldRequest) -> FoldResult:
        return fold_pin_observation(
            request.prior_row, request.observation, self._config
        )

    def handle(
        self, message: ModelPinFrictionFoldRequest | ModelPinOutcomeEvent
    ) -> ModelPinFrictionFoldResult:
        """The next row and, when the observation newly flagged one, its friction event.

        An outcome event arriving on its own topic is folded with no prior row.
        """
        request = (
            ModelPinFrictionFoldRequest(observation=message)
            if isinstance(message, ModelPinOutcomeEvent)
            else message
        )
        folded = self.fold(request)
        return ModelPinFrictionFoldResult(
            row=folded.row,
            changed=folded.row is not None and folded.row != request.prior_row,
            friction_event=folded.friction[0] if folded.friction else None,
        )


__all__ = ["HandlerReceiptGatePinFrictionReducer", "load_friction_config"]
