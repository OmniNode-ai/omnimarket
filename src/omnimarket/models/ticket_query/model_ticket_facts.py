# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Input and output of node_ticket_query's read_ticket_facts operation (OMN-22418)."""

from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict, Field

_IDENTIFIER = re.compile(r"^[A-Z][A-Z0-9]*-\d+$")


class ModelTicketFactsInput(BaseModel):
    """One ticket, named by its identifier (``OMN-1234``)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    identifier: str = Field(pattern=_IDENTIFIER.pattern)


class ModelTicketFactsOutput(BaseModel):
    """The two facts a launch decision reads: workflow state type and assignee id."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    identifier: str
    state_type: str = Field(description="Linear workflow state type, e.g. started.")
    assignee_id: str = Field(default="", description="Empty when unassigned.")


__all__ = ["ModelTicketFactsInput", "ModelTicketFactsOutput"]
