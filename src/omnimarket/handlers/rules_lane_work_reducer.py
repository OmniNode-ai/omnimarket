# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Load the lane work reducer's state machine from its packaged contract (OMN-26706).

The reducer handler does no file read: composition loads the contract here, once,
and hands the parsed machine to the handler.
"""

from __future__ import annotations

from importlib.resources import files

import yaml
from omnibase_core.models.contracts.subcontracts.model_fsm_subcontract import (
    ModelFSMSubcontract,
)

_REDUCER_PACKAGE = "omnimarket.nodes.node_lane_work_reducer"


def load_lane_work_fsm() -> ModelFSMSubcontract:
    """The packaged reducer contract's ``state_machine`` block as a subcontract."""
    raw = yaml.safe_load(
        files(_REDUCER_PACKAGE).joinpath("contract.yaml").read_text("utf-8")
    )
    return ModelFSMSubcontract.model_validate(raw["state_machine"])
