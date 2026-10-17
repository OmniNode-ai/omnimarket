# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The parts of a lane brief that code fills; the model never writes them."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.lane_brief_draft.model_brief_quoted_criteria import (
    ModelBriefQuotedCriteria,
)
from omnimarket.models.lane_brief_draft.model_brief_ticket_state_ack import (
    ModelBriefTicketStateAck,
)


class ModelBriefShellParts(BaseModel):
    """Header facts, ticket-state acknowledgements, quoted criteria and the pasted blocks.

    ``constraints_block`` is the 3a block as ``lane_brief.py constraints`` printed it and
    ``rules_block`` is the committed standing rules; both are pasted verbatim.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    parent: str = Field(min_length=1)
    repo: str = Field(min_length=1)
    model: str = Field(min_length=1)
    effort: str = Field(min_length=1)
    ticket_state_acks: tuple[ModelBriefTicketStateAck, ...] = ()
    quoted_criteria: tuple[ModelBriefQuotedCriteria, ...] = ()
    out_of_scope: str = Field(min_length=1)
    rules_block: str = Field(min_length=1)
    constraints_block: str = Field(min_length=1)
