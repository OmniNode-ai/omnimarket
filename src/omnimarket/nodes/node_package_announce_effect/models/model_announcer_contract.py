# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""The announcer's own contract: its announce lifecycle (OMN-21597)."""

from omnibase_core.models.contracts.subcontracts.model_fsm_subcontract import (
    ModelFSMSubcontract,
)
from pydantic import BaseModel, ConfigDict


class ModelAnnouncerContract(BaseModel):
    """What the announcer reads from its contract file.

    Attributes:
        lifecycle: The announce lifecycle, from the contract's ``state_transitions`` block.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, arbitrary_types_allowed=True)

    lifecycle: ModelFSMSubcontract
